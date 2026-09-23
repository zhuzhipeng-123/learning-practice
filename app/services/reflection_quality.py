"""Validate reflection structure and visible text before saving a model-authored version."""

import json

from app.services.model_jobs import ModelJobError
from app.services.model_json import ModelJSONError, complete_json, parse_model_json
from app.storage.transactions import transaction


def validate_reflection(context, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('content'), str) or not payload['content'].strip():
        raise ModelJobError('模型复盘缺少正文')
    allowed = {record['id'] for record in context['records']}
    ids = payload.get('covered_ids')
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or item not in allowed for item in ids):
        raise ModelJobError('模型复盘引用了不存在的记录')
    if any(record_id in payload['content'] for record_id in allowed):
        raise ModelJobError('复盘正文应使用题目名称，不能显示内部记录编号')
    if any(field in payload['content'] for field in ('code_self_result', 'activity_units', 'wrong_answers', 'covered_ids')):
        raise ModelJobError('复盘正文不能显示内部字段名，请改写为自评、练习数量等自然中文')
    if '\\n' in payload['content']:
        raise ModelJobError('复盘换行被重复转义，正文不可正常阅读')
    return payload


def checked_reflection(connection, context, reply, config, model, job_id, execution_id, messages):
    try:
        validate_reflection(context, parse_model_json(reply.content))
        return reply
    except (ModelJobError, ModelJSONError) as error:
        instruction = ('修正复盘输出：' + str(error) + '。只输出原协议要求的 content 与 covered_ids；'
                       '正文用题目名称，不含内部ID，JSON解析后使用实际换行。代码自评不会是已知薄弱点，不是待评价。'
                       '尚未回答的追问只能说未检验。依据输入已知事实，不复制模型旧总结，不推断未提交的实现。')
    from app.services.model_diagnostics import log_event, started_at
    from app.services.model_jobs import load_model_request_owned, update_model_request_owned
    with transaction(connection):
        stored = json.loads(load_model_request_owned(connection, job_id, execution_id)['config_json'])
        stored['format_repair'] = {'rejected_response':reply.content, 'instruction':instruction, 'max_tokens':config['max_tokens']}
        update_model_request_owned(connection, job_id, execution_id,
                                   config_json=json.dumps(stored, ensure_ascii=False))
    repair_started = started_at()
    fixed = complete_json(model, [*messages, {'role':'assistant','content':reply.content},
                                 {'role':'user','content':instruction}], config['max_tokens'])
    log_event(job_id, 'daily_reflection', execution_id, 'repair', started=repair_started, reply=fixed)
    validate_reflection(context, parse_model_json(fixed.content))
    return fixed
