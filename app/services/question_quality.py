"""Check generated question/answer pairs before any learning records are published."""

import hashlib
import json
import re

from app.services.llm_config import client_for_config
from app.services.model_jobs import ModelJobError
from app.services.model_json import complete_json, parse_model_json

CHECKS = ('scope_match', 'answer_matches', 'factually_sound', 'examples_consistent', 'code_complete')
QUESTION_MODULES = {'practice_generation', 'interview_preparation', 'interview_followup', 'interview_review', 'interview_reference'}


def validate_pair(value, question_field='question', question_limit=4000):
    if not isinstance(value, dict):
        raise ModelJobError('模型没有返回成对的问题和参考答案，请重试')
    for field, limit in ((question_field, question_limit), ('reference_text', 20000)):
        if not isinstance(value.get(field), str) or not 5 <= len(value[field].strip()) <= limit:
            raise ModelJobError('模型缺少完整问题或对应参考答案，本次没有创建题目，请重试')
    return value


def validate_quality(context, value):
    items = value.get('items') if isinstance(value, dict) else None
    expected = {item['id'] for item in context['items']}
    if not isinstance(items, list) or len(items) != len(expected):
        raise ModelJobError('问答核对没有覆盖全部题目，未发布，请重试')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] not in expected or item['id'] in seen:
            raise ModelJobError('问答核对引用了错误或重复的题目，未发布')
        issues = item.get('issues')
        if (any(type(item.get(field)) is not bool for field in CHECKS) or not isinstance(issues, list)
                or len(issues) > 12 or any(not isinstance(issue, str) or not 1 <= len(issue.strip()) <= 1000 for issue in issues)):
            raise ModelJobError('问答核对结果格式错误，未发布，请重试')
        seen.add(item['id'])
        original = next(source for source in context['items'] if source['id'] == item['id'])
        if original.get('examples'):
            validate_example_evidence(original, item)
    return items


def code_examples(question):
    headings = list(re.finditer(r'(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:示例|example)\s*\d+\s*[:：]', question))
    if not headings:
        # Legacy prose or inline examples still require evidence, never skip the check.
        return [{'id': 'example_1', 'text': question}]
    return [{'id': f'example_{index + 1}', 'text': question[match.start():headings[index + 1].start() if index + 1 < len(headings) else len(question)]}
            for index, match in enumerate(headings)]


def output_mismatch(example, computed):
    stated = re.search(r'(?im)^\s*(?:\*\*)?(?:输出|output)\s*[:：](?:\*\*)?\s*', example)
    if not stated:
        return False
    try:
        decoder = json.JSONDecoder()
        expected, _ = decoder.raw_decode(example[stated.end():].lstrip('` \n'))
        actual = json.loads(computed.strip().strip('`'))
        return expected != actual
    except (ValueError, TypeError):
        return False


def validate_example_evidence(original, item):
    examples = {example['id']: example['text'] for example in original['examples']}
    evidence = item.get('example_checks')
    if not isinstance(evidence, list) or len(evidence) != len(examples):
        raise ModelJobError('代码示例核对缺少逐例重算证据，未发布，请重试')
    seen = set()
    for check in evidence:
        if not isinstance(check, dict) or not isinstance(check.get('example_id'), str) or check['example_id'] not in examples or check['example_id'] in seen:
            raise ModelJobError('代码示例核对引用错误或重复，未发布')
        quote = check.get('input_quote')
        if (not isinstance(quote, str) or len(quote.strip()) < 3 or quote not in examples[check['example_id']]
                or not isinstance(check.get('computed_output'), str) or not 1 <= len(check['computed_output']) <= 2000
                or not isinstance(check.get('reason'), str) or not 10 <= len(check['reason']) <= 2000
                or type(check.get('consistent')) is not bool):
            raise ModelJobError('代码示例核对缺少实际输入、独立结果或推演依据，未发布')
        seen.add(check['example_id'])
        if output_mismatch(examples[check['example_id']], check['computed_output']):
            check['consistent'] = False
            item['issues'].append(f"{check['example_id']}：题干最初列出的输出与独立重算结果不同，须统一示例输出与解释；后文改口不能覆盖前文错误。")
        if not check['consistent']:
            item['examples_consistent'] = False
            item['issues'].append(f"{check['example_id']}：{check['reason']}；重算结果：{check['computed_output']}")


def checked_quality_format(connection, context, reply, config, model, job_id, execution_id, messages):
    from app.services.model_diagnostics import log_event, started_at
    from app.services.model_jobs import load_model_request_owned, update_model_request_owned
    from app.services.model_json import ModelJSONError
    from app.storage.transactions import transaction
    try:
        validate_quality(context, parse_model_json(reply.content))
        return reply
    except (ModelJobError, ModelJSONError) as error:
        instruction = ('核对结果格式未通过：' + str(error) +
            '。重新核对并输出完整items；每个含examples的题目都必须在该item内提供example_checks数组，'
            '覆盖全部示例，不得把JSON写成issues里的字符串。每项有example_id、input_quote、computed_output、reason、consistent。'
            'input_quote逐字引用原示例的输入；独立重算每一例。输出与解释互相矛盾，即使后面改口纠正，也必须consistent=false。'
            '保留五个布尔检查和issues，只输出完整JSON。')
    with transaction(connection):
        stored = json.loads(load_model_request_owned(connection, job_id, execution_id)['config_json'])
        stored['format_repair'] = {'rejected_response': reply.content, 'instruction': instruction}
        update_model_request_owned(connection, job_id, execution_id,
                                   config_json=json.dumps(stored, ensure_ascii=False))
    repair_started = started_at()
    fixed = complete_json(model, [*messages, {'role': 'assistant', 'content': reply.content},
                                  {'role': 'user', 'content': instruction}], config['max_tokens'])
    log_event(job_id, 'question_quality', execution_id, 'repair', started=repair_started, reply=fixed)
    with transaction(connection):
        update_model_request_owned(connection, job_id, execution_id,
                                   response_text=fixed.content, response_model=fixed.model)
    validate_quality(context, parse_model_json(fixed.content))
    return fixed


def candidate_pairs(module, context, text):
    value = parse_model_json(text)
    if module == 'interview_preparation' and context['mode'] == 'suggest':
        return []
    if module == 'practice_generation':
        from app.services.practice_generation import validate_variants
        originals = {item['id']: item for item in context['questions']}
        return [{'id': item['base_question_id'], 'question': item['prompt'], 'reference_text': item['reference_text'],
                 'question_type': context['question_type'], 'scope': context['spec']['theme'],
                 'source_context': {key: originals[item['base_question_id']].get(key) for key in ('prompt','category_path','version_id','reference_correction')}}
                for item in validate_variants(context, value)]
    if module == 'interview_preparation':
        from app.services.interview_setup import validate_preparation
        validate_preparation(context, value)
    if module == 'interview_reference':
        if not isinstance(value, dict):
            raise ModelJobError('模型没有返回参考答案，未采用结果')
        value = {**value, 'question': context['question']}
    question_field = 'prompt' if module == 'interview_review' else 'question'
    validate_pair(value, question_field)
    return [{'id': 'question', 'question': value[question_field], 'reference_text': value['reference_text'],
             'question_type': context.get('question_type', 'theory'),
             'scope': context.get('direction') or context.get('job_focus') or context.get('main_question') or context.get('question', ''),
             'source_context': {key: value for key, value in context.items() if key not in {'reference','source_reference','selected_reference'}}}]


def _review(connection, module, context, text, parent_job, messages):
    from app.services.module_jobs import run_module_job
    items = candidate_pairs(module, context, text)
    if not items:
        return []
    if module == 'interview_followup':
        def normalized(value):
            return ' '.join(value.split()).casefold()
        previous_questions = [context['question'], *context.get('prior_questions', []),
                              *(turn['content'] for turn in context['turns'] if turn['role'] == 'assistant')]
        if normalized(items[0]['question']) in {normalized(question) for question in previous_questions}:
            return ['追问重复了主问题或已经问过的问题，请围绕最近回答提出不同且可独立回答的追问。']
    for item in items:
        source = item['source_context']
        item['reference_correction'] = source.get('reference_correction')
        if item['question_type'] == 'code':
            item['examples'] = code_examples(item['question'])
    digest = hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    target = 'quality:' + parent_job + ':' + digest
    check = {'source_id': target, 'items': items, 'generation_job_id': parent_job,
             'revision_instruction': messages[-1]['content'] if len(messages) > 2 else None}
    response = run_module_job(connection, 'question_quality', target, target, context_override=check,
                              client_factory=client_for_config)
    results = validate_quality(check, parse_model_json(response['response_text']))
    problems = []
    for index, item in enumerate(results, 1):
        # Written objections override optimistic flags; inconsistent approval never publishes.
        if not all(item[field] for field in CHECKS) or item['issues']:
            problems.append(f"第 {index} 题：" + '；'.join(item['issues'] or ['范围、答案或关键依据未通过核对']))
            correction = next(source['reference_correction'] for source in items if source['id'] == item['id'])
            if correction:
                problems.append('该题已核对的局部依据：' + correction['content'] + '\n出处：' + ' '.join(correction['sources']))
    return problems


def checked_reply(connection, module, context, reply, config, model, parent_job, execution_id, messages):
    if module not in QUESTION_MODULES:
        return reply
    if module == 'interview_preparation' and context['mode'] == 'suggest':
        from app.services.interview_setup import checked_suggestions
        return checked_suggestions(connection, context, reply, config, model, parent_job,
                                   execution_id, messages)
    problems = _review(connection, module, context, reply.content, parent_job, messages)
    if not problems:
        return reply
    # One bounded revision uses the original frozen context plus specific review findings.
    revision = [*messages, {'role': 'assistant', 'content': reply.content},
                {'role': 'user', 'content': json.dumps({'task': '按检查意见修正整组问答，保留原范围和来源ID。原笔记和检查意见都可能有错误，独立核实核心概念、数学维度和假设；不要机械照抄。不要增加无关话题。只返回原协议的完整JSON。',
                                                       'issues': problems}, ensure_ascii=False)}]
    fixed = complete_json(model, revision, config['max_tokens'])
    remaining = _review(connection, module, context, fixed.content, parent_job, revision)
    if remaining:
        raise ModelJobError('问答内容核对未通过，未发布：' + '；'.join(remaining)[:1200])
    return fixed
