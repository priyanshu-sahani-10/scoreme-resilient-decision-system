"""Loads and validates declarative workflow definitions."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALLOWED_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists"}
ALLOWED_OUTCOMES = {"REJECT", "MANUAL_REVIEW"}
ALLOWED_FIELD_TYPES = {"string", "number", "integer", "boolean", "object", "array"}
ALLOWED_DECISIONS = {"APPROVED", "REJECTED", "MANUAL_REVIEW", "FAILED"}
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ConditionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=1)
    operator: str
    value: Any = None

    @field_validator("operator")
    @classmethod
    def valid_operator(cls, value: str) -> str:
        if value not in ALLOWED_OPERATORS:
            raise ValueError(f"Unsupported operator: {value}")
        return value

    @model_validator(mode="after")
    def valid_value_shape(self):
        if self.operator in {"in", "not_in"} and not isinstance(self.value, list):
            raise ValueError(f"Operator {self.operator} requires a list value")
        return self


class RuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    priority: int = 100
    field: str | None = Field(default=None, min_length=1)
    operator: str | None = None
    value: Any = None
    match: Literal["all", "any"] = "all"
    conditions: list[ConditionConfig] = Field(default_factory=list)
    on_fail: str = "REJECT"
    reason: str = Field(min_length=1)

    @field_validator("operator")
    @classmethod
    def valid_operator(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_OPERATORS:
            raise ValueError(f"Unsupported operator: {value}")
        return value

    @field_validator("on_fail")
    @classmethod
    def valid_outcome(cls, value: str) -> str:
        if value not in ALLOWED_OUTCOMES:
            raise ValueError(f"Unsupported failure outcome: {value}")
        return value

    @model_validator(mode="after")
    def valid_condition_shape(self):
        if self.conditions:
            if self.field is not None or self.operator is not None:
                raise ValueError("A rule must define either a single condition or a conditions group, not both")
            return self
        if self.field is None or self.operator is None:
            raise ValueError("A rule requires field and operator, or a non-empty conditions list")
        if self.operator in {"in", "not_in"} and not isinstance(self.value, list):
            raise ValueError(f"Operator {self.operator} requires a list value")
        return self


class StageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    rules: list[RuleConfig] = Field(default_factory=list)


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: int = Field(ge=1)
    description: str = ""
    required_fields: dict[str, str]
    stages: list[StageConfig]
    external_dependency: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=lambda: {"max_attempts": 1, "base_delay_seconds": 0})
    decision: dict[str, str] = Field(default_factory=lambda: {"default": "APPROVED", "dependency_failure": "FAILED", "manual_review": "MANUAL_REVIEW", "rejection": "REJECTED"})

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not NAME_PATTERN.fullmatch(value):
            raise ValueError("Workflow name must match ^[a-z0-9][a-z0-9_-]{0,63}$")
        return value

    @field_validator("required_fields")
    @classmethod
    def valid_field_types(cls, value: dict[str, str]) -> dict[str, str]:
        for field, expected in value.items():
            if expected not in ALLOWED_FIELD_TYPES:
                raise ValueError(f"Unsupported configured type for {field}: {expected}")
        return value

    @field_validator("decision")
    @classmethod
    def valid_decisions(cls, value: dict[str, str]) -> dict[str, str]:
        for outcome, status in value.items():
            if status not in ALLOWED_DECISIONS:
                raise ValueError(f"Unsupported decision status for {outcome}: {status}")
        return value

    @model_validator(mode="after")
    def valid_workflow_rules(self):
        rule_ids = [rule.id for stage in self.stages for rule in stage.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("Rule ids must be unique within a workflow")
        retry = self.retry_policy
        if "max_attempts" in retry and int(retry["max_attempts"]) < 1:
            raise ValueError("retry_policy.max_attempts must be at least 1")
        if "base_delay_seconds" in retry and float(retry["base_delay_seconds"]) < 0:
            raise ValueError("retry_policy.base_delay_seconds cannot be negative")
        return self


class ConfigurationManager:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._configs: dict[str, WorkflowConfig] = {}
        self._history: dict[str, dict[int, WorkflowConfig]] = {}
        self.reload()

    @property
    def history_dir(self) -> Path:
        return self.directory / "history"

    @staticmethod
    def _load_file(path: Path) -> WorkflowConfig:
        return WorkflowConfig.model_validate(json.loads(path.read_text()))

    def reload(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        loaded = {}
        for path in sorted(self.directory.glob("*.json")):
            config = self._load_file(path)
            if config.name in loaded:
                raise ValueError(f"Duplicate workflow name: {config.name}")
            loaded[config.name] = config
        history: dict[str, dict[int, WorkflowConfig]] = {}
        if self.history_dir.exists():
            for path in sorted(self.history_dir.glob("*.json")):
                config = self._load_file(path)
                versions = history.setdefault(config.name, {})
                if config.version in versions:
                    raise ValueError(f"Duplicate historical version: {config.name} v{config.version}")
                versions[config.version] = config
        self._configs = loaded
        self._history = history

    def list(self) -> list[dict[str, Any]]:
        return [{"name": c.name, "version": c.version, "description": c.description} for c in self._configs.values()]

    def get(self, name: str) -> WorkflowConfig:
        try:
            return self._configs[name]
        except KeyError:
            raise KeyError(f"Unknown workflow: {name}")

    def versions(self, name: str) -> list[dict[str, Any]]:
        merged = dict(self._history.get(name, {}))
        current = self._configs.get(name)
        if current is None and not merged:
            raise KeyError(f"Unknown workflow: {name}")
        if current is not None:
            merged[current.version] = current
        return [{"version": c.version, "description": c.description} for c in sorted(merged.values(), key=lambda c: c.version)]

    def get_version(self, name: str, version: int) -> WorkflowConfig:
        current = self._configs.get(name)
        if current is not None and current.version == version:
            return current
        try:
            return self._history[name][version]
        except KeyError:
            raise KeyError(f"Unknown workflow version: {name} v{version}")

    def register(self, raw: dict[str, Any]) -> WorkflowConfig:
        config = WorkflowConfig.model_validate(raw)
        existing = self._configs.get(config.name)
        known_versions = set(self._history.get(config.name, {}))
        if existing is not None:
            known_versions.add(existing.version)
        if config.version in known_versions:
            raise ValueError(f"Workflow {config.name} version {config.version} already exists; increment the version")
        if known_versions and config.version <= max(known_versions):
            raise ValueError(f"Workflow {config.name} version must be greater than the latest known version ({max(known_versions)})")
        if existing is not None:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            archive = self.history_dir / f"{config.name}.v{existing.version}.json"
            temp_archive = archive.with_name(archive.name + ".tmp")
            temp_archive.write_text(json.dumps(existing.model_dump(), indent=2))
            temp_archive.replace(archive)
            self._history.setdefault(config.name, {})[existing.version] = existing
        target = self.directory / f"{config.name}.json"
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps(config.model_dump(), indent=2))
        temp.replace(target)
        self._configs[config.name] = config
        self._history.setdefault(config.name, {})[config.version] = config
        return config
