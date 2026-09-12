from dataclasses import dataclass
from typing import Any

from app.adapters.lark_cli import LarkCliClient
from app.services.sync import SyncIntegrityError


@dataclass(frozen=True)
class DocxRead:
    revision: str
    blocks: list[dict[str, Any]]
    page_count: int


class DocxReader:
    """Read a complete latest Docx snapshot with revision consistency checks."""

    def __init__(self, client: LarkCliClient, identity: str = "bot") -> None:
        self.client = client
        self.identity = identity

    def read_latest(self, document_id: str) -> DocxRead:
        before = self._read_revision(document_id)
        blocks, page_count = self._read_pages(document_id)
        after = self._read_revision(document_id)
        if before != after:
            raise SyncIntegrityError("source revision changed during pagination")
        return DocxRead(revision=after, blocks=blocks, page_count=page_count)

    def _read_revision(self, document_id: str) -> str:
        result = self.client.run_read(
            [
                "api",
                "GET",
                f"/open-apis/docx/v1/documents/{document_id}",
                "--as",
                self.identity,
            ]
        )
        document = result.data["document"]
        return str(document["revision_id"])

    def _read_pages(self, document_id: str) -> tuple[list[dict[str, Any]], int]:
        blocks: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        page_count = 0
        while True:
            params: dict[str, Any] = {"page_size": 500, "document_revision_id": -1}
            if page_token:
                params["page_token"] = page_token
            result = self.client.run_read(
                [
                    "api",
                    "GET",
                    f"/open-apis/docx/v1/documents/{document_id}/blocks",
                    "--params",
                    _compact_json(params),
                    "--as",
                    self.identity,
                ]
            )
            data = result.data
            if not isinstance(data, dict) or not isinstance(data.get("items"), list) or type(data.get("has_more")) is not bool:
                raise SyncIntegrityError("pagination response lacks explicit items/has_more")
            page_count += 1
            blocks.extend(data["items"])
            if page_count > 100 or len(blocks) > 50_000:
                raise SyncIntegrityError("document exceeded bounded pagination limits")
            if not data.get("has_more"):
                return blocks, page_count
            page_token = data.get("page_token")
            if not page_token or page_token in seen_tokens:
                raise SyncIntegrityError("pagination cursor is missing or repeated")
            seen_tokens.add(page_token)


def _compact_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))
