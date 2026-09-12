import json
import os
from pathlib import Path

from dotenv import load_dotenv

from app.parsers.p0_samples import SAMPLES_PATH, load_reviewed_samples
from app.services.sync import publish_snapshot
from app.storage.database import connect_database, initialize_database

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def ensure_sources(connection) -> None:
    sources = [
        (
            "source-code",
            "demo-code-document",
            "https://example.feishu.cn/wiki/demo-code",
            "code",
        ),
        (
            "source-theory",
            "demo-theory-document",
            "https://example.feishu.cn/wiki/demo-theory",
            "theory",
        ),
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO source(id, document_id, wiki_url, question_type) "
        "VALUES (?, ?, ?, ?)",
        sources,
    )
    connection.commit()


def seed() -> dict[str, object]:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    data_directory = Path(os.environ.get("LEARNING_DATA_DIR", PROJECT_ROOT / "data-demo"))
    data_directory.mkdir(parents=True, exist_ok=True)
    connection = connect_database(data_directory / "learning.db")
    try:
        initialize_database(connection)
        if connection.execute("SELECT 1 FROM source_snapshot LIMIT 1").fetchone():
            raise RuntimeError("demo seeding requires an empty database; existing snapshots were preserved")
        ensure_sources(connection)
        drafts, candidates = load_reviewed_samples()
        raw = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))
        results = {}
        for source_id, revision in (("source-code", "1583"), ("source-theory", "12089")):
            source_drafts = [draft for draft in drafts if draft.source_id == source_id]
            blocks = _fixture_blocks(source_drafts)
            results[source_id] = publish_snapshot(
                connection,
                source_id,
                revision,
                revision,
                blocks,
                source_drafts,
                raw["parser_version"],
            )
        return {"published": results, "candidate_count": len(candidates)}
    finally:
        connection.close()


def _fixture_blocks(drafts) -> list[dict[str, object]]:
    block_ids = set()
    for draft in drafts:
        block_ids.update(draft.prompt_block_ids)
        block_ids.update(draft.reference_block_ids)
    return [{"block_id": block_id, "children": []} for block_id in sorted(block_ids)]


def main() -> None:
    print(json.dumps(seed(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
