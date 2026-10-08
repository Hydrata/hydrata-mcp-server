# hydrata-mcp-server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

<!-- mcp-name: com.hydrata/hydrata-mcp-server -->

MCP server for [Hydrata Cloud](https://hydrata.com) — build [ANUGA](https://github.com/anuga-community/anuga_core) flood models from terrain and input layers, run simulations, track progress, and retrieve results through the [Model Context Protocol](https://modelcontextprotocol.io).

## Connect and authenticate

**Endpoint:** `https://hydrata.com/mcp/` — **Transport:** Streamable HTTP.

Every tool call needs the caller's own hydrata.com credential. The server holds no identity of its own: a `tools/call` must carry the caller's `Authorization` header (HTTP Basic for a hydrata.com account), which the server forwards verbatim to the Hydrata REST API — so results are scoped by the real user's project permissions and the audit trail names them. A `tools/call` without it is answered with `401` and `WWW-Authenticate: Basic realm="hydrata.com"` before any tool runs. `initialize`, `tools/list` and `ping` answer anonymously, so the tool catalog is browsable without an account and the MCP registry's liveness check stays true.

The server is **not yet open to external accounts**: there is no self-serve way to connect your own hydrata.com login to it today, and this README deliberately carries no client-side credential recipe. If you would like to use it, contact us at [hydrata.com](https://hydrata.com).

## Tools

Version 0.2.0 exposes nineteen tools: eleven that read projects, inputs and scenarios and drive a run, and eight that build a model from terrain and input layers the agent has already uploaded. Every tool carries MCP annotations (a `title`; `readOnlyHint` on the nine read tools; `destructiveHint` only on `cancel_run`).

| Tool | Description |
|------|-------------|
| `list_projects` | List ANUGA simulation projects (paginated) |
| `get_project` | Get one project — name, projection, visibility, your role, `base_map` — plus `ui_url`, the link that opens its map |
| `get_scenario` | Get a scenario's compact state — `computed_status`, its inputs (terrain, boundary, friction, inflow, rainfall, structure, mesh region, resolution, duration), the mesh-triangle estimate + breakdown, the price (`compute_cost_estimate`, `vcpu_hours_estimate`) and the latest run's id/status/error — never the raw detail |
| `start_simulation` | Start a flood simulation (local/EC2/Batch backends); the returned run record has its presigned download links elided; hand-back link opens the run |
| `get_run_status` | Lightweight status poll (<50ms); `phase` is never empty (`preparing_inputs` while a computing run has no progress yet) |
| `get_run` | Full run details with timing, log and result layers; presigned `s3_*_url` download links are elided |
| `cancel_run` | Cancel an in-flight simulation; hand-back link opens the scenario |
| `retry_run` | Retry a failed simulation (rebuilds the scenario as a new run); hand-back link opens the scenario |
| `list_runs` | List runs across a project (with status filter); presigned `s3_*_url` download links are elided from every row |
| `list_inputs` | Every input row of a project per kind — row id, title, dataset, WFS typename and whether it has features — so the agent never has to probe files |
| `list_time_series` | A project's time series — id, name, type, units, row count and first/last timestamp; the data rows are never returned |
| `create_project` | Create an ANUGA project with a name and EPSG projection; returns its `base_map` and the `ui_url` of its map |
| `presign_terrain_upload` | Return a presigned URL + key so the agent PUTs the terrain GeoTIFF itself (no file bytes pass through MCP) |
| `finalize_terrain_upload` | Register the uploaded terrain; the import chain seeds the project's six default boundary/friction/inflow/rainfall/structure/mesh-region rows |
| `get_terrain` | Poll the terrain until it is ready (bounded; `timed_out` means call again); `phase` is never empty; summary = WGS84 bbox, elevation min/max, nodata fraction, native CRS; the link frames the DEM |
| `create_time_series` | Create a time series (rain gauge, hydrograph or tide/stage — `series_type` and `units` are top-level fields) from `{"rowData": [...]}`; a rainfall polygon binds to its gauge by the series name; summary = row count + first/last timestamp; the link opens the Hydrology panel |
| `attach_input_layer` | Attach a GeoJSON dataset the agent already uploaded as the project's boundary/friction/inflow/rainfall/structure or mesh-region layer; breakline and culvert are refused (culvert flow is not conveyed); summary = feature count (WFS hits) + WGS84 bbox + whether the map now shows the layer; the link opens Inputs and frames the layer |
| `create_scenario` | Create a draft scenario and return its mesh-triangle estimate and price (`resolution` — scenario and mesh-region — is a length in metres); the link opens the scenario |
| `build_scenario` | Build the scenario package; shows the estimate first and needs `confirm=true` above 100,000 triangles; polls `computed_status` to built; never re-posts a build that is in flight or already built (`rebuild=true` to force one); surfaces the server's 422 refusals (MESH_TOO_LARGE, BOUNDARY_NO_EXTERNAL, MESH_REGION_UNITS_UNMARKED) verbatim; a missing/empty/perimeter-less boundary sends the human to the map to draw or fix it; summary = triangle count + re-priced cost |

### The hand-back: the engineer signs off in the map

Every tool that changes something (`create_project`, `finalize_terrain_upload`, `get_terrain`, `create_time_series`, `attach_input_layer`, `create_scenario`, `build_scenario`, `start_simulation`, `cancel_run`, `retry_run`) also returns:

- `ui_url` — `https://<site>/catalogue/#/map/<base_map>?panel=…&layer=…&scenario=…&run=…`, which opens the project map on the thing that changed (the Inputs panel framing the attached layer, the Hydrology panel, the scenario, the run);
- `summary` — numbers the engineer can check (feature count and bbox, elevation range, row count and time span, triangle estimate and price);
- `say_to_user` — the sentence the agent should relay.

The server's instructions tell the agent to hand the link to the user after each layer lands and to wait for the user before `start_simulation`. Refusals the user can fix in the map (no boundary, no External boundary edge) carry the link too. Every request the server forwards carries `X-Hydrata-Client: mcp/<version>` and `X-Hydrata-Session: <uuid>` — informational only (client-asserted), never used for authorship.

No tool accepts file contents inline. The agent moves the bytes itself — the terrain GeoTIFF to the presigned URL, a GeoJSON layer to the REST API upload endpoint — and hands the server the resulting key or upload id.

No read tool relays a presigned S3 URL. A run's result links are 6-hour capabilities carrying a temporary AWS credential, and the server forwards the caller's own credential rather than holding one, so a tool that echoed such a link would widen what an agent transcript is worth beyond what the caller asked for. Every read tool (and `start_simulation`) elides them, recursively, by key name (`s3_*_url`) and by value shape (any string carrying `X-Amz-Signature` / `X-Amz-Security-Token`, redacted in place so the rest of a log line survives); results stay viewable through the `gn_layer_*` WMS entries. `presign_terrain_upload` is the sole exception: its `upload_url` is the URL the caller explicitly asked for.

### Typical workflow

Run a scenario that already exists:

```
list_projects → get_scenario → start_simulation → poll get_run_status → get_run
```

Import a model and run it:

```
create_project → presign_terrain_upload → PUT the GeoTIFF → finalize_terrain_upload → get_terrain
→ create_time_series / attach_input_layer → create_scenario → build_scenario
→ start_simulation → poll get_run_status → get_run
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
