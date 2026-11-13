import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.database import Base
from app.configuration import ConfigurationManager
from app.service import WorkflowService

@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close(); Base.metadata.drop_all(engine); engine.dispose()

@pytest.fixture
def service():
    from pathlib import Path
    configs = ConfigurationManager(Path(__file__).resolve().parents[1] / "configs")
    return WorkflowService(configs, sleeper=lambda _: None)

@pytest.fixture
def loan_data():
    return {"applicant_id": "APP-100", "monthly_income": 60000, "credit_score": 720, "requested_amount": 500000}
