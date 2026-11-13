from __future__ import annotations
import os
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timezone
from sqlalchemy import create_engine, String, Integer, DateTime, JSON, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

class Base(DeclarativeBase): pass

def utcnow(): return datetime.now(timezone.utc)

class WorkflowRequest(Base):
    __tablename__ = "workflow_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    workflow_name: Mapped[str] = mapped_column(String(100), index=True)
    workflow_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), index=True)
    input_data: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class RetryAttempt(Base):
    __tablename__ = "retry_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./decision_system.db")
_engine_args = {"connect_args": {"check_same_thread": False, "timeout": 30}} if DATABASE_URL.startswith("sqlite") else {"pool_pre_ping": True}
engine = create_engine(DATABASE_URL, **_engine_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def init_db():
    if DATABASE_URL.startswith("sqlite"):
        from sqlalchemy import event
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor(); cursor.execute("PRAGMA journal_mode=WAL"); cursor.execute("PRAGMA foreign_keys=ON"); cursor.close()
    Base.metadata.create_all(bind=engine)
