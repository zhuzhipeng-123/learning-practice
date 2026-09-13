import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.domain import Submission
from app.main import app
from app.services.alignment_report import inventory_changes, source_inventory
from app.services.dashboard import day_details, heatmap
from app.services.interview import add_turn, start_session
from app.services.practice import start_attempt, submit_code
from app.services.source_coverage import validate_suggestions
from app.services.source_refresh import analyze_alignment, queue_source_refresh, source_status
from app.services.source_state import observe_missing_questions
from app.services.sync import publish_snapshot
from app.services.tasks import create_daily_plan, fill_daily_plan
from app.storage.database import connect_database
from tests.helpers import add_source
from tests.test_docx_parser import heading, text_block
from tests.test_repair_sync import live_theory, run_live
from tests.test_sync import blocks, draft


def seed_ten_days(connection, today):
    add_source(connection)
    publish_snapshot(connection, 'source-code', '1', '1', blocks(), [draft()], 'v1')
    for offset in range(10):
        day = today - timedelta(days=9-offset)
        plan = create_daily_plan(connection, day, 1, 0, {}, f'day-{offset}')
        task = plan['tasks'][0]['id']
        now = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=8)
        start_attempt(connection, task, 'daily', now)
        submit_code(connection, Submission(task, f'answer-{offset}', now, 'daily', code_self_result='can_solve'))


def test_ten_days_visible_and_every_day_clickable():
    with TestClient(app) as client:
        db = connect_database(app.state.database_path)
        try:
            db.execute("DELETE FROM source")
            db.commit()
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            seed_ten_days(db, today)
            result = heatmap(db, today)
            assert result['active_days'] == result['activities'] == 10
            assert len(day_details(db, today)['groups']['can']) == 1
            active = [cell for week in result['weeks'] for cell in week if cell['total']]
            assert len(active) == 10
            homepage = client.get('/')
            assert homepage.status_code == 200
            for cell in active:
                assert f'href="/days/{cell["date"]}?scope=daily"' in homepage.text
                response = client.get('/days/' + cell['date'])
                assert response.status_code == 200 and 'Move zeroes' in response.text
            client.get('/')
            assert heatmap(db, today)['activities'] == 10
        finally:
            db.close()


def test_heatmap_deduplicates_and_uses_actual_local_day(database):
    add_source(database)
    publish_snapshot(database, 'source-code', '1', '1', blocks(), [draft()], 'v1')
    plan = create_daily_plan(database, date(2026, 9, 1), 1, 0, {}, 'plan')
    task = plan['tasks'][0]['id']
    now = datetime(2026, 9, 10, 16, 30, tzinfo=UTC)
    session = start_session(database, task, now)
    add_turn(database, session, 'assistant', 'Question', now-timedelta(days=1))
    start_attempt(database, task, 'daily', now)
    submit_code(database, Submission(task, 'submitted', now, 'daily', code_self_result='can_solve'))
    add_turn(database, session, 'user', 'First answer', now)
    add_turn(database, session, 'user', 'Second answer', now)
    assert heatmap(database, date(2026, 9, 11))['activities'] == 1
    assert day_details(database, date(2026, 9, 11))['interviews'][0]['answers'] == 2
    assert day_details(database, date(2026, 9, 1))['activity']['total'] == 0


def test_fill_preserves_existing_tasks_and_original_target(database):
    add_source(database)
    publish_snapshot(database, 'source-code', '1', '1', blocks(), [draft()], 'v1')
    first = create_daily_plan(database, date(2026, 9, 1), 1, 2, {}, 'plan')
    old_task = first['tasks'][0]
    add_source(database, 'source-theory')
    theory = [replace(draft(), source_id='source-theory', question_type='theory', main_anchor_block_id=f'q{i}',
                      category_path='LLM > Attention', prompt=f'Why {i}?') for i in range(2)]
    publish_snapshot(database, 'source-theory', '1', '1', blocks(), theory, 'v1')
    filled = fill_daily_plan(database, first['plan_id'], {'LLM > Attention': 2})
    assert filled['filled'] == 2 and filled['shortages'] == {}
    assert old_task in filled['tasks']
    assert fill_daily_plan(database, first['plan_id'])['filled'] == 0
    assert database.execute('SELECT code_target,theory_target,added_target FROM daily_plan').fetchone()[:] == (1, 2, 0)


def test_alignment_tracks_details_and_keeps_completed_history(database, monkeypatch):
    live_theory(database)
    raw = [heading('module', 1, 'LLM'), heading('q', 3, 'Why?'), text_block('r', 'Old reference')]
    first = run_live(database, monkeypatch, 1, raw)
    assert len(first['changes']['added']) == 1
    plan = create_daily_plan(database, date(2026, 9, 1), 0, 1, {}, 'plan')
    old = plan['tasks'][0]
    database.execute("UPDATE task SET status='completed' WHERE id=?", (old['id'],))
    database.commit()
    changed = [heading('module', 1, 'New LLM'), heading('q', 3, 'Why?'), text_block('new-ref', 'New reference')]
    result = run_live(database, monkeypatch, 2, changed)
    assert len(result['changes']['updated']) == 1
    assert result['changes']['modules_updated'] == [{'before': 'LLM', 'after': 'New LLM'}, {'before': 'LLM > Why?', 'after': 'New LLM > Why?'}]
    assert database.execute('SELECT question_version_id,status FROM task').fetchone()[:] == (old['question_version_id'], 'completed')
    assert database.execute('SELECT reference_text FROM question_version WHERE id=?', (old['question_version_id'],)).fetchone()[0] == 'Old reference'
    again = run_live(database, monkeypatch, 2, changed)
    assert again['changes']['updated'] == [] and again['changes']['added'] == []
    assert again['changes']['unchanged'] == 1


def test_ambiguous_absence_suspends_without_deleting_history(database):
    add_source(database)
    publish_snapshot(database, 'source-code', '1', '1', blocks(), [draft()], 'v1')
    before = source_inventory(database, 'source-code')
    observe_missing_questions(database, 'source-code', set(), True, True, True)
    report = inventory_changes(before, source_inventory(database, 'source-code'))
    assert len(report['missing']) == 1 and report['removed'] == []
    assert database.execute('SELECT missing_observation_count FROM source_binding').fetchone()[0] == 0
    assert create_daily_plan(database, date(2026, 9, 1), 1, 0, {}, 'plan')['tasks'] == []


def test_alignment_includes_model_analysis_and_persists_failure(database, monkeypatch):
    live_theory(database)
    result = run_live(database, monkeypatch, 1, [heading('q', 1, 'Why?'), text_block('r', 'Reference')])
    def reply(messages, **kwargs):
        payload = json.loads(messages[1]['content'])
        suggestions = [{'anchor_id': item['anchor_id'], 'decision': 'single', 'reason': 'one question', 'parts': []} for item in payload['candidates']]
        return SimpleNamespace(content=json.dumps({'suggestions': suggestions}), model='test')
    analysis = analyze_alignment(database, 'live', result, SimpleNamespace(complete=reply))
    assert analysis['status'] == 'complete' and analysis['processed'] == 1
    assert database.execute('SELECT COUNT(*) FROM question').fetchone()[0] == 0
    stored = json.loads(database.execute('SELECT summary_json FROM sync_run').fetchone()[0])
    assert stored['analysis']['suggestions'][0]['anchor_id'] == 'q'
    validate_suggestions({'candidates': [{'anchor_id': 'q'}]}, {'suggestions': [{'anchor_id': 'q', 'decision': 'single', 'reason': 'source', 'parts': []}]})


def test_day_details_distinguishes_cannot_and_pending_theory(database):
    from app.services.practice import submit_theory
    from tests.test_practice_review import seed_question
    seed_question(database, 'code')
    seed_question(database, 'theory')
    now = datetime(2026, 9, 12, 8, tzinfo=UTC)
    plan = create_daily_plan(database, now.date(), 1, 1, {}, 'mixed')
    for task in plan['tasks']:
        start_attempt(database, task['id'], 'daily', now)
        kind = database.execute('SELECT question_type FROM question WHERE id=?', (task['question_id'],)).fetchone()[0]
        if kind == 'code':
            submit_code(database, Submission(task['id'], 'cannot', now, 'daily', code_self_result='cannot_solve'))
        else:
            submit_theory(database, Submission(task['id'], 'theory', now, 'daily', answer_text='Saved answer'))
    groups = day_details(database, now.date())['groups']
    assert len(groups['cannot']) == len(groups['pending']) == 1
    assert groups['can'] == groups['unknown'] == []


def test_day_details_uses_only_adopted_theory_verdict(database):
    from app.services.practice import adopt_theory_evaluation, submit_theory
    from tests.test_practice_review import seed_question
    seed_question(database, 'theory')
    now = datetime(2026, 9, 12, 8, tzinfo=UTC)
    task = create_daily_plan(database, now.date(), 0, 1, {}, 'verdicts')['tasks'][0]['id']
    start_attempt(database, task, 'daily', now)
    saved = submit_theory(database, Submission(task, 'answer', now, 'daily', answer_text='Saved answer'))
    for verdict, key in [('aligned', 'can'), ('needs_review', 'cannot'), ('unable_to_assess', 'unknown')]:
        adopt_theory_evaluation(database, saved['attempt_id'], verdict, now, corrected_by_user=True)
        groups = day_details(database, now.date())['groups']
        assert len(groups[key]) == 1
        assert sum(len(items) for items in groups.values()) == 1
    assert heatmap(database, now.date())['activities'] == 1


def test_alignment_retries_malformed_json_only_once(database, monkeypatch):
    live_theory(database)
    result = run_live(database, monkeypatch, 1, [heading('q', 1, 'Why?'), text_block('r', 'Reference')])
    calls = []
    def reply(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(content='{"suggestions": INVALID}', model='test')
    analysis = analyze_alignment(database, 'live', result, SimpleNamespace(complete=reply))
    assert len(calls) == 2
    assert analysis['status'] == 'partial' and analysis['processed'] == 0
    assert analysis['errors']
    assert database.execute('SELECT COUNT(*) FROM question').fetchone()[0] == 0


def test_every_click_refreshes_but_inflight_clicks_deduplicate(database, monkeypatch):
    add_source(database)
    started, release = Event(), Event()
    calls = []
    def fake(location, source_id):
        calls.append(source_id)
        started.set()
        assert release.wait(5)
    monkeypatch.setattr('app.services.source_refresh._refresh', fake)
    queue_source_refresh(database, force=True)
    assert started.wait(2)
    queue_source_refresh(database, force=True)
    assert source_status(database)[0]['running']
    assert calls == ['source-code']
    release.set()
    from app.services.source_refresh import _jobs
    key = (database.execute('PRAGMA database_list').fetchone()[2], 'source-code')
    _jobs[key].result(timeout=2)
    queue_source_refresh(database, force=True)
    _jobs[key].result(timeout=2)
    assert calls == ['source-code', 'source-code']


def test_parallel_api_allocation_never_fetches_sources(monkeypatch):
    monkeypatch.setattr('app.routes.api.local_today', lambda: date(2026, 9, 1))
    def forbidden(*args, **kwargs):
        raise AssertionError('ordinary allocation must not fetch sources')
    monkeypatch.setattr('app.routes.api.queue_source_refresh', forbidden)
    with TestClient(app) as client:
        def request(index):
            return client.post('/api/plans', headers={'X-Requested-With': 'learning-practice', 'Idempotency-Key': f'parallel-{index}'},
                               json={'plan_date': '2026-09-01', 'code_target': 1, 'theory_target': 1})
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(request, range(8)))
        assert all(response.status_code == 200 for response in responses)
        assert len({response.json()['plan_id'] for response in responses}) == 1
