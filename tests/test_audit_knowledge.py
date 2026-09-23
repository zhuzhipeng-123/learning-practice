import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.domain import Submission
from app.services.interview import (
    InterviewError,
    add_turn,
    create_derived_theory_question,
    start_session,
)
from app.services.interview_review import preview_review
from app.services.interview_setup import prepare_direction
from app.services.knowledge_editing import edit_knowledge, read_knowledge
from app.services.model_jobs import ModelJobError, create_reevaluation_job, run_evaluation_job
from app.services.practice import adopt_theory_evaluation, start_attempt, submit_theory
from app.services.practice_generation import generate_variants
from app.services.reference_corrections import save_correction
from app.services.reference_state import verification
from app.services.review_tasks import start_review_task
from app.services.tasks import IdempotencyConflictError
from tests.helpers import remove_migrations_after
from tests.test_practice_review import make_plan, seed_question

NOW = datetime.now(UTC)


def client(value):
    return SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content=json.dumps(value), model='isolated-model'))


def test_source_reference_is_not_labeled_as_human_verification(database):
    question = seed_question(database, 'theory')
    version = database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0]
    state = verification(database, version)
    assert state['verified'] is True
    assert state['kind'] == 'source'
    assert state['human_verified'] is False


def verdict(value='aligned'):
    return {'verdict': value, 'covered_points': [], 'missing_points': [], 'errors': [], 'brief_feedback': '测试反馈', 'evidence_refs': []}


def interview_derived(database, verified=False):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    session = start_session(database, task['id'], NOW)
    turn = add_turn(database, session, 'assistant', '有界队列满时应如何处理生产者？', NOW)
    question = create_derived_theory_question(database, session, '有界队列满时如何进行背压？',
        '生产者阻塞等待容量，或通过显式失败让上游降低速率。', '并发控制', True, NOW, turn, 'derived-create', verified)
    return question, session, turn


@pytest.mark.parametrize('origin', ['variant', 'opening'])
def test_all_generated_origins_are_unverified_even_when_quality_check_passes(database, origin):
    if origin == 'variant':
        question = seed_question(database, 'theory')
        from app.services.tasks import create_daily_plan
        plan = create_daily_plan(database, NOW.date(), 0, 0, {}, 'empty-plan')
        result = generate_variants(database, plan['plan_id'], '', 1, 'theory', False, 'generate', client=client({
            'questions': [{'base_question_id': question, 'prompt': '如何识别异步队列的背压？', 'reference_text': '观察消费者处理能力及排队情况，并限制生产速率。'}]}))
        task_id = result['task_ids'][0]
    else:
        result = prepare_direction(database, 'opening', '异步处理', '', [], 'opening', client({
            'question': '如何保证异步任务的取消可以被观察？', 'reference_text': '设置取消信号，在工作循环中检查并记录最终状态。'}))
        task_id = database.execute('SELECT task_id FROM interview_session WHERE id=?', (result['session_id'],)).fetchone()[0]
    start_attempt(database, task_id, 'web', NOW)
    saved = submit_theory(database, Submission(task_id, 'submit', NOW, 'web', answer_text='关键概念已经说明。'))
    with pytest.raises(ModelJobError, match='参考'):
        run_evaluation_job(database, saved['job_id'], client(verdict()))
    assert database.execute('SELECT COUNT(*) FROM evaluation WHERE adopted=1').fetchone()[0] == 0
    run_evaluation_job(database, saved['job_id'], client(verdict('unable_to_assess')))
    assert database.execute('SELECT verdict FROM evaluation WHERE adopted=1').fetchone()[0] == 'unable_to_assess'


def test_derived_confirmation_records_exposure_even_for_verified_reference(database):
    question, _, _ = interview_derived(database, True)
    task = start_review_task(database, question, NOW.date(), 'review')['task_id']
    start_attempt(database, task, 'review', NOW + timedelta(seconds=10))
    saved = submit_theory(database, Submission(task, 'after-preview', NOW + timedelta(seconds=20), 'review', answer_text='阻塞生产者或显式返回失败。'))
    run_evaluation_job(database, saved['job_id'], client(verdict()))
    assert database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM question_exposure WHERE question_id=?', (question,)).fetchone()[0] == 1


def test_edit_is_versioned_replay_safe_and_old_task_cannot_inherit_verification(database):
    question, session, turn = interview_derived(database)
    original = read_knowledge(database, question)
    task = start_review_task(database, question, NOW.date(), 'old-task')['task_id']
    updated = edit_knowledge(database, question, original['version_id'], '如何处理有界队列中的优先级反转？',
        '分析优先级与等待关系，并避免高优先级任务长期等待低优先级任务释放资源。', '调度', True, 'edit-one')
    assert updated['version_id'] != original['version_id']
    assert verification(database, original['version_id'])['verified'] is False
    assert verification(database, updated['version_id'])['verified'] is True
    again = edit_knowledge(database, question, updated['version_id'], '修改为资源饥饿问题？',
        '通过公平调度和有界等待减少任务长期得不到资源的风险。', '调度', False, 'edit-two')
    assert edit_knowledge(database, question, original['version_id'], '如何处理有界队列中的优先级反转？',
        '分析优先级与等待关系，并避免高优先级任务长期等待低优先级任务释放资源。', '调度', True, 'edit-one') == updated
    assert database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0] == again['version_id']
    assert database.execute('SELECT question_version_id FROM task WHERE id=?', (task,)).fetchone()[0] == original['version_id']
    with pytest.raises(IdempotencyConflictError, match='题目已更新'):
        edit_knowledge(database, question, original['version_id'], '陈旧页面修改？', '不能覆盖最新版本的答案内容。', '调度', True, 'stale-edit')
    reopened = preview_review(database, session, turn, 'reopen')
    assert reopened['reference_text'] == '通过公平调度和有界等待减少任务长期得不到资源的风险。'
    assert reopened['version_id'] == again['version_id']
    start_attempt(database, task, 'review', NOW)
    saved = submit_theory(database, Submission(task, 'old-answer', NOW, 'review', answer_text='阻塞或显式失败。'))
    with pytest.raises(ModelJobError):
        run_evaluation_job(database, saved['job_id'], client(verdict()))
    adopt_theory_evaluation(database, saved['attempt_id'], 'aligned', NOW, corrected_by_user=True)
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 1


def test_changed_derivation_payload_is_never_silently_discarded(database):
    _, session, turn = interview_derived(database)
    with pytest.raises(InterviewError, match='没有被保存'):
        create_derived_theory_question(database, session, '改过的追问内容？', '改过的完整答案内容。', '调度', True, NOW, turn, 'different-key', True)


@pytest.mark.parametrize('status', ['insufficient', 'contradictory'])
def test_model_declared_reference_gap_cannot_become_a_score(status):
    from app.services.evaluations import EvaluationValidationError, validate_evaluation
    with pytest.raises(EvaluationValidationError, match='未采用'):
        validate_evaluation({**verdict('needs_review'), 'reference_status': status}, set())


@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_repeated_followup_gets_one_revision_without_publishing_duplicates(database, repair_succeeds):
    from app.services.module_jobs import run_module_job
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    session = start_session(database, task['id'], NOW)
    add_turn(database, session, 'user', '我的第一份回答。', NOW)
    question = '若消息在确认前重复送达，该如何处理？'
    add_turn(database, session, 'assistant', question, NOW)
    add_turn(database, session, 'user', '使用稳定的消息标识去重。', NOW)
    calls = []
    def generate(messages, **kwargs):
        calls.append(messages)
        text = '去重记录失效后如何界定重复消息？' if len(calls) == 2 and repair_succeeds else question
        return SimpleNamespace(content=json.dumps({'question':text,'reference_text':'结合重试窗口和业务唯一标识设计去重记录的保留期限。'}), model='isolated-model')
    if repair_succeeds:
        run_module_job(database, 'interview_followup', session, 'duplicate-followup', SimpleNamespace(complete=generate))
    else:
        with pytest.raises(ModelJobError, match='未发布'):
            run_module_job(database, 'interview_followup', session, 'duplicate-followup', SimpleNamespace(complete=generate))
    assert len(calls) == 2
    assert database.execute('SELECT COUNT(*) FROM interview_turn WHERE content=?', (question,)).fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM interview_turn WHERE role='assistant'").fetchone()[0] == (2 if repair_succeeds else 1)


def test_evaluation_retry_preserves_correction_but_reevaluation_uses_new_evidence(database):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    correction_a = save_correction(database, task['question_version_id'], '第一份校正：核对定义与边界条件。', ['https://example.test/a'], 'correction-a', '')
    start_attempt(database, task['id'], 'web', NOW)
    saved = submit_theory(database, Submission(task['id'], 'answer', NOW, 'web', answer_text='我的答案保持不变。'))
    def fail(*args, **kwargs):
        raise RuntimeError('simulated response loss')
    with pytest.raises(ModelJobError):
        run_evaluation_job(database, saved['job_id'], SimpleNamespace(complete=fail))
    correction_b = save_correction(database, task['question_version_id'], '第二份校正：补充并发条件与反例。', ['https://example.test/b'], 'correction-b', correction_a['id'])
    assert save_correction(database, task['question_version_id'], '第一份校正：核对定义与边界条件。', ['https://example.test/a'], 'correction-a', '') == correction_a
    captured = []
    def assess(messages, **kwargs):
        captured.append(json.loads(messages[1]['content'])['reference_correction'])
        return SimpleNamespace(content=json.dumps(verdict()), model='isolated-model')
    run_evaluation_job(database, saved['job_id'], SimpleNamespace(complete=assess))
    new_job = create_reevaluation_job(database, saved['attempt_id'], 'reevaluate')
    run_evaluation_job(database, new_job, SimpleNamespace(complete=assess))
    assert [item['id'] for item in captured] == [correction_a['id'], correction_b['id']]
    assert read_knowledge(database, task['question_id'])['reference_correction'] == correction_b
    assert database.execute('SELECT COUNT(*) FROM reference_correction_history').fetchone()[0] == 2
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 1


def test_migration_retracts_only_legacy_unverified_automatic_scores(database):
    from app.storage.database import initialize_database
    question, _, _ = interview_derived(database)
    for index, days in enumerate([2, 4, 6, 8, 10]):
        at = NOW + timedelta(days=days)
        task = start_review_task(database, question, at.date(), f'legacy-task-{index}')['task_id']
        start_attempt(database, task, 'review', at)
        saved = submit_theory(database, Submission(task, f'legacy-submit-{index}', at, 'review', answer_text='保存的原始作答'))
        evaluation = adopt_theory_evaluation(database, saved['attempt_id'], 'aligned', at, corrected_by_user=True)
        # Reconstruct the invalid adopted state that version 11 allowed.
        database.execute('UPDATE evaluation SET corrected_by_user=0 WHERE id=?', (evaluation,))
        database.commit()
    assert database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0] == 5
    assert database.execute("SELECT status FROM review_round WHERE question_id=?", (question,)).fetchone()[0] == 'ended'
    remove_migrations_after(database, 11)
    database.execute('DROP TABLE reference_verification')
    database.execute('DROP TABLE reference_correction_history')
    database.execute('DELETE FROM schema_version WHERE version>=12')
    database.commit()
    initialize_database(database)
    assert database.execute('SELECT COUNT(*) FROM evaluation').fetchone()[0] == 5
    assert database.execute('SELECT COUNT(*) FROM evaluation WHERE adopted=1').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM attempt WHERE submitted_at IS NOT NULL').fetchone()[0] == 5
    assert database.execute("SELECT COUNT(*) FROM review_event WHERE event_type='criteria_revoked'").fetchone()[0] >= 1
