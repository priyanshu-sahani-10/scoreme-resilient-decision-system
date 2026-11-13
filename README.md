# Resilient Decision System

A configuration-driven workflow decision platform built for the ScoreMe placement hackathon. It demonstrates a reusable workflow engine, a safe declarative rules DSL with composite conditions, lifecycle persistence, idempotent request intake, retryable external dependencies, actionable manual-review resolution, configuration version history, and explainable audit history.

## Highlights

- **Two workflows, one engine:** loan approval and insurance claim triage are defined in JSON under `configs/`.
- **Configuration-driven decisions:** priorities, fields, operators, thresholds, composite `all`/`any` conditions, failure outcomes, dependency requirements, and retry policies are data—not Python business logic.
- **Safe rule evaluation:** the engine supports a small allow-listed operator set (`eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `exists`). It never evaluates arbitrary Python or expressions from configuration.
- **Request lifecycle:** `RECEIVED → VALIDATED → RULES_EVALUATED → APPROVED | REJECTED | MANUAL_REVIEW | FAILED`, with `MANUAL_REVIEW → APPROVED | REJECTED` through the review API.
- **Idempotency:** `Idempotency-Key` is stored under a database uniqueness constraint. A replay with the same workflow and payload returns the original result and does not execute the workflow again. Reusing the key with a different workflow or payload returns `409`.
- **Reliability simulation:** dependency modes support `success`, `timeout`, `failure`, `permanent_failure`, and `transient_then_success`. Transient failures use bounded exponential backoff; permanent failures are not retried.
- **Explainability:** each rule result, composite condition, skipped rule, dependency call, retry, state transition, review decision, and final decision is recorded.
- **Configuration history:** registering a new workflow version archives the previous version under `configs/history/`; old versions remain retrievable by API.
- **REST API:** interactive OpenAPI docs at `/docs`.

## Tech stack

Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2, SQLite (WAL mode for local execution), and pytest. SQLite keeps the hackathon submission easy to run; the persistence boundary can be moved to PostgreSQL for multi-instance deployment.

## Quick start

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Open `http://localhost:8000/docs`. The database `decision_system.db` is created automatically on startup. Override settings with environment variables shown in `.env.example`.

Run automated tests:

```bash
pytest -q
```

## API overview

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/workflows` | List configured workflows |
| GET | `/workflows/{name}/config` | Read a workflow definition |
| GET | `/workflows/{name}/versions` | List all retained versions of a workflow |
| GET | `/workflows/{name}/versions/{version}` | Read a historical workflow version |
| PUT | `/workflows/config` | Register/update a validated workflow definition |
| POST | `/requests` | Validate and execute a workflow (requires `Idempotency-Key`) |
| POST | `/requests/{id}/review` | Resolve a `MANUAL_REVIEW` request as `APPROVED` or `REJECTED` |
| GET | `/requests/{id}` | Retrieve current request and result |
| GET | `/requests/{id}/explanation` | Retrieve decision explanation and rule trace |
| GET | `/requests/{id}/audit` | Retrieve chronological audit events and retry history |

### Example: approved loan

```bash
curl -X POST http://localhost:8000/requests \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: loan-app-1001' \
  -d '{
    "workflow_name": "loan_approval",
    "data": {
      "applicant_id": "APP-1001",
      "monthly_income": 60000,
      "credit_score": 720,
      "requested_amount": 500000
    },
    "simulation_mode": "success"
  }'
```

### Example: transient dependency failure then recovery

Use the same body but set `simulation_mode` to `transient_then_success` and use a fresh idempotency key. The simulator fails once, records the attempt, retries, and succeeds. A `timeout` mode exhausts the loan workflow's three configured attempts; `permanent_failure` is attempted only once.

### Example: manual review

For loan approval, request `requested_amount` above the configured automatic limit of INR 1,000,000. The request is routed to `MANUAL_REVIEW`. For rejection, use a credit score below the configured minimum of 650.

### Example: resolving a manual review

```bash
curl -X POST http://localhost:8000/requests/<request_id>/review \
  -H 'Content-Type: application/json' \
  -d '{"decision": "APPROVED", "reviewer": "ops-1", "notes": "Income documents verified."}'
```

The request transitions from `MANUAL_REVIEW` to `APPROVED` (or `REJECTED`), and the audit history records a `REVIEW_DECIDED` event plus the state transition. Reviewing a request that is not in `MANUAL_REVIEW` returns `409`, so a review cannot be applied twice.

## Configuration model

Each JSON file contains:

- `name`, `version`, `description`
- `required_fields` with primitive input types
- ordered `stages` containing prioritized `rules`
- each rule's `id`, `field`, allow-listed `operator`, `value`, `on_fail`, and human-readable `reason`
- optional composite rules: `match: "all" | "any"` with a `conditions` array, instead of a single `field`/`operator`/`value`
- optional external dependency name/requirement
- retry policy (`max_attempts`, `base_delay_seconds`)
- decision mapping

To demonstrate a rule change, edit a threshold in `configs/loan_approval.json`, increment its version, and restart the application (or register the modified config with `PUT /workflows/config`). The same engine applies the new rule. Configuration is validated before activation and written via an atomic temporary-file replacement. The previous version is archived under `configs/history/` and remains readable through `GET /workflows/{name}/versions/{version}`. Versions must be unique and increasing.

## Data model

- `workflow_requests`: one row per idempotency key, including workflow version, original input, current status, and final result.
- `audit_events`: append-only event records linked by request ID.
- `retry_attempts`: one record per dependency attempt, including outcome and error.

The request and its audit/retry records are committed in a single SQL transaction. A failure before commit rolls the unit of work back rather than leaving a partially finalized request. The external dependency is simulated and has no real-world side effects. For a real external integration, use an outbox/inbox or durable job queue and explicit compensation/reconciliation policies.

## Testing

The test suite covers successful approval, invalid input, duplicate idempotency key replay, idempotency-key conflicts, transient recovery, exhausted timeout retries, permanent failure without retry, business rejection, manual review routing and resolution, double-review rejection, composite `all` conditions, a second workflow on the shared engine, configuration changes affecting outcomes, configuration version retention and validation, audit history, operator semantics, numeric-vs-boolean validation, rollback on partial save failure, and API error paths (404, 409, 422) including config registration and review endpoints.

## Architecture and trade-offs

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for component boundaries, data flow, state machine, retry flow, consistency semantics, scaling, security, assumptions, and Mermaid diagrams.

## Known limitations / deliberate scope

- The external service is a deterministic simulator, not a real bureau/document provider.
- Execution is synchronous in the API request. Long-running workflows should be moved to a durable worker queue.
- SQLite is suitable for a local demonstration, not high-concurrency multi-region deployment.
- Authentication, authorization, signed configuration changes, and tenant isolation are not included; protect configuration-write endpoints before production use.
- Operator-triggered replay of failed requests is not exposed; automatic retry of transient dependency errors is implemented and audited.
- Manual review can be resolved through the API, but reviewer assignment, queues, and SLA management are not implemented.
- Delivery semantics are **at-most-once per idempotency key within this database** for committed requests, not distributed exactly-once execution. A process crash during a real external side effect would require idempotent downstream calls and reconciliation.
