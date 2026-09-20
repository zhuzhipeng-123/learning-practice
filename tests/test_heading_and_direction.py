import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.parsers.docx import parse_docx_blocks
from app.services.interview_setup import prepare_direction
from app.services.learning_clock import local_today
from app.services.local_reparse import preview_theory_reparse, reparse_theory_snapshots
from app.services.model_jobs import ModelJobError
from app.services.plan_editing import update_daily_plan
from app.services.practice import start_attempt
from app.services.sync import publish_snapshot
from app.services.tasks import add_tasks, create_daily_plan
from app.storage.database import connect_database
from tests.test_docx_parser import heading, text_block
from tests.test_repair_sync import live_theory, run_live
from tests.test_student_workflow import pool
from tests.test_sync import draft


@pytest.mark.parametrize('level', range(1, 7))
def test_only_h3_is_a_standalone_question_without_container_context(level):
    raw = [heading('q', level, '工具调用'), text_block('answer', '参数校验'), text_block('body-question', '为什么？')]
    result = parse_docx_blocks('s', 'd', 'theory', raw, '八股 > Agent')
    assert len(result.published) == int(level == 3)
    assert result.candidates == []
    if level == 3:
        assert result.published[0].reference_block_ids == ('answer', 'body-question')


def test_marked_deep_heading_under_container_becomes_child_question():
    raw = [heading('module', 1, 'Agent'), heading('q', 3, '工具调用'),
           heading('detail', 4, '问题：为什么需要校验？'), text_block('a', '先校验参数'),
           text_block('body', '有哪些错误？'), text_block('more', '参数缺失等'),
           heading('next', 3, '记忆'), text_block('b', '外部存储')]
    result = parse_docx_blocks('s', 'd', 'theory', raw, '八股')
    assert [d.main_anchor_block_id for d in result.published] == ['detail', 'next']
    assert result.published[0].reference_block_ids == ('a', 'body', 'more')
    assert result.published[1].reference_block_ids == ('b',)


def test_local_rule_reparse_keeps_history_and_remote_freshness(database, monkeypatch):
    live_theory(database)
    raw = [heading('module', 1, 'Agent'), heading('q', 3, '工具调用'), text_block('legacy', '为什么？'), text_block('a', '原答案')]
    old = replace(draft(), source_id='live', document_id='document-live', question_type='theory',
                  main_anchor_block_id='legacy', prompt_block_ids=('legacy',), reference_block_ids=('a',),
                  prompt='为什么？', reference_text='原答案', category_path='Agent')
    publish_snapshot(database, 'live', '1', '1', raw, [old], 'old-parser')
    plan = create_daily_plan(database, local_today(), 0, 1, {}, 'old-plan')
    task = plan['tasks'][0]
    start_attempt(database, task['id'], 'daily', datetime.now(UTC))
    before_task = tuple(database.execute('SELECT * FROM task').fetchone())
    before_attempt = tuple(database.execute('SELECT * FROM attempt').fetchone())
    before_source = tuple(database.execute('SELECT * FROM source').fetchone())
    report = reparse_theory_snapshots(database)
    assert report[0]['remote_checked'] is False and report[0]['excluded_by_heading_rule'] == 1
    assert tuple(database.execute('SELECT * FROM source').fetchone()) == before_source
    assert tuple(database.execute('SELECT * FROM task').fetchone()) == before_task
    assert tuple(database.execute('SELECT * FROM attempt').fetchone()) == before_attempt
    assert database.execute('SELECT prompt FROM question_version WHERE id=?', (task['question_version_id'],)).fetchone()[0] == '为什么？'
    assert database.execute('SELECT source_status FROM question WHERE id=?', (task['question_id'],)).fetchone()[0] == 'excluded_by_rule'
    assert reparse_theory_snapshots(database)[0]['changes']['added'] == []
    run_live(database, monkeypatch, 2, raw)
    assert database.execute('SELECT source_status FROM question WHERE id=?', (task['question_id'],)).fetchone()[0] == 'excluded_by_rule'
    assert update_daily_plan(database, plan['plan_id'], 0, 1, {})['tasks'][0]['id'] == task['id']


def test_reparse_preview_is_read_only_and_stale_preview_cannot_apply(database):
    live_theory(database)
    raw = [heading('module', 1, 'Agent'), heading('q', 3, '工具调用'), text_block('answer', '校验参数。')]
    publish_snapshot(database, 'live', '1', '1', raw, [], 'old-parser')
    before = {table: [tuple(row) for row in database.execute(f'SELECT * FROM {table} ORDER BY rowid')]
              for table in ('question', 'question_version', 'source_binding', 'source_sync_state', 'sync_run')}

    preview = preview_theory_reparse(database)

    assert preview[0]['published_anchors'] == ['q']
    assert before == {table: [tuple(row) for row in database.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                      for table in before}
    database.execute("UPDATE source_snapshot SET revision='changed' WHERE id=?", (preview[0]['snapshot_id'],))
    with pytest.raises(ValueError, match='重新生成迁移预览'):
        reparse_theory_snapshots(database, preview)
    assert database.execute('SELECT COUNT(*) FROM question').fetchone()[0] == 0


def test_edit_replaces_excluded_untouched_task(database):
    pool(database, count=3)
    plan = create_daily_plan(database, local_today(), 0, 1, {}, 'plan')
    task = plan['tasks'][0]
    database.execute("UPDATE question SET source_status='excluded_by_rule' WHERE id=?", (task['question_id'],))
    updated = update_daily_plan(database, plan['plan_id'], 0, 1, {})
    assert updated['tasks'][0]['id'] != task['id']
    assert database.execute('SELECT status FROM task WHERE id=?', (task['id'],)).fetchone()[0] == 'cancelled'


def test_home_does_not_restore_saved_base_extra_or_older_tasks(monkeypatch):
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        db = connect_database(app.state.database_path)
        try:
            pool(db, count=8, kind='code')
            pool(db, count=8, kind='theory')
            old = create_daily_plan(db, local_today() - timedelta(days=1), 1, 0, {}, 'old')
            plan = create_daily_plan(db, local_today(), 2, 3, {}, 'today')
            selected = db.execute("SELECT q.id FROM question q WHERE q.question_type='theory' AND NOT EXISTS "
                                  "(SELECT 1 FROM task t WHERE t.question_id=q.id) LIMIT 2").fetchall()
            add_tasks(db, plan['plan_id'], [r[0] for r in selected], 'free_practice', 'extra')
            db.commit()
            html = client.get('/').text
            assert html.index('heatmap-panel') < html.index('id="code-target"')
            assert html.index('id="code-target"') < html.index('id="daily-reflection"')
            base = html.split('id="daily-task-list"')[1].split('id="older-practice"')[0]
            assert base.count('class="task-row"') == 0
            assert all(t['id'] not in html for t in plan['tasks'])
            assert 'id="added-practice"' not in html and 'id="older-practice"' not in html
            old_task = db.execute('SELECT id FROM task WHERE plan_id=?', (old['plan_id'],)).fetchone()[0]
            assert f'/practice/{old_task}' not in html
            assert f'/practice/{old_task}' not in client.get('/history').text
            assert db.execute('SELECT status FROM task WHERE id=?', (old_task,)).fetchone()[0] == 'cancelled'
            headers = {'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'edit'}
            result = client.put('/api/plans/' + plan['plan_id'], headers=headers,
                                json={'plan_date': local_today().isoformat(), 'code_target': 1, 'theory_target': 1, 'module_quotas': {}})
            assert result.status_code == 200
            html = client.get('/').text
            base = html.split('id="daily-task-list"')[1].split('id="older-practice"')[0]
            assert base.count('class="task-row"') == 0
            assert 'id="added-practice"' not in html and old['tasks'][0]['id'] not in html
            assert old['tasks'][0]['id'] not in client.get('/history').text
            assert 'earlier-count' not in client.get('/free-practice').text
            assert '从题库选' not in client.get('/interview').text
        finally:
            db.close()


def test_direction_suggestions_and_opening_use_model_with_frozen_context(database):
    calls = []
    def reply(messages, **kwargs):
        context = json.loads(messages[1]['content'])
        calls.append(context)
        value = {'directions': ['工具调用', 'RAG评估', '模型部署']} if context['mode'] == 'suggest' else {'question': '如何处理工具调用失败？', 'reference_text': '区分可重试错误，保留稳定请求标识并限制次数。'}
        return SimpleNamespace(content=json.dumps(value), model='fake')
    fake = SimpleNamespace(complete=reply)
    suggestions = prepare_direction(database, 'suggest', '', '', ['其他方向'], 'suggest', fake)
    assert suggestions['directions'] == ['工具调用', 'RAG评估', '模型部署']
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 0
    result = prepare_direction(database, 'opening', '工具异常处理', 'Agent实习', [], 'start', fake)
    assert prepare_direction(database, 'opening', '工具异常处理', 'Agent实习', [], 'start', fake) == result
    assert len(calls) == 2
    assert calls[1]['direction'] == '工具异常处理' and calls[1]['job_focus'] == 'Agent实习'
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1
    assert database.execute('SELECT prompt FROM question_version').fetchone()[0] == '如何处理工具调用失败？'


@pytest.mark.parametrize('response', ['Internal Server Error', '{"directions":["相同","相同","相同"]}', '{"question":""}'])
def test_bad_direction_response_does_not_create_empty_session(database, response):
    fake = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content=response, model='fake'))
    with pytest.raises(ModelJobError):
        prepare_direction(database, 'suggest', '', '', [], 'bad', fake)
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['code', 'theory'])
def test_free_api_draws_only_requested_type_and_hides_references(monkeypatch, kind):
    from app.services.free_batches import run_batch
    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    monkeypatch.setattr('app.services.free_batches.schedule_batch', lambda *a: None)
    with TestClient(app) as client:
        db = connect_database(app.state.database_path)
        pool(db, 4, 'code')
        pool(db, 4, 'theory')
        db.commit()
        db.close()
        page = client.get('/free-practice').text
        assert page.count('class="free-current" hidden') == 2
        headers = {'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'typed-draw'}
        body = {'question_type': kind, 'count': 2, 'mode': 'random'}
        first = client.post('/api/free-practice/batches', headers=headers, json=body)
        assert first.status_code == 200
        batch_id = first.json()['id']
        run_batch(app.state.database_path, batch_id)
        value = client.get('/api/free-practice/state').json()[kind]['result_batch']['result']
        assert value['added'] == 2 and {t['question_type'] for t in value['tasks']} == {kind}
        assert all('reference_text' not in t for t in value['tasks'])
        assert client.post('/api/free-practice/batches', headers=headers, json=body).json()['result'] == value
        home = client.get('/').text
        assert all(t['id'] not in home for t in value['tasks'])
        history = client.get('/free-practice').text
        assert all(t['id'] in history for t in value['tasks'])
        assert client.get('/api/free-practice/batches/' + batch_id).json()['result'] == value


def test_topic_model_only_sees_requested_question_type(database):
    from app.services.practice_selection import select_by_description
    pool(database, 3, 'code')
    pool(database, 3, 'theory')
    allowed = {row[0] for row in database.execute("SELECT id FROM question WHERE question_type='code'")}
    def reply(messages, **kwargs):
        context = json.loads(messages[1]['content'])
        assert {q['id'] for q in context['questions']} == allowed
        return SimpleNamespace(content=json.dumps({'question_ids': [context['questions'][0]['id']]}), model='fake')
    assert set(select_by_description(database, '我想练习', 1, 'typed-topic', client=SimpleNamespace(complete=reply), question_type='code')) <= allowed
