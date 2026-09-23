import json
import subprocess
import sys

import pytest

from app.adapters.lark_cli import (
    LarkCliClient,
    LarkCliPermissionError,
    LarkCliResponseError,
    _run_bounded,
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
    monkeypatch.setattr('app.adapters.lark_cli._run_bounded', lambda *args, **kwargs: result)

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
    monkeypatch.setattr('app.adapters.lark_cli._run_bounded', lambda *args, **kwargs: completed(payload, 4))

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
    monkeypatch.setattr('app.adapters.lark_cli._run_bounded', lambda *args, **kwargs: result)

    with pytest.raises(LarkCliResponseError):
        LarkCliClient(max_output_bytes=10).run_read(["auth", "status"])


@pytest.mark.parametrize(('stream', 'script'), [
    ('stdout', "import os; os.write(1,b'x'*200000)"),
    ('stderr', "import os; os.write(2,b'x'*200000)"),
])
def test_bounded_runner_stops_real_process_when_one_stream_exceeds_limit(stream, script):
    with pytest.raises(LarkCliResponseError, match=stream):
        _run_bounded([sys.executable, '-c', script], None, 2, 1024)


def test_bounded_runner_drains_both_streams_without_deadlock():
    script = "import os; [os.write(fd,b'x'*2048) for _ in range(20) for fd in (1,2)]"
    result = _run_bounded([sys.executable, '-c', script], None, 3, 100000)
    assert result.returncode == 0
    assert len(result.stdout) == len(result.stderr) == 40960


def test_bounded_runner_times_out_and_preserves_utf8_boundaries():
    with pytest.raises(subprocess.TimeoutExpired):
        _run_bounded([sys.executable, '-c', 'import time; time.sleep(5)'], None, 0.1, 1024)
    result = _run_bounded([sys.executable, '-c',
                           "import os; os.write(1,bytes.fromhex('e5ada6e4b9a0e5ae8ce68890'))"],
                          None, 2, 1024)
    assert result.stdout.decode('utf-8').strip() == '学习完成'
