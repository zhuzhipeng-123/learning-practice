import json
import sqlite3
from datetime import UTC, datetime, timedelta

from app.adapters.agnes import AgnesClient, AgnesError, AgnesRateLimitError
from app.services.evaluations import EvaluationValidationError, adopt_model_evaluation
from app.services.llm_config import client_for_config, freeze_request
from app.services.model_json import ModelJSONError, complete_json, parse_model_json
from app.storage.ids import new_id
from app.storage.transactions import transaction

LEASE_DURATION = timedelta(minutes=5)


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
    claimed_at = context["claimed_at"]
    try:
        request = context["request"]
        config = json.loads(request["config_json"])
        model = client or client_for_config(config)
        reply = complete_json(model, [
            {"role": "system", "content": config["system_prompt"]},
            {"role": "user", "content": request["input_json"]},
        ], config["max_tokens"])
        with transaction(connection):
            connection.execute("UPDATE model_request SET response_text=?,response_model=? WHERE job_id=? AND EXISTS "
                               "(SELECT 1 FROM model_job WHERE id=? AND status='running' AND updated_at=?)",
                               (reply.content, reply.model, job_id, job_id, claimed_at))
        payload = parse_model_json(reply.content)
        if isinstance(payload, dict) and not (context["reference_text"] or "").strip() and payload.get("verdict") != "unable_to_assess":
            raise EvaluationValidationError("缺少可核验参考文字，不能采用通过或需复习判断；请人工评价")
        with transaction(connection):
            owner = connection.execute(
                "SELECT 1 FROM model_job WHERE id=? AND status='running' AND updated_at=?",
                (job_id, claimed_at),
            ).fetchone()
            if owner is None:
                raise ModelJobError("job lease changed; late result was not adopted")
            evaluation_id = adopt_model_evaluation(
                connection, context["attempt_id"], payload, set(context["reference_ids"]),
                datetime.now(UTC), expected_adoption=context["adopted_id"],
                protect_adoption=True, model_id=reply.model,
            )
            connection.execute("UPDATE evaluation SET prompt_version=? WHERE id=?", (request["prompt_hash"], evaluation_id))
            connection.execute("UPDATE model_request SET response_text=? WHERE job_id=?", (reply.content, job_id))
            connection.execute(
                "UPDATE model_job SET status='complete',result_id=?,error=NULL,updated_at=? WHERE id=?",
                (evaluation_id, datetime.now(UTC).isoformat(), job_id),
            )
        return evaluation_id
    except Exception as error:
        raise fail_job(connection, job_id, claimed_at, error) from error


def fail_job(connection, job_id, claimed_at, error):
    safe = str(error) if isinstance(error, (AgnesError, EvaluationValidationError, ModelJobError, ModelJSONError)) else '模型请求未完成，请按原设置重试；若反复失败，请检查模型服务或稍后再试。'
    retry_at = error.retry_at if isinstance(error, (AgnesRateLimitError, ModelJobError)) else None
    if retry_at:
        safe += f"；请在 {retry_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} 后重试"
    with transaction(connection):
        changed = connection.execute(
            "UPDATE model_job SET status='failed',retry_count=retry_count+1,error=?,updated_at=?,next_retry_at=? "
            "WHERE id=? AND status='running' AND updated_at=?",
            (safe, datetime.now(UTC).isoformat(), retry_at.isoformat() if retry_at else None, job_id, claimed_at),
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
        if job["status"] == "running" and job["updated_at"] > cutoff:
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
        context["claimed_at"] = now.isoformat()
        connection.execute(
            "UPDATE model_job SET status='running',updated_at=?,error=NULL,next_retry_at=NULL WHERE id=?",
            (context["claimed_at"], job_id),
        )
        return context


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
                                       for item in json.loads(refs['materials_json'])))
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
