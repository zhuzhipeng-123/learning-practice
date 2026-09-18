"""Explicit model calls for interview and reflection, each with its own job lease."""

import json
from datetime import UTC, date, datetime

from app.services.interview import add_turn
from app.services.learning_clock import local_date, utc_bounds_for_local_days
from app.services.llm_config import client_for_config, freeze_request
from app.services.model_jobs import ModelJobError, _claim_job, fail_job
from app.services.model_json import complete_json, parse_model_json
from app.services.reflections import (
    activity_timeline,
    mark_model_reflections_stale,
    save_model_reflection,
)
from app.storage.ids import new_id
from app.storage.transactions import transaction


def interview_context(connection, session_id, module):
    from app.services.interview_conversation import (
        bounded_dialogue,
        conversation_turns,
        learning_context,
    )
    row = connection.execute("SELECT s.*,v.prompt,v.reference_text,q.question_type,q.source_kind FROM interview_session s "
                             "JOIN question_version v ON v.id=s.question_version_id JOIN question q ON q.id=v.question_id WHERE s.id=?", (session_id,)).fetchone()
    if row is None or (module == "interview_followup" and row["status"] != "active"):
        raise ModelJobError("没有可继续的面试会话")
    turns = conversation_turns(connection, session_id)
    if not any(turn["role"] == "user" for turn in turns):
        raise ModelJobError("请先保存回答，再生成追问或总结")
    if module == "interview_followup" and turns[-1]["role"] != "user":
        raise ModelJobError("请先回答当前追问")
    compact, window = bounded_dialogue(turns)
    return {"session_id": session_id, "question": row["prompt"][:4000], "question_version_id": row['question_version_id'], "turns": compact,
            'conversation_revision': turns[-1]['id'], 'context_window': window,
            **({'feedback_format': 'evidence_v1'} if module == 'interview_feedback' else {}),
            'prior_questions': [turn['content'][:300] for turn in turns[:-24] if turn['role'] == 'assistant'][-32:],
            **({'learning_context': learning_context(connection)} if module == 'interview_followup' else {}),
            'unanswered_question_ids': unanswered_questions(turns),
            'question_type': row['question_type'],
            'job_focus': (connection.execute('SELECT job_focus FROM interview_setup WHERE session_id=?', (session_id,)).fetchone() or [''])[0],
            "reference": (row["reference_text"] or '')[:8000] if module == "interview_feedback" and row['source_kind'] == 'feishu' else None}


def unanswered_questions(turns):
    answered_sessions, unanswered = set(), []
    for turn in reversed(turns):
        session = turn.get('session_id', '')
        if turn['role'] == 'user':
            answered_sessions.add(session)
        elif session not in answered_sessions:
            unanswered.append(turn['id'])
    return list(reversed(unanswered))


def reflection_context(connection, day):
    timeline = activity_timeline(connection, day)
    start_at, end_at = utc_bounds_for_local_days(day, day)
    turns = [dict(row) for row in connection.execute(
        "SELECT it.id,it.session_id,it.role,it.content,v.prompt AS main_question,t.question_id,"
        "EXISTS(SELECT 1 FROM model_job j WHERE j.result_id=it.id AND j.purpose='interview_feedback') AS is_feedback "
        "FROM interview_turn it JOIN interview_session s ON s.id=it.session_id "
        "JOIN task t ON t.id=s.task_id JOIN question_version v ON v.id=s.question_version_id "
        "WHERE julianday(it.created_at)>=julianday(?) AND julianday(it.created_at)<julianday(?) "
        "AND NOT EXISTS (SELECT 1 FROM model_job j WHERE j.result_id=it.id "
        "AND j.purpose='interview_feedback') ORDER BY it.rowid", (start_at, end_at))]
    evaluations = [dict(row) for row in connection.execute("SELECT e.id,e.attempt_id,e.verdict,e.raw_json FROM evaluation e "
                                                          "JOIN attempt a ON a.id=e.attempt_id WHERE a.activity_date=? "
                                                          "AND e.adopted=1 ORDER BY e.rowid", (day.isoformat(),))]
    for turn in turns:
        if turn['role'] != 'user':
            continue
        preceding = connection.execute("SELECT p.id,p.content FROM interview_turn p WHERE p.session_id=? "
            "AND p.role='assistant' AND p.rowid<(SELECT rowid FROM interview_turn WHERE id=?) "
            "AND NOT EXISTS(SELECT 1 FROM model_job j WHERE j.result_id=p.id AND j.purpose='interview_feedback') "
            "ORDER BY p.rowid DESC LIMIT 1", (turn['session_id'], turn['id'])).fetchone()
        turn['answered_question'] = dict(preceding) if preceding else {'content': turn['main_question']}
    records = [*timeline["attempts"], *turns, *evaluations]
    if not timeline['activity_units']:
        raise ModelJobError("当天没有可用于反思的学习记录")
    adopted = {item['attempt_id']: item['verdict'] for item in evaluations}
    wrong = [item for item in timeline['attempts'] if item['code_self_result'] == 'cannot_solve' or adopted.get(item['id']) == 'needs_review']
    for item in timeline['attempts']:
        item['assessment_status'] = ('自评会做' if item['code_self_result'] == 'can_solve' else '自评不会，已知薄弱点') if item['code_self_result'] else {
            'aligned':'已采用通过', 'needs_review':'已采用需复习', 'unable_to_assess':'依据不足',
        }.get(adopted.get(item['id']), '待评价')
        item['code_self_result'] = {'can_solve': '会', 'cannot_solve': '不会'}.get(item['code_self_result'])
    return {"activity_date": day.isoformat(), "activity_units": timeline["activity_units"], "records": records, 'wrong_answers': wrong,
            'unanswered_question_ids': unanswered_questions(turns),
            'practice_format': '代码练习在外部编写代码，本应用只记录会做/不会做与可选备注；八股有文字回答。unanswered_question_ids 中的追问没有后续用户回答，只能说尚未作答和检验，不能说已经回应或答错。'}


def run_module_job(connection, module, target, request_key, client=None, context_override=None, client_factory=None, expected_revision=None):
    if module not in {"interview_reference", "question_quality", "interview_review", "practice_generation", "interview_preparation", "interview_followup", "interview_feedback", "daily_reflection", "source_parsing", "practice_selection"}:
        raise ModelJobError("不支持的模型模块")
    if module == 'interview_followup' and expected_revision is not None:
        # One continuation per observed conversation, also after reload or across tabs.
        request_key = f'{target}:{expected_revision}'
    with transaction(connection):
        business_key = f"{module}:{request_key}"
        job = connection.execute("SELECT * FROM model_job WHERE business_key=?", (business_key,)).fetchone()
        if job:
            job_id = job["id"]
            saved = json.loads(connection.execute("SELECT input_json FROM model_request WHERE job_id=?", (job_id,)).fetchone()[0])
            if saved.get("activity_date", saved.get("session_id", saved.get("source_id"))) != target:
                raise ModelJobError("同一请求标识不能用于不同对象")
            if expected_revision is not None and saved.get('conversation_revision', (saved.get('turns') or [{}])[-1].get('id', '')) != expected_revision:
                raise ModelJobError('同一请求标识不能用于不同轮对话')
        else:
            if module in {'interview_reference', 'question_quality', 'interview_review', 'source_parsing', 'practice_selection', 'interview_preparation', 'practice_generation'}:
                from app.services.source_coverage import parsing_context
                context = context_override or parsing_context(connection, target)
            else:
                context = reflection_context(connection, date.fromisoformat(target)) if module == "daily_reflection" else interview_context(connection, target, module)
            if expected_revision is not None and context.get('conversation_revision') != expected_revision:
                raise ModelJobError('对话已更新，请刷新对话，再继续面试')
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
        model = client or (client_factory or client_for_config)(config)
        messages = [{"role": "system", "content": config["system_prompt"]},
                    {"role": "user", "content": request["input_json"]}]
        reply = (model.complete(messages, max_tokens=config['max_tokens']) if module == 'interview_feedback' and not config.get('feedback_format')
                 else complete_json(model, messages, config['max_tokens']))
        with transaction(connection):
            connection.execute("UPDATE model_request SET response_text=?,response_model=? WHERE job_id=? AND EXISTS "
                               "(SELECT 1 FROM model_job WHERE id=? AND status='running' AND updated_at=?)",
                               (reply.content, reply.model, job_id, job_id, claimed["claimed_at"]))
        from app.services.question_quality import checked_reply
        context = json.loads(request['input_json'])
        if module == 'question_quality' and any(item.get('examples') for item in context['items']):
            from app.services.question_quality import checked_quality_format
            reply = checked_quality_format(connection, context, reply, config, model, job_id, messages)
        if module == 'daily_reflection':
            from app.services.reflection_quality import checked_reflection
            reply = checked_reflection(connection, context, reply, config, model, job_id, messages)
        if module == 'interview_feedback' and context.get('feedback_format'):
            from app.services.interview_feedback import checked_feedback
            reply = checked_feedback(connection, context, reply, config, model, job_id, messages)
        reply = checked_reply(connection, module, context, reply, config, model, job_id, messages)
        with transaction(connection):
            if not connection.execute("SELECT 1 FROM model_job WHERE id=? AND status='running' AND updated_at=?",
                                      (job_id, claimed["claimed_at"])).fetchone():
                raise ModelJobError("请求已过期，迟到结果没有写入学习记录")
            context = json.loads(request["input_json"])
            result_id = _save_result(connection, module, context, reply.content)
            connection.execute("UPDATE model_request SET response_text=?,response_model=? WHERE job_id=?", (reply.content, reply.model, job_id))
            connection.execute("UPDATE model_job SET status='complete',result_id=?,updated_at=?,error=NULL WHERE id=?",
                               (result_id, datetime.now(UTC).isoformat(), job_id))
        return _result(connection, job_id)
    except Exception as error:
        raise fail_job(connection, job_id, claimed["claimed_at"], error) from error


def _load_request(connection, job_id):
    return {"request": dict(connection.execute("SELECT * FROM model_request WHERE job_id=?", (job_id,)).fetchone())}


def _save_result(connection, module, context, text):
    now = datetime.now(UTC)
    if module == 'question_quality':
        from app.services.question_quality import validate_quality
        validate_quality(context, parse_model_json(text))
        return new_id('quality')
    if module == 'interview_reference':
        from app.services.question_quality import validate_pair
        validate_pair({**parse_model_json(text), 'question': context['question']})
        return new_id('reference')
    if module == 'interview_review':
        from app.services.interview_review import validate_review_preview
        validate_review_preview(parse_model_json(text))
        return new_id('preview')
    if module == 'practice_generation':
        from app.services.practice_generation import validate_variants
        validate_variants(context, parse_model_json(text))
        return new_id('generation')
    if module == 'interview_preparation':
        from app.services.interview_setup import validate_preparation
        validate_preparation(context, parse_model_json(text))
        return new_id('preparation')
    if module == 'practice_selection':
        from app.services.practice_selection import validate_selection
        validate_selection(context, parse_model_json(text))
        return new_id('selection')
    if module == "source_parsing":
        from app.services.source_coverage import validate_suggestions
        validate_suggestions(context, parse_model_json(text))
        return new_id("analysis")
    if module == "daily_reflection":
        from app.services.reflection_quality import validate_reflection
        payload = validate_reflection(context, parse_model_json(text))
        ids = payload['covered_ids']
        day = date.fromisoformat(context["activity_date"])
        result_id = save_model_reflection(connection, day, payload["content"], ids, now)
        if reflection_context(connection, day) != context:
            connection.execute("UPDATE reflection SET stale=1 WHERE id=?", (result_id,))
        return result_id
    current = interview_context(connection, context["session_id"], module)
    unchanged = (current['conversation_revision'] == context['conversation_revision'] if 'conversation_revision' in context
                 else current["turns"] == context["turns"])
    if not unchanged:
        raise ModelJobError("对话已更新，本次结果未追加；请刷新后重新生成")
    if module == "interview_followup":
        from app.services.question_quality import validate_pair
        value = validate_pair(parse_model_json(text))
        return add_turn(connection, context["session_id"], "assistant", value['question'], now)
    if context.get('feedback_format'):
        from app.services.interview_feedback import render_feedback
        text = render_feedback(context, parse_model_json(text))
    turn_id = new_id("turn")
    connection.execute("INSERT INTO interview_turn VALUES (?,?, 'assistant',?,?)",
                       (turn_id, context["session_id"], text, now.isoformat()))
    mark_model_reflections_stale(connection, local_date(now))
    return turn_id


def _result(connection, job_id):
    request = connection.execute("SELECT module,input_json FROM model_request WHERE job_id=?", (job_id,)).fetchone()
    reflection_content = None
    if request['module'] == 'daily_reflection':
        from app.services.reflections import view_model_reflection
        result_id = connection.execute('SELECT result_id FROM model_job WHERE id=?', (job_id,)).fetchone()[0]
        reflection_content = view_model_reflection(connection, result_id, datetime.now(UTC))
    if request["module"] == "interview_feedback":
        session_id = json.loads(request["input_json"])["session_id"]
        with transaction(connection):
            connection.execute("INSERT OR IGNORE INTO question_exposure SELECT t.question_id,? FROM task t "
                               "JOIN interview_session s ON s.task_id=t.id WHERE s.id=?", (datetime.now(UTC).isoformat(), session_id))
    row = connection.execute("SELECT j.id AS job_id,j.result_id,r.response_text,r.response_model FROM model_job j "
                             "JOIN model_request r ON r.job_id=j.id WHERE j.id=?", (job_id,)).fetchone()
    if request['module'] in {'interview_followup', 'interview_feedback'}:
        # Keep raw model structures private; return the saved question or rendered feedback.
        visible = connection.execute('SELECT content FROM interview_turn WHERE id=?', (row['result_id'],)).fetchone()[0]
        return {**dict(row), 'response_text': visible}
    return {**dict(row), **(reflection_content if reflection_content is not None else {})}
