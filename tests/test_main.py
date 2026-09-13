from fastapi.testclient import TestClient

from app.main import app


def test_health_reports_foundation() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "phase": "P0-P6 foundation"}


def test_core_pages_render() -> None:
    with TestClient(app) as client:
        responses = [
            client.get("/"),
            client.get("/sources"),
            client.get("/review"),
            client.get("/history"),
        ]

    assert all(response.status_code == 200 for response in responses)
    assert "今天" in responses[0].text
    assert "题库与来源" in responses[1].text
    assert "复习库" in responses[2].text
    assert responses[3].url.path == '/review'
    assert 'href="/history"' not in responses[3].text
