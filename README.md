# hydrata-mcp-server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

MCP server for [Hydrata](https://hydrata.com), a geospatial hydraulic modeling platform built on [ANUGA](https://github.com/anuga-community/anuga_core). Lets AI agents run flood simulations, track progress, and retrieve results through the [Model Context Protocol](https://modelcontextprotocol.io).

## Quickstart

```bash
pip install -e .
```

Set environment variables:

```bash
export HYDRATA_API_URL="https://your-instance.com/api/v2/anuga"
export HYDRATA_API_USERNAME="your-username"
export HYDRATA_API_PASSWORD="your-password"
```

Run the server:

```bash
hydrata-mcp                          # CLI (stdio or HTTP)
uvicorn hydrata_mcp.server:app       # ASGI (StreamableHTTP)
```

## Claude Code integration

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "hydrata": {
      "type": "streamable-http",
      "url": "https://your-instance.com/mcp/mcp"
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `list_projects` | List ANUGA simulation projects (paginated) |
| `get_project` | Get project details including scenarios |
| `get_scenario` | Get scenario status and latest run |
| `start_simulation` | Start a flood simulation (local/EC2/Batch backends) |
| `get_run_status` | Lightweight status poll (<50ms) |
| `get_run` | Full run details with timing and results |
| `cancel_run` | Cancel an in-flight simulation |
| `retry_run` | Retry a failed simulation |
| `list_runs` | List runs across a project (with status filter) |

### Typical workflow

```
list_projects → get_scenario → start_simulation → poll get_run_status → get_run
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HYDRATA_API_URL` | Yes | | Base URL for the Hydrata ANUGA API |
| `HYDRATA_API_USERNAME` | Yes | | HTTP Basic auth username |
| `HYDRATA_API_PASSWORD` | Yes | | HTTP Basic auth password |
| `HYDRATA_MCP_PORT` | No | `8001` | Server listen port |
| `HYDRATA_MCP_HOST` | No | `127.0.0.1` | Server bind address |

## Development

```bash
pip install -e ".[dev]"
pytest -v
```

## License

[MIT](LICENSE)
