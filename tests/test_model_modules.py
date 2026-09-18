import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.agnes import AgnesConfigurationError
from app.main import app
from app.services.interview import add_turn, end_session, start_session
from app.services.llm_config import (
    MODULES,
    client_for_config,
    get_module_config,
    normalize_model_settings,
    save_module_config,
)
from app.services.model_jobs import ModelJobError, run_evaluation_job
from app.services.model_json import ModelJSONError, parse_model_json
from app.services.module_jobs import run_module_job
from app.services.practice import adopt_theory_evaluation
from app.services.reflections import save_user_reflection
from tests.interview_fixtures import feedback_json
from tests.test_practice_review import make_plan, seed_question
from tests.test_repair_business import submitted_theory

NOW = datetime.now(UTC)
HEADERS = {"X-Requested-With": "learning-practice"}


def test_json_wrapper_compatibility_is_limited_to_one_complete_value():
    assert parse_model_json('\n```json\n{"verdict":"aligned"}\n```\n') == {"verdict":"aligned"}
    for invalid in ('Explanation: {"verdict":"aligned"}', '```json\n{"verdict":', '{} trailing'):
        with pytest.raises(ModelJSONError):
            parse_model_json(invalid)


def test_agnes_uses_configured_key_and_standard_chat_format(database, monkeypatch):
    monkeypatch.setenv("AGNES_API_KEY", "agnes-only")
    config = save_module_config(database, "interview_followup", "agnes-custom", "custom interview", 1024)
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return httpx.Response(200, json={"model": "agnes-custom", "choices": [{"message": {"content": "question?"}}]})

    monkeypatch.setattr(httpx, "post", post)
    reply = client_for_config(config).complete([{"role": "user", "content": "sample"}], 1024)
    assert reply.content == "question?"
    assert captured["url"] == "https://apihub.agnes-ai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer agnes-only"
    assert captured["json"]["model"] == "agnes-custom"
    assert captured["json"]["messages"] == [{"role": "user", "content": "sample"}]
    monkeypatch.setenv("AGNES_API_KEY", "")
    with pytest.raises(AgnesConfigurationError, match="AGNES_API_KEY"):
        client_for_config(config)


def test_legacy_model_settings_are_normalized_to_agnes(database):
    database.execute(
        "INSERT INTO llm_module_settings VALUES (?,?,?,?,?,?)",
        ('theory_evaluation', 'legacy-service', 'legacy-model', 'valid prompt', 1024, NOW.isoformat()),
    )
    database.execute(
        "INSERT INTO provider_cooldown VALUES (?,?)",
        ('legacy-service', NOW.isoformat()),
    )
    database.commit()

    normalize_model_settings(database)

    config = get_module_config(database, 'theory_evaluation')
    assert config['provider'] == 'agnes'
    assert config['model'] == 'agnes-2.5-flash'
    assert config['prompt'] == 'valid prompt'
    assert database.execute("SELECT COUNT(*) FROM provider_cooldown").fetchone()[0] == 0


def test_module_settings_page_persists_independent_prompts():
    with TestClient(app) as client:
        before = client.get('/api/model-settings').json()
        assert len({item['prompt'] for item in before['modules']}) == len(MODULES)
        body = {"model": "agnes-custom", "prompt": "custom <script> prompt", "max_tokens": 3072}
        assert client.post('/api/model-settings/theory_evaluation', headers=HEADERS, json=body).status_code == 200
        after = client.get('/api/model-settings').json()
        assert after['modules'][0]['prompt'] == body['prompt']
        assert after['modules'][1] == before['modules'][1]
        html = client.get('/settings').text
        assert 'custom &lt;script&gt; prompt' in html
        assert 'custom <script> prompt' not in html
        assert after['credential_configured'] is False


def test_interview_dialogue_requires_explicit_view_and_answer_retry_is_idempotent(monkeypatch):
    monkeypatch.setattr('app.services.current_practice.local_today', lambda: date(2026, 9, 11))
    from app.storage.database import connect_database
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        try:
            database.execute("DELETE FROM source WHERE id='source-theory'")
            seed_question(database, 'theory')
            task = dict(make_plan(database, 'theory'))
        finally:
            database.close()
        session = client.post('/api/interviews', headers=HEADERS, json={'task_id':task['id'],'created_at':NOW.isoformat()}).json()['session_id']
        body = {'role':'user','content':'private saved dialogue','created_at':NOW.isoformat()}
        headers = {**HEADERS,'Idempotency-Key':'dialogue-answer'}
        first = client.post(f'/api/interviews/{session}/turns',headers=headers,json=body)
        assert client.post(f'/api/interviews/{session}/turns',headers=headers,json=body).json() == first.json()
        page = client.get(f'/interview/{session}')
        assert page.status_code == 200 and 'private saved dialogue' not in page.text
        response = client.post(f'/api/interviews/{session}/dialogue',headers=HEADERS)
        turns = response.json()['turns']
        assert len(turns) == 1 and turns[0]['role'] == 'user' and turns[0]['content'] == 'private saved dialogue'
        assert turns[0]['id'] == first.json()['turn_id'] and not turns[0]['is_feedback']
        database = connect_database(app.state.database_path)
        try:
            assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 1
            assert database.execute('SELECT COUNT(*) FROM interview_turn').fetchone()[0] == 1
        finally:
            database.close()


def test_evaluation_uses_frozen_custom_prompt_and_protects_existing_manual_result(database):
    result = submitted_theory(database)
    save_module_config(database, 'theory_evaluation', 'agnes-2.5-flash', 'custom evaluator', 2048)
    adopt_theory_evaluation(database, result['attempt_id'], 'needs_review', NOW, True)
    observed = []

    def complete(messages, **kwargs):
        observed.append(messages)
        return SimpleNamespace(content=json.dumps({'verdict':'aligned','covered_points':[], 'missing_points':[],
                                                  'errors':[], 'brief_feedback':'ok','evidence_refs':[]}),model='fake')

    run_evaluation_job(database, result['job_id'], SimpleNamespace(complete=complete))
    assert 'custom evaluator' in observed[0][0]['content']
    assert database.execute('SELECT verdict FROM evaluation WHERE adopted=1').fetchone()[0] == 'needs_review'
    saved = database.execute('SELECT config_json,prompt_hash FROM model_request').fetchone()
    assert json.loads(saved[0])['prompt'] == 'custom evaluator'
    assert database.execute('SELECT prompt_version FROM evaluation ORDER BY rowid DESC').fetchone()[0] == saved[1]


def seed_interview(database):
    seed_question(database, 'theory')
    task = make_plan(database, 'theory')
    session = start_session(database, task['id'], NOW)
    add_turn(database, session, 'user', 'my answer', NOW, 'first-turn')
    return session


def test_followup_and_feedback_use_distinct_prompts_and_do_not_add_tasks(database):
    session = seed_interview(database)
    seen = []

    def complete(messages, **kwargs):
        seen.append(messages)
        return SimpleNamespace(content=json.dumps({'question':'one followup question', 'reference_text':'complete matching reference answer'}) if len(seen) == 1 else feedback_json(messages), model='fake')

    fake = SimpleNamespace(complete=complete)
    first = run_module_job(database, 'interview_followup', session, 'followup', fake)
    assert first['response_model'] == 'fake'
    assert run_module_job(database, 'interview_followup', session, 'followup', fake) == first
    assert len(seen) == 1
    add_turn(database, session, 'user', 'followup answer', NOW, 'second-turn')
    end_session(database, session, NOW)
    run_module_job(database, 'interview_feedback', session, 'feedback', fake)
    assert seen[0][0]['content'] != seen[1][0]['content']
    assert MODULES['interview_followup']['prompt'] in seen[0][0]['content']
    assert database.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1
    assert database.execute('SELECT COUNT(*) FROM question_exposure').fetchone()[0] == 1
    assert database.execute('SELECT COUNT(*) FROM valid_review_pass').fetchone()[0] == 0
    assert database.execute('SELECT COUNT(*) FROM interview_turn').fetchone()[0] == 4


def test_reflection_validates_evidence_and_preserves_personal_version(database):
    result = submitted_theory(database)
    day = database.execute('SELECT activity_date FROM attempt').fetchone()[0]
    from datetime import date
    save_user_reflection(database, date.fromisoformat(day), 'personal content', NOW)
    database.commit()
    seen = []

    def complete(messages, **kwargs):
        seen.append(messages)
        return SimpleNamespace(content=json.dumps({'content':'model reflection','covered_ids':[result['attempt_id']]}),model='fake')

    run_module_job(database, 'daily_reflection', day, 'reflection', SimpleNamespace(complete=complete))
    assert MODULES['daily_reflection']['prompt'] in seen[0][0]['content']
    assert database.execute("SELECT content FROM reflection WHERE author='user'").fetchone()[0] == 'personal content'
    assert database.execute("SELECT content FROM reflection WHERE author='model'").fetchone()[0] == 'model reflection'
    bad = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(content=json.dumps({'content':'invalid','covered_ids':['invented']}),model='fake'))
    with pytest.raises(ModelJobError, match='不存在'):
        run_module_job(database, 'daily_reflection', day, 'bad-reflection', bad)
    assert database.execute("SELECT COUNT(*) FROM reflection WHERE author='model'").fetchone()[0] == 1


def test_late_followup_does_not_append_to_changed_dialogue(database):
    session = seed_interview(database)
    def complete(*args, **kwargs):
        add_turn(database, session, 'user', 'new information', NOW)
        return SimpleNamespace(content=json.dumps({'question':'stale followup', 'reference_text':'matching reference answer'}),model='fake')
    with pytest.raises(ModelJobError, match='对话已更新'):
        run_module_job(database, 'interview_followup', session, 'late', SimpleNamespace(complete=complete))
    assert database.execute("SELECT COUNT(*) FROM interview_turn WHERE role='assistant'").fetchone()[0] == 0


def test_model_failure_keeps_prompt_snapshot_and_retry_uses_same_configuration(database):
    session = seed_interview(database)
    save_module_config(database, 'interview_followup', 'agnes-2.5-flash', 'first prompt', 1024)
    def fail(*args, **kwargs):
        raise RuntimeError('simulated transport failure')
    with pytest.raises(ModelJobError):
        run_module_job(database, 'interview_followup', session, 'retry', SimpleNamespace(complete=fail))
    save_module_config(database, 'interview_followup', 'agnes-next', 'changed prompt', 2048)
    def complete(messages, **kwargs):
        assert 'first prompt' in messages[0]['content']
        assert 'changed prompt' not in messages[0]['content']
        return SimpleNamespace(content=json.dumps({'question':'followup', 'reference_text':'matching reference answer'}),model='fake')
    run_module_job(database, 'interview_followup', session, 'retry', SimpleNamespace(complete=complete))
    assert database.execute("SELECT COUNT(*) FROM interview_turn WHERE role='user'").fetchone()[0] == 1
    assert get_module_config(database, 'interview_followup')['model'] == 'agnes-next'
