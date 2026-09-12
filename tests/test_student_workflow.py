import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.domain import Submission
from app.main import app
from app.services.candidates import reject_candidate
from app.services.dashboard import heatmap
from app.services.free_practice import add_free_practice
from app.services.interview import add_turn
from app.services.interview_setup import prepare_interview
from app.services.llm_config import get_module_config
from app.services.model_jobs import ModelJobError
from app.services.module_jobs import interview_context, reflection_context
from app.services.plan_editing import update_daily_plan
from app.services.practice import start_attempt, submit_code
from app.services.practice_selection import select_by_description
from app.services.source_sync import _store_candidates
from app.services.sync import publish_snapshot
from app.services.tasks import create_daily_plan
from app.services.wiki_alignment import _review_changes
from app.storage.database import connect_database
from tests.helpers import add_source
from tests.test_practice_review import seed_question
from tests.test_sync import blocks, draft


def pool(database, count=5, kind='theory'):
    source = 'source-' + kind
    add_source(database, source)
    database.execute('UPDATE source SET question_type=? WHERE id=?', (kind, source))
    drafts = [replace(draft(), source_id=source, document_id='document-' + source, question_type=kind,
                      category_path='Old module', prompt=f'Question {i}', main_anchor_block_id=f'q{i}') for i in range(count)]
    publish_snapshot(database, source, '1', '1', blocks(), drafts, 'v1')
    return drafts


def test_edit_targets_increase_reduce_and_keep_started_answer(database):
    pool(database, kind='code')
    plan = create_daily_plan(database, date(2026, 9, 12), 3, 0, {}, 'plan')
    fixed = plan['tasks'][0]
    start_attempt(database, fixed['id'], 'daily', datetime.now(UTC))
    reduced = update_daily_plan(database, plan['plan_id'], 1, 0, {})
    assert len(reduced['tasks']) == 1 and reduced['tasks'][0]['id'] == fixed['id']
    assert reduced['cancelled'] == 2
    with pytest.raises(ValueError, match='不能少于'):
        update_daily_plan(database, plan['plan_id'], 0, 0, {})
    increased = update_daily_plan(database, plan['plan_id'], 4, 0, {})
    assert len(increased['tasks']) == 4
    assert update_daily_plan(database, plan['plan_id'], 4, 0, {})['filled'] == 0
    assert database.execute('SELECT question_version_id FROM task WHERE id=?', (fixed['id'],)).fetchone()[0] == fixed['question_version_id']
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 1


def test_module_rename_allows_explicit_new_allocation(database):
    drafts = pool(database, count=1)
    plan = create_daily_plan(database, date(2026, 9, 12), 0, 2, {'Old module': 2}, 'plan')
    updated = [replace(drafts[0], category_path='New module'), replace(drafts[0], category_path='New module', main_anchor_block_id='q2', prompt='Second')]
    publish_snapshot(database, 'source-theory', '2', '2', blocks('Changed'), updated, 'v1')
    result = update_daily_plan(database, plan['plan_id'], 0, 2, {'New module': 2})
    assert result['shortages'] == {} and len(result['tasks']) == 2


def test_rejected_note_stays_ignored_until_its_content_changes(database):
    drafts = pool(database, count=1)
    candidate = replace(drafts[0], main_anchor_block_id='note', confirmation_status='pending')
    for index, content in enumerate(['same', 'same', 'actually changed']):
        snapshot = publish_snapshot(database, 'source-theory', str(index+2), str(index+2), blocks(str(index)), drafts, 'v1')
        _store_candidates(database, 'source-theory', snapshot['snapshot_id'], [replace(candidate, reference_text=content)])
        database.commit()
        row = database.execute('SELECT id,status FROM parser_candidate ORDER BY rowid DESC LIMIT 1').fetchone()
        if index == 0:
            reject_candidate(database, row['id'])
        else:
            assert row['status'] == ('rejected' if index == 1 else 'pending')


def test_random_can_include_previously_done_and_remains_idempotent(database):
    seed_question(database, 'code')
    now = datetime.now(UTC)
    plan = create_daily_plan(database, now.date(), 1, 0, {}, 'plan')
    task = plan['tasks'][0]['id']
    start_attempt(database, task, 'daily', now)
    submit_code(database, Submission(task, 'done', now, 'daily', code_self_result='can_solve'))
    result = add_free_practice(database, plan['plan_id'], '', 1, 'random', True, True, only_new=False)
    assert result.added == 1
    assert add_free_practice(database, plan['plan_id'], '', 1, 'random', True, True, only_new=False) == result


def test_topic_model_can_only_select_supplied_question_ids(database):
    pool(database)
    def reply(messages, **kwargs):
        context = json.loads(messages[1]['content'])
        assert context['description'] == 'Focus on the topics I described'
        return SimpleNamespace(content=json.dumps({'question_ids': [context['questions'][0]['id']]}), model='test')
    assert len(select_by_description(database, 'Focus on the topics I described', 2, 'selection', client=SimpleNamespace(complete=reply))) == 1
    bad = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content='{"question_ids":["invented"]}', model='test'))
    with pytest.raises(ModelJobError):
        select_by_description(database, 'anything', 2, 'bad-selection', client=bad)


def test_custom_interview_focus_and_duplicate_click(database):
    result = prepare_interview(database, None, 'Explain my project choices', 'My actual job requirements', 'interview')
    assert prepare_interview(database, None, 'Explain my project choices', 'My actual job requirements', 'interview') == result
    add_turn(database, result['session_id'], 'user', 'My real answer', datetime.now(UTC))
    context = interview_context(database, result['session_id'], 'interview_followup')
    assert context['question'] == 'Explain my project choices'
    assert context['job_focus'] == 'My actual job requirements'
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1


def test_reflection_contains_wrong_question_and_preserves_user_note(database):
    seed_question(database, 'code')
    now = datetime.now(UTC)
    plan = create_daily_plan(database, now.date(), 1, 0, {}, 'plan')
    task = plan['tasks'][0]['id']
    start_attempt(database, task, 'daily', now)
    submit_code(database, Submission(task, 'done', now, 'daily', code_self_result='cannot_solve', note='Forgot the invariant'))
    from zoneinfo import ZoneInfo
    context = reflection_context(database, now.astimezone(ZoneInfo('Asia/Shanghai')).date())
    wrong = context['wrong_answers'][0]
    assert wrong['prompt'] == 'code prompt' and wrong['note'] == 'Forgot the invariant'
    assert wrong['category_path'] == 'category'
    assert wrong['code_self_result'] == '不会'


def test_interview_summary_does_not_become_a_followup_or_block_continuation(database):
    from app.services.module_jobs import run_module_job
    result = prepare_interview(database, None, 'My question', '', 'interview-summary')
    session = result['session_id']
    add_turn(database, session, 'user', 'My answer', datetime.now(UTC))
    fake = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content='Saved summary', model='test'))
    run_module_job(database, 'interview_feedback', session, 'summary', fake)
    context = interview_context(database, session, 'interview_followup')
    assert [turn['content'] for turn in context['turns']] == ['My answer']
    followup = run_module_job(database, 'interview_followup', session, 'continue', fake)
    assert followup['result_id']
    assert database.execute('SELECT COUNT(*) FROM interview_turn').fetchone()[0] == 3


def test_day_rollover_changes_home_date_and_heatmap_without_new_activity(monkeypatch):
    day = date(2026, 9, 12)
    monkeypatch.setattr('app.routes.pages.local_today', lambda: day)
    monkeypatch.setattr('app.routes.api.local_today', lambda: day)
    with TestClient(app) as client:
        first = client.get('/')
        assert 'data-local-date="2026-09-12"' in first.text
        day += timedelta(days=1)
        assert client.get('/api/day-status').json()['date'] == '2026-09-13'
        second = client.get('/')
        assert '/days/2026-09-13' in second.text and 'data-local-date="2026-09-13"' in second.text
        connection = connect_database(app.state.database_path)
        try:
            assert heatmap(connection, day)['activities'] == 0
        finally:
            connection.close()


def test_change_description_reaches_model_with_actual_report(database, monkeypatch):
    add_source(database)
    summary = {'change_note': 'I moved a chapter', 'tree_complete': True, 'tree_errors': [], 'document_changes': {'moved': []}, 'changes': {'added': [], 'removed': []}, 'documents': []}
    def run(connection, module, target, key, context_override):
        assert context_override['user_change_description'] == summary['change_note']
        assert context_override['document_changes']['moved'] == []
        return {'response_text': '{"review":"No move confirmed by the latest diff","unresolved":["Check the chapter"]}'}
    monkeypatch.setattr('app.services.module_jobs.run_module_job', run)
    _review_changes(database, 'source-code', 'run', summary)
    assert summary['user_review']['unresolved'] == ['Check the chapter']


def test_module_budgets_match_output_purpose(database):
    assert get_module_config(database, 'interview_followup')['max_tokens'] == 1024
    assert get_module_config(database, 'daily_reflection')['max_tokens'] == 4096
    assert get_module_config(database, 'source_parsing')['max_tokens'] == 4096


def test_model_outage_stops_more_analysis_but_keeps_reading_documents(database, monkeypatch):
    from threading import Event

    from app.services.wiki_alignment import _align_document
    calls = []
    reads = []
    def read(connection, source_id, remote):
        reads.append(source_id)
        return {'candidate_count': 1, 'partial': True}
    def analyze(connection, source_id, result):
        calls.append(source_id)
        return {'errors': ['模型连接失败'], 'total': 1, 'processed': 0, 'suggestions': []}
    monkeypatch.setattr('app.services.wiki_alignment.sync_registered_source', read)
    location = database.execute('PRAGMA database_list').fetchone()[2]
    stopped = Event()
    first, second = {'source_id': 'first'}, {'source_id': 'second'}
    _align_document(location, first, analyze, None, stopped)
    _align_document(location, second, analyze, None, stopped)
    assert reads == ['first', 'second'] and calls == ['first']
    assert second['result']['analysis']['processed'] == 0
    assert '尚未复核' in second['result']['analysis']['errors'][0]


def test_free_practice_api_without_existing_plan_returns_question_cards():
    with TestClient(app) as client:
        connection = connect_database(app.state.database_path)
        try:
            connection.execute('DELETE FROM source')
            connection.commit()
            seed_question(connection, 'code')
        finally:
            connection.close()
        response = client.post('/api/free-practice', headers={'X-Requested-With': 'learning-practice', 'Idempotency-Key': 'cards'}, json={'mode': 'random', 'count': 1})
        assert response.status_code == 200
        result = response.json()
        assert result['tasks'][0]['prompt'] == 'code prompt' and result['added'] == 1
        assert 'freshness' not in result


def test_viewing_model_reflection_records_exposure_without_changing_activity(database):
    from app.services.reflections import save_model_reflection, view_model_reflection
    from app.services.review import exposed_recently
    seed_question(database, 'code')
    now = datetime.now(UTC)
    plan = create_daily_plan(database, now.date(), 1, 0, {}, 'plan')
    task = plan['tasks'][0]
    start_attempt(database, task['id'], 'daily', now)
    answer = submit_code(database, Submission(task['id'], 'done', now, 'daily', code_self_result='cannot_solve'))
    reflection = save_model_reflection(database, now.date(), 'A study suggestion', [answer['attempt_id']], now)
    database.commit()
    assert not exposed_recently(database, task['question_id'], now)
    assert view_model_reflection(database, reflection, now)['content'] == 'A study suggestion'
    assert exposed_recently(database, task['question_id'], now)
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 1
