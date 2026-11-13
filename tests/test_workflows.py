import pytest
from sqlalchemy import select
from app.service import InputError, ConflictError
from app.database import WorkflowRequest, AuditEvent, RetryAttempt
from app.rules import evaluate_condition


def submit(service, db, data, key="key-1", workflow="loan_approval", mode="success"):
    with db.begin():
        return service.submit(db, workflow, data, key, mode)


def test_loan_happy_path_approved(service, db_session, loan_data):
    request, duplicate = submit(service, db_session, loan_data)
    assert request.status == "APPROVED" and not duplicate
    assert request.result["rule_summary"] == {"passed": 3, "failed": 0, "skipped": 0}


def test_invalid_input_rejected(service, db_session):
    with pytest.raises(InputError): submit(service, db_session, {"applicant_id": "A"})


def test_idempotency_returns_original_request(service, db_session, loan_data):
    first, _ = submit(service, db_session, loan_data)
    replay, duplicate = submit(service, db_session, dict(loan_data), key="key-1")
    assert replay.id == first.id and duplicate


def test_idempotency_conflicting_payload_rejected(service, db_session, loan_data):
    submit(service, db_session, loan_data, key="conflict-key")
    with pytest.raises(ConflictError):
        submit(service, db_session, {**loan_data, "credit_score": 500}, key="conflict-key")


def test_idempotency_conflicting_workflow_rejected(service, db_session, loan_data):
    submit(service, db_session, loan_data, key="conflict-workflow")
    claim = {"claim_id": "CLM-9", "policy_active": True, "claim_amount": 50000, "incident_days_ago": 5, "documents_complete": True}
    with pytest.raises(ConflictError):
        submit(service, db_session, claim, key="conflict-workflow", workflow="insurance_claim")


def test_dependency_failure_exhausts_retries(service, db_session, loan_data):
    request, _ = submit(service, db_session, loan_data, mode="timeout")
    attempts = db_session.scalars(select(RetryAttempt).where(RetryAttempt.request_id == request.id)).all()
    assert request.status == "FAILED" and len(attempts) == 3


def test_transient_failure_then_success(service, db_session, loan_data):
    request, _ = submit(service, db_session, loan_data, mode="transient_then_success")
    attempts = db_session.scalars(select(RetryAttempt).where(RetryAttempt.request_id == request.id)).all()
    assert request.status == "APPROVED" and [a.outcome for a in attempts] == ["TRANSIENT_FAILURE", "SUCCESS"]


def test_permanent_dependency_failure_not_retried(service, db_session, loan_data):
    request, _ = submit(service, db_session, loan_data, mode="permanent_failure")
    attempts = db_session.scalars(select(RetryAttempt).where(RetryAttempt.request_id == request.id)).all()
    assert request.status == "FAILED" and len(attempts) == 1


def test_business_rule_rejection(service, db_session, loan_data):
    request, _ = submit(service, db_session, {**loan_data, "credit_score": 500})
    assert request.status == "REJECTED"
    assert "Credit score" in request.result["explanation"]


def test_manual_review_for_high_loan_amount(service, db_session, loan_data):
    request, _ = submit(service, db_session, {**loan_data, "requested_amount": 1500000})
    assert request.status == "MANUAL_REVIEW"


def test_manual_review_can_be_approved(service, db_session, loan_data):
    request, _ = submit(service, db_session, {**loan_data, "requested_amount": 1500000}, key="review-approve")
    resolved = service.resolve_review(db_session, request.id, "APPROVED", "reviewer-1", "Income documents verified.")
    db_session.commit()
    assert resolved.status == "APPROVED"
    assert resolved.result["manual_review"]["reviewer"] == "reviewer-1"
    assert resolved.result["transitions"][-1] == {"from": "MANUAL_REVIEW", "to": "APPROVED"}
    events = db_session.scalars(select(AuditEvent).where(AuditEvent.request_id == request.id)).all()
    assert "REVIEW_DECIDED" in [event.event_type for event in events]


def test_manual_review_can_be_rejected(service, db_session, loan_data):
    request, _ = submit(service, db_session, {**loan_data, "requested_amount": 1500000}, key="review-reject")
    resolved = service.resolve_review(db_session, request.id, "REJECTED", "reviewer-2", "Supporting income proof missing.")
    db_session.commit()
    assert resolved.status == "REJECTED"
    assert "reviewer-2" in resolved.result["explanation"]


def test_review_requires_manual_review_state(service, db_session, loan_data):
    request, _ = submit(service, db_session, loan_data, key="review-invalid")
    with pytest.raises(ConflictError):
        service.resolve_review(db_session, request.id, "APPROVED", "reviewer-1")
    with pytest.raises(KeyError):
        service.resolve_review(db_session, "missing-request", "APPROVED", "reviewer-1")


def test_review_cannot_be_resolved_twice(service, db_session, loan_data):
    request, _ = submit(service, db_session, {**loan_data, "requested_amount": 1500000}, key="review-twice")
    service.resolve_review(db_session, request.id, "APPROVED", "reviewer-1")
    db_session.commit()
    with pytest.raises(ConflictError):
        service.resolve_review(db_session, request.id, "REJECTED", "reviewer-2")


def test_second_workflow_reuses_engine(service, db_session):
    data = {"claim_id": "CLM-1", "policy_active": True, "claim_amount": 50000, "incident_days_ago": 5, "documents_complete": True}
    request, _ = submit(service, db_session, data, workflow="insurance_claim")
    assert request.status == "APPROVED" and request.workflow_name == "insurance_claim"


def test_composite_condition_routes_to_manual_review(service, db_session):
    data = {"claim_id": "CLM-2", "policy_active": True, "claim_amount": 50000, "incident_days_ago": 1, "documents_complete": True}
    request, _ = submit(service, db_session, data, workflow="insurance_claim", key="claim-recent")
    assert request.status == "MANUAL_REVIEW"
    trace = {rule["rule_id"]: rule for rule in request.result["rules"]}
    assert trace["standard_timeline_and_value"]["status"] == "FAILED"
    assert trace["standard_timeline_and_value"]["match"] == "all"
    assert len(trace["standard_timeline_and_value"]["conditions"]) == 2


def test_composite_condition_can_pass(service, db_session):
    data = {"claim_id": "CLM-3", "policy_active": True, "claim_amount": 150000, "incident_days_ago": 5, "documents_complete": True}
    request, _ = submit(service, db_session, data, workflow="insurance_claim", key="claim-standard")
    assert request.status == "APPROVED"
    trace = {rule["rule_id"]: rule for rule in request.result["rules"]}
    assert trace["standard_timeline_and_value"]["status"] == "PASSED"


def test_rule_change_changes_decision_without_engine_change(service, db_session, loan_data, tmp_path):
    raw = service.configs.get("loan_approval").model_dump()
    raw["version"] = 2
    raw["stages"][0]["rules"][0]["value"] = 70000
    service.configs.directory = tmp_path
    service.configs.register(raw)
    request, _ = submit(service, db_session, loan_data, key="changed-rule")
    assert request.status == "REJECTED" and request.workflow_version == 2
    assert {entry["version"] for entry in service.configs.versions("loan_approval")} == {1, 2}
    assert service.configs.get_version("loan_approval", 1).version == 1


def test_configuration_version_must_increase(service):
    raw = service.configs.get("loan_approval").model_dump()
    with pytest.raises(ValueError):
        service.configs.register(raw)


def test_invalid_workflow_name_rejected(service):
    raw = service.configs.get("loan_approval").model_dump()
    raw["name"] = "../../evil"
    raw["version"] = 2
    with pytest.raises(Exception):
        service.configs.register(raw)


def test_invalid_membership_rule_rejected(service):
    raw = service.configs.get("loan_approval").model_dump()
    raw["version"] = 2
    raw["stages"][0]["rules"][0] = {"id": "bad_in", "field": "credit_score", "operator": "in", "value": 650, "reason": "bad config"}
    with pytest.raises(Exception):
        service.configs.register(raw)


def test_audit_records_decision_and_rule_trace(service, db_session, loan_data):
    request, _ = submit(service, db_session, loan_data)
    events = db_session.scalars(select(AuditEvent).where(AuditEvent.request_id == request.id)).all()
    kinds = [event.event_type for event in events]
    assert "REQUEST_RECEIVED" in kinds and "RULES_EVALUATED" in kinds and "DECISION_FINALIZED" in kinds


def test_rule_operators():
    assert evaluate_condition(10, "gte", 10)
    assert evaluate_condition("x", "in", ["x", "y"])
    assert evaluate_condition(None, "exists", None) is False
    assert evaluate_condition("not-a-number", "gt", 5) is False


def test_boolean_not_accepted_as_number(service, db_session, loan_data):
    with pytest.raises(InputError): submit(service, db_session, {**loan_data, "monthly_income": True}, key="bad-bool")


def test_partial_processing_failure_rolls_back_all_rows(service, db_session, loan_data, monkeypatch):
    original = service._audit
    calls = {"count": 0}
    def fail_during_audit(db, request_id, event_type, details):
        calls["count"] += 1
        if event_type == "RULES_EVALUATED": raise RuntimeError("simulated persistence-stage failure")
        return original(db, request_id, event_type, details)
    monkeypatch.setattr(service, "_audit", fail_during_audit)
    with pytest.raises(RuntimeError):
        with db_session.begin(): service.submit(db_session, "loan_approval", loan_data, "rollback-key")
    assert db_session.scalars(select(WorkflowRequest)).all() == []
    assert db_session.scalars(select(AuditEvent)).all() == []


def test_invalid_configuration_operator_rejected(service):
    raw = service.configs.get("loan_approval").model_dump()
    raw["stages"][0]["rules"][0]["operator"] = "execute_python"
    with pytest.raises(Exception): service.configs.register(raw)
