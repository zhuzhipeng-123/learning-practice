import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from app.domain import Submission
from app.services.learning_clock import local_date, local_today
from app.services.reflections import mark_model_reflections_stale
from app.services.review import (
    enter_review,
    exposed_recently,
    get_active_round,
    record_valid_pass,
)
from app.services.tasks import IdempotencyConflictError
from app.storage.ids import new_id
from app.storage.transactions import atomic, transaction


class PracticeError(RuntimeError):
    """A requested practice transition is invalid."""


MASTERY_LEVELS = {'unknown', 'vague', 'partial'}


@atomic
def start_attempt(
    connection: sqlite3.Connection,
    task_id: str,
    entry_mode: str,
    started_at: datetime,
) -> str:
    """Freeze task version and active review round when answering starts."""
    task = _load_task_context(connection, task_id)
    _reject_expired_practice(task)
    if task["status"] == 'cancelled':
        raise PracticeError('这道旧待办已作废，请返回当前题单；需要保留的题可在复习库继续练')
    if task["status"] == 'completed':
        raise PracticeError('这次练习已经完成，请查看已保存的记录或从复习库重新开始')
    existing = connection.execute(
        "SELECT id FROM attempt WHERE task_id=? AND submitted_at IS NULL",
        (task_id,),
    ).fetchone()
    if existing is not None:
        return existing["id"]
    active_round = get_active_round(connection, task["question_id"])
    round_id = None
    if active_round is not None and active_round["review_basis_id"] == task["review_basis_id"]:
        round_id = active_round["id"]
    attempt_id = new_id("attempt")
    with transaction(connection):
        connection.execute(
            "INSERT INTO attempt(id, task_id, question_version_id, review_round_id, "
            "entry_mode, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                task_id,
                task["question_version_id"],
                round_id,
                entry_mode,
                started_at.astimezone(UTC).isoformat(),
            ),
        )
        connection.execute(
            "UPDATE task SET status='in_progress', started_at=COALESCE(started_at, ?) WHERE id=?",
            (started_at.astimezone(UTC).isoformat(), task_id),
        )
    return attempt_id


@atomic
def expose_answer(
    connection: sqlite3.Connection,
    task_id: str,
    exposed_at: datetime,
) -> str:
    """Persist answer exposure before reference material is returned."""
    attempt = connection.execute(
        "SELECT * FROM attempt WHERE task_id=? ORDER BY (submitted_at IS NULL) DESC,rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if attempt is None:
        raise PracticeError("no attempt exists")
    timestamp = exposed_at.astimezone(UTC).isoformat()
    connection.execute(
        "UPDATE attempt SET answer_exposed_at=COALESCE(answer_exposed_at, ?) WHERE id=?",
        (timestamp, attempt["id"]),
    )
    connection.execute("INSERT OR IGNORE INTO answer_exposure VALUES (?,?)", (attempt["id"], timestamp))
    return attempt["id"]


@atomic
def submit_code(connection: sqlite3.Connection, submission: Submission) -> dict[str, object]:
    if submission.code_self_result not in {"can_solve", "cannot_solve"}:
        raise PracticeError("code self result is required")
    payload = _submission_payload(submission)
    existing = _load_submission_result(connection, submission.request_key, payload)
    if existing is not None:
        return existing
    attempt = _load_attempt_for_submit(connection, submission.task_id)
    task = _load_task_context(connection, submission.task_id)
    kind = connection.execute("SELECT question_type FROM question WHERE id=?", (task["question_id"],)).fetchone()[0]
    if kind != "code":
        raise PracticeError("task requires a theory answer")
    if submission.mastery_level and submission.mastery_level not in MASTERY_LEVELS:
        raise PracticeError('unsupported mastery level')
    if submission.code_self_result == 'can_solve' and submission.mastery_level:
        raise PracticeError('会做的提交不需要设置不会程度')
    submitted_utc = submission.submitted_at.astimezone(UTC)
    activity_date = local_date(submission.submitted_at).isoformat()
    if submission.code_self_result == 'cannot_solve':
        _require_review_basis_available(connection, task['question_id'], task['review_basis_id'])
    with transaction(connection):
        _complete_attempt(connection, attempt["id"], submission, submitted_utc, activity_date)
        round_id = attempt["review_round_id"]
        mastery_id = None
        if submission.code_self_result == "cannot_solve":
            round_id = enter_review(
                connection,
                task["question_id"],
                task["review_basis_id"],
                "code_cannot_solve",
                submitted_utc,
            )
            connection.execute(
                "UPDATE attempt SET review_round_id=? WHERE id=?",
                (round_id, attempt["id"]),
            )
            if submission.mastery_level:
                mastery_id = _record_mastery(connection, task, attempt['id'], submission.mastery_level,
                                             submission.request_key, submitted_utc)
        passed = submission.code_self_result == "can_solve"
        if passed and not exposed_recently(
            connection,
            task["question_id"],
            submitted_utc,
            exclude_attempt_id=attempt["id"],
        ):
            record_valid_pass(connection, attempt["id"], True)
        result = {
            "attempt_id": attempt["id"],
            "task_completed": True,
            "passed": passed,
            "review_round_id": round_id,
            "review_basis_conflict": False,
            "mastery_id": mastery_id,
            "mastery_level": submission.mastery_level,
        }
        _save_submission_result(connection, submission.request_key, payload, result, submitted_utc)
    return result


@atomic
def submit_theory(connection: sqlite3.Connection, submission: Submission) -> dict[str, object]:
    if not submission.answer_text or not submission.answer_text.strip():
        raise PracticeError("theory answer is required")
    payload = _submission_payload(submission)
    existing = _load_submission_result(connection, submission.request_key, payload)
    if existing is not None:
        return existing
    attempt = _load_attempt_for_submit(connection, submission.task_id)
    kind = connection.execute(
        "SELECT q.question_type FROM task t JOIN question q ON q.id=t.question_id WHERE t.id=?",
        (submission.task_id,),
    ).fetchone()[0]
    if kind != "theory":
        raise PracticeError("task requires a code self assessment")
    submitted_utc = submission.submitted_at.astimezone(UTC)
    activity_date = local_date(submission.submitted_at).isoformat()
    job_id = new_id("job")
    with transaction(connection):
        _complete_attempt(connection, attempt["id"], submission, submitted_utc, activity_date)
        connection.execute(
            "INSERT INTO model_job VALUES (?, ?, 'theory_evaluation', 'pending', 0, NULL, NULL, NULL, ?, ?)",
            (
                job_id,
                f"evaluation:{attempt['id']}",
                submitted_utc.isoformat(),
                submitted_utc.isoformat(),
            ),
        )
        connection.execute("INSERT INTO evaluation_job_target VALUES (?,?)", (job_id, attempt["id"]))
        result = {
            "attempt_id": attempt["id"],
            "task_completed": True,
            "evaluation_status": "pending",
            "review_membership_changed": False,
            "job_id": job_id,
        }
        _save_submission_result(connection, submission.request_key, payload, result, submitted_utc)
    return result


@atomic
def submit_unable(connection: sqlite3.Connection, submission: Submission) -> dict[str, object]:
    if submission.mastery_level not in MASTERY_LEVELS:
        raise PracticeError('请选择不会程度')
    payload = {**_submission_payload(submission), 'submission_mode': 'unable'}
    existing = _load_submission_result(connection, submission.request_key, payload)
    if existing is not None:
        return existing
    attempt = _load_attempt_for_submit(connection, submission.task_id)
    task = _load_task_context(connection, submission.task_id)
    kind = connection.execute('SELECT question_type FROM question WHERE id=?', (task['question_id'],)).fetchone()[0]
    if kind != 'theory':
        raise PracticeError('代码题请使用代码自评提交')
    _require_review_basis_available(connection, task['question_id'], task['review_basis_id'])
    submitted_utc = submission.submitted_at.astimezone(UTC)
    with transaction(connection):
        _complete_attempt(connection, attempt['id'], submission, submitted_utc,
                          local_date(submission.submitted_at).isoformat())
        round_id = enter_review(connection, task['question_id'], task['review_basis_id'],
                                'theory_explicit_unable', submitted_utc)
        connection.execute('UPDATE attempt SET review_round_id=? WHERE id=?', (round_id, attempt['id']))
        mastery_id = _record_mastery(connection, task, attempt['id'], submission.mastery_level,
                                     submission.request_key, submitted_utc)
        result = {'attempt_id': attempt['id'], 'task_completed': True, 'explicit_unable': True,
                  'evaluation_status': 'not_requested', 'review_round_id': round_id,
                  'mastery_id': mastery_id, 'mastery_level': submission.mastery_level}
        _save_submission_result(connection, submission.request_key, payload, result, submitted_utc)
    return result


@atomic
def set_mastery_level(connection, task_id, level, request_key, expected_mastery_id):
    from app.services.tasks import _load_idempotent, _save_idempotent
    if level not in MASTERY_LEVELS:
        raise PracticeError('unsupported mastery level')
    payload = {'task_id': task_id, 'level': level, 'expected_mastery_id': expected_mastery_id}
    saved = _load_idempotent(connection, request_key, 'mastery_level', payload)
    if saved is not None:
        return saved
    task = _load_task_context(connection, task_id)
    _require_review_basis_available(connection, task['question_id'], task['review_basis_id'])
    current = current_mastery(connection, task['question_id'], task['review_basis_id'])
    if expected_mastery_id != (current['id'] if current else None):
        raise IdempotencyConflictError('掌握程度已在其他页面更新，请刷新后重试')
    now = datetime.now(UTC)
    mastery_id = _record_mastery(connection, task, None, level, request_key, now)
    attempt = connection.execute('SELECT activity_date FROM attempt WHERE task_id=? AND submitted_at IS NOT NULL '
                                 'ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone()
    if attempt and attempt['activity_date']:
        connection.execute("UPDATE reflection SET stale=1 WHERE activity_date=? AND author='model'",
                           (attempt['activity_date'],))
    result = {'mastery_id': mastery_id, 'mastery_level': level}
    _save_idempotent(connection, request_key, 'mastery_level', payload, result, now.isoformat())
    return result


def current_mastery(connection, question_id, review_basis_id):
    return connection.execute('SELECT * FROM mastery_assessment WHERE question_id=? AND review_basis_id=? '
                              'ORDER BY rowid DESC LIMIT 1', (question_id, review_basis_id)).fetchone()


@atomic
def adopt_theory_evaluation(
    connection: sqlite3.Connection,
    attempt_id: str,
    verdict: str,
    created_at: datetime,
    corrected_by_user: bool = False,
    expected_adoption: str | None = None,
    protect_adoption: bool = False,
    raw_json: str | None = None,
    model_id: str | None = None,
) -> str:
    if verdict not in {"aligned", "needs_review", "unable_to_assess"}:
        raise ValueError("unsupported verdict")
    if not corrected_by_user and verdict != 'unable_to_assess':
        from app.services.reference_state import verification
        version = connection.execute('SELECT question_version_id FROM attempt WHERE id=?', (attempt_id,)).fetchone()
        if version and not verification(connection, version[0])['verified']:
            raise PracticeError('本版本参考尚未独立核对，不能采用自动评分；请先核对或手动评价')
    evaluation_id = new_id("evaluation")
    with transaction(connection):
        previous = connection.execute(
            "SELECT id FROM evaluation WHERE attempt_id=? AND adopted=1",
            (attempt_id,),
        ).fetchone()
        previous_id = previous["id"] if previous is not None else None
        adopted = not protect_adoption or previous_id == expected_adoption
        if adopted:
            connection.execute("UPDATE evaluation SET adopted=0 WHERE attempt_id=?", (attempt_id,))
        connection.execute(
            "INSERT INTO evaluation VALUES (?, ?, 'complete', ?, ?, ?, 'repair-v1', 'v1', ?, ?, ?, ?)",
            (
                evaluation_id,
                attempt_id,
                verdict,
                raw_json,
                model_id,
                created_at.isoformat(),
                previous_id,
                int(adopted),
                int(corrected_by_user),
            ),
        )
        if adopted:
            record_valid_pass(connection, attempt_id, verdict == "aligned")
            activity = connection.execute("SELECT activity_date FROM attempt WHERE id=?", (attempt_id,)).fetchone()
            if activity and activity[0]:
                connection.execute("UPDATE reflection SET stale=1 WHERE activity_date=? AND author='model'", (activity[0],))
    return evaluation_id


@atomic
def correct_theory_evaluation(connection, attempt_id, verdict, request_key, expected_adoption):
    from app.services.tasks import _load_idempotent, _save_idempotent
    payload = {'attempt_id': attempt_id, 'verdict': verdict, 'expected_adoption': expected_adoption}
    saved = _load_idempotent(connection, request_key, 'manual_evaluation', payload)
    if saved:
        return saved
    current = connection.execute('SELECT id FROM evaluation WHERE attempt_id=? AND adopted=1', (attempt_id,)).fetchone()
    if expected_adoption != (current[0] if current else None):
        raise IdempotencyConflictError('评价已更新，请重新读取当前评价后再修改')
    now = datetime.now(UTC)
    result = {'evaluation_id': adopt_theory_evaluation(connection, attempt_id, verdict, now, corrected_by_user=True)}
    _save_idempotent(connection, request_key, 'manual_evaluation', payload, result, now.isoformat())
    return result


def add_theory_to_review(
    connection: sqlite3.Connection,
    question_id: str,
    entered_by: str,
    happened_at: datetime,
    version_id: str | None = None,
) -> str:
    version = connection.execute(
        "SELECT v.review_basis_id FROM question q JOIN question_version v "
        "ON v.question_id=q.id AND v.id=COALESCE(?,q.current_version_id) WHERE q.id=? AND q.question_type='theory'",
        (version_id, question_id),
    ).fetchone()
    if version is None:
        raise PracticeError("theory question was not found")
    with transaction(connection):
        active = get_active_round(connection, question_id)
        if active and active['review_basis_id'] != version['review_basis_id']:
            raise PracticeError('复习库已有另一个版本，请前往复习库继续该版本；旧任务不会替换当前复习依据')
        return enter_review(
            connection,
            question_id,
            version["review_basis_id"],
            entered_by,
            happened_at.astimezone(UTC),
        )


def _complete_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
    submission: Submission,
    submitted_at: datetime,
    activity_date: str,
) -> None:
    mark_model_reflections_stale(connection, local_date(submission.submitted_at))
    connection.execute(
        "UPDATE attempt SET submitted_at=?, activity_date=?, answer_text=?, "
        "code_self_result=?, note=?, answer_exposed_at=COALESCE(answer_exposed_at, ?) "
        "WHERE id=? AND submitted_at IS NULL",
        (
            submitted_at.isoformat(),
            activity_date,
            submission.answer_text,
            submission.code_self_result,
            submission.note,
            submission.answer_exposed_at.isoformat() if submission.answer_exposed_at else None,
            attempt_id,
        ),
    )
    connection.execute(
        "UPDATE task SET status='completed', completed_at=COALESCE(completed_at, ?) WHERE id=?",
        (submitted_at.isoformat(), submission.task_id),
    )
    connection.execute(
        "UPDATE question SET first_submitted_at=COALESCE(first_submitted_at, ?) "
        "WHERE id=(SELECT question_id FROM task WHERE id=?)",
        (submitted_at.isoformat(), submission.task_id),
    )


def _load_task_context(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT t.*,p.plan_date,v.review_basis_id FROM task t JOIN daily_plan p ON p.id=t.plan_id "
        "JOIN question_version v ON v.id=t.question_version_id WHERE t.id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise PracticeError("task was not found")
    return row


def _load_attempt_for_submit(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    task = _load_task_context(connection, task_id)
    _reject_expired_practice(task)
    row = connection.execute(
        "SELECT a.* FROM attempt a JOIN task t ON t.id=a.task_id "
        "WHERE a.task_id=? AND a.submitted_at IS NULL AND t.status='in_progress'",
        (task_id,),
    ).fetchone()
    if row is None:
        if task['status'] == 'cancelled':
            raise PracticeError('这道旧待办已作废，请返回当前题单；需要保留的题可在复习库继续练')
        raise PracticeError('这次作答已结束或尚未开始，请刷新后查看当前状态')
    return row


def _reject_expired_practice(task) -> None:
    if task['origin'] in {'daily', 'free_practice'} and task['plan_date'] < local_today().isoformat():
        raise PracticeError('这道旧题已跨天过期，回答没有提交；草稿仍保留。请返回今天并重新确认题单')


def _require_review_basis_available(connection, question_id, review_basis_id):
    active = get_active_round(connection, question_id)
    if active and active['review_basis_id'] != review_basis_id:
        raise PracticeError('复习库已有另一个版本，请先选择要保留的复习依据；本次提交尚未保存')


def _record_mastery(connection, task, attempt_id, level, request_key, created_at):
    mastery_id = new_id('mastery')
    connection.execute('INSERT INTO mastery_assessment VALUES (?,?,?,?,?,?,?)',
                       (mastery_id, task['question_id'], task['review_basis_id'], attempt_id,
                        level, request_key, created_at.isoformat()))
    return mastery_id


def _submission_payload(submission: Submission) -> dict[str, str | None]:
    payload = {
        "task_id": submission.task_id,
        "answer_text": submission.answer_text,
        "code_self_result": submission.code_self_result,
        "note": submission.note,
    }
    # Preserve the exact legacy hash when an old client did not submit a level.
    if submission.mastery_level is not None:
        payload['mastery_level'] = submission.mastery_level
    return payload


def _payload_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _load_submission_result(
    connection: sqlite3.Connection,
    request_key: str,
    payload: dict[str, object],
) -> dict[str, object] | None:
    row = connection.execute(
        "SELECT operation, payload_hash, result_json FROM idempotency_record WHERE request_key=?",
        (request_key,),
    ).fetchone()
    if row is None:
        return None
    if row["operation"] != "submit" or row["payload_hash"] != _payload_hash(payload):
        raise IdempotencyConflictError("request key was reused with different input")
    return json.loads(row["result_json"])


def _save_submission_result(
    connection: sqlite3.Connection,
    request_key: str,
    payload: dict[str, object],
    result: dict[str, object],
    submitted_at: datetime,
) -> None:
    connection.execute(
        "INSERT INTO idempotency_record VALUES (?, 'submit', ?, ?, ?)",
        (
            request_key,
            _payload_hash(payload),
            json.dumps(result),
            submitted_at.isoformat(),
        ),
    )
