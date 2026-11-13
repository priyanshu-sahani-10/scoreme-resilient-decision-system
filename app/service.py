from __future__ import annotations
import time, uuid
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import WorkflowRequest, AuditEvent, RetryAttempt, utcnow
from .dependencies import ExternalDependencySimulator, DependencyError
from .rules import evaluate_rules

class InputError(ValueError): pass

class ConflictError(ValueError): pass

class WorkflowService:
    def __init__(self, config_manager, dependency=None, sleeper=time.sleep):
        self.configs = config_manager
        self.dependency = dependency or ExternalDependencySimulator()
        self.sleeper = sleeper

    @staticmethod
    def _validate(config, payload: dict) -> None:
        errors = []
        type_map = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "object": dict, "array": list}
        for field, expected in config.required_fields.items():
            if field not in payload: errors.append({"field": field, "error": "required"}); continue
            py_type = type_map.get(expected)
            if py_type is None: errors.append({"field": field, "error": f"unsupported configured type: {expected}"})
            elif not isinstance(payload[field], py_type) or (expected == "number" and isinstance(payload[field], bool)):
                errors.append({"field": field, "error": f"expected {expected}"})
        if errors: raise InputError(str(errors))

    @staticmethod
    def _audit(db: Session, request_id: str, event_type: str, details: dict):
        db.add(AuditEvent(request_id=request_id, event_type=event_type, details=details))

    def submit(self, db: Session, workflow_name: str, payload: dict, idempotency_key: str, simulation_mode: str = "success") -> tuple[WorkflowRequest, bool]:
        if not idempotency_key or len(idempotency_key) > 200: raise InputError("A non-empty idempotency key of at most 200 characters is required.")
        existing = db.scalar(select(WorkflowRequest).where(WorkflowRequest.idempotency_key == idempotency_key))
        if existing:
            stored_mode = (existing.result or {}).get("simulation_mode")
            if existing.workflow_name != workflow_name or existing.input_data != payload or (stored_mode is not None and stored_mode != simulation_mode):
                raise ConflictError("This idempotency key was already used for a different workflow, payload, or simulation mode.")
            return existing, True
        config = self.configs.get(workflow_name)
        self._validate(config, payload)
        request_id = str(uuid.uuid4())
        request = WorkflowRequest(id=request_id, idempotency_key=idempotency_key, workflow_name=config.name, workflow_version=config.version, status="RECEIVED", input_data=payload, result={})
        db.add(request)
        db.flush()  # unique key is reserved before external work
        self._audit(db, request_id, "REQUEST_RECEIVED", {"workflow": config.name, "version": config.version, "input": payload})
        request.status = "VALIDATED"
        self._audit(db, request_id, "INPUT_VALIDATED", {"required_fields": list(config.required_fields)})
        rules = evaluate_rules(config, payload)
        self._audit(db, request_id, "RULES_EVALUATED", rules)
        transitions = [{"from": "RECEIVED", "to": "VALIDATED"}, {"from": "VALIDATED", "to": "RULES_EVALUATED"}]
        final = config.decision.get("default", "APPROVED")
        dependency_result = None
        if rules["outcome"] == "REJECT":
            final = config.decision.get("rejection", "REJECTED")
        elif rules["outcome"] == "MANUAL_REVIEW":
            final = config.decision.get("manual_review", "MANUAL_REVIEW")
        elif config.external_dependency.get("required", False):
            dep_name = config.external_dependency.get("name", "external_service")
            retry = config.retry_policy
            max_attempts = max(1, int(retry.get("max_attempts", 1)))
            base_delay = max(0.0, float(retry.get("base_delay_seconds", 0.0)))
            for attempt in range(1, max_attempts + 1):
                try:
                    dependency_result = self.dependency.verify(dep_name, simulation_mode, attempt)
                    db.add(RetryAttempt(request_id=request_id, attempt_number=attempt, outcome="SUCCESS"))
                    self._audit(db, request_id, "DEPENDENCY_SUCCEEDED", {"attempt": attempt, "result": dependency_result})
                    break
                except DependencyError as exc:
                    db.add(RetryAttempt(request_id=request_id, attempt_number=attempt, outcome="TRANSIENT_FAILURE" if exc.transient else "PERMANENT_FAILURE", error=str(exc)))
                    self._audit(db, request_id, "DEPENDENCY_FAILED", {"attempt": attempt, "transient": exc.transient, "error": str(exc)})
                    if not exc.transient or attempt == max_attempts:
                        final = config.decision.get("dependency_failure", "FAILED")
                        break
                    delay = base_delay * (2 ** (attempt - 1))
                    self._audit(db, request_id, "RETRY_SCHEDULED", {"attempt": attempt + 1, "delay_seconds": delay})
                    if delay: self.sleeper(delay)
        transitions.append({"from": "RULES_EVALUATED", "to": final})
        for transition in transitions:
            self._audit(db, request_id, "STATE_TRANSITION", transition)
        request.status = final
        request.result = {"decision": final, "explanation": self._explain(final, rules["reasons"], dependency_result), "rules": rules["trace"], "rule_summary": {"passed": sum(r["status"] == "PASSED" for r in rules["trace"]), "failed": sum(r["status"] == "FAILED" for r in rules["trace"]), "skipped": sum(r["status"] == "SKIPPED" for r in rules["trace"])}, "dependency": dependency_result, "transitions": transitions, "workflow": config.name, "workflow_version": config.version, "simulation_mode": simulation_mode}
        request.updated_at = utcnow()
        self._audit(db, request_id, "DECISION_FINALIZED", request.result)
        db.flush()
        return request, False

    def resolve_review(self, db: Session, request_id: str, decision: str, reviewer: str, notes: str = "") -> WorkflowRequest:
        if decision not in {"APPROVED", "REJECTED"}:
            raise InputError("Manual review decision must be APPROVED or REJECTED.")
        if not reviewer:
            raise InputError("A reviewer identity is required.")
        request = db.get(WorkflowRequest, request_id, with_for_update=True)
        if not request:
            raise KeyError("Request not found")
        if request.status != "MANUAL_REVIEW":
            raise ConflictError(f"Request {request.id} is not awaiting manual review (current status: {request.status}).")
        result = dict(request.result or {})
        result["decision"] = decision
        explanation = f"Manual review by {reviewer} resulted in {decision}."
        if notes:
            explanation += f" Notes: {notes}"
        result["explanation"] = explanation
        result["manual_review"] = {"decision": decision, "reviewer": reviewer, "notes": notes, "resolved_at": utcnow().isoformat()}
        transitions = list(result.get("transitions", []))
        transitions.append({"from": "MANUAL_REVIEW", "to": decision})
        result["transitions"] = transitions
        request.result = result
        request.status = decision
        request.updated_at = utcnow()
        self._audit(db, request.id, "REVIEW_DECIDED", {"decision": decision, "reviewer": reviewer, "notes": notes})
        self._audit(db, request.id, "STATE_TRANSITION", transitions[-1])
        db.flush()
        return request

    @staticmethod
    def _explain(decision: str, reasons: list[str], dependency: dict | None) -> str:
        if decision == "APPROVED":
            if dependency is not None: return "All configured business rules passed and the external verification dependency succeeded."
            return "All configured business rules passed. No external verification was required."
        if decision == "REJECTED": return "The request was rejected because: " + " ".join(reasons)
        if decision == "MANUAL_REVIEW": return "The request requires human review because: " + " ".join(reasons)
        return "The request could not be completed because an external dependency failed after the configured retry policy."
