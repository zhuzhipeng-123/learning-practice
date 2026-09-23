"""Render feedback only from explicitly quoted learner evidence."""

import json

from app.services.model_jobs import ModelJobError
from app.services.model_json import ModelJSONError, complete_json, parse_model_json
from app.storage.transactions import transaction

KINDS = {'strength': '已表现出的能力', 'mistake': '回答中的问题', 'gap': '仍需检验', 'correction': '已经修正的认识'}


def validate_feedback(context, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('observations'), list) or not 1 <= len(payload['observations']) <= 8:
        raise ModelJobError('面试总结需要引用你的实际回答，不能只给无依据的评价')
    users = {turn['id']: turn['content'] for turn in context['turns'] if turn['role'] == 'user'}
    order = {turn['id']: index for index, turn in enumerate(context['turns'])}
    for item in payload['observations']:
        if (not isinstance(item, dict) or not isinstance(item.get('kind'), str) or item['kind'] not in KINDS
                or not isinstance(item.get('turn_id'), str) or item['turn_id'] not in users):
            raise ModelJobError('总结不能把面试官的话或不存在的回答当成你的表现')
        quote, comment = item.get('quote'), item.get('comment')
        if not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 300 or quote not in users[item['turn_id']]:
            raise ModelJobError('总结引用的原话不在对应用户回答中')
        if not isinstance(comment, str) or not 1 <= len(comment.strip()) <= 600:
            raise ModelJobError('总结的判断缺失或过长')
        if item['kind'] == 'correction':
            prior, prior_quote = item.get('prior_turn_id'), item.get('prior_quote')
            if (not isinstance(prior, str) or prior not in users or order[prior] >= order[item['turn_id']] or not isinstance(prior_quote, str)
                    or not 1 <= len(prior_quote.strip()) <= 300 or prior_quote not in users[prior]):
                raise ModelJobError('自我修正必须同时引用修正前后的用户原话')
    steps = payload.get('next_steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 3 or any(not isinstance(step, str) or not 1 <= len(step.strip()) <= 500 for step in steps):
        raise ModelJobError('总结需要一至三条具体的后续练习建议')
    return payload


def render_feedback(context, payload):
    value = validate_feedback(context, payload)
    lines = ['### 基于这场对话的复盘']
    corrected = {item['prior_turn_id'] for item in value['observations'] if item['kind'] == 'correction'}
    if context.get('context_window', {}).get('omitted_turns') or context.get('context_window', {}).get('excerpted_turns'):
        lines.append('以下只依据保留的对话片段，不把省略内容视为遗漏或错误。')
    for item in value['observations']:
        label = '此前的回答（后续已修正）' if item['kind'] in {'mistake', 'gap'} and item['turn_id'] in corrected else KINDS[item['kind']]
        lines.extend([f'#### {label}', '你的原话：' + item['quote'], item['comment']])
        if item['kind'] == 'correction':
            lines.append('此前原话：' + item['prior_quote'])
    unanswered = set(context['unanswered_question_ids'])
    if unanswered:
        lines.append('#### 尚未作答，不能判断对错')
        lines.extend(turn['content'] for turn in context['turns'] if turn['id'] in unanswered)
    lines.extend(['#### 下一次练习', *('- ' + step for step in value['next_steps'])])
    return '\n\n'.join(lines)


def checked_feedback(connection, context, reply, config, model, job_id, execution_id, messages):
    try:
        validate_feedback(context, parse_model_json(reply.content))
        return reply
    except (ModelJobError, ModelJSONError) as error:
        instruction = ('总结证据校验失败：' + str(error) + '。重新检查每条原话的 role，必须来自 user。'
                       '面试官给出的解释不能算学生会了。只输出原协议 JSON，不编造引文；自我修正引用前后两次用户回答。')
    from app.services.model_diagnostics import log_event, started_at
    from app.services.model_jobs import load_model_request_owned, update_model_request_owned
    with transaction(connection):
        stored = json.loads(load_model_request_owned(connection, job_id, execution_id)['config_json'])
        stored['format_repair'] = {'rejected_response': reply.content, 'instruction': instruction, 'max_tokens': config['max_tokens']}
        update_model_request_owned(connection, job_id, execution_id,
                                   config_json=json.dumps(stored, ensure_ascii=False))
    repair_started = started_at()
    fixed = complete_json(model, [*messages, {'role':'assistant', 'content':reply.content}, {'role':'user', 'content':instruction}], config['max_tokens'])
    log_event(job_id, 'interview_feedback', execution_id, 'repair', started=repair_started, reply=fixed)
    validate_feedback(context, parse_model_json(fixed.content))
    return fixed
