import hashlib
import json
import random
import sqlite3
from datetime import date, datetime
from typing import Any

from app.storage.ids import new_id
from app.storage.transactions import atomic, transaction


class IdempotencyConflictError(RuntimeError):
    """The same request key was reused with different input."""


class CandidateShortageError(RuntimeError):
    """The requested fixed allocation cannot be satisfied."""


@atomic
def create_daily_plan(
    connection: sqlite3.Connection,
    plan_date: date,
    code_target: int,
    theory_target: int,
    module_quotas: dict[str, int],
    request_key: str,
    random_seed: int | None = None,
    required_date: date | None = None,
) -> dict[str, Any]:
    payload = {
        "date": plan_date.isoformat(),
        "code_target": code_target,
        "theory_target": theory_target,
        "module_quotas": module_quotas,
    }
    existing = _load_idempotent(connection, request_key, "create_daily_plan", payload)
    if existing is not None:
        return existing
    if required_date is not None and plan_date != required_date:
        raise ValueError('日期已变化，请回到今天安排练习')
    current = connection.execute(
        "SELECT id,code_target,theory_target FROM daily_plan WHERE plan_date=?",
        (plan_date.isoformat(),),
    ).fetchone()
    if current is not None and (current["code_target"] or current["theory_target"] or not (code_target or theory_target)):
        result = _plan_result(connection, current["id"])
        _save_idempotent(connection, request_key, 'create_daily_plan', payload, result, datetime.now().astimezone().isoformat())
        return result
    _validate_targets(code_target, theory_target, module_quotas)
    selected, shortages = _select_questions(
        connection,
        code_target,
        theory_target,
        module_quotas,
        random.Random(random_seed),
    )
    plan_id = current["id"] if current else new_id("plan")
    now = datetime.now().astimezone().isoformat()
    with transaction(connection):
        if current:
            connection.execute("UPDATE daily_plan SET code_target=?,theory_target=?,allocation_json=? WHERE id=?",
                               (code_target, theory_target, json.dumps(module_quotas, ensure_ascii=False, sort_keys=True), plan_id))
        else:
            connection.execute(
            "INSERT INTO daily_plan VALUES (?, ?, 'Asia/Shanghai', ?, ?, 0, ?, ?)",
            (
                plan_id,
                plan_date.isoformat(),
                code_target,
                theory_target,
                json.dumps(module_quotas, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        for question in selected:
            connection.execute(
                "INSERT INTO task VALUES (?, ?, ?, ?, 'daily', 'base', 'pending', ?, NULL, NULL, NULL)",
                (
                    new_id("task"),
                    plan_id,
                    question["id"],
                    question["current_version_id"],
                    now,
                ),
            )
        result = _plan_result(connection, plan_id)
        result["shortages"] = shortages
        _save_idempotent(connection, request_key, "create_daily_plan", payload, result, now)
    return result


@atomic
def add_tasks(
    connection: sqlite3.Connection,
    plan_id: str,
    question_ids: list[str],
    origin: str,
    request_key: str,
    version_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = {"plan_id": plan_id, "question_ids": question_ids, "origin": origin,
               "version_ids": version_ids or {}}
    existing = _load_idempotent(connection, request_key, "add_tasks", payload)
    if existing is not None:
        return existing
    now = datetime.now().astimezone().isoformat()
    created: list[str] = []
    with transaction(connection):
        for question_id in question_ids:
            version = connection.execute(
                "SELECT current_version_id FROM question WHERE id=?",
                (question_id,),
            ).fetchone()
            if version is None or version["current_version_id"] is None:
                continue
            version_id = (version_ids or {}).get(question_id, version["current_version_id"])
            chosen = connection.execute(
                "SELECT id,material_status FROM question_version WHERE id=? AND question_id=?",
                (version_id, question_id),
            ).fetchone()
            if chosen is None or chosen["material_status"] not in {"complete", "verified", "text_complete"}:
                continue
            pending = connection.execute(
                "SELECT id FROM task WHERE plan_id=? AND question_id=? "
                "AND status IN ('pending', 'in_progress')",
                (plan_id, question_id),
            ).fetchone()
            if pending is not None:
                continue
            task_id = new_id("task")
            connection.execute(
                "INSERT INTO task VALUES (?, ?, ?, ?, ?, 'added', 'pending', ?, NULL, NULL, NULL)",
                (task_id, plan_id, question_id, version_id, origin, now),
            )
            created.append(task_id)
        connection.execute(
            "UPDATE daily_plan SET added_target=added_target+? WHERE id=?",
            (len(created), plan_id),
        )
        result = {"plan_id": plan_id, "created_task_ids": created, "added": len(created)}
        _save_idempotent(connection, request_key, "add_tasks", payload, result, now)
    return result


def _validate_targets(code_target: int, theory_target: int, quotas: dict[str, int]) -> None:
    if code_target < 0 or theory_target < 0 or any(value < 0 for value in quotas.values()):
        raise ValueError("targets cannot be negative")
    if sum(quotas.values()) > theory_target:
        raise ValueError("module quotas exceed the theory target")
    paths = list(quotas)
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if left.startswith(right + " > ") or right.startswith(left + " > "):
                raise ValueError("parent and child module quotas overlap")


def _select_questions(
    connection: sqlite3.Connection,
    code_target: int,
    theory_target: int,
    quotas: dict[str, int],
    generator: random.Random,
    exclude_ids: set[str] | None = None,
) -> tuple[list[sqlite3.Row], dict[str, int]]:
    rows = connection.execute(
        "SELECT q.id, q.question_type, q.current_version_id, v.category_path "
        "FROM question q JOIN question_version v ON v.id=q.current_version_id "
        "WHERE q.source_kind='feishu' AND q.source_status='active' "
        "AND v.material_status IN ('complete','verified','text_complete') "
        "AND NOT EXISTS (SELECT 1 FROM task pending WHERE pending.question_id=q.id "
        "AND pending.status IN ('pending', 'in_progress'))",
    ).fetchall()
    rows = [row for row in rows if row["id"] not in (exclude_ids or set())]
    selected: list[sqlite3.Row] = []
    shortages: dict[str, int] = {}
    code_rows = [row for row in rows if row["question_type"] == "code"]
    selected.extend(_sample(generator, code_rows, code_target))
    if len(code_rows) < code_target:
        shortages["code"] = code_target - len(code_rows)
    theory_rows = [row for row in rows if row["question_type"] == "theory"]
    used_ids = {row["id"] for row in selected}
    for path, target in quotas.items():
        candidates = [
            row
            for row in theory_rows
            if row["id"] not in used_ids and (
                row["category_path"] == path or row["category_path"].startswith(path + " > ")
            )
        ]
        chosen = _sample(generator, candidates, target)
        selected.extend(chosen)
        used_ids.update(row["id"] for row in chosen)
        if len(chosen) < target:
            shortages[path] = target - len(chosen)
    remaining_target = theory_target - sum(quotas.values())
    remaining = [row for row in theory_rows if row["id"] not in used_ids]
    selected.extend(_sample(generator, remaining, remaining_target))
    actual_theory = sum(row["question_type"] == "theory" for row in selected)
    if actual_theory < theory_target:
        shortages["unallocated"] = theory_target - actual_theory
    return selected, shortages


@atomic
def fill_daily_plan(connection, plan_id, module_quotas=None):
    plan = connection.execute("SELECT * FROM daily_plan WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise ValueError("今日计划不存在")
    assigned = connection.execute("SELECT t.question_id,q.question_type,v.category_path FROM task t "
                                  "JOIN question q ON q.id=t.question_id JOIN question_version v ON v.id=t.question_version_id "
                                  "WHERE t.plan_id=? AND t.target_kind='base' AND t.status!='cancelled'", (plan_id,)).fetchall()
    quotas = json.loads(plan["allocation_json"])
    if module_quotas is not None and module_quotas != quotas:
        if any(row["question_type"] == "theory" for row in assigned):
            raise ValueError("已有八股任务，模块分配已固定；补齐不会改动原分配")
        _validate_targets(plan["code_target"], plan["theory_target"], module_quotas)
        quotas = module_quotas
        connection.execute("UPDATE daily_plan SET allocation_json=? WHERE id=?", (json.dumps(quotas, ensure_ascii=False), plan_id))
    remaining = {path: max(0, count - sum(row["question_type"] == "theory" and
                 (row["category_path"] == path or row["category_path"].startswith(path + " > ")) for row in assigned))
                 for path, count in quotas.items()}
    code = max(0, plan["code_target"] - sum(row["question_type"] == "code" for row in assigned))
    theory = max(0, plan["theory_target"] - sum(row["question_type"] == "theory" for row in assigned))
    selected, shortages = _select_questions(connection, code, theory, remaining, random.Random(),
                                            {row["question_id"] for row in assigned})
    for row in selected:
        connection.execute("INSERT INTO task VALUES (?,?,?,?,'daily','base','pending',?,NULL,NULL,NULL)",
                           (new_id("task"), plan_id, row["id"], row["current_version_id"], datetime.now().astimezone().isoformat()))
    return {**_plan_result(connection, plan_id), "shortages": shortages, "filled": len(selected)}


def _sample(generator: random.Random, rows: list[sqlite3.Row], count: int) -> list[sqlite3.Row]:
    return generator.sample(rows, min(count, len(rows)))


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def _load_idempotent(
    connection: sqlite3.Connection,
    request_key: str,
    operation: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT operation, payload_hash, result_json FROM idempotency_record WHERE request_key=?",
        (request_key,),
    ).fetchone()
    if row is None:
        return None
    if row["operation"] != operation or row["payload_hash"] != _payload_hash(payload):
        raise IdempotencyConflictError("request key was reused with different input")
    return json.loads(row["result_json"])


def _save_idempotent(
    connection: sqlite3.Connection,
    request_key: str,
    operation: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    now: str,
) -> None:
    connection.execute(
        "INSERT INTO idempotency_record VALUES (?, ?, ?, ?, ?)",
        (request_key, operation, _payload_hash(payload), json.dumps(result), now),
    )


def _plan_result(connection: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT id, question_id, question_version_id, status, target_kind FROM task "
        "WHERE plan_id=? AND status!='cancelled' ORDER BY created_at, id",
        (plan_id,),
    ).fetchall()
    plan = connection.execute("SELECT * FROM daily_plan WHERE id=?", (plan_id,)).fetchone()
    counts = dict(connection.execute("SELECT q.question_type,COUNT(*) FROM task t JOIN question q "
                                     "ON q.id=t.question_id WHERE t.plan_id=? AND t.target_kind='base' AND t.status!='cancelled' "
                                     "GROUP BY q.question_type", (plan_id,)).fetchall())
    shortages = {key: plan[field] - counts.get(kind, 0)
                 for key, field, kind in (("code", "code_target", "code"), ("unallocated", "theory_target", "theory"))
                 if plan[field] > counts.get(kind, 0)}
    return {"plan_id": plan_id, "tasks": [dict(row) for row in rows], "shortages": shortages}
