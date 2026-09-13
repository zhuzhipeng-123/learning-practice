import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app


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
