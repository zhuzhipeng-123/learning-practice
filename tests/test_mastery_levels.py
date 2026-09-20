from datetime import UTC, date, datetime

import pytest

from app.domain import Submission
from app.services.dashboard import day_details, heatmap
from app.services.module_jobs import reflection_context
from app.services.practice import (
    PracticeError,
    adopt_theory_evaluation,
    set_mastery_level,
    start_attempt,
    submit_code,
    submit_unable,
)
from app.services.tasks import IdempotencyConflictError
from tests.test_practice_review import make_plan, seed_question

NOW = datetime(2026, 9, 11, 8, tzinfo=UTC)


@pytest.mark.parametrize('level', ['unknown', 'vague', 'partial'])
def test_theory_unable_is_atomic_without_model_or_fake_answer(database, monkeypatch, level):
    monkeypatch.setattr('app.services.practice.local_today', lambda: date(2026, 9, 11))
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    start_attempt(database, task['id'], 'daily', NOW)

    result = submit_unable(database, Submission(
        task['id'], f'unable-{level}', NOW, 'daily', mastery_level=level,
    ))

    attempt = database.execute('SELECT * FROM attempt').fetchone()
    assert result['evaluation_status'] == 'not_requested'
    assert attempt['answer_text'] is None and attempt['submitted_at']
    assert database.execute('SELECT COUNT(*) FROM model_job').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM review_round WHERE status="active"').fetchone()[0] == 1
    assert database.execute('SELECT level FROM mastery_assessment').fetchone()[0] == level
    assert heatmap(database, NOW.date())['activities'] == 1
    details = day_details(database, NOW.date())
    assert details['groups']['pending'] == []
    assert details['groups']['cannot'][0]['explicit_unable'] == 1
    assert '明确不会' in reflection_context(database, NOW.date())['records'][0]['assessment_status']


def test_code_unable_level_does_not_create_a_valid_pass(database, monkeypatch):
    monkeypatch.setattr('app.services.practice.local_today', lambda: date(2026, 9, 11))
    seed_question(database, 'code')
    task = make_plan(database, 'code')
    start_attempt(database, task['id'], 'daily', NOW)

    result = submit_code(database, Submission(
        task['id'], 'code-unable', NOW, 'daily', code_self_result='cannot_solve',
        mastery_level='vague',
    ))

    assert result['mastery_level'] == 'vague'
    assert database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0] == 0
    assert database.execute('SELECT level FROM mastery_assessment').fetchone()[0] == 'vague'


def test_review_basis_conflict_rolls_back_entire_code_submission(database, monkeypatch):
    monkeypatch.setattr('app.services.practice.local_today', lambda: date(2026, 9, 11))
    question = seed_question(database, 'code')
    task = make_plan(database, 'code')
    start_attempt(database, task['id'], 'daily', NOW)
    database.execute('INSERT INTO review_basis VALUES (?,?,?,?)',
                     ('other-basis', question, 'other-hash', NOW.isoformat()))
    database.execute("INSERT INTO review_round VALUES ('other-round',?,?,'active','test',?,NULL,NULL)",
                     (question, 'other-basis', NOW.isoformat()))
    database.commit()

    with pytest.raises(PracticeError, match='另一个版本'):
        submit_code(database, Submission(
            task['id'], 'conflicting', NOW, 'daily', code_self_result='cannot_solve',
            mastery_level='unknown',
        ))

    assert database.execute('SELECT submitted_at FROM attempt').fetchone()[0] is None
    assert database.execute('SELECT status FROM task').fetchone()[0] == 'in_progress'
    assert database.execute('SELECT COUNT(*) FROM mastery_assessment').fetchone()[0] == 0


def test_review_basis_conflict_rolls_back_entire_theory_unable(database, monkeypatch):
    monkeypatch.setattr('app.services.practice.local_today', lambda: date(2026, 9, 11))
    question = seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    start_attempt(database, task['id'], 'daily', NOW)
    database.execute('INSERT INTO review_basis VALUES (?,?,?,?)',
                     ('other-theory-basis', question, 'other-theory-hash', NOW.isoformat()))
    database.execute("INSERT INTO review_round VALUES ('other-theory-round',?,?,'active','test',?,NULL,NULL)",
                     (question, 'other-theory-basis', NOW.isoformat()))
    database.commit()

    with pytest.raises(PracticeError, match='另一个版本'):
        submit_unable(database, Submission(
            task['id'], 'theory-conflicting', NOW, 'daily', mastery_level='unknown',
        ))

    assert database.execute('SELECT submitted_at FROM attempt').fetchone()[0] is None
    assert database.execute('SELECT COUNT(*) FROM model_job').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM mastery_assessment').fetchone()[0] == 0


def test_old_submission_retry_does_not_replace_newer_mastery(database, monkeypatch):
    monkeypatch.setattr('app.services.practice.local_today', lambda: date(2026, 9, 11))
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    start_attempt(database, task['id'], 'daily', NOW)
    submission = Submission(task['id'], 'original-unable', NOW, 'daily', mastery_level='unknown')
    original = submit_unable(database, submission)
    changed = set_mastery_level(database, task['id'], 'partial', 'newer-level', original['mastery_id'])

    assert submit_unable(database, submission) == original
    assert database.execute('SELECT level FROM mastery_assessment ORDER BY rowid DESC').fetchone()[0] == 'partial'
    assert changed['mastery_level'] == 'partial'
    with pytest.raises(IdempotencyConflictError):
        submit_unable(database, Submission(task['id'], 'original-unable', NOW, 'daily', mastery_level='vague'))


def test_level_only_change_is_idempotent_and_does_not_add_activity(database, monkeypatch):
    monkeypatch.setattr('app.services.practice.local_today', lambda: date(2026, 9, 11))
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    start_attempt(database, task['id'], 'daily', NOW)
    submitted = submit_unable(database, Submission(
        task['id'], 'unable', NOW, 'daily', mastery_level='unknown',
    ))
    before = (heatmap(database, NOW.date())['activities'],
              database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0],
              database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0])

    first = set_mastery_level(database, task['id'], 'vague', 'level-change', submitted['mastery_id'])
    assert set_mastery_level(database, task['id'], 'vague', 'level-change', submitted['mastery_id']) == first
    after = (heatmap(database, NOW.date())['activities'],
             database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0],
             database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0])
    assert after == before
    with pytest.raises(IdempotencyConflictError, match='已在其他页面更新'):
        set_mastery_level(database, task['id'], 'partial', 'stale-page', submitted['mastery_id'])


def test_model_evaluation_does_not_change_mastery(database, monkeypatch):
    monkeypatch.setattr('app.services.practice.local_today', lambda: date(2026, 9, 11))
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    start_attempt(database, task['id'], 'daily', NOW)
    submitted = submit_unable(database, Submission(
        task['id'], 'unable-for-evaluation', NOW, 'daily', mastery_level='partial',
    ))

    adopt_theory_evaluation(database, submitted['attempt_id'], 'unable_to_assess', NOW)

    assert database.execute('SELECT level FROM mastery_assessment').fetchone()[0] == 'partial'


def test_legacy_review_data_remains_ungraded(database):
    assert database.execute('SELECT COUNT(*) FROM mastery_assessment').fetchone()[0] == 0
