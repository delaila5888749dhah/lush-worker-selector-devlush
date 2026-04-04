# Deployment & Monitoring Specification

spec-version: 1.0

## Overview

Phase 8 — Production Deployment & Monitoring.  Defines the runtime
monitoring contract that tracks worker stability, restart patterns and
error rates once the system is deployed to production.

## Runtime Monitoring Contract

### get_deployment_status

- **Module:** `integration.runtime`
- **Input:** none
- **Output:** `dict` with keys:

| Key | Type | Description |
|-----|------|-------------|
| `running` | `bool` | Whether the runtime loop is active |
| `state` | `str` | Current lifecycle state (`INIT`, `RUNNING`, `STOPPING`, `STOPPED`) |
| `worker_count` | `int` | Number of active workers |
| `active_workers` | `list[str]` | List of active worker IDs |
| `consecutive_rollbacks` | `int` | Count of consecutive rollback events |
| `trace_id` | `str \| None` | Current trace ID for log correlation |
| `metrics` | `dict \| None` | Monitor metrics snapshot (see below), or `None` if monitor is unavailable |

### Monitor Metrics (from `modules.monitor`)

When available, the `metrics` dict contains:

| Key | Type | Description |
|-----|------|-------------|
| `success_count` | `int` | Total successful task completions |
| `error_count` | `int` | Total task errors |
| `success_rate` | `float` | Success rate `[0.0, 1.0]` |
| `error_rate` | `float` | Error rate `[0.0, 1.0]` |
| `memory_usage_bytes` | `int` | Process RSS memory in bytes |
| `restarts_last_hour` | `int` | Worker restarts in last 60 minutes |
| `baseline_success_rate` | `float \| None` | Baseline snapshot for delta checks |

### Monitoring Scope

1. **Worker Stability** — tracked via `worker_count`, `active_workers`,
   and `state`.  A healthy deployment maintains `RUNNING` state with the
   expected number of workers.
2. **Restart Patterns** — tracked via `restarts_last_hour` and
   `consecutive_rollbacks`.  Threshold: ≤ 3 restarts per hour.
3. **Error Rates** — tracked via `error_rate` and `success_rate`.
   Threshold: error rate ≤ 5 %, success rate drop ≤ 10 % from baseline.

### Constraints

- Thread-safe: all state access guarded by `threading.Lock`.
- No cross-module imports: the integration layer is the only code that
  reads from both `modules.monitor` and `modules.rollout`.
- Monitor failure resilience: if `monitor.get_metrics()` raises, the
  function must return `metrics: None` rather than propagating the error.

---

## Extension Spec — Future Upgrades

This section defines the extension points for Phase 8.  Each extension
can be added incrementally without breaking the current system.

### Extension 1 — Metrics Export

- **Purpose:** Export runtime metrics to an external monitoring system
  (e.g. Prometheus, CloudWatch, Datadog).
- **Steps to add:**
  1. Create `modules/observability/` with `metrics_exporter.py`.
  2. Define `export_metrics(metrics: dict) -> None` function.
  3. Call from `_runtime_loop` after `monitor.get_metrics()`.
  4. Add spec entry in `spec/integration/interface.md`.
- **Backward compatibility:** Additive only — existing code unchanged.

### Extension 2 — Alerting Rules

- **Purpose:** Evaluate alert conditions and send notifications.
- **Steps to add:**
  1. Create `modules/observability/alerting.py`.
  2. Define `evaluate_alerts(metrics: dict) -> list[str]` function.
  3. Define `send_alert(message: str) -> None` function.
  4. Integrate into `_runtime_loop` alongside rollback check.
  5. Add spec entry in `spec/integration/interface.md`.
- **Backward compatibility:** Additive only — existing rollback logic
  unchanged.

### Extension 3 — Health Check Endpoint

- **Purpose:** Expose an HTTP endpoint for external health probes.
- **Steps to add:**
  1. Create `modules/observability/healthcheck.py`.
  2. Define `get_health() -> dict` (calls `get_deployment_status()`).
  3. Optionally add lightweight HTTP server (stdlib `http.server`).
  4. Add spec entry in `spec/integration/interface.md`.
- **Backward compatibility:** Additive only — no existing interface
  changes.

### Extension 4 — Structured Log Aggregation

- **Purpose:** Forward structured logs to a central log sink.
- **Steps to add:**
  1. Add a JSON log formatter to `_log_event` in `integration/runtime.py`.
  2. Create `modules/observability/log_sink.py` for transport.
  3. Add spec entry describing the log schema.
- **Backward compatibility:** Additive — current pipe-delimited format
  remains the default.

### Extension 5 — Deployment Automation

- **Purpose:** Automate blue-green or rolling deployment via CI/CD.
- **Steps to add:**
  1. Add GitHub Actions workflow `deploy.yml`.
  2. Define deployment stages (build → test → stage → prod).
  3. Add rollback step using `modules/rollout` logic.
  4. Document in runbook.
- **Backward compatibility:** Infrastructure only — no code changes.

### Extension Guidelines

- Each extension MUST be introduced as a MINOR version bump (additive).
- Extensions MUST NOT modify existing function signatures.
- Extensions MUST include unit tests and a spec update.
- Extensions MUST preserve thread-safety invariants.

## Changelog

### v1.0 (2026-04-04)
- Initial deployment & monitoring spec.
- Defined `get_deployment_status()` contract.
- Created extension spec for 5 future upgrade paths.
