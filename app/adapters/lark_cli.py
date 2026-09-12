import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LarkCliError(RuntimeError):
    """Base error for a controlled lark-cli invocation."""


class LarkCliPermissionError(LarkCliError):
    """The selected identity lacks a required read permission."""


class LarkCliResponseError(LarkCliError):
    """The CLI returned invalid JSON or an unsuccessful response."""


@dataclass(frozen=True)
class LarkCliResult:
    data: Any
    identity: str | None


class LarkCliClient:
    """Run allow-listed read commands without invoking a shell."""

    def __init__(
        self,
        executable: str = "lark-cli",
        timeout_seconds: float = 120,
        max_output_bytes: int = 8_000_000,
        work_directory: Path | None = None,
    ) -> None:
        configured = os.environ.get("LARK_CLI_PATH", executable) if executable == "lark-cli" else executable
        resolved = shutil.which(configured) or configured
        # npm installs a shell shim on Windows; execute its native binary without cmd /c.
        if Path(resolved).suffix.lower() in {".cmd", ".ps1", ".bat"}:
            native = Path(resolved).parent / "node_modules" / "@larksuite" / "cli" / "bin" / "lark-cli.exe"
            if native.is_file():
                resolved = str(native)
        self.executable = resolved
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.work_directory = work_directory

    def run_read(self, arguments: Sequence[str]) -> LarkCliResult:
        self._validate_arguments(arguments)
        command = [self.executable, *arguments, "--format", "json"]
        try:
            completed = subprocess.run(command, capture_output=True, check=False,
                                       shell=False, timeout=self.timeout_seconds, cwd=self.work_directory)
        except subprocess.TimeoutExpired as error:
            raise LarkCliError("lark-cli timed out; cached content remains available") from error
        except OSError as error:
            raise LarkCliError(f"could not start lark-cli: {type(error).__name__}") from error
        stdout = self._decode_limited(completed.stdout, "stdout")
        stderr = self._decode_limited(completed.stderr, "stderr")
        payload = self._parse_payload(stdout, stderr)
        return self._validate_payload(payload, completed.returncode)

    def download_media(self, token: str, output: Path, identity: str) -> LarkCliResult:
        """Download one referenced media object into a caller-owned directory."""
        output = output.resolve()
        arguments = [
            "docs",
            "+media-download",
            "--token",
            token,
            "--type",
            "media",
            "--output",
            output.name,
            "--as",
            identity,
        ]
        downloader = LarkCliClient(self.executable, self.timeout_seconds, self.max_output_bytes,
                                   work_directory=output.parent)
        return downloader.run_read(arguments)

    def _validate_arguments(self, arguments: Sequence[str]) -> None:
        if not arguments:
            raise ValueError("at least one lark-cli argument is required")
        if any(argument == "--format" for argument in arguments):
            raise ValueError("format is controlled by LarkCliClient")
        if arguments[0] not in {"docs", "sheets", "wiki", "drive", "api", "auth"}:
            raise ValueError("unsupported lark-cli command group")

    def _decode_limited(self, output: bytes, stream_name: str) -> str:
        if len(output) > self.max_output_bytes:
            raise LarkCliResponseError(f"{stream_name} exceeded the output limit")
        return output.decode("utf-8", errors="replace")

    def _parse_payload(self, stdout: str, stderr: str) -> dict[str, Any]:
        candidates = [stdout.strip(), stderr.strip()]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                # Downloads can emit progress lines before their JSON envelope.
                payload = None
                for index, character in enumerate(candidate):
                    if character != "{":
                        continue
                    try:
                        value, end = json.JSONDecoder().raw_decode(candidate[index:])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict) and "ok" in value and not candidate[index + end:].strip():
                        payload = value
                        break
            if isinstance(payload, dict):
                return payload
        raise LarkCliResponseError("lark-cli did not return a JSON object")

    def _validate_payload(
        self,
        payload: dict[str, Any],
        return_code: int,
    ) -> LarkCliResult:
        if payload.get("ok") is True and return_code == 0:
            return LarkCliResult(data=payload.get("data"), identity=payload.get("identity"))
        error = payload.get("error") or {}
        if not isinstance(error, dict):
            raise LarkCliResponseError("lark-cli returned an invalid error envelope")
        message = str(error.get("message") or "lark-cli request failed")
        if self._is_permission_error(error, message):
            raise LarkCliPermissionError(message)
        raise LarkCliResponseError(f"lark-cli exited with {return_code}: {message}")

    def _is_permission_error(self, error: dict[str, Any], message: str) -> bool:
        permission_codes = {99991672, 1770032}
        return (
            error.get("code") in permission_codes
            or error.get("subtype") in {"app_scope_not_applied", "permission_denied"}
            or "Access denied" in message
        )
