from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Literal


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_name: str
    data: dict[str, Any]
    simulation_mode: str = Field(default="success", pattern="^(success|timeout|failure|permanent_failure|transient_then_success)$")


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVED", "REJECTED"]
    reviewer: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)
