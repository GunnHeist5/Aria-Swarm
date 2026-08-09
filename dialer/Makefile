VENV := .venv/bin
ORCH := $(VENV)/orchestrator

.PHONY: install test migrate sync dry-run serve worker report readiness

install:
	python3 -m venv .venv
	$(VENV)/pip install -e ".[dev]"

test:
	$(VENV)/python -m pytest tests/ -q

migrate:
	$(ORCH) migrate

sync:
	$(ORCH) sync

# Phase 1: the CSV deliverable. Zero calls placed.
dry-run:
	$(ORCH) dry-run --touch both

serve:
	$(ORCH) serve

worker:
	$(ORCH) worker

report:
	$(ORCH) report

readiness:
	$(ORCH) readiness
