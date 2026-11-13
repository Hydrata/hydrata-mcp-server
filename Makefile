# `make check` is the one gate: CI runs it, and so should you before pushing.
# After `uv sync`, `make check` runs everything through uv with the dev extras from
# uv.lock (`--locked` fails if uv.lock is stale against pyproject.toml).
# Without uv: pip install -e '.[dev]' and run `make check PY=python`.
PY ?= uv run --locked --extra dev python

.PHONY: check lint parity test

check: lint parity test

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

parity:
	$(PY) scripts/check_version_parity.py

test:
	$(PY) -m pytest -q
