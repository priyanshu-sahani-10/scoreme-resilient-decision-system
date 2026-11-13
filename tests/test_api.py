import copy
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.database import Base
from app.main import app, get_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        db = Session()
        try: yield db
        finally: db.close()

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(main_module, "init_db", lambda: None)
    original_directory = main_module.config_manager.directory
    monkeypatch.setattr(main_module.config_manager, "directory", tmp_path)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    main_module.config_manager.directory = original_directory
    main_module.config_manager.reload()
    Base.metadata.drop_all(engine)
    engine.dispose()


LOAN = {"applicant_id": "A1", "monthly_income": 60000, "credit_score": 720, "requested_amount": 400000}


def submit(client, data=None, key="api-key-1", workflow="loan_approval", mode="success"):
    return client.post("/requests", json={"workflow_name": workflow, "data": data or LOAN, "simulation_mode": mode}, headers={"Idempotency-Key": key})


def test_api_submission_and_explanation(client):
    response = submit(client)
    assert response.status_code == 201
    request_id = response.json()["request_id"]
    assert response.json()["status"] == "APPROVED"
    assert client.get(f"/requests/{request_id}").json()["status"] == "APPROVED"
    explanation = client.get(f"/requests/{request_id}/explanation").json()
    assert explanation["decision"] == "APPROVED" and explanation["rule_trace"]
    audit = client.get(f"/requests/{request_id}/audit").json()
    assert len(audit["events"]) >= 4
    replay = submit(client)
    assert replay.status_code == 201 and replay.json()["idempotent_replay"] is True


def test_api_requires_idempotency_key(client):
    response = client.post("/requests", json={"workflow_name": "loan_approval", "data": LOAN})
    assert response.status_code == 422


def test_api_unknown_workflow_returns_404(client):
    assert submit(client, workflow="missing_workflow").status_code == 404


def test_api_invalid_payload_returns_422(client):
    assert submit(client, data={"applicant_id": "A1"}).status_code == 422


def test_api_invalid_simulation_mode_returns_422(client):
    assert submit(client, mode="explode").status_code == 422


def test_api_idempotency_conflict_returns_409(client):
    assert submit(client, key="api-conflict").status_code == 201
    assert submit(client, data={**LOAN, "credit_score": 500}, key="api-conflict").status_code == 409


def test_api_manual_review_resolution(client):
    response = submit(client, data={**LOAN, "requested_amount": 1500000}, key="api-review")
    assert response.status_code == 201
    request_id = response.json()["request_id"]
    assert response.json()["status"] == "MANUAL_REVIEW"

    resolved = client.post(f"/requests/{request_id}/review", json={"decision": "APPROVED", "reviewer": "ops-1", "notes": "Documents verified."})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "APPROVED"
    assert resolved.json()["result"]["manual_review"]["reviewer"] == "ops-1"

    audit = client.get(f"/requests/{request_id}/audit").json()
    assert "REVIEW_DECIDED" in [event["event_type"] for event in audit["events"]]
    assert client.post(f"/requests/{request_id}/review", json={"decision": "REJECTED", "reviewer": "ops-2"}).status_code == 409
    assert client.post("/requests/missing/review", json={"decision": "APPROVED", "reviewer": "ops-1"}).status_code == 404


def test_api_review_requires_valid_decision(client):
    response = submit(client, data={**LOAN, "requested_amount": 1500000}, key="api-review-invalid")
    request_id = response.json()["request_id"]
    assert client.post(f"/requests/{request_id}/review", json={"decision": "MAYBE", "reviewer": "ops-1"}).status_code == 422
    assert client.post(f"/requests/{request_id}/review", json={"decision": "APPROVED", "reviewer": ""}).status_code == 422


def test_api_workflow_config_registration_and_versions(client):
    config = {
        "name": "api_test_workflow",
        "version": 1,
        "required_fields": {"score": "number"},
        "stages": [{"name": "main", "rules": [{"id": "score_min", "field": "score", "operator": "gte", "value": 10, "reason": "Score is too low."}]}],
    }
    assert client.put("/workflows/config", json=config).status_code == 200
    assert client.put("/workflows/config", json=config).status_code == 422

    version_two = copy.deepcopy(config)
    version_two["version"] = 2
    version_two["stages"][0]["rules"][0]["value"] = 20
    assert client.put("/workflows/config", json=version_two).status_code == 200

    versions = client.get("/workflows/api_test_workflow/versions").json()["versions"]
    assert [entry["version"] for entry in versions] == [1, 2]
    assert client.get("/workflows/api_test_workflow/versions/1").json()["stages"][0]["rules"][0]["value"] == 10
    assert client.get("/workflows/api_test_workflow/versions/99").status_code == 404

    response = submit(client, data={"score": 15}, key="api-config", workflow="api_test_workflow")
    assert response.status_code == 201
    assert response.json()["status"] == "REJECTED" and response.json()["workflow_version"] == 2

    invalid = copy.deepcopy(version_two)
    invalid["version"] = 3
    invalid["stages"][0]["rules"][0]["operator"] = "execute_python"
    assert client.put("/workflows/config", json=invalid).status_code == 422


def test_api_audit_unknown_request_returns_404(client):
    assert client.get("/requests/missing/audit").status_code == 404
