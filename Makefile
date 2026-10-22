# `make check` is the one gate: CI runs it, and so should you before pushing.
# Needs the dev extras: `uv sync --extra dev` (or pip install -e '.[dev]').
PY ?= python

.PHONY: check lint parity test

check: lint parity test

lint:
	$(PY) -m ruff check .
	# TASK-4015 (W5.1) does the one-off `ruff format` of the tree and then adds
	# `ruff format --check .` here; today 12 files (~6.5k lines) would be reformatted.

parity:
	$(PY) scripts/check_version_parity.py

test:
	$(PY) -m pytest -q
