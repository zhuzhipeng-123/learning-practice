import json
from types import SimpleNamespace

import httpx
import pytest

from app.adapters.agnes import AgnesClient, AgnesSettings
from app.services.interview_setup import prepare_direction
from app.services.model_jobs import ModelJobError
from app.services.model_json import ModelJSONError, parse_model_json
from app.services.question_quality import validate_quality
from tests.quality_fixtures import quality_result


def reply(value):
    return SimpleNamespace(content=json.dumps(value, ensure_ascii=False), model='fixture')


def test_json_transport_is_requested_for_quotes_and_multiline_answers(monkeypatch):
    captured = {}
    answer = {'question':'如何处理带引号的文本？', 'reference_text':'例如 "训练样本"。\n第二行保留。'}
    def post(url, **kwargs):
        captured.update(kwargs['json'])
        return httpx.Response(200, json={'model':'fixture', 'choices':[{'message':{'content':json.dumps(answer)}, 'finish_reason':'stop'}]})
    monkeypatch.setattr(httpx, 'post', post)
    reply = AgnesClient(AgnesSettings('https://example.test', 'test-key')).complete_json(
        [{'role':'user', 'content':'生成问题和对应答案。'}], max_tokens=2048)
    assert captured['response_format'] == {'type':'json_object'}
    assert parse_model_json(reply.content) == answer


def test_malformed_quotes_are_not_reported_as_output_truncation():
    text = '{"question":"为什么需要难负样本？","reference_text":"例子中出现 "未转义引号"，正文实际完整。"}'
    with pytest.raises(ModelJSONError) as result:
        parse_model_json(text)
    assert '格式' in str(result.value)
    assert '输出上限' not in str(result.value)


def test_generated_opening_without_its_answer_never_creates_session(database):
    fake = SimpleNamespace(complete=lambda *a, **k:SimpleNamespace(content=json.dumps({'question':'如何处理工具调用失败？'}),model='fake'))
    with pytest.raises(ModelJobError):
        prepare_direction(database, 'opening', '工具调用恢复', '', [], 'missing-answer', fake)
    assert database.execute('SELECT COUNT(*) FROM interview_session').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'unknown', 'string_bool'])
def test_checker_requires_complete_unambiguous_decisions(fault):
    context = {'items':[{'id':'a'}, {'id':'b'}]}
    value = quality_result(context['items'])
    if fault == 'missing': value['items'].pop()
    if fault == 'duplicate': value['items'][1]['id'] = 'a'
    if fault == 'unknown': value['items'][1]['id'] = 'invented'
    if fault == 'string_bool': value['items'][0]['answer_matches'] = 'true'
    with pytest.raises(ModelJobError):
        validate_quality(context, value)


def test_optimistic_flags_with_written_objections_never_publish(database, monkeypatch):
    def review(messages, **kwargs):
        value = quality_result(json.loads(messages[1]['content'])['items'])
        value['items'][0]['issues'] = ['核心概念错误']
        return reply(value)
    monkeypatch.setattr('app.services.question_quality.client_for_config', lambda _:SimpleNamespace(complete=review))
    fake = SimpleNamespace(complete=lambda *a, **k:reply({'question':'工具调用为什么需要幂等？','reference_text':'内容仍有严重错误，不能发布。'}))
    with pytest.raises(ModelJobError, match='核对未通过'):
        prepare_direction(database, 'opening', '工具调用恢复', '', [], 'contradiction', fake)
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 0


@pytest.mark.parametrize('recover', [False, True])
def test_semantic_failure_has_one_revision_and_no_partial_publication(database, monkeypatch, recover):
    from app.services.learning_clock import local_today
    from app.services.practice_generation import generate_variants
    from app.services.tasks import create_daily_plan
    from tests.test_student_workflow import pool
    pool(database, 2, 'theory')
    plan = create_daily_plan(database, local_today(), 0, 0, {}, 'qa-plan')
    calls = []
    def generate(messages, **kwargs):
        calls.append(messages)
        originals = json.loads(messages[1]['content'])['questions']
        answer = '正确答案：定义查询相关性后，选择词汇接近但与查询目标无关的负样本。' if recover and len(calls) == 2 else '关键词训练：苹果发布会与所有苹果问题总是不相关。'
        return reply({'questions':[{'base_question_id':q['id'], 'prompt':'如何定义难负样本并避免假负例？'+q['prompt'], 'reference_text':answer} for q in originals]})
    reviews = []
    def review(messages, **kwargs):
        context = json.loads(messages[1]['content'])
        reviews.append(context)
        return reply(quality_result(context['items'], all('正确答案' in i['reference_text'] for i in context['items'])))
    monkeypatch.setattr('app.services.question_quality.client_for_config', lambda _:SimpleNamespace(complete=review))
    action = lambda: generate_variants(database, plan['plan_id'], '检索训练', 2, 'theory', False, 'semantic', client=SimpleNamespace(complete=generate))
    if recover:
        result = action()
        assert result['added'] == 2
        assert all('正确答案' in r[0] for r in database.execute("SELECT v.reference_text FROM question q JOIN question_version v ON v.id=q.current_version_id WHERE q.source_kind='derived'"))
        assert len(reviews) == 2 and 'revision_instruction' not in reviews[1]
        config = json.loads(database.execute("SELECT config_json FROM model_request WHERE module='question_quality' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        assert config['generation_provenance']['revision_instruction']
    else:
        with pytest.raises(ModelJobError, match='核对未通过'):
            action()
        assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 0
        assert database.execute('SELECT COUNT(*) FROM question_derivation').fetchone()[0] == 0
    assert len(calls) == 2
    assert '检查意见' in calls[1][-1]['content']
    assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0


def test_checker_outage_prevents_question_publication(database, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError('fixture provider outage')
    monkeypatch.setattr('app.services.question_quality.client_for_config', lambda _:SimpleNamespace(complete=unavailable))
    fake = SimpleNamespace(complete=lambda *a, **k:reply({'question':'工具调用为什么需要幂等？','reference_text':'相同操作重试时应只产生一次业务效果。'}))
    with pytest.raises(ModelJobError):
        prepare_direction(database, 'opening', '工具调用恢复', '', [], 'checker-outage', fake)
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM model_job WHERE status='failed'").fetchone()[0] == 2


def test_opening_and_followup_answers_are_stored_hidden_and_explicitly_read(database):
    from datetime import UTC, datetime

    from app.services.interview import add_turn
    from app.services.interview_answers import view_reference
    from app.services.module_jobs import run_module_job
    opening = {'question':'工具调用为什么需要幂等？','reference_text':'主问题答案：相同操作重试时应只产生一次业务效果。'}
    session = prepare_direction(database, 'opening', '工具调用恢复', '', [], 'paired', SimpleNamespace(complete=lambda *a, **k:reply(opening)))['session_id']
    add_turn(database, session, 'user', '可使用稳定请求标识。', datetime.now(UTC))
    followup = {'question':'响应丢失后如何判断操作结果？','reference_text':'追问答案：使用原标识查询状态，或按原标识重试。'}
    result = run_module_job(database, 'interview_followup', session, 'paired-followup', SimpleNamespace(complete=lambda *a, **k:reply(followup)))
    assert result['response_text'] == followup['question']
    assert 'reference_text' not in result and followup['reference_text'] not in str(result)
    assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
    assert view_reference(database, session)['reference_text'] == opening['reference_text']
    assert view_reference(database, session, result['result_id'])['reference_text'] == followup['reference_text']
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0


def test_legacy_completion_preserves_question_and_is_idempotent(database):
    from app.services.interview_answers import generate_reference, view_reference
    from app.services.interview_setup import prepare_interview
    session = prepare_interview(database, None, '工具调用为什么需要幂等？', '', 'legacy')['session_id']
    before = [tuple(r) for r in database.execute('SELECT * FROM question_version')]
    assert view_reference(database, session)['available'] is False
    assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
    calls = []
    def complete(*a, **k):
        calls.append(1)
        return reply({'reference_text':'补充答案：相同操作重试时应只产生一次业务效果。'})
    assert generate_reference(database, session, client=SimpleNamespace(complete=complete))['available']
    assert generate_reference(database, session, client=SimpleNamespace(complete=complete))['available']
    assert calls == [1]
    assert before == [tuple(r) for r in database.execute('SELECT * FROM question_version')]
    assert database.execute('SELECT COUNT(*) FROM attempt').fetchone()[0] == 0


def test_reference_endpoint_does_not_leak_on_page_or_accept_foreign_turn(monkeypatch):
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.interview import add_turn
    from app.services.interview_setup import prepare_interview
    from app.storage.database import connect_database
    headers = {'X-Requested-With':'learning-practice'}
    with TestClient(app) as client:
        db = connect_database(app.state.database_path)
        first = prepare_interview(db, None, 'First standalone question', '', 'first', 'HIDDEN FIRST REFERENCE')['session_id']
        other = prepare_interview(db, None, 'Other standalone question', '', 'other')['session_id']
        turn = add_turn(db, other, 'assistant', 'Other followup question', datetime.now(UTC))
        assert 'HIDDEN FIRST REFERENCE' not in client.get('/interview/'+first).text
        assert db.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
        response = client.post(f'/api/interviews/{first}/reference', json={'turn_id':turn}, headers=headers)
        assert response.status_code == 409
        assert db.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 0
        response = client.post(f'/api/interviews/{first}/reference', json={}, headers=headers)
        assert response.json()['reference_text'] == 'HIDDEN FIRST REFERENCE'
        db.close()


def test_restart_releases_orphan_quality_job(database):
    from app.services.free_batches import interrupt_batches
    database.execute("INSERT INTO model_job VALUES ('orphan','quality:orphan','question_quality','running',0,NULL,NULL,NULL,'2026-09-12','2026-09-12')")
    database.commit()
    interrupt_batches(database)
    assert database.execute("SELECT status FROM model_job WHERE id='orphan'").fetchone()[0] == 'failed'


def test_corrections_are_version_bound_and_preserved_in_backup(database, tmp_path):
    import sqlite3

    from app.services.exports import export_learning_data, verify_export
    from app.services.reference_corrections import get_correction, save_correction
    from app.storage.database import backup_database
    from tests.test_student_workflow import pool
    pool(database, 2, 'theory')
    versions = [r[0] for r in database.execute('SELECT id FROM question_version')]
    original = [tuple(r) for r in database.execute('SELECT * FROM question_version')]
    save_correction(database, versions[0], '已按原始资料核对的局部校正，不改写历史记录。', ['https://example.test/paper'])
    assert get_correction(database, versions[0])['sources'] == ['https://example.test/paper']
    assert get_correction(database, versions[1]) is None
    assert original == [tuple(r) for r in database.execute('SELECT * FROM question_version')]
    database.commit()
    backup_database(database, tmp_path/'restored.db')
    with sqlite3.connect(tmp_path/'restored.db') as restored:
        assert restored.execute('SELECT COUNT(*) FROM reference_correction').fetchone()[0] == 1
    exported = export_learning_data(database, tmp_path/'qa.zip', tmp_path/'media')
    assert verify_export(exported, tmp_path/'verify')['question_count'] == 2


def test_correction_supplied_to_generation_and_checker(database, monkeypatch):
    from app.services.learning_clock import local_today
    from app.services.practice_generation import generate_variants
    from app.services.reference_corrections import save_correction
    from app.services.tasks import create_daily_plan
    from tests.test_student_workflow import pool
    pool(database, 1, 'theory')
    version = database.execute('SELECT id FROM question_version').fetchone()[0]
    save_correction(database, version, '此处使用带来源的局部校正，不能照抄原来的错误。', ['https://example.test/paper'])
    plan = create_daily_plan(database, local_today(), 0, 0, {}, 'correction-plan')
    def generate(messages, **kwargs):
        q = json.loads(messages[1]['content'])['questions'][0]
        assert q['reference_correction']['version_id'] == version
        return reply({'questions':[{'base_question_id':q['id'], 'prompt':'如何应用校正后的方法？', 'reference_text':'对照已核对的条件逐项应用该方法。'}]})
    def review(messages, **kwargs):
        items = json.loads(messages[1]['content'])['items']
        assert items[0]['reference_correction']['sources'] == ['https://example.test/paper']
        return reply(quality_result(items))
    monkeypatch.setattr('app.services.question_quality.client_for_config', lambda _:SimpleNamespace(complete=review))
    assert generate_variants(database, plan['plan_id'], '', 1, 'theory', False, 'correction', client=SimpleNamespace(complete=generate))['added'] == 1


def test_v10_migration_preserves_existing_records_and_creates_backup(database):
    from pathlib import Path

    from app.storage.database import initialize_database
    from app.storage.migrations import CURRENT_VERSION
    database.execute('DROP TABLE reference_verification')
    database.execute('DROP TABLE reference_correction_history')
    database.execute('DROP TABLE reference_correction')
    database.execute('DELETE FROM schema_version WHERE version>=11')
    database.commit()
    initialize_database(database)
    assert database.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] == CURRENT_VERSION
    assert database.execute('SELECT COUNT(*) FROM reference_correction').fetchone()[0] == 0
    directory = Path(database.execute('PRAGMA database_list').fetchone()[2]).parent
    assert list(directory.glob('pre-migration-v10-*.db'))
