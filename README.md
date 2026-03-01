# hydrata-mcp-server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

<!-- mcp-name: io.github.Hydrata/hydrata-mcp-server -->

MCP server for [Hydrata Cloud](https://hydrata.com) — run [ANUGA](https://github.com/anuga-community/anuga_core) flood simulations, track progress, and retrieve results through the [Model Context Protocol](https://modelcontextprotocol.io).

## Connect

Add to your `.mcp.json` (Claude Code, Cursor, Windsurf, etc.):

```json
{
  "mcpServers": {
    "hydrata": {
      "type": "streamable-http",
      "url": "https://hydrata.com/mcp/"
    }
  }
}
```

A [Hydrata Cloud](https://hydrata.com) account is required. Contact us at hydrata.com to get started.

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

## What is Hydrata?

[Hydrata](https://hydrata.com) is a geospatial hydraulic modeling platform. It runs [ANUGA](https://github.com/anuga-community/anuga_core) flood simulations in the cloud — upload terrain data, configure scenarios, run simulations on managed compute (Celery, EC2, or AWS Batch), and visualise results on interactive maps.

ANUGA is an open-source hydrodynamic model developed by [Geoscience Australia](https://www.ga.gov.au/) and the [Australian National University](https://www.anu.edu.au/). It solves the shallow water wave equations using finite volumes on an unstructured triangular mesh.

## Development

```bash
git clone https://github.com/Hydrata/hydrata-mcp-server.git
cd hydrata-mcp-server
pip install -e ".[dev]"
pytest -v
```

Contributions welcome — see [issues](https://github.com/Hydrata/hydrata-mcp-server/issues).

## License

[MIT](LICENSE)
