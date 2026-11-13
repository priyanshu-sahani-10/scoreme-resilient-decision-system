from __future__ import annotations
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .database import SessionLocal, init_db, WorkflowRequest, AuditEvent, RetryAttempt
from .configuration import ConfigurationManager
from .service import WorkflowService, InputError, ConflictError
from .schemas import SubmitRequest, ReviewDecision

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", str(BASE_DIR / "configs")))
config_manager = ConfigurationManager(CONFIG_DIR)
service = WorkflowService(config_manager)
@asynccontextmanager
async def lifespan(application):
    init_db()
    yield

app = FastAPI(title="Resilient Decision System", version="1.1.0", description="Configuration-driven workflow decision platform with auditability, idempotency, manual review resolution and retry handling.", lifespan=lifespan)

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

def request_view(r: WorkflowRequest):
    return {"request_id": r.id, "idempotency_key": r.idempotency_key, "workflow": r.workflow_name, "workflow_version": r.workflow_version, "status": r.status, "input": r.input_data, "result": r.result, "created_at": r.created_at, "updated_at": r.updated_at}

@app.get("/health")
def health(): return {"status": "ok"}

@app.get("/workflows")
def list_workflows(): return {"workflows": config_manager.list()}

@app.get("/workflows/{name}/config")
def get_workflow_config(name: str):
    try: return config_manager.get(name).model_dump()
    except KeyError as exc: raise HTTPException(404, str(exc))

@app.get("/workflows/{name}/versions")
def list_workflow_versions(name: str):
    try: return {"versions": config_manager.versions(name)}
    except KeyError as exc: raise HTTPException(404, str(exc))

@app.get("/workflows/{name}/versions/{version}")
def get_workflow_version(name: str, version: int):
    try: return config_manager.get_version(name, version).model_dump()
    except KeyError as exc: raise HTTPException(404, str(exc))

@app.put("/workflows/config")
def register_workflow(raw: dict):
    try:
        config = config_manager.register(raw)
        return {"message": "Workflow configuration registered", "name": config.name, "version": config.version}
    except ValueError as exc: raise HTTPException(422, detail=str(exc))

@app.post("/requests", status_code=status.HTTP_201_CREATED)
def submit_request(body: SubmitRequest, idempotency_key: str = Header(..., alias="Idempotency-Key"), db: Session = Depends(get_db)):
    try:
        with db.begin():
            request, duplicate = service.submit(db, body.workflow_name, body.data, idempotency_key, body.simulation_mode)
        response = request_view(request)
        response["idempotent_replay"] = duplicate
        return response
    except KeyError as exc: raise HTTPException(404, str(exc))
    except InputError as exc: raise HTTPException(422, str(exc))
    except ConflictError as exc: raise HTTPException(409, str(exc))
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(WorkflowRequest).where(WorkflowRequest.idempotency_key == idempotency_key))
        if existing:
            stored_mode = (existing.result or {}).get("simulation_mode")
            if existing.workflow_name != body.workflow_name or existing.input_data != body.data or (stored_mode is not None and stored_mode != body.simulation_mode):
                raise HTTPException(409, "This idempotency key was already used for a different workflow, payload, or simulation mode.")
            response = request_view(existing); response["idempotent_replay"] = True
            return response
        raise HTTPException(409, "Concurrent request conflict; retry with the same idempotency key")
    except HTTPException: raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, detail=f"Processing failed safely: {type(exc).__name__}")

@app.post("/requests/{request_id}/review")
def resolve_review(request_id: str, body: ReviewDecision, db: Session = Depends(get_db)):
    try:
        request = service.resolve_review(db, request_id, body.decision, body.reviewer, body.notes)
        db.commit()
        return request_view(request)
    except KeyError as exc: db.rollback(); raise HTTPException(404, str(exc))
    except ConflictError as exc: db.rollback(); raise HTTPException(409, str(exc))
    except InputError as exc: db.rollback(); raise HTTPException(422, str(exc))
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, detail=f"Review resolution failed safely: {type(exc).__name__}")

@app.get("/requests/{request_id}")
def get_request(request_id: str, db: Session = Depends(get_db)):
    request = db.get(WorkflowRequest, request_id)
    if not request: raise HTTPException(404, "Request not found")
    return request_view(request)

@app.get("/requests/{request_id}/explanation")
def get_explanation(request_id: str, db: Session = Depends(get_db)):
    request = db.get(WorkflowRequest, request_id)
    if not request: raise HTTPException(404, "Request not found")
    return {"request_id": request.id, "decision": request.result.get("decision"), "explanation": request.result.get("explanation"), "rule_trace": request.result.get("rules", []), "dependency": request.result.get("dependency"), "manual_review": request.result.get("manual_review"), "workflow": request.workflow_name, "workflow_version": request.workflow_version}

@app.get("/requests/{request_id}/audit")
def get_audit(request_id: str, db: Session = Depends(get_db)):
    if not db.get(WorkflowRequest, request_id): raise HTTPException(404, "Request not found")
    events = db.scalars(select(AuditEvent).where(AuditEvent.request_id == request_id).order_by(AuditEvent.id)).all()
    retries = db.scalars(select(RetryAttempt).where(RetryAttempt.request_id == request_id).order_by(RetryAttempt.id)).all()
    return {"request_id": request_id, "events": [{"event_type": e.event_type, "details": e.details, "timestamp": e.created_at} for e in events], "retry_history": [{"attempt": r.attempt_number, "outcome": r.outcome, "error": r.error, "timestamp": r.created_at} for r in retries]}
