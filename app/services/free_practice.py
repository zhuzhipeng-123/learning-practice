import random
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from app.services.tasks import _load_idempotent, _save_idempotent, add_tasks
from app.storage.transactions import atomic


@dataclass(frozen=True)
class FreePracticeResult:
    requested: int
    added: int
    missing: int
    task_ids: list[str]
    source_checked: bool
    used_cache: bool


THEME_ALIASES = {
    "优化器": ("优化器", "Adam", "AdamW"),
    "transformer": ("Transformer", "attention", "注意力"),
    "链表": ("链表", "linked list"),
}


def find_new_originals(
    connection: sqlite3.Connection,
    theme: str,
    only_new: bool = True,
) -> list[sqlite3.Row]:
    """Find eligible originals without allowing generated fallback questions."""
    terms = THEME_ALIASES.get(theme.lower(), (theme,))
    rows = connection.execute(
        "SELECT q.id, v.prompt, v.reference_text, v.category_path "
        "FROM question q JOIN question_version v ON v.id=q.current_version_id "
        "WHERE q.source_kind='feishu' AND q.source_status='active' "
        "AND v.material_status IN ('complete','verified','text_complete') "
        "AND (?=0 OR q.first_submitted_at IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM task t WHERE t.question_id=q.id "
        "AND t.status IN ('pending', 'in_progress'))",
        (int(only_new),),
    ).fetchall()
    return [row for row in rows if _matches(row, terms)]


@atomic
def add_free_practice(
    connection: sqlite3.Connection,
    plan_id: str,
    theme: str,
    count: int,
    request_key: str,
    source_checked: bool,
    used_cache: bool,
    random_seed: int | None = None,
    selected_ids: list[str] | None = None,
    only_new: bool = True,
) -> FreePracticeResult:
    payload = {"plan_id": plan_id, "theme": theme, "count": count, 'only_new': only_new}
    previous = _load_idempotent(connection, request_key, "free_practice", payload)
    if previous is not None:
        return FreePracticeResult(**previous)
    if count <= 0:
        raise ValueError("count must be positive")
    if not source_checked:
        raise ValueError("free practice requires a source freshness check")
    candidates = find_new_originals(connection, '' if selected_ids is not None else theme, only_new)
    if selected_ids is not None:
        candidates = [row for row in candidates if row['id'] in selected_ids]
    generator = random.Random(random_seed)
    selected = generator.sample(candidates, min(count, len(candidates)))
    task_result = add_tasks(
        connection,
        plan_id,
        [row["id"] for row in selected],
        "free_practice",
        f"free-tasks:{request_key}",
    )
    added = int(task_result["added"])
    result = FreePracticeResult(
        requested=count,
        added=added,
        missing=count - added,
        task_ids=list(task_result["created_task_ids"]),
        source_checked=source_checked,
        used_cache=used_cache,
    )
    _save_idempotent(connection, request_key, "free_practice", payload, asdict(result),
                     datetime.now(UTC).isoformat())
    return result


def _matches(row: sqlite3.Row, terms: tuple[str, ...]) -> bool:
    searchable = " ".join(
        [row["prompt"] or "", row["reference_text"] or "", row["category_path"] or ""]
    ).casefold()
    return any(term.casefold() in searchable for term in terms)
