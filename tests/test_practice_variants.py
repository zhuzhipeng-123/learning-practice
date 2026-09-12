import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain import Submission
from app.services.dashboard import day_details, heatmap
from app.services.free_practice import add_free_practice, find_new_originals
from app.services.learning_clock import local_today
from app.services.model_jobs import ModelJobError
from app.services.practice import expose_answer, start_attempt, submit_code, submit_theory
from app.services.practice_generation import generate_variants
from app.services.tasks import create_daily_plan
from tests.test_student_workflow import pool


def model_reply(messages, **kwargs):
    context = json.loads(messages[1]['content'])
    assert kwargs['max_tokens'] == 6144
    return SimpleNamespace(content=json.dumps({'questions': [
        {'base_question_id': q['id'], 'prompt': '新的约束下如何求解？' + q['prompt'],
         'reference_text': '先检查边界，再给出解法并说明复杂度。'} for q in context['questions']]}), model='fake')


@pytest.mark.parametrize('kind', ['code', 'theory'])
def test_variants_keep_provenance_and_originals_and_use_normal_submission(database, kind):
    pool(database, 3, kind)
    plan = create_daily_plan(database, local_today(), 0, 0, {}, 'empty-plan')
    result = generate_variants(database, plan['plan_id'], '', 2, kind, False, 'variants', client=SimpleNamespace(complete=model_reply))
    assert result['added'] == 2
    assert generate_variants(database, plan['plan_id'], '', 2, kind, False, 'variants') == result
    assert database.execute('SELECT COUNT(*) FROM question_derivation').fetchone()[0] == 2
    assert len(find_new_originals(database, '', False, kind)) == 3
    now = datetime.now(UTC)
    for index, task in enumerate(result['task_ids']):
        start_attempt(database, task, 'web', now)
        expose_answer(database, task, now)
        submission = Submission(task, 'submit-' + str(index), now, 'web', answer_text='我的理解是先处理边界。', code_self_result='cannot_solve')
        value = submit_code(database, submission) if kind == 'code' else submit_theory(database, submission)
        assert value['task_completed']
    assert heatmap(database, local_today(), 'daily')['activities'] == 0
    assert heatmap(database, local_today(), 'free_practice')['activities'] == 2
    assert database.execute("SELECT COUNT(*) FROM review_round WHERE status='active'").fetchone()[0] == (2 if kind == 'code' else 0)
    original_ids = [r[0] for r in database.execute("SELECT id FROM question WHERE source_kind='feishu'")]
    for key in original_ids:
        database.execute("UPDATE question SET source_status='source_deleted' WHERE id=?", (key,))
    assert database.execute("SELECT COUNT(*) FROM question WHERE source_kind='derived' AND source_status='active'").fetchone()[0] == 2
    assert database.execute('SELECT COUNT(*) FROM question_derivation d JOIN question_version v ON v.id=d.base_version_id').fetchone()[0] == 2


@pytest.mark.parametrize('text', ['Internal Server Error', '{"questions":[]}', '{"questions":[{"base_question_id":"invented","prompt":"伪造的来源题目","reference_text":"伪造的参考资料"}]}'])
def test_bad_generation_never_publishes_partial_tasks(database, text):
    pool(database, 1, 'theory')
    plan = create_daily_plan(database, local_today(), 0, 0, {}, 'empty-plan')
    fake = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content=text, model='fake'))
    with pytest.raises(ModelJobError):
        generate_variants(database, plan['plan_id'], '', 1, 'theory', False, 'bad-generation', client=fake)
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM question_derivation').fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM question WHERE source_kind='derived'").fetchone()[0] == 0
    recovered = generate_variants(database, plan['plan_id'], '', 1, 'theory', False, 'bad-generation', client=SimpleNamespace(complete=model_reply))
    assert recovered['added'] == 1


def test_ten_days_daily_and_free_heatmaps_are_separate(database):
    pool(database, 3, 'code')
    today = local_today()
    for index in range(10):
        day = today - timedelta(days=9-index)
        now = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=2)
        plan = create_daily_plan(database, day, 1, 0, {}, f'plan-{index}')
        task = plan['tasks'][0]['id']
        start_attempt(database, task, 'web', now)
        submit_code(database, Submission(task, f'daily-{index}', now, 'web', code_self_result='can_solve'))
        extra = add_free_practice(database, plan['plan_id'], '', 1, f'extra-{index}', True, True, only_new=False, question_type='code')
        task = extra.task_ids[0]
        start_attempt(database, task, 'web', now)
        submit_code(database, Submission(task, f'free-{index}', now, 'web', code_self_result='cannot_solve'))
    for scope in ('daily', 'free_practice'):
        result = heatmap(database, today, scope)
        assert result['activities'] == result['active_days'] == 10
        assert len([c for week in result['weeks'] for c in week if c['total']]) == 10
    assert heatmap(database, today)['activities'] == 20
    assert len(day_details(database, today, 'daily')['groups']['can']) == 1
    assert len(day_details(database, today, 'free_practice')['groups']['cannot']) == 1
