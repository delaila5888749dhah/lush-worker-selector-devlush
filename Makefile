# Lush Givex worker — Make targets
#
# The E2E suite (P2-4) lives under tests/integration/e2e/ and is executed
# separately from the unit/integration suites because the acceptance criterion
# requires: "CI job mới: make test-e2e (tách khỏi unit test)".

.PHONY: test test-unit test-integration test-e2e test-all \
        lint format typecheck coverage audit

# Default target — unit suite (fast path used by most contributors).
test: test-unit

test-unit:
	python -m unittest discover tests

# L3 harness + L4 smoke (existing integration suite).
test-integration:
	python -m unittest discover tests/integration -v

# P2-4 — 14 E2E tests (T-01 … T-14).  Kept separate from the unit suite
# because they exercise the full FSM + orchestrator loop and stub the CDP
# driver in ways that mutate shared idempotency state.
test-e2e:
	python -m unittest discover -s tests/integration/e2e -t . -v

# Convenience: run everything locally.
test-all: test-unit test-integration test-e2e

# ── Quality gates (issue #226) ─────────────────────────────────────
lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy app modules integration

coverage:
	python -m coverage run --source=app,modules,integration \
	    -m unittest discover tests
	python -m coverage report
	python -m coverage xml -o coverage.xml

audit:
	pip-audit -r requirements.txt --strict
