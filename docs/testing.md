# Testing

## What the automated tests cover

`make check` runs `pytest -q` (about 400 tests, under a minute). Every test is **mocked**: Hydrata's REST API is
replaced with [respx](https://lundberg.github.io/respx/) responses, so no test needs a network, a Hydrata account or a
key.

They cover:

- **Tool contract.** Every tool's name, description, input schema and annotations (`tests/test_tool_snapshot.py` against
  `tests/snapshots/tools.json`).
- **Request building.** Each tool sends the expected method, path, query and body to the Hydrata API
  (one `tests/test_<family>.py` per tool family, `tests/test_client.py` and the per-feature files; shared helpers in `tests/helpers.py`).
- **Response shaping.** What the tool hands back to the agent: summaries, `ui_url` links, elided presigned URLs,
  the data envelope, the relay of Hydrata's refusals (for example `CONSENT_REQUIRED` and unreviewed inputs).
- **Credential shape gate.** Requests without exactly one accepted `Authorization` credential get `401` before reaching
  the MCP SDK; the credential is forwarded unchanged (`tests/test_connect_token.py` and `tests/test_app.py`).
- **Configuration and version parity** (`tests/test_config.py`, `tests/test_version.py`).

## What they do not cover

- **The real Hydrata API.** A mocked response proves the server handles the shape we expect, not that Hydrata still
  returns it. There is deliberately no live CI against hydrata.com: it would need a real key, spend real compute and
  couple this public repository's CI to a production service.
- **Credential verification.** The server checks the *shape* of a key. Whether a key is valid, revoked, or allowed to do
  something is decided by Hydrata and is not tested here.
- **Real MCP clients** (claude.ai and others) and their sign-in flows.
- **Long-running behaviour** against real simulations, real uploads, real latency.

## Manual recipe: run against a local server

Use this before a release, or after a change a mock cannot judge. Two ways to see the tool list. Neither needs a real
key: the server answers `initialize` and `tools/list` after a **shape check only**, so any `Bearer hk_...` value works.
Calling a tool does need a real key and a reachable Hydrata (see the last section).

### 1. Start the server

```bash
uv sync --frozen --extra dev
export HYDRATA_API_URL=http://127.0.0.1:8000   # your local Hydrata, or any URL for a tools/list-only check
export HYDRATA_MCP_PORT=8765                   # default 8001
uv run --frozen --extra dev hydrata-mcp
```

### 2a. With curl (no browser, no Node)

```bash
H=(-H 'Authorization: Bearer hk_test' -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
curl -s "${H[@]}" http://localhost:8765/ -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
curl -s "${H[@]}" http://localhost:8765/ -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | sed -n 's/^data: //p' | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["result"]["tools"]), "tools")'
```

Expected: `28 tools`, the same count as `tests/snapshots/tools.json`. With no `Authorization` header, or a non-`hk_` Bearer,
expect `401` instead.

### 2b. With MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

In the browser UI choose transport **Streamable HTTP**, URL `http://localhost:8765/`, and add the header
`Authorization: Bearer hk_test`. Connect, open **Tools**, **List Tools**: the same tools should appear, each with its
schema and annotations. (The Inspector also has a `--cli` mode that takes the same URL and header.)

### Calling a tool for real

To call tools you need a real agent key (`hk_...`, created on your Hydrata account's Agent keys page) and
`HYDRATA_API_URL` pointing at a Hydrata that knows it, for example a local dev stack. Start with a read tool such as
`list_projects`. Never paste a real key into an issue, a pull request or a log.
