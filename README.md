# hydrata-mcp-server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

<!-- mcp-name: com.hydrata/hydrata-mcp-server -->

MCP server for [Hydrata Cloud](https://hydrata.com) — build [ANUGA](https://github.com/anuga-community/anuga_core) flood models from terrain and input layers, run simulations, track progress, and retrieve results through the [Model Context Protocol](https://modelcontextprotocol.io).

## Connect and authenticate

**Endpoint:** `https://hydrata.com/mcp/` — **Transport:** Streamable HTTP.

Every tool call needs the caller's own hydrata.com credential: an **agent key** (`Authorization: Bearer hk_…`), a named, revocable grant at one of three levels (read < draft < launch) that the account owner creates — and can revoke, one at a time or all at once — on the site's Agent keys page (`/agents/`). The server holds no identity and no key store of its own: it checks only the shape of the credential (an `hk_` prefix, or with the connect flow configured a JWT-shaped token) and forwards the one accepted `Authorization` header unchanged to the Hydrata REST API, where Django verifies it on every tool call (for a connect-flow token: signature, issuer, audience, expiry and the live grant; for an agent key: the stored hash and the live grant) — so results are scoped by the real user's project permissions and the key's level, and the audit trail names them. Every JSON-RPC request — `initialize`, `tools/list` and `ping` as well as `tools/call` — that does not carry exactly one accepted credential (an agent key, or with the one-click connect flow configured its access token) is answered with `401`, a JSON-RPC error `-32001` and `WWW-Authenticate: Bearer realm="hydrata.com"` (plus `resource_metadata="…"` when the connect flow is configured) before it reaches the MCP SDK: no `Authorization` header, HTTP Basic, or any other `Bearer` token such as a GeoNode OAuth token. Agent keys and connect-flow tokens both map to an agent grant, so both are subject to the grant's level, the account's pause switch, the spend limit and the agent activity log. The 401 on the first request is what tells an OAuth-aware client such as claude.ai that the server needs sign-in (TASK-3987), so the tool catalog is not served to a request without a credential of an accepted shape; for `initialize`, `tools/list` and `ping` that credential is checked for shape only, not verified. The one exception (TASK-4177): MCP catalog crawlers whose `User-Agent` matches an allowlist (`HYDRATA_MCP_DISCOVERY_USER_AGENTS`, default `mcptry.com indexer,SmitheryBot`) may run `initialize`, `ping` and `tools/list` with no `Authorization` header, so catalogs can show the tools; `tools/call` always needs a credential, and real clients never match, so they still get the 401 and the sign-in default. Each claim about this server's security behaviour is cited to code or a test in [docs/security-model.md](docs/security-model.md); to report a vulnerability see [SECURITY.md](SECURITY.md). A body that is not JSON-RPC is left to the SDK's own error, and a GET on the endpoint is answered `405`. A static MCP Server Card (SEP-2127), built from [server.json](server.json), is served without credentials at `GET https://hydrata.com/mcp/server-card`. The catalog icon, a 512×512 PNG of the Hydrata mark, is served the same way at `GET https://hydrata.com/mcp/icon.png` and listed in `server.json` and `serverInfo.icons`.

Anyone with a [hydrata.com](https://hydrata.com) account can connect. There are two ways in, and both give the assistant a named, revocable permission at one access level (Read, Draft or Launch):

- **One-click connect (OAuth)** — for claude.ai, ChatGPT and any MCP client that can add a connector by URL. Add `https://hydrata.com/mcp/` as a custom connector, sign in with your Hydrata account, name the connection and choose its level. The assistant never sees your password.
- **Agent key** — for Claude Code, scripts and any client that can send an HTTP header. Create a key on the Agent keys page (`https://hydrata.com/agents/`) and send it as `Authorization: Bearer hk_…`. In Claude Code:

  ```bash
  claude mcp add --transport http hydrata https://hydrata.com/mcp/ --header "Authorization: Bearer hk_YOUR_KEY"
  ```

  Do not add `--scope project`: that writes the key into the project's shared `.mcp.json`.

Keys and connections are listed, and revoked, on the same Agent keys page. A run that costs money waits for your consent in the browser unless the key's monthly spend limit covers it. Full guide: [Connect an AI assistant](https://hydrata.com/docs/mcp/connect/) in the [Hydrata MCP docs](https://hydrata.com/docs/mcp/).

## Tools

Version 0.2.2 exposes forty-six tools, all listed below; twenty of them only read. Every tool carries MCP annotations: a `title` and explicit boolean `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` (`openWorldHint` is false on every tool: they reach only your Hydrata account). `destructiveHint` is true only on `cancel_run`, `delete_time_series` and `delete_observation`.

| Tool | Description |
|------|-------------|
| `list_projects` | List ANUGA simulation projects (paginated) |
| `get_project` | Get one project — name, projection, visibility, your role, `base_map` — plus `ui_url`, the link that opens its map |
| `get_scenario` | Get a scenario's compact state — `computed_status`, its inputs (terrain, boundary, friction, inflow, rainfall, building, mesh region, culvert, resolution, duration), the mesh-triangle estimate + breakdown, the price (`compute_cost_estimate`, `vcpu_hours_estimate`) and the latest run's id/status/error — never the raw detail |
| `start_simulation` | Start a flood simulation (local/EC2/Batch backends); the returned run record has its presigned download links elided; hand-back link opens the run. A run over the agent key's monthly spend limit (default $0), or one Hydrata cannot price, is not started: the tool relays Hydrata's `CONSENT_REQUIRED` at once with the `consent_url` the person opens in their browser (`consent_timeout_seconds` > 0 also polls the run for their click) |
| `get_run_status` | Lightweight status poll (<50ms); `phase` is never empty (`preparing_inputs` while a computing run has no progress yet) |
| `get_run` | Full run details with timing, log and result layers; presigned `s3_*_url` download links are elided |
| `get_flow_hydrograph` | Flow (Q) across a line you draw, at every frame of a completed run: peak, time of peak, volume and a mass balance, from the run's playback store (the model's flow, not a gauge reading) |
| `cancel_run` | Cancel an in-flight simulation; hand-back link opens the scenario |
| `retry_run` | Retry a failed simulation (rebuilds the scenario as a new run); hand-back link opens the scenario |
| `list_runs` | List runs across a project (with status filter); presigned `s3_*_url` download links are elided from every row |
| `list_inputs` | Every input row of a project per kind (boundary, friction, inflow, rainfall, building, mesh region, culvert) — row id, title, dataset, WFS typename and whether it has features — so the agent never has to probe files |
| `list_time_series` | A project's time series — id, name, type, units, row count and first/last timestamp; the data rows are never returned |
| `create_project` | Create an ANUGA project with a name and EPSG projection; returns its `base_map` and the `ui_url` of its map |
| `presign_terrain_upload` | Return a presigned URL + key so the agent PUTs the terrain GeoTIFF itself (no file bytes pass through MCP) |
| `finalize_terrain_upload` | Register the uploaded terrain; the import chain seeds the project's six default boundary/friction/inflow/rainfall/building/mesh-region rows (no culvert row: `attach_input_layer` creates 'Culvert 01' on demand) |
| `convert_terrain_datum` | Make a copy of a terrain shifted from ellipsoid heights to EGM2008 (sea-level) heights, keeping the original; for a GeoTIFF still being uploaded, `finalize_terrain_upload(convert_to_egm2008=true)` does the same |
| `set_levels_datum` | Record which height datum the user's own levels are on, for a project whose terrain already exists (Editor); relays Hydrata's mismatch warning against the terrain's datum, never changes the terrain's own datum |
| `get_terrain` | Poll the terrain until it is ready (bounded; `timed_out` means call again); `phase` is never empty; summary = WGS84 bbox, elevation min/max, nodata fraction, native CRS; the link frames the DEM |
| `list_terrain_sources` | The terrain Hydrata can fetch for a site, finest first (Copernicus GLO-30 worldwide, finer regional LiDAR such as Geoscience Australia 5 m where it has data), with coverage, size cap, vertical datum and licence |
| `create_terrain_from_source` | Have Hydrata fetch, reproject and publish a DEM from one of those sources, then wait for the import like `get_terrain` |
| `merge_terrains` | Merge two or more of the project's terrains into one at a chosen cell size (highest priority first; gaps in a higher terrain are filled from the ones below), with a size estimate in grid cells; Hydrata's refusal (too large, a coarser terrain above a finer one) is returned unchanged |
| `get_terrain_merge` | Wait for a merge until the merged terrain is ready (bounded; `timed_out` means call again); `phase` is merging, importing, ready or error |
| `sample_terrain` | Ground level at points (null where the terrain has no data), to place a culvert, inflow or boundary on the ground |
| `terrain_profile` | Ground along a line (a cross-section) with the lowest sample, to find where a channel runs |
| `create_time_series` | Create a time series (rain gauge, hydrograph or tide/stage — `series_type` and `units` are top-level fields) from `{"rowData": [...]}`; a rainfall polygon binds to its gauge by the series name; summary = row count + first/last timestamp; the link opens the Hydrology panel |
| `update_time_series` | Correct a time series (PATCH); the same validation as create, and a series a built scenario uses sends that scenario back for rebuild and review |
| `delete_time_series` | Delete a time series (Manager role); refused while a layer feature still uses it, naming the layer and feature |
| `attach_input_layer` | Attach a GeoJSON dataset the agent already uploaded as the project's boundary/friction/inflow/rainfall/building/mesh-region or culvert layer (kind `structure` = deprecated alias of `building`); culvert attributes are mapped onto the culvert fields with defaults filled, and `culvert_problems` lists what the build will still refuse; breakline is refused; summary = feature count (WFS hits) + WGS84 bbox + whether the map now shows the layer; the link opens Inputs and frames the layer |
| `describe_input_kinds` | What each input layer kind looks like (geometry, properties, allowed values, a minimal example), generated from the definitions the build reads |
| `read_input_layer` | Read a project input layer's features as GeoJSON, each with its author (a person, or the agent that wrote it) |
| `write_input_layer` | Write an input layer (boundary, friction, inflow, rainfall, building, mesh region or culvert) from GeoJSON sent in the call; `target` writes a separate row for a variant |
| `request_input_upload` | The link where the person uploads a big input layer (over about 150 KB) in their browser: GeoJSON, zipped shapefile or GeoPackage up to 100 MB |
| `create_scenario` | Create a draft scenario and return its mesh-triangle estimate and price (`resolution` — scenario and mesh-region — is a length in metres); the link opens the scenario |
| `update_scenario` | Edit a scenario's name, description, resolution, duration or attached inputs; never builds or runs, the changed scenario is marked stale |
| `clone_scenario` | Copy a scenario into a sibling for a variant (no run copied, nothing built), with overrides checked before the copy is made |
| `build_scenario` | Build the scenario package; shows the estimate first and needs `confirm=true` above 100,000 triangles; polls `computed_status` to built; never re-posts a build that is in flight or already built (`rebuild=true` to force one); surfaces the server's 422 refusals (MESH_TOO_LARGE, BOUNDARY_NO_EXTERNAL, MESH_REGION_UNITS_UNMARKED) verbatim; a missing/empty/perimeter-less boundary sends the human to the map to draw or fix it; summary = triangle count + re-priced cost |
| `create_observation_layer` | Create a flood observation layer (project-level, never a scenario input) with its vertical datum (`terrain`, `AHD`, `EGM2008`, `ellipsoidal`, or `local` + `datum_offset_m`); waits for the map layer (`ready`, bounded); the link frames it in the Observations group |
| `list_observations` | A project's flood observation layers, or one layer's observations — `fid`, subtype, stage/depth, sigma, bound, observed window in UTC, datum status (`unknown` = not scored), `photo_count` and `problems` |
| `add_observations` | Add Flood marks, Timed levels, Gauge records and Dry points (up to 200 per call; `srid` for native coordinates, `entry_timezone` for naive times); the API validates each one, refused rows come back with the field that is wrong while the rest land |
| `update_observation` | Edit one observation (PATCH of the fields given; validated against the stored observation) |
| `delete_observation` | Delete one observation and its photos |
| `add_observation_photo_link` | Attach a photo to an observation as a reference link only (stored, never fetched); a photo FILE is refused with the UI path — the engineer uploads files in the photo strip on the observation's form in the map |
| `get_observation_comparison` | How a historic run compares with the project's flood observations: `headline` (figures at a tolerance, 0.3 m default; no pass or fail), `statistics` (n, within, bias, RMSE, weighted RMSE, MAE, max), Gauge record peak/timing/NSE, datum checks and one row per observation (residual = modelled - observed; unscored and excluded rows included). `layer_id` + `fid` narrow it to one observation with its exclusion history; Gauge records' modelled series are elided unless `include_series`. A design or unlabelled run reports `has_comparison: false`, not an error |
| `set_observation_exclusion` | Exclude one observation from a run's comparison with the engineer's reason (required: the tool refuses a blank one and the agent must ask), or include it again; logged with user and reason; returns `headline_after` |
| `set_comparison_tolerance` | Save the project's default comparison tolerance (metres, above 0 and at most 5; Editor+). `get_observation_comparison` uses it unless `tolerance` is passed, which saves nothing |
| `suggest_capability` | Tell the Hydrata team about something the person asked for that no tool can do (read-only; the reply gives the words to say to the person; 20 a day per person) |

### The hand-back: the engineer signs off in the map

Every tool that changes something (`create_project`, `finalize_terrain_upload`, `get_terrain`, `create_time_series`, `attach_input_layer`, `create_scenario`, `build_scenario`, `start_simulation`, `cancel_run`, `retry_run`, the five flood observation writes, and the three comparison tools) also returns:

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
