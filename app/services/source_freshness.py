import sqlite3
from datetime import UTC, datetime, timedelta

from app.adapters.docx_reader import DocxReader
from app.adapters.lark_cli import LarkCliClient, LarkCliError
from app.services.source_sync import sync_registered_source
from app.services.sync import SyncIntegrityError

FRESHNESS_WINDOW = timedelta(minutes=10)


def ensure_sources_fresh(
    connection: sqlite3.Connection,
    client: LarkCliClient | None = None,
) -> dict[str, object]:
    """Check enabled sources on the server before a freshness-sensitive query."""
    now = datetime.now(UTC)
    results = []
    used_cache = False
    for source in connection.execute("SELECT * FROM source WHERE enabled=1 ORDER BY id"):
        last_success = _parse_time(source["last_check_success_at"])
        if last_success is not None and now - last_success <= FRESHNESS_WINDOW and not source["last_error"]:
            results.append({"source_id": source["id"], "status": "fresh"})
            continue
        try:
            reader = DocxReader(client or LarkCliClient(), source["identity"])
            revision = reader._read_revision(source["document_id"])
            latest = connection.execute(
                "SELECT ss.revision FROM source_snapshot ss JOIN source_sync_state state "
                "ON state.snapshot_id=ss.id WHERE state.source_id=?",
                (source["id"],),
            ).fetchone()
            if (
                latest is None
                or latest["revision"] != revision
                or _has_incomplete_work(connection, source["id"])
                or connection.execute("SELECT 1 FROM source_dependency WHERE source_id=? AND status!='obsolete' LIMIT 1", (source["id"],)).fetchone()
            ):
                summary = sync_registered_source(connection, source["id"], client)
                used_cache = used_cache or summary["partial"]
                results.append({"source_id": source["id"], "status": "synced", "summary": summary})
            else:
                connection.execute(
                    "UPDATE source SET last_check_at=?, last_check_success_at=?, last_error=NULL WHERE id=?",
                    (now.isoformat(), now.isoformat(), source["id"]),
                )
                connection.commit()
                results.append({"source_id": source["id"], "status": "unchanged"})
        except (LarkCliError, SyncIntegrityError, KeyError, TypeError, ValueError) as error:
            used_cache = True
            connection.execute("UPDATE source SET last_check_at=?,last_error=? WHERE id=?",
                               (now.isoformat(), str(error), source["id"]))
            connection.commit()
            results.append(
                {
                    "source_id": source["id"],
                    "status": "cache",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    return {"sources": results, "used_cache": used_cache, "checked_at": now.isoformat()}


def _has_incomplete_work(connection: sqlite3.Connection, source_id: str) -> bool:
    pending_dependency = connection.execute(
        "SELECT 1 FROM source_dependency WHERE source_id=? AND status NOT IN ('complete','obsolete') LIMIT 1",
        (source_id,),
    ).fetchone()
    missing_pending = connection.execute(
        "SELECT 1 FROM source_binding b JOIN question q ON q.id=b.question_id "
        "WHERE b.source_id=? AND q.source_status='missing_pending' LIMIT 1",
        (source_id,),
    ).fetchone()
    return pending_dependency is not None or missing_pending is not None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)
