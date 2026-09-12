import re
import sqlite3

from app.adapters.lark_cli import LarkCliClient
from app.storage.ids import new_id

WIKI_TOKEN_PATTERN = re.compile(r"/wiki/([A-Za-z0-9]+)")
DOCX_TOKEN_PATTERN = re.compile(r"/docx/([A-Za-z0-9]+)")


class SourceRegistrationError(RuntimeError):
    """A source URL cannot be registered safely."""


def inspect_and_register_source(
    connection: sqlite3.Connection,
    url: str,
    question_type: str,
    identity: str = "bot",
    client: LarkCliClient | None = None,
) -> str:
    if not _valid_feishu_document_url(url):
        raise SourceRegistrationError("only a Feishu wiki or docx URL is allowed")
    result = (client or LarkCliClient()).run_read(
        ["drive", "+inspect", "--url", url, "--as", identity]
    )
    if result.data.get("type") != "docx":
        raise SourceRegistrationError("source must resolve to a Docx document")
    return register_source(connection, url, result.data["token"], question_type, identity)


def register_source(
    connection: sqlite3.Connection,
    url: str,
    document_id: str,
    question_type: str,
    identity: str = "bot",
) -> str:
    if question_type not in {"code", "theory"}:
        raise SourceRegistrationError("question type must be code or theory")
    if identity not in {"bot", "user"}:
        raise SourceRegistrationError("identity must be fixed to bot or user")
    if not _valid_feishu_document_url(url):
        raise SourceRegistrationError("only a Feishu wiki or docx URL is allowed")
    if not document_id.strip():
        raise SourceRegistrationError("document ID is required")
    existing = connection.execute(
        "SELECT id FROM source WHERE document_id=?",
        (document_id,),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    source_id = new_id("source")
    connection.execute(
        "INSERT INTO source(id, document_id, wiki_url, question_type, identity) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, document_id, url, question_type, identity),
    )
    connection.commit()
    return source_id


def set_source_enabled(
    connection: sqlite3.Connection,
    source_id: str,
    enabled: bool,
) -> None:
    changed = connection.execute(
        "UPDATE source SET enabled=? WHERE id=?",
        (int(enabled), source_id),
    ).rowcount
    connection.commit()
    if changed != 1:
        raise SourceRegistrationError("source was not found")


def _valid_feishu_document_url(url: str) -> bool:
    if not url.startswith("https://") or ".feishu.cn/" not in url:
        return False
    return WIKI_TOKEN_PATTERN.search(url) is not None or DOCX_TOKEN_PATTERN.search(url) is not None
