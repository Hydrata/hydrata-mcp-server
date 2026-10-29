# Contributing to hydrata-mcp-server

Thanks for helping. Security problems do not belong in issues or pull requests: see [SECURITY.md](SECURITY.md).

## Set up

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Hydrata/hydrata-mcp-server
cd hydrata-mcp-server
uv sync --frozen --extra dev      # exact versions from uv.lock, same as CI
```

Optional, once per clone:

```bash
uvx pre-commit install                                  # ruff + ruff format on every commit
git config blame.ignoreRevsFile .git-blame-ignore-revs  # blame skips the one-off format commit
```

## `make check` is the one gate

```bash
uv run --frozen --extra dev make check
```

CI runs exactly this command, so a green local run means a green CI run. It runs, in order:

1. `lint`: `ruff check .` and `ruff format --check .` (ruff 0.15.2, line length 120).
2. `parity`: `scripts/check_version_parity.py` (`pyproject.toml` and `server.json` carry the same version).
3. `test`: `pytest -q`, the mocked suite described in [docs/testing.md](docs/testing.md).

To fix formatting: `uv run --frozen --extra dev ruff format .`

## The tool-list snapshot

`tests/snapshots/tools.json` is the committed list of the tools this server advertises (names, descriptions, input
schemas, annotations). `tests/test_tool_snapshot.py` fails when the live tool list differs from it, so a tool can never
change by accident.

When you change a tool on purpose, regenerate the snapshot deliberately and commit it with the code change:

```bash
UPDATE_SNAPSHOT=1 uv run --frozen --extra dev pytest tests/test_tool_snapshot.py
git diff tests/snapshots/tools.json   # read it: this diff is what clients will see
```

CI never writes the snapshot. Do not set `UPDATE_SNAPSHOT=1` to make a red build green without reading the diff.

## Pull requests

- Branch from `main`; one logical change per pull request.
- Run `make check` first and say so in the pull request.
- Add or update a test with every behaviour change. A new test should be shown to fail on the broken behaviour first.
- If the tool list changed, say which tools and why, and include the snapshot diff.
- Keep the commit message subject short and in the imperative; reference the issue number if there is one.
- Do not put keys, tokens or real project data in code, tests, fixtures, issues or logs.
