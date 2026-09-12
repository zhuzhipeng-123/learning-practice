from typing import Any

import pytest

from app.adapters.docx_reader import DocxReader
from app.adapters.lark_cli import LarkCliResult
from app.services.sync import SyncIntegrityError


class FakeClient:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies

    def run_read(self, arguments: list[str]) -> LarkCliResult:
        return LarkCliResult(data=self.replies.pop(0), identity="bot")


def test_reader_follows_all_pages_and_checks_revision() -> None:
    client = FakeClient(
        [
            {"document": {"revision_id": 7}},
            {"items": [{"block_id": "a"}], "has_more": True, "page_token": "next"},
            {"items": [{"block_id": "b"}], "has_more": False},
            {"document": {"revision_id": 7}},
        ]
    )

    result = DocxReader(client).read_latest("document")  # type: ignore[arg-type]

    assert result.revision == "7"
    assert result.page_count == 2
    assert [block["block_id"] for block in result.blocks] == ["a", "b"]


def test_reader_rejects_revision_change() -> None:
    client = FakeClient(
        [
            {"document": {"revision_id": 7}},
            {"items": [{"block_id": "a"}], "has_more": False},
            {"document": {"revision_id": 8}},
        ]
    )

    with pytest.raises(SyncIntegrityError):
        DocxReader(client).read_latest("document")  # type: ignore[arg-type]


def test_reader_rejects_cursor_loop() -> None:
    client = FakeClient(
        [
            {"document": {"revision_id": 7}},
            {"items": [], "has_more": True, "page_token": "same"},
            {"items": [], "has_more": True, "page_token": "same"},
        ]
    )

    with pytest.raises(SyncIntegrityError):
        DocxReader(client).read_latest("document")  # type: ignore[arg-type]
