from __future__ import annotations
from typing import Any


def evaluate_condition(actual: Any, operator: str, expected: Any) -> bool:
    try:
        if operator == "exists": return actual is not None
        if operator == "eq": return actual == expected
        if operator == "ne": return actual != expected
        if operator == "gt": return actual is not None and not isinstance(actual, bool) and actual > expected
        if operator == "gte": return actual is not None and not isinstance(actual, bool) and actual >= expected
        if operator == "lt": return actual is not None and not isinstance(actual, bool) and actual < expected
        if operator == "lte": return actual is not None and not isinstance(actual, bool) and actual <= expected
        if operator == "in": return isinstance(expected, list) and actual in expected
        if operator == "not_in": return isinstance(expected, list) and actual not in expected
    except TypeError:
        return False
    raise ValueError(f"Unsupported operator: {operator}")


def _evaluate_rule(rule, payload: dict) -> tuple[bool, list[dict] | None]:
    if rule.conditions:
        details, results = [], []
        for condition in rule.conditions:
            actual = payload.get(condition.field)
            passed = evaluate_condition(actual, condition.operator, condition.value)
            results.append(passed)
            details.append({"field": condition.field, "operator": condition.operator, "expected": condition.value, "actual": actual, "status": "PASSED" if passed else "FAILED"})
        matched = all(results) if rule.match == "all" else any(results)
        return matched, details
    actual = payload.get(rule.field)
    return evaluate_condition(actual, rule.operator, rule.value), None


def evaluate_rules(config, payload: dict) -> dict:
    trace, outcome, reasons = [], None, []
    for stage in config.stages:
        for rule in sorted(stage.rules, key=lambda r: r.priority):
            if outcome in {"REJECT", "MANUAL_REVIEW"}:
                trace.append({"stage": stage.name, "rule_id": rule.id, "status": "SKIPPED", "reason": "An earlier rule determined the outcome."})
                continue
            passed, conditions = _evaluate_rule(rule, payload)
            entry = {"stage": stage.name, "rule_id": rule.id, "status": "PASSED" if passed else "FAILED", "reason": "Condition satisfied." if passed else rule.reason}
            if conditions is None:
                entry.update({"field": rule.field, "operator": rule.operator, "expected": rule.value, "actual": payload.get(rule.field)})
            else:
                entry.update({"match": rule.match, "conditions": conditions})
            trace.append(entry)
            if not passed:
                reasons.append(rule.reason)
                if rule.on_fail == "REJECT":
                    outcome = "REJECT"
                elif outcome != "REJECT":
                    outcome = "MANUAL_REVIEW"
    return {"trace": trace, "outcome": outcome, "reasons": reasons}
