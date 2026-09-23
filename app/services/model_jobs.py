import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from time import monotonic

from app.adapters.agnes import AgnesClient, AgnesError, AgnesRateLimitError
from app.services.evaluations import EvaluationValidationError, adopt_model_evaluation
from app.services.llm_config import client_for_config, freeze_request
from app.services.materials import material_closure
from app.services.model_diagnostics import log_event, started_at
from app.services.model_json import ModelJSONError, complete_json, parse_model_json
from app.storage.ids import new_id
from app.storage.transactions import transaction

LEASE_DURATION = timedelta(minutes=5)
JOB_EXECUTION_BUDGET = timedelta(minutes=60)
HEARTBEAT_INTERVAL_SECONDS = 30
_deadline_lock = Lock()
_monotonic_deadlines = {}


class ModelJobError(RuntimeError):
    """A model job is busy, invalid, or failed; its saved answer remains intact."""

    def __init__(self, message, retry_at=None, *, in_progress=False):
        super().__init__(message)
        self.retry_at = retry_at
        self.in_progress = in_progress


def create_reevaluation_job(connection, attempt_id, request_key):
    if not request_key or len(request_key) > 200:
        raise ModelJobError("请求标识不能为空或超过 200 字符")
    with transaction(connection):
        existing = connection.execute("SELECT j.id,t.attempt_id FROM model_job j JOIN evaluation_job_target t "
                                      "ON t.job_id=j.id WHERE j.business_key=?", (f"reevaluation:{request_key}",)).fetchone()
        if existing:
            if existing["attempt_id"] != attempt_id:
                raise ModelJobError("同一请求标识不能用于不同作答")
            return existing["id"]
        answer = connection.execute("SELECT a.id FROM attempt a JOIN task t ON t.id=a.task_id "
                                    "JOIN question q ON q.id=t.question_id WHERE a.id=? "
                                    "AND a.submitted_at IS NOT NULL AND a.answer_text IS NOT NULL "
                                    "AND q.question_type='theory'", (attempt_id,)).fetchone()
        if answer is None:
            raise ModelJobError("没有已保存的理论作答可重新评价")
        job_id = new_id("job")
        now = datetime.now(UTC).isoformat()
        connection.execute("INSERT INTO model_job VALUES (?,?,'theory_evaluation','pending',0,NULL,NULL,NULL,?,?)",
                           (job_id, f"reevaluation:{request_key}", now, now))
        connection.execute("INSERT INTO evaluation_job_target VALUES (?,?)", (job_id, attempt_id))
        context = _load_job_context(connection, job_id)
        freeze_request(connection, job_id, "theory_evaluation", json.loads(_evaluation_prompt(context)))
        return job_id


def run_evaluation_job(connection, job_id: str, client: AgnesClient | None = None) -> str:
    context = _claim_job(connection, job_id)
    if context.get("result_id"):
        return context["result_id"]
    execution_id = context["execution_id"]
    log_event(job_id, "theory_evaluation", execution_id, "claim")
    try:
        with job_lease_heartbeat(connection, job_id, execution_id):
            request = context["request"]
            config = json.loads(request["config_json"])
            model = client or client_for_config(config)
            call_started = started_at()
            reply = complete_json(model, [
                {"role": "system", "content": config["system_prompt"]},
                {"role": "user", "content": request["input_json"]},
            ], config["max_tokens"])
            log_event(job_id, "theory_evaluation", execution_id, "call",
                      started=call_started, reply=reply)
            with transaction(connection):
                update_model_request_owned(connection, job_id, execution_id,
                                           response_text=reply.content, response_model=reply.model)
            payload = parse_model_json(reply.content)
            if isinstance(payload, dict) and not (context["reference_text"] or "").strip() and payload.get("verdict") != "unable_to_assess":
                raise EvaluationValidationError("缺少可核验参考文字，不能采用通过或需复习判断；请人工评价")
            with transaction(connection):
                assert_job_owner(connection, job_id, execution_id)
                evaluation_id = adopt_model_evaluation(
                    connection, context["attempt_id"], payload, set(context["reference_ids"]),
                    datetime.now(UTC), expected_adoption=context["adopted_id"],
                    protect_adoption=True, model_id=reply.model,
                )
                connection.execute("UPDATE evaluation SET prompt_version=? WHERE id=?", (request["prompt_hash"], evaluation_id))
                update_model_request_owned(connection, job_id, execution_id, response_text=reply.content)
                changed = connection.execute(
                    "UPDATE model_job SET status='complete',result_id=?,error=NULL,updated_at=? WHERE id=? "
                    "AND status='running' AND EXISTS(SELECT 1 FROM model_job_execution x "
                    "WHERE x.job_id=model_job.id AND x.execution_id=? AND x.deadline_at>?)",
                    (evaluation_id, datetime.now(UTC).isoformat(), job_id, execution_id,
                     datetime.now(UTC).isoformat()),
                ).rowcount
                if not changed:
                    raise ModelJobError("job lease changed; late result was not adopted")
            log_event(job_id, "theory_evaluation", execution_id, "adopt")
            return evaluation_id
    except Exception as error:
        log_event(job_id, "theory_evaluation", execution_id, "fail", error=error)
        raise fail_job(connection, job_id, execution_id, error) from error


def fail_job(connection, job_id, execution_id, error):
    safe = str(error) if isinstance(error, (AgnesError, EvaluationValidationError, ModelJobError, ModelJSONError)) else '模型请求未完成，请按原设置重试；若反复失败，请检查模型服务或稍后再试。'
    retry_at = error.retry_at if isinstance(error, (AgnesRateLimitError, ModelJobError)) else None
    if retry_at:
        safe += f"；请在 {retry_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} 后重试"
    with transaction(connection):
        changed = connection.execute(
            "UPDATE model_job SET status='failed',retry_count=retry_count+1,error=?,updated_at=?,next_retry_at=? "
            "WHERE id=? AND status='running' AND EXISTS(SELECT 1 FROM model_job_execution x "
            "WHERE x.job_id=model_job.id AND x.execution_id=?)",
            (safe, datetime.now(UTC).isoformat(), retry_at.isoformat() if retry_at else None, job_id, execution_id),
        ).rowcount
        if changed and retry_at:
            request = connection.execute("SELECT config_json FROM model_request WHERE job_id=?", (job_id,)).fetchone()
            provider = json.loads(request[0])["provider"]
            connection.execute("INSERT INTO provider_cooldown VALUES (?,?) ON CONFLICT(provider) "
                               "DO UPDATE SET retry_at=MAX(retry_at,excluded.retry_at)", (provider, retry_at.isoformat()))
    return ModelJobError(safe, retry_at, in_progress=getattr(error, 'in_progress', False))


def _claim_job(connection: sqlite3.Connection, job_id: str, context_loader=None) -> dict:
    now = datetime.now(UTC)
    cutoff = (now - LEASE_DURATION).isoformat()
    with transaction(connection):
        job = connection.execute("SELECT * FROM model_job WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise ModelJobError("evaluation job was not found")
        if job["status"] == "complete":
            return {"result_id": job["result_id"]}
        execution = connection.execute("SELECT * FROM model_job_execution WHERE job_id=?", (job_id,)).fetchone()
        if job["status"] == "running":
            live_execution = execution and execution["lease_expires_at"] > now.isoformat() and execution["deadline_at"] > now.isoformat()
            legacy_execution = execution is None and job["updated_at"] > cutoff
            if live_execution or legacy_execution:
                raise ModelJobError("请求仍在处理中，请稍后恢复同一请求。", in_progress=True)
        context = (context_loader or _load_job_context)(connection, job_id)
        if context_loader is None:
            context["request"] = freeze_request(connection, job_id, "theory_evaluation", json.loads(_evaluation_prompt(context)))
            frozen = json.loads(context['request']['input_json'])
            # Retries use precisely the reference/correction that the model received.
            context['reference_text'] = frozen['reference'] if context['reference_verification']['verified'] else None
            context['reference_ids'] = frozen['reference_ids']
            adopted = connection.execute("SELECT id,corrected_by_user FROM evaluation WHERE attempt_id=? AND adopted=1",
                                         (context["attempt_id"],)).fetchone()
            context["adopted_id"] = ("manual:" + adopted["id"] if adopted["corrected_by_user"] else adopted["id"]) if adopted else None
        provider = json.loads(context["request"]["config_json"])["provider"]
        cooldown = connection.execute("SELECT retry_at FROM provider_cooldown WHERE provider=?", (provider,)).fetchone()
        if cooldown and datetime.fromisoformat(cooldown[0]) > now:
            deadline = datetime.fromisoformat(cooldown[0])
            raise ModelJobError(f"{provider} 正在限流，请在 {deadline.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} 后重试", deadline)
        context["execution_id"] = new_id("execution")
        context["claimed_at"] = now.isoformat()
        deadline = now + JOB_EXECUTION_BUDGET
        lease_expires = min(now + LEASE_DURATION, deadline)
        connection.execute(
            "INSERT INTO model_job_execution VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "execution_id=excluded.execution_id,started_at=excluded.started_at,heartbeat_at=excluded.heartbeat_at,"
            "lease_expires_at=excluded.lease_expires_at,deadline_at=excluded.deadline_at",
            (job_id, context["execution_id"], now.isoformat(), now.isoformat(),
             lease_expires.isoformat(), deadline.isoformat()),
        )
        with _deadline_lock:
            _monotonic_deadlines[context["execution_id"]] = monotonic() + JOB_EXECUTION_BUDGET.total_seconds()
        connection.execute(
            "UPDATE model_job SET status='running',updated_at=?,error=NULL,next_retry_at=NULL WHERE id=?",
            (context["claimed_at"], job_id),
        )
        return context


def assert_job_owner(connection, job_id, execution_id):
    if not _within_monotonic_budget(execution_id):
        raise ModelJobError("模型任务已超过本次总预算，迟到结果未写入")
    owner = connection.execute(
        "SELECT 1 FROM model_job j JOIN model_job_execution x ON x.job_id=j.id "
        "WHERE j.id=? AND j.status='running' AND x.execution_id=? AND x.deadline_at>?",
        (job_id, execution_id, datetime.now(UTC).isoformat()),
    ).fetchone()
    if owner is None:
        raise ModelJobError("请求执行权已转移，迟到结果未写入")


def load_model_request_owned(connection, job_id, execution_id):
    assert_job_owner(connection, job_id, execution_id)
    row = connection.execute("SELECT * FROM model_request WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise ModelJobError("模型请求快照不存在")
    return dict(row)


def update_model_request_owned(connection, job_id, execution_id, **values):
    allowed = {"config_json", "response_text", "response_model"}
    if not values or not set(values) <= allowed:
        raise ValueError("unsupported model request update")
    if not _within_monotonic_budget(execution_id):
        raise ModelJobError("模型任务已超过本次总预算，迟到的中间结果未写入")
    assignments = ",".join(f"{name}=?" for name in values)
    changed = connection.execute(
        f"UPDATE model_request SET {assignments} WHERE job_id=? AND EXISTS("
        "SELECT 1 FROM model_job j JOIN model_job_execution x ON x.job_id=j.id "
        "WHERE j.id=model_request.job_id AND j.status='running' AND x.execution_id=? "
        "AND x.deadline_at>?)",
        (*values.values(), job_id, execution_id, datetime.now(UTC).isoformat()),
    ).rowcount
    if not changed:
        raise ModelJobError("请求执行权已转移，迟到的中间结果未写入")


def renew_job_lease(connection, job_id, execution_id, now=None):
    now = now or datetime.now(UTC)
    if not _within_monotonic_budget(execution_id):
        return False
    row = connection.execute(
        "SELECT deadline_at FROM model_job_execution WHERE job_id=? AND execution_id=?",
        (job_id, execution_id),
    ).fetchone()
    if row is None or datetime.fromisoformat(row["deadline_at"]) <= now:
        return False
    deadline = datetime.fromisoformat(row["deadline_at"])
    lease_expires = min(now + LEASE_DURATION, deadline)
    with transaction(connection):
        return bool(connection.execute(
            "UPDATE model_job_execution SET heartbeat_at=?,lease_expires_at=? WHERE job_id=? "
            "AND execution_id=? AND EXISTS(SELECT 1 FROM model_job j WHERE j.id=? AND j.status='running')",
            (now.isoformat(), lease_expires.isoformat(), job_id, execution_id, job_id),
        ).rowcount)


@contextmanager
def job_lease_heartbeat(connection, job_id, execution_id):
    location = connection.execute("PRAGMA database_list").fetchone()[2]
    stop = Event()

    def maintain():
        while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            heartbeat_connection = None
            try:
                from app.storage.database import connect_database
                heartbeat_connection = connect_database(location)
                if not renew_job_lease(heartbeat_connection, job_id, execution_id):
                    return
            except sqlite3.Error:
                continue
            finally:
                if heartbeat_connection is not None:
                    heartbeat_connection.close()

    worker = None
    if location:
        worker = Thread(target=maintain, name=f"model-heartbeat-{job_id[-8:]}", daemon=True)
        worker.start()
    try:
        yield
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=1)
        with _deadline_lock:
            _monotonic_deadlines.pop(execution_id, None)


def _within_monotonic_budget(execution_id):
    with _deadline_lock:
        deadline = _monotonic_deadlines.get(execution_id)
    return deadline is None or monotonic() < deadline


def _load_job_context(connection: sqlite3.Connection, job_id: str) -> dict:
    row = connection.execute(
        "SELECT a.id AS attempt_id,a.answer_text,v.prompt,v.reference_text,v.id AS version_id,"
        "v.material_status "
        "FROM model_job j JOIN evaluation_job_target target ON target.job_id=j.id "
        "JOIN attempt a ON a.id=target.attempt_id JOIN question_version v ON v.id=a.question_version_id "
        "WHERE j.id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise ModelJobError("saved answer for this job was not found")
    refs = connection.execute(
        "SELECT reference_ids_json,materials_json FROM version_resources WHERE version_id=?", (row["version_id"],),
    ).fetchone()
    image_reference = bool(refs and any(item.get('role') == 'reference' and item.get('kind') == 'media'
                                       for item in material_closure(json.loads(refs['materials_json']))))
    from app.services.reference_corrections import get_correction
    from app.services.reference_state import verification
    state = verification(connection, row['version_id'])
    correction = get_correction(connection, row['version_id'])
    reference_ids = json.loads(refs[0] if refs else "[]")
    if correction:
        reference_ids.append(correction['id'])
    return {"attempt_id": row["attempt_id"], "prompt": row["prompt"],
            "reference_text": row["reference_text"] if state['verified'] and not image_reference and row['material_status'] in {'complete','verified','text_complete'} else None,
            "answer_text": row["answer_text"],
            "reference_verification": state, "reference_correction": correction, "reference_ids": reference_ids}


def _evaluation_prompt(context: dict) -> str:
    return json.dumps({"question": context["prompt"], "reference": context["reference_text"],
                       "reference_verification": context['reference_verification'], "reference_correction": context['reference_correction'],
                       "reference_ids": context["reference_ids"], "user_answer": context["answer_text"]},
                      ensure_ascii=False)
