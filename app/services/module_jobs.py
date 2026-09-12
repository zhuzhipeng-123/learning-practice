"""Explicit model calls for interview and reflection, each with its own job lease."""

import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.services.interview import add_turn
from app.services.llm_config import client_for_config, freeze_request
from app.services.model_jobs import ModelJobError, _claim_job, fail_job
from app.services.model_json import parse_model_json
from app.services.reflections import (
    activity_timeline,
    mark_model_reflections_stale,
    save_model_reflection,
)
from app.storage.ids import new_id
from app.storage.transactions import transaction


def interview_context(connection, session_id, module):
    row = connection.execute("SELECT s.*,v.prompt,v.reference_text FROM interview_session s "
                             "JOIN question_version v ON v.id=s.question_version_id WHERE s.id=?", (session_id,)).fetchone()
    if row is None or (module == "interview_followup" and row["status"] != "active"):
        raise ModelJobError("没有可继续的面试会话")
    turns = [dict(item) for item in connection.execute("SELECT t.id,t.role,t.content FROM interview_turn t "
        "WHERE t.session_id=? AND NOT EXISTS (SELECT 1 FROM model_job j "
        "WHERE j.result_id=t.id AND j.purpose='interview_feedback') ORDER BY t.rowid", (session_id,))]
    if not any(turn["role"] == "user" for turn in turns):
        raise ModelJobError("请先保存回答，再生成追问或总结")
    if module == "interview_followup" and turns[-1]["role"] != "user":
        raise ModelJobError("请先回答当前追问")
    return {"session_id": session_id, "question": row["prompt"], "turns": turns,
            'job_focus': (connection.execute('SELECT job_focus FROM interview_setup WHERE session_id=?', (session_id,)).fetchone() or [''])[0],
            "reference": row["reference_text"] if module == "interview_feedback" else None}


def reflection_context(connection, day):
    timeline = activity_timeline(connection, day)
    turns = [dict(row) for row in connection.execute("SELECT id,role,content FROM interview_turn "
                                                    "WHERE date(created_at,'+8 hours')=? ORDER BY rowid", (day.isoformat(),))]
    evaluations = [dict(row) for row in connection.execute("SELECT e.id,e.attempt_id,e.verdict,e.raw_json FROM evaluation e "
                                                          "JOIN attempt a ON a.id=e.attempt_id WHERE a.activity_date=? "
                                                          "AND e.adopted=1 ORDER BY e.rowid", (day.isoformat(),))]
    records = [*timeline["attempts"], *turns, *evaluations]
    if not records:
        raise ModelJobError("当天没有可用于反思的学习记录")
    adopted = {item['attempt_id']: item['verdict'] for item in evaluations}
    wrong = [item for item in timeline['attempts'] if item['code_self_result'] == 'cannot_solve' or adopted.get(item['id']) == 'needs_review']
    for item in timeline['attempts']:
        item['code_self_result'] = {'can_solve': '会', 'cannot_solve': '不会'}.get(item['code_self_result'])
    return {"activity_date": day.isoformat(), "activity_units": timeline["activity_units"], "records": records, 'wrong_answers': wrong}


def run_module_job(connection, module, target, request_key, client=None, context_override=None):
    if module not in {"interview_followup", "interview_feedback", "daily_reflection", "source_parsing", "practice_selection"}:
        raise ModelJobError("不支持的模型模块")
    with transaction(connection):
        business_key = f"{module}:{request_key}"
        job = connection.execute("SELECT * FROM model_job WHERE business_key=?", (business_key,)).fetchone()
        if job:
            job_id = job["id"]
            saved = json.loads(connection.execute("SELECT input_json FROM model_request WHERE job_id=?", (job_id,)).fetchone()[0])
            if saved.get("activity_date", saved.get("session_id", saved.get("source_id"))) != target:
                raise ModelJobError("同一请求标识不能用于不同对象")
        else:
            if module in {'source_parsing', 'practice_selection'}:
                from app.services.source_coverage import parsing_context
                context = context_override or parsing_context(connection, target)
            else:
                context = reflection_context(connection, date.fromisoformat(target)) if module == "daily_reflection" else interview_context(connection, target, module)
            job_id = new_id("job")
            now = datetime.now(UTC).isoformat()
            connection.execute("INSERT INTO model_job VALUES (?, ?, ?, 'pending',0,NULL,NULL,NULL,?,?)", (job_id, business_key, module, now, now))
            freeze_request(connection, job_id, module, context)
    claimed = _claim_job(connection, job_id, context_loader=_load_request)
    if claimed.get("result_id"):
        return _result(connection, job_id)
    try:
        request = claimed["request"]
        config = json.loads(request["config_json"])
        model = client or client_for_config(config)
        reply = model.complete([{"role": "system", "content": config["system_prompt"]},
                                {"role": "user", "content": request["input_json"]}], max_tokens=config["max_tokens"])
        with transaction(connection):
            connection.execute("UPDATE model_request SET response_text=?,response_model=? WHERE job_id=? AND EXISTS "
                               "(SELECT 1 FROM model_job WHERE id=? AND status='running' AND updated_at=?)",
                               (reply.content, reply.model, job_id, job_id, claimed["claimed_at"]))
        with transaction(connection):
            if not connection.execute("SELECT 1 FROM model_job WHERE id=? AND status='running' AND updated_at=?",
                                      (job_id, claimed["claimed_at"])).fetchone():
                raise ModelJobError("请求已过期，迟到结果没有写入学习记录")
            context = json.loads(request["input_json"])
            result_id = _save_result(connection, module, context, reply.content)
            connection.execute("UPDATE model_request SET response_text=? WHERE job_id=?", (reply.content, job_id))
            connection.execute("UPDATE model_job SET status='complete',result_id=?,updated_at=?,error=NULL WHERE id=?",
                               (result_id, datetime.now(UTC).isoformat(), job_id))
        return _result(connection, job_id)
    except Exception as error:
        raise fail_job(connection, job_id, claimed["claimed_at"], error) from error


def _load_request(connection, job_id):
    return {"request": dict(connection.execute("SELECT * FROM model_request WHERE job_id=?", (job_id,)).fetchone())}


def _save_result(connection, module, context, text):
    now = datetime.now(UTC)
    if module == 'practice_selection':
        from app.services.practice_selection import validate_selection
        validate_selection(context, parse_model_json(text))
        return new_id('selection')
    if module == "source_parsing":
        from app.services.source_coverage import validate_suggestions
        validate_suggestions(context, parse_model_json(text))
        return new_id("analysis")
    if module == "daily_reflection":
        payload = parse_model_json(text)
        allowed = {record["id"] for record in context["records"]}
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str) or not payload["content"].strip():
            raise ModelJobError("模型反思格式不正确")
        ids = payload.get("covered_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or item not in allowed for item in ids):
            raise ModelJobError("模型反思引用了不存在的记录")
        day = date.fromisoformat(context["activity_date"])
        result_id = save_model_reflection(connection, day, payload["content"], ids, now)
        if reflection_context(connection, day) != context:
            connection.execute("UPDATE reflection SET stale=1 WHERE id=?", (result_id,))
        return result_id
    current = interview_context(connection, context["session_id"], module)
    if current["turns"] != context["turns"]:
        raise ModelJobError("对话已更新，本次结果未追加；请刷新后重新生成")
    if module == "interview_followup":
        return add_turn(connection, context["session_id"], "assistant", text, now)
    turn_id = new_id("turn")
    connection.execute("INSERT INTO interview_turn VALUES (?,?, 'assistant',?,?)",
                       (turn_id, context["session_id"], text, now.isoformat()))
    mark_model_reflections_stale(connection, now.astimezone(ZoneInfo("Asia/Shanghai")).date())
    return turn_id


def _result(connection, job_id):
    request = connection.execute("SELECT module,input_json FROM model_request WHERE job_id=?", (job_id,)).fetchone()
    reflection_content = None
    if request['module'] == 'daily_reflection':
        from app.services.reflections import view_model_reflection
        result_id = connection.execute('SELECT result_id FROM model_job WHERE id=?', (job_id,)).fetchone()[0]
        reflection_content = view_model_reflection(connection, result_id, datetime.now(UTC))['content']
    if request["module"] == "interview_feedback":
        session_id = json.loads(request["input_json"])["session_id"]
        with transaction(connection):
            connection.execute("INSERT OR IGNORE INTO question_exposure SELECT t.question_id,? FROM task t "
                               "JOIN interview_session s ON s.task_id=t.id WHERE s.id=?", (datetime.now(UTC).isoformat(), session_id))
    row = connection.execute("SELECT j.id AS job_id,j.result_id,r.response_text,r.response_model FROM model_job j "
                             "JOIN model_request r ON r.job_id=j.id WHERE j.id=?", (job_id,)).fetchone()
    return {**dict(row), **({'content': reflection_content} if reflection_content is not None else {})}
