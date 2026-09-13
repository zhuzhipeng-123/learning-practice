from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from app.main import app
from app.services.bootstrap import register_initial_sources
from app.services.sync import publish_snapshot
from app.storage.database import connect_database
from tests.test_sync import blocks, draft


def test_api_plan_and_code_submission_flow(monkeypatch) -> None:
    monkeypatch.setattr('app.routes.api.local_today', lambda: date(2026, 9, 11))
    monkeypatch.setattr('app.services.current_practice.local_today', lambda: date(2026, 9, 11))
    with TestClient(app) as client:
        database = connect_database(app.state.database_path)
        try:
            register_initial_sources(database)
            publish_snapshot(database, "source-code", "1", "1", blocks(), [draft()], "v1")
        finally:
            database.close()
        plan_response = client.post(
            "/api/plans",
            headers={
                "Idempotency-Key": "api-plan",
                "X-Requested-With": "learning-practice",
            },
            json={
                "plan_date": "2026-09-11",
                "code_target": 1,
                "theory_target": 0,
                "module_quotas": {},
            },
        )
        assert plan_response.status_code == 200
        task_id = plan_response.json()["tasks"][0]["id"]
        now = datetime(2026, 9, 11, 8, tzinfo=UTC).isoformat()
        start_response = client.post(
            f"/api/tasks/{task_id}/attempts",
            headers={"X-Requested-With": "learning-practice"},
            json={"entry_mode": "daily", "started_at": now},
        )
        submit_response = client.post(
            f"/api/tasks/{task_id}/code-submit",
            headers={
                "Idempotency-Key": "api-submit",
                "X-Requested-With": "learning-practice",
            },
            json={
                "submitted_at": now,
                "entry_mode": "daily",
                "code_self_result": "cannot_solve",
                "note": "需要复习",
            },
        )

    assert start_response.status_code == 200
    assert submit_response.status_code == 200
    assert submit_response.json()["task_completed"] is True
    assert submit_response.json()["review_round_id"] is not None


def test_cross_origin_write_is_rejected() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/sources/bootstrap",
            headers={"Origin": "https://attacker.example"},
        )

    assert response.status_code == 403
