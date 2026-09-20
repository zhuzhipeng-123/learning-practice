"""Ten real service/API days with synthetic sources and deterministic model replies."""

import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.domain import Submission
from app.main import app
from app.services.dashboard import day_details, heatmap
from app.services.exports import export_learning_data, verify_export
from app.services.free_practice import add_free_practice
from app.services.interview import add_turn, create_derived_theory_question, start_session
from app.services.model_jobs import run_evaluation_job
from app.services.module_jobs import run_module_job
from app.services.practice import add_theory_to_review, start_attempt, submit_code, submit_theory
from app.services.reflections import save_user_reflection, view_model_reflection
from app.services.review_tasks import start_review_task
from app.services.source_sync import sync_registered_source
from app.services.sync import SyncIntegrityError
from app.services.tasks import create_daily_plan
from app.storage.database import connect_database
from tests.helpers import add_source
from tests.test_docx_reader import FakeClient
from tests.test_practice_review import seed_question
from tests.test_review_acceptance import text_block

TOPICS = [
    ('什么是幂等？', '重复执行相同操作不会导致额外状态变化，常用请求标识去重。'),
    ('RAG 如何检索？', '查询经过编码，与文档向量计算相似度后取相关上下文。'),
    ('工具调用如何恢复？', '区分可重试失败与业务拒绝，保留请求状态并限制重试次数。'),
    ('为什么需要评估集？', '固定覆盖真实场景的标注数据，比较修改前后的效果和回归。'),
    ('如何隔离提示注入？', '外部内容不获得系统指令权限，工具参数和业务授权由程序校验。'),
    ('如何控制上下文长度？', '保留当前任务所需事实，压缩已完成历史，并检查预算上限。'),
    ('如何设计工作流？', '把状态转换与外部调用边界明确记录，失败后能从已知状态继续。'),
    ('什么是事务？', '一组数据库更新整体成功或整体回滚，避免只写入一部分事实。'),
    ('如何观测模型故障？', '记录耗时、状态和错误类别，保护密钥，分开服务失败与学习结论。'),
    ('如何做语义评分？', '判断核心观点与事实是否正确，接受同义表述，不按关键词机械匹配。'),
]


def source_reply(index):
    raw = [text_block('module', 'Agent fundamentals' if index < 4 else 'Agent preparation', 4)]
    for number, (prompt, reference) in enumerate(TOPICS[:index+1]):
        if number == 0 and index >= 4:
            reference += ' 外部服务超时后应重用相同请求标识。'
        raw.extend([text_block(f'q{number}', prompt, 5), text_block(f'r{number}', reference)])
    # Exercise actual pagination, revision check, parsing, identity binding and publication.
    midpoint = max(1, len(raw)//2)
    return FakeClient([
        {'document': {'revision_id': index+1}},
        {'items': raw[:midpoint], 'has_more': True, 'page_token': 'second'},
        {'items': raw[midpoint:], 'has_more': False},
        {'document': {'revision_id': index+1}},
    ])


def model_reply(messages, **kwargs):
    context = json.loads(messages[1]['content'])
    if 'records' in context:
        value = {'content': '今天记录了实际作答。明天先独立练习薄弱点，再核对参考。',
                 'covered_ids': [item['id'] for item in context['records']]}
    else:
        value = {'verdict': 'aligned', 'covered_points': ['core'], 'missing_points': [], 'errors': [],
                 'brief_feedback': 'Fixture assessment', 'evidence_refs': context['reference_ids']}
    return SimpleNamespace(content=json.dumps(value), model='fixture-only')


def test_ten_days_sources_reflections_review_and_restored_pages(database, tmp_path, monkeypatch):
    code_id = seed_question(database, 'code')
    add_source(database, 'daily-theory')
    database.execute("UPDATE source SET question_type='theory' WHERE id='daily-theory'")
    database.commit()
    first_day = date(2026, 9, 3)
    fake_model = SimpleNamespace(complete=model_reply)
    theory_id = None
    old_version = None
    all_tasks = []
    for index in range(10):
        day = first_day + timedelta(days=index)
        now = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=12)
        source = sync_registered_source(database, 'daily-theory', source_reply(index))
        assert source['page_count'] == 2 and not source['partial']
        assert database.execute("SELECT COUNT(*) FROM question WHERE question_type='theory'").fetchone()[0] == index+1
        if index == 5:
            # A failed remote page cannot erase yesterday's question bank.
            broken = FakeClient([{'document': {'revision_id': 6}}, {'items': [], 'has_more': True}])
            try:
                sync_registered_source(database, 'daily-theory', broken)
            except SyncIntegrityError:
                pass
            else:
                raise AssertionError('incomplete pagination must fail')
            assert database.execute("SELECT COUNT(*) FROM question WHERE source_status='active'").fetchone()[0] == 7
        plan = create_daily_plan(database, day, 1, 0, {}, f'day-{index}', index)
        code_task = plan['tasks'][0]['id']
        start_attempt(database, code_task, 'daily', now)
        submit_code(database, Submission(code_task, f'code-{index}', now, 'daily',
                                         code_self_result='cannot_solve' if index == 0 else 'can_solve'))
        new = add_free_practice(database, plan['plan_id'], '', 1, f'new-{index}', False, True, question_type='theory')
        assert new.added == 1
        task = new.task_ids[0]
        start_attempt(database, task, 'free_practice', now)
        answer = submit_theory(database, Submission(task, f'theory-{index}', now, 'free_practice', answer_text=TOPICS[index][1]))
        assert submit_theory(database, Submission(task, f'theory-{index}', now, 'free_practice', answer_text=TOPICS[index][1])) == answer
        run_evaluation_job(database, answer['job_id'], fake_model)
        if index == 0:
            row = database.execute('SELECT question_id,question_version_id FROM task WHERE id=?', (task,)).fetchone()
            theory_id, old_version = row
            add_theory_to_review(database, theory_id, 'explicit_click', now)
        else:
            review = start_review_task(database, theory_id, day, f'review-{index}')
            review_task = review['task_id']
            start_attempt(database, review_task, 'review', now)
            result = submit_theory(database, Submission(review_task, f'review-answer-{index}', now, 'review', answer_text=TOPICS[0][1]))
            run_evaluation_job(database, result['job_id'], fake_model)
            assert database.execute('SELECT question_version_id FROM task WHERE id=?', (review_task,)).fetchone()[0] == old_version
        save_user_reflection(database, day, f'Day {index+1}: my own reflection', now)
        generated = run_module_job(database, 'daily_reflection', day.isoformat(), f'reflect-{index}', fake_model)
        view_model_reflection(database, generated['result_id'], now + timedelta(minutes=30))
        details = day_details(database, day)
        assert details['activity']['total'] == (2 if index == 0 else 3)
        assert len(details['reflections']) == 2
        assert database.execute('SELECT COUNT(*) FROM valid_review_pass WHERE activity_date=?', (day.isoformat(),)).fetchone()[0] == (0 if index == 0 else 2)
        all_tasks.append(task)
        database.commit()
    last_day = first_day + timedelta(days=9)
    assert heatmap(database, last_day)['active_days'] == 10
    assert heatmap(database, last_day)['activities'] == 29
    assert heatmap(database, last_day, 'daily')['activities'] == 10
    assert heatmap(database, last_day, 'free_practice')['activities'] == 10
    assert database.execute("SELECT COUNT(*) FROM review_round WHERE end_reason='criteria_met'").fetchone()[0] == 2
    assert database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0] == 18
    assert database.execute('SELECT COUNT(*) FROM reflection').fetchone()[0] == 20
    assert database.execute('SELECT current_version_id FROM question WHERE id=?', (theory_id,)).fetchone()[0] != old_version
    assert database.execute('PRAGMA foreign_key_check').fetchall() == []
    assert database.execute('SELECT first_submitted_at FROM question WHERE id=?', (code_id,)).fetchone()[0]
    # A separately created interview exercises structured derivation in the backup too.
    plan = create_daily_plan(database, last_day + timedelta(days=1), 0, 1, {}, 'interview-day')
    session = start_session(database, plan['tasks'][0]['id'], datetime.now(UTC))
    turn = add_turn(database, session, 'assistant', 'How would you recover a timed-out idempotent request?', datetime.now(UTC))
    add_turn(database, session, 'user', 'I need to learn the recovery rules.', datetime.now(UTC), 'interview-answer')
    derived = create_derived_theory_question(database, session, 'Explain safe retry after timeout', 'Keep the same request key.', 'Agent', True, datetime.now(UTC), turn, 'confirm-derived')
    media = tmp_path / 'media'
    media.mkdir()
    (media / 'sample.txt').write_text('archived synthetic material', encoding='utf-8')
    archive = export_learning_data(database, tmp_path / 'backup.zip', media)
    assert verify_export(archive, tmp_path / 'verify')['media_count'] == 1
    restored_dir = tmp_path / 'restored'
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(restored_dir)
    monkeypatch.setenv('LEARNING_DATA_DIR', str(restored_dir))
    with TestClient(app) as client:
        for path in [f'/days/{first_day}', f'/practice/{all_tasks[0]}', f'/questions/{theory_id}/history',
                     f'/questions/{derived}/history', f'/interview/{session}', '/review', '/history']:
            assert client.get(path).status_code == 200, path
        assert 'Day 1: my own reflection' in client.get(f'/days/{first_day}').text
        recovered = connect_database(Path(app.state.database_path))
        assert recovered.execute('SELECT COUNT(*) FROM reflection').fetchone()[0] == 20
        assert recovered.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0] == 18
        recovered.close()
