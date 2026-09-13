"""Behavior regressions from the September review, using isolated real SQLite."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.parsers.docx import parse_docx_blocks
from app.services.practice import add_theory_to_review
from app.services.practice_selection import validate_selection
from app.services.review import exposed_recently
from app.services.review_tasks import start_review_task
from app.services.tasks import create_daily_plan
from tests.test_practice_review import seed_question


def text_block(key, text, kind=2):
    name = {2: 'text', 3: 'heading1', 4: 'heading2'}.get(kind, 'text')
    return {'block_id': key, 'block_type': kind,
            name: {'elements': [{'text_run': {'content': text}}]}}


@pytest.mark.parametrize('zone', ['UTC', 'Asia/Shanghai', 'Asia/Tokyo'])
def test_exposure_compares_absolute_instants(database, zone):
    question = seed_question(database, 'theory')
    now = datetime(2026, 9, 12, 12, tzinfo=UTC)
    database.execute('INSERT INTO question_exposure VALUES (?,?)',
                     (question, (now - timedelta(hours=20)).isoformat()))
    assert exposed_recently(database, question, now.astimezone(ZoneInfo(zone)))
    assert not exposed_recently(database, question, now + timedelta(hours=4))


def test_review_version_conflict_returns_existing_without_duplicate(database):
    question = seed_question(database, 'theory')
    add_theory_to_review(database, question, 'test', datetime.now(UTC))
    version = dict(database.execute('SELECT * FROM question_version').fetchone())
    database.execute('INSERT INTO review_basis VALUES (?,?,?,?)',
                     ('new-basis', question, 'changed-reference', datetime.now(UTC).isoformat()))
    version.update(id='new-version', review_basis_id='new-basis', content_hash='changed')
    database.execute('INSERT INTO question_version VALUES (?,?,?,?,?,?,?,?,?,?,?)', tuple(version.values()))
    database.execute('UPDATE question SET current_version_id=? WHERE id=?', ('new-version', question))
    database.commit()
    plan = create_daily_plan(database, date(2026, 9, 12), 0, 1, {}, 'today')
    result = start_review_task(database, question, date(2026, 9, 12), 'review')
    assert result['task_id'] == plan['tasks'][0]['id']
    assert result['version_conflict'] is True
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1
    assert database.execute('SELECT added_target FROM daily_plan').fetchone()[0] == 0


@pytest.mark.parametrize('tail', [
    [text_block(f'p{i}', 'reference') for i in range(201)],
    [text_block('p', 'See the necessary table'), {'block_id': 'unknown', 'block_type': 999}],
    [text_block('p', 'See the necessary table'), {'block_id': 'table', 'block_type': 31}],
])
def test_incomplete_reference_never_claims_complete(tail):
    blocks = [text_block('module', 'Module', 3), text_block('question', 'Explain?', 4), *tail]
    result = parse_docx_blocks('source', 'document', 'theory', blocks)
    assert not result.published
    assert result.candidates[0].material_status != 'complete'


def test_topic_matching_returns_pool_larger_than_draw():
    context = {'count': 3, 'questions': [{'id': str(i)} for i in range(20)]}
    assert len(validate_selection(context, {'question_ids': [str(i) for i in range(20)]})) == 20


def test_random_sampling_changes_members_and_request_retry_stays_stable(database):
    from app.services.free_practice import add_free_practice
    from tests.test_student_workflow import pool
    pool(database, 20)
    plan = create_daily_plan(database, date(2026, 9, 12), 0, 0, {}, 'plan')
    candidates = [row[0] for row in database.execute('SELECT id FROM question')]
    memberships = set()
    for seed in range(5):
        database.execute('SAVEPOINT sample')
        first = add_free_practice(database, plan['plan_id'], 'topic', 3, 'sample', False, True, seed, candidates)
        again = add_free_practice(database, plan['plan_id'], 'topic', 3, 'sample', False, True, seed+50, candidates)
        assert again == first
        memberships.add(tuple(sorted(row[0] for row in database.execute('SELECT question_id FROM task'))))
        database.execute('ROLLBACK TO sample')
        database.execute('RELEASE sample')
    assert len(memberships) > 1
