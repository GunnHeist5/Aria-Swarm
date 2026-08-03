PY := .venv/bin/python

.PHONY: venv dev seed test

venv:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

dev:
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload

seed:
	$(PY) -m app.cli seed-demo

test:
	$(PY) -m pytest tests/ -q
