import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from app.domain import Submission
from app.parsers.p0_samples import load_reviewed_samples
from app.services.practice import start_attempt, submit_code
from app.services.sync import publish_snapshot
from app.services.tasks import create_daily_plan
from app.storage.database import connect_database, initialize_database


def run_demo(database_path: Path) -> dict[str, object]:
    if database_path.exists():
        raise FileExistsError("acceptance demo requires a new database path")
    database = connect_database(database_path)
    try:
        initialize_database(database)
        drafts, _ = load_reviewed_samples()
        code = [draft for draft in drafts if draft.question_type == "code"]
        theory = [draft for draft in drafts if draft.question_type == "theory"]
        _add_source(database, "source-code", "code")
        _add_source(database, "source-theory", "theory")
        first_versions = _first_sync(database, code, theory)
        plan = create_daily_plan(database, date(2026, 9, 11), 1, 1, {}, "demo-plan", 7)
        task = next(item for item in plan["tasks"] if _task_type(database, item["id"]) == "code")
        submitted_at = datetime(2026, 9, 11, 8, tzinfo=UTC)
        start_attempt(database, task["id"], "daily", submitted_at)
        failed = submit_code(
            database,
            Submission(
                task_id=task["id"],
                request_key="demo-failure",
                submitted_at=submitted_at,
                entry_mode="daily",
                code_self_result="cannot_solve",
            ),
        )
        changed = replace(code[0], reference_text=code[0].reference_text + " 新版补充。")
        added = replace(
            code[1],
            main_anchor_block_id="demo-new-anchor",
            prompt_block_ids=("demo-new-anchor",),
            prompt="新增代码题",
        )
        _second_sync(database, changed, code[1], added, theory)
        return _assert_and_report(database, plan, task, failed, first_versions)
    finally:
        database.close()


def _add_source(database, source_id: str, question_type: str) -> None:
    database.execute(
        "INSERT INTO source(id, document_id, wiki_url, question_type) VALUES (?, ?, ?, ?)",
        (source_id, f"doc-{source_id}", f"https://example.test/{source_id}", question_type),
    )
    database.commit()


def _first_sync(database, code, theory) -> dict[str, str]:
    publish_snapshot(database, "source-code", "1", "1", _blocks(code), code, "demo-v1")
    publish_snapshot(database, "source-theory", "1", "1", _blocks(theory), theory, "demo-v1")
    return {
        row["question_id"]: row["id"]
        for row in database.execute("SELECT id, question_id FROM question_version")
    }


def _second_sync(database, changed, unchanged, added, theory) -> None:
    publish_snapshot(
        database,
        "source-code",
        "2",
        "2",
        _blocks([changed, unchanged, added]),
        [changed, unchanged, added],
        "demo-v1",
    )
    publish_snapshot(database, "source-theory", "1", "1", _blocks(theory), theory, "demo-v1")


def _blocks(drafts) -> list[dict[str, object]]:
    ids = {block_id for draft in drafts for block_id in draft.prompt_block_ids}
    return [{"block_id": block_id, "children": []} for block_id in sorted(ids)]


def _task_type(database, task_id: str) -> str:
    return database.execute(
        "SELECT q.question_type FROM task t JOIN question q ON q.id=t.question_id WHERE t.id=?",
        (task_id,),
    ).fetchone()[0]


def _assert_and_report(database, plan, task, failed, first_versions) -> dict[str, object]:
    stored = database.execute("SELECT * FROM task WHERE id=?", (task["id"],)).fetchone()
    attempt = database.execute("SELECT * FROM attempt WHERE task_id=?", (task["id"],)).fetchone()
    original_version = first_versions[task["question_id"]]
    assert stored["question_version_id"] == original_version
    assert attempt["question_version_id"] == original_version
    assert stored["status"] == "completed"
    assert failed["review_round_id"] is not None
    return {
        "question_count": database.execute("SELECT COUNT(*) FROM question").fetchone()[0],
        "version_count": database.execute("SELECT COUNT(*) FROM question_version").fetchone()[0],
        "task_id_stable": stored["id"] == task["id"],
        "task_version_stable": stored["question_version_id"] == original_version,
        "history_preserved": attempt["id"] == failed["attempt_id"],
        "review_preserved": failed["review_round_id"] is not None,
    }


if __name__ == "__main__":
    with TemporaryDirectory(prefix="learning-demo-") as temporary:
        report = run_demo(Path(temporary) / "acceptance-demo.db")
    print(json.dumps(report, ensure_ascii=False, indent=2))
