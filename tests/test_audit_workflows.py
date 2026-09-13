import hashlib
import json
import zipfile
from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.dashboard import day_details, latest_reflections
from app.services.exports import ExportError, export_learning_data, verify_export
from app.services.interview import add_turn, start_session
from app.services.module_jobs import reflection_context
from app.services.plan_editing import update_daily_plan
from app.services.practice import PracticeError, add_theory_to_review
from app.services.reflections import ReflectionError, save_model_reflection, save_user_reflection
from app.services.tasks import IdempotencyConflictError
from tests.test_practice_review import make_plan, seed_question

NOW = datetime(2026, 9, 11, 15, 59, tzinfo=UTC)


def test_plan_retry_never_reverts_later_edit_even_after_midnight(database):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    original = {'code_target': 0, 'theory_target': 1, 'module_quotas': {}}
    first = update_daily_plan(database, task['plan_id'], 0, 2, {}, 'edit-a', original)
    second_state = {**original, 'theory_target': 2}
    update_daily_plan(database, task['plan_id'], 0, 3, {}, 'edit-b', second_state)
    assert update_daily_plan(database, task['plan_id'], 0, 2, {}, 'edit-a', original, date(2026, 9, 12)) == first
    assert database.execute('SELECT theory_target FROM daily_plan').fetchone()[0] == 3
    with pytest.raises(ValueError, match='其他页面'):
        update_daily_plan(database, task['plan_id'], 0, 4, {}, 'stale-tab', second_state)
    with pytest.raises(IdempotencyConflictError):
        update_daily_plan(database, task['plan_id'], 0, 4, {}, 'edit-a', original)


def test_reflection_replay_keeps_newer_text_and_rejects_stale_edit(database):
    day = NOW.date()
    first = save_user_reflection(database, day, '第一版思考', NOW, 'reflection-a', '')
    second = save_user_reflection(database, day, '第二版补充', NOW, 'reflection-b', first)
    assert save_user_reflection(database, day, '第一版思考', NOW, 'reflection-a', '') == first
    assert latest_reflections(database, day)['user']['id'] == second
    with pytest.raises(ReflectionError, match='其他页面'):
        save_user_reflection(database, day, '陈旧页面内容', NOW, 'reflection-c', first)
    assert database.execute('SELECT COUNT(*) FROM reflection').fetchone()[0] == 2


def test_late_stale_reflection_does_not_hide_matching_result(database):
    current = save_model_reflection(database, NOW.date(), '新事实的复盘', ['new'], NOW)
    late = save_model_reflection(database, NOW.date(), '旧事实的迟到复盘', ['old'], NOW)
    database.execute('UPDATE reflection SET stale=1 WHERE id=?', (late,))
    assert latest_reflections(database, NOW.date())['model']['id'] == current
    assert day_details(database, NOW.date())['reflections'][0]['id'] == current


def test_cross_midnight_answer_keeps_actual_followup_without_extra_activity(database):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    session = start_session(database, task['id'], NOW)
    add_turn(database, session, 'user', '第一题的回答', NOW)
    question = add_turn(database, session, 'assistant', '日志为何必须在提交之前持久化？', NOW)
    answer = add_turn(database, session, 'user', '这样可以在崩溃后恢复已提交操作。', NOW + timedelta(minutes=2))
    context = reflection_context(database, date(2026, 9, 12))
    record = next(row for row in context['records'] if row['id'] == answer)
    assert record['answered_question'] == {'id': question, 'content': '日志为何必须在提交之前持久化？'}
    assert question not in {row['id'] for row in context['records']}
    assert context['activity_units'] == 1


def test_old_task_review_uses_frozen_version_and_cannot_replace_other_basis(database):
    question = seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    original = database.execute('SELECT * FROM question_version').fetchone()
    database.execute('INSERT INTO review_basis VALUES (?,?,?,?)', ('new-basis', question, 'changed', NOW.isoformat()))
    updated = dict(original) | {'id': 'new-version', 'review_basis_id': 'new-basis', 'reference_text': 'New basis', 'content_hash': 'changed'}
    database.execute('INSERT INTO question_version VALUES (?,?,?,?,?,?,?,?,?,?,?)', tuple(updated.values()))
    database.execute('UPDATE question SET current_version_id=? WHERE id=?', ('new-version', question))
    first = add_theory_to_review(database, question, 'old-task', NOW, task['question_version_id'])
    assert database.execute('SELECT review_basis_id FROM review_round WHERE id=?', (first,)).fetchone()[0] == original['review_basis_id']
    assert add_theory_to_review(database, question, 'old-task', NOW, task['question_version_id']) == first
    with pytest.raises(PracticeError, match='另一个版本'):
        add_theory_to_review(database, question, 'new-version', NOW)
    assert database.execute("SELECT COUNT(*) FROM review_round WHERE status='active'").fetchone()[0] == 1


@pytest.mark.parametrize('corrupt', [False, True])
def test_export_checks_media_referenced_by_historical_version(database, tmp_path, corrupt):
    question = seed_question(database, 'theory')
    version = database.execute('SELECT current_version_id FROM question WHERE id=?', (question,)).fetchone()[0]
    media = tmp_path / 'media'
    media.mkdir()
    item = {'kind': 'media', 'role': 'reference', 'status': 'complete', 'path': 'original.png', 'sha256': hashlib.sha256(b'original').hexdigest()}
    database.execute('UPDATE version_resources SET materials_json=? WHERE version_id=?', (json.dumps([item]), version))
    # A detached historical version is still part of the backup contract.
    database.execute('UPDATE question SET current_version_id=NULL WHERE id=?', (question,))
    database.commit()
    if corrupt:
        (media / 'original.png').write_bytes(b'changed')
    with pytest.raises(ExportError, match='附件'):
        export_learning_data(database, tmp_path / 'incomplete.zip', media)
    assert not (tmp_path / 'incomplete.zip').exists()
    (media / 'original.png').write_bytes(b'original')
    archive = export_learning_data(database, tmp_path / 'valid.zip', media)
    assert verify_export(archive, tmp_path / 'verify')['media_count'] == 1
    # A manifest omitting the DB dependency is invalid even if all listed files exist.
    with zipfile.ZipFile(archive) as good, zipfile.ZipFile(tmp_path / 'forged.zip', 'w') as bad:
        manifest = json.loads(good.read('manifest.json'))
        manifest['media'] = {}
        bad.writestr('learning.db', good.read('learning.db'))
        bad.writestr('manifest.json', json.dumps(manifest))
    with pytest.raises(ExportError, match='附件'):
        verify_export(tmp_path / 'forged.zip', tmp_path / 'verify')
