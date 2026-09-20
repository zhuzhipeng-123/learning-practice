import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(('path', 'title'), [
    ('/review', '复习库 · 学习练习'),
    ('/interview', '秋招练习 · 学习练习'),
    ('/settings', '设置与备份 · 学习练习'),
])
def test_primary_page_titles_include_product_name(path, title):
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert f'<title>{title}</title>' in response.text


def test_install_verifier_runs_directly_outside_project_directory(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / 'scripts' / 'verify_install.py')],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result['all_routes_ok'] is True
    assert result['all_resources_present'] is True


@pytest.mark.parametrize('path,status', [('/missing-page', 404), ('/practice/missing', 404), ('/days/not-a-date', 422)])
def test_page_errors_offer_html_recovery(path, status):
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path, headers={'Accept': 'text/html'})
    assert response.status_code == status
    assert response.headers['content-type'].startswith('text/html')
    assert '返回今天' in response.text
    assert '<html lang="zh-CN">' in response.text


@pytest.mark.parametrize('error', [RuntimeError('private stack details'), sqlite3.OperationalError('database is locked')])
def test_unexpected_page_error_preserves_navigation_without_leaking_internals(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr('app.routes.pages.latest_reflections', fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get('/')
    assert response.status_code == (503 if isinstance(error, sqlite3.OperationalError) else 500)
    assert response.headers['content-type'].startswith('text/html')
    assert '返回今天' in response.text
    assert str(error) not in response.text


def test_api_missing_resource_stays_json():
    with TestClient(app) as client:
        response = client.get('/api/missing-endpoint')
    assert response.status_code == 404
    assert response.headers['content-type'].startswith('application/json')


@pytest.mark.parametrize('change', ['edit', 'add', 'remove'])
def test_runtime_changes_block_operations_before_mutation(tmp_path, change):
    from fastapi import FastAPI

    from app.services.runtime_state import RuntimeVersionGuard
    source = tmp_path / 'page.html'
    source.write_text('original', encoding='utf-8')
    service = FastAPI()
    calls = []
    @service.post('/api/save')
    def save():
        calls.append('saved')
        return {'saved': True}
    service.add_middleware(RuntimeVersionGuard, source_root=tmp_path)
    with TestClient(service) as client:
        assert client.post('/api/save').status_code == 200
        if change == 'edit':
            source.write_text('new template requiring new data', encoding='utf-8')
        elif change == 'add':
            (tmp_path / 'new.js').write_text('new API usage', encoding='utf-8')
        else:
            source.unlink()
        response = client.post('/api/save')
        assert response.status_code == 503
        assert response.json()['request_state'] == 'failed'
        assert response.json()['restart_required'] is True
        page = client.get('/')
        assert page.status_code == 503
        assert '服务需要重新启动' in page.text
        assert page.headers['content-type'].startswith('text/html')
    assert calls == ['saved']


@pytest.mark.parametrize('url', ['https://unrelated.test/?next=team.feishu.cn/wiki/Token',
    'https://team.feishu.cn/wiki/Token/extra', 'https://user:password@team.feishu.cn/docx/Token'])
def test_source_registration_rejects_misleading_links_before_external_read(database, url):
    from types import SimpleNamespace

    from app.services.sources import SourceRegistrationError, inspect_and_register_source
    calls = []
    def inspect(args):
        calls.append(args)
        return SimpleNamespace(data={'type': 'docx', 'token': 'synthetic-document'})
    with pytest.raises(SourceRegistrationError):
        inspect_and_register_source(database, url, 'theory', client=SimpleNamespace(run_read=inspect))
    assert not calls


def test_registered_source_cannot_silently_keep_a_different_question_type(database):
    from app.services.sources import SourceRegistrationError, register_source
    url = 'https://team.feishu.cn/docx/Token?from=copy#reference'
    first = register_source(database, url, 'document', 'code')
    assert register_source(database, url, 'document', 'code') == first
    with pytest.raises(SourceRegistrationError, match='另一题型'):
        register_source(database, url, 'document', 'theory')


def test_today_opens_with_footprint_and_ends_with_reflection():
    with TestClient(app) as client:
        html = client.get('/').text
    assert html.index('class="page-heading"') < html.index('heatmap-panel')
    assert html.index('heatmap-panel') < html.index('id="daily-practice"')
    assert html.index('id="daily-practice"') < html.index('id="daily-plan"')
    assert html.index('id="daily-plan"') < html.index('id="daily-reflection"')
    assert '<details class="panel progressive-panel" open><summary><span>今天的复盘</span>' in html
    assert '<details class="panel progressive-panel" open><summary><span>学习足迹</span>' in html
    assert 'id="personal-reflection"' in html


def test_practice_keeps_frozen_correction_entry_without_daily_history_or_classic_actions(monkeypatch):
    from app.services.current_practice import draw_daily_batch
    from app.services.learning_clock import local_today
    from app.storage.database import connect_database
    from tests.test_student_workflow import pool

    monkeypatch.setattr('app.services.bootstrap.load_initial_sources', list)
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        pool(database, 1, 'theory')
        database.commit()
        batch = draw_daily_batch(database, local_today(), 0, 1, {}, 'b7-page', '')
        task_id = batch['task_ids'][0]
        task = database.execute('SELECT question_id,question_version_id FROM task WHERE id=?', (task_id,)).fetchone()
        database.execute('UPDATE question_version SET prompt=? WHERE id=?', ('很长的题目标题' * 80, task['question_version_id']))
        database.execute('UPDATE version_resources SET materials_json=? WHERE version_id=?', (json.dumps([
            {'block_id': 'prompt-image-a', 'kind': 'media', 'role': 'prompt', 'status': 'complete'},
            {'block_id': 'prompt-image-b', 'kind': 'media', 'role': 'prompt', 'status': 'complete'},
        ]), task['question_version_id']))
        database.commit()
        html = client.get(f'/practice/{task_id}').text
        assert '这道题的全部记录' not in html
        assert '标为经典面试题' not in html
        assert 'toggle-classic' not in html
        assert '修正本次冻结版本的参考' in html
        assert f'/questions/{task["question_id"]}/history?version={task["question_version_id"]}#knowledge-editor' in html
        assert f'data-question-version="{task["question_version_id"]}"' in html
        prompt_materials = client.get(
            f'/api/questions/{task["question_id"]}/versions/{task["question_version_id"]}/prompt-materials',
        ).json()['materials']
        assert [item['block_id'] for item in prompt_materials] == ['prompt-image-a', 'prompt-image-b']
        classic = client.post(
            f'/api/questions/{task["question_id"]}/classic',
            json={'is_classic': True},
            headers={'X-Requested-With': 'learning-practice'},
        )
        assert classic.status_code == 200 and classic.json() == {'is_classic': True}
        assert client.get(f'/questions/{task["question_id"]}/history').status_code == 200
        database.close()


def test_narrow_layout_and_rich_content_have_local_overflow_guards():
    css = Path('app/static/app.css').read_text(encoding='utf-8')
    model_text = Path('app/static/model_text.js').read_text(encoding='utf-8')
    knowledge_editor = Path('app/static/knowledge_editor.js').read_text(encoding='utf-8')
    assert '@media(max-width:390px)' in css
    assert '.prose table{display:block;max-width:100%;overflow-x:auto' in css
    assert '.prompt-media{display:grid' in css
    assert 'button:focus-visible' in css and 'summary:focus-visible' in css
    assert "wrapper.style.overflowX = 'auto'" in model_text
    assert "new URLSearchParams(location.search).get('version')" in knowledge_editor


def test_today_plan_refresh_does_not_replace_reflection_region():
    script = Path('app/static/today.js').read_text(encoding='utf-8')
    assert "for (const id of ['daily-practice','plan-progress'])" in script
    assert "const preserveSelection = !body && selectionState() !== originalSelection" in script
    assert "window.addEventListener('pageshow'" in script
    assert "document.addEventListener('visibilitychange'" in script
    assert "'daily-reflection'" not in script
