# `make check` is the one gate: CI runs it, and so should you before pushing.
# Needs the dev extras: `uv sync --extra dev` (or pip install -e '.[dev]').
PY ?= python

.PHONY: check lint parity test

check: lint parity test

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

parity:
	$(PY) scripts/check_version_parity.py

test:
	$(PY) -m pytest -q
