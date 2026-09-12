import json
import subprocess

import pytest

from app.adapters.lark_cli import (
    LarkCliClient,
    LarkCliPermissionError,
    LarkCliResponseError,
)


def completed(payload: dict, return_code: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["lark-cli"],
        returncode=return_code,
        stdout=json.dumps(payload).encode(),
        stderr=b"",
    )


def test_run_read_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    result = completed({"ok": True, "identity": "bot", "data": {"revision_id": 1}})
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)

    response = LarkCliClient().run_read(["api", "GET", "/example"])

    assert response.identity == "bot"
    assert response.data == {"revision_id": 1}


def test_inner_permission_code_is_not_treated_as_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "ok": False,
        "error": {
            "type": "network",
            "subtype": "transport",
            "code": 400,
            "message": 'HTTP 400: {"code":99991672,"msg":"Access denied"}',
        },
    }
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed(payload, 4))

    with pytest.raises(LarkCliPermissionError):
        LarkCliClient().run_read(["docs", "+media-download", "--token", "safe"])


def test_rejects_unapproved_command_group() -> None:
    with pytest.raises(ValueError):
        LarkCliClient().run_read(["calendar", "list"])


def test_rejects_oversized_output(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.CompletedProcess(
        args=["lark-cli"],
        returncode=0,
        stdout=b"x" * 11,
        stderr=b"",
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)

    with pytest.raises(LarkCliResponseError):
        LarkCliClient(max_output_bytes=10).run_read(["auth", "status"])
