# Security model

This page says what `hydrata-mcp-server` does with a caller's credential and with the data it returns. Every statement links to the code or the test that backs it. A statement with no citation is not made.

**Read at:** `hydrata-mcp-server` commit `075bd4192e1f384492b3d445e1d137e72b86d78c` (the server code last changed at `56364ea5d82ac510903ef339eb054d2bbb9d047f`) and `Hydrata/hydrata` (private) commit `45f2e1da390e4f998a5ea997fb1c1a363b701720` (`apps/agents` and `tests/unit/test_agents/test_oauth_tokens.py` last changed at `4b000aad7fc6bf285732a4cacc11e0a8653caac1`). Line numbers are for those commits. `hydrata:` paths are in the private Hydrata application repository; `mcp:` paths are in this repository. Each claim was checked against the cited lines by an independent reviewer before publication (28-10-26).

## 1. Who verifies the credential

The MCP server is a stateless protocol adapter in front of Hydrata's REST API. It holds no identity and no key store, and it validates no credential itself. It accepts a request only if it carries exactly one `Authorization` header of an accepted shape, and it forwards that header, unchanged, to Hydrata. In mechanism this is token passthrough: Hydrata, not this server, is where a credential is accepted or refused.

- **Connect-flow tokens** are issued for this MCP resource (`aud` = the MCP resource URL). Hydrata refuses a token that fails any check in section 2. Hydrata also accepts these tokens when they are presented directly to its agent API.
- **Agent keys** (`hk_…`) are Hydrata API credentials, not audience-bound tokens, and are valid against the REST API directly.
- **Requests that never reach Hydrata** (`initialize`, `tools/list`, `ping`) are answered after the shape check alone, so a caller with a well-formed but invalid credential can read the tool catalog. Reading project data, writing, or spending always goes through Hydrata's verification.

| Claim | Evidence |
|---|---|
| The server checks only the shape of the credential: `Bearer` plus a token starting `hk_` (agent key), or, when the connect flow is configured, `Bearer` plus three dot-separated base64url segments (the signature segment may be empty) | `mcp: hydrata_mcp/server.py:54-78` |
| The server never validates a key or token itself. Hydrata verifies it on every request the server makes to the REST API, which is every tool call; `initialize`, `tools/list` and `ping` are answered after the shape check alone | `mcp: hydrata_mcp/server.py:113-155`; test `mcp: tests/test_connect_token.py:98` (a shape-valid unsigned token gets the catalog) |
| The accepted `Authorization` header is forwarded to the REST API with its value unchanged. The server also relays the inbound `Mcp-Session-Id`, normalised to a UUID, as `X-Hydrata-Session`, and adds `X-Hydrata-Client` and `Host` | `mcp: hydrata_mcp/client.py:37`, `:127-147`; tests `mcp: tests/test_server.py:3284`, `mcp: tests/test_connect_token.py:55` |
| A request with no credential, HTTP Basic, a non-`hk_` non-JWT bearer token (such as a GeoNode OAuth token), or two `Authorization` headers is answered `401` (`-32001`, `WWW-Authenticate: Bearer realm="hydrata.com"`) before any tool runs, and is never forwarded | `mcp: hydrata_mcp/server.py:192-234`; tests `mcp: tests/test_server.py:250`, `:287`, `:3294`, `:3302`, `:3311`, `:3319`, `:3339`, `mcp: tests/test_connect_token.py:90` |
| That refusal applies to every JSON-RPC method (`initialize`, `tools/list`, `ping`, `tools/call`), not only tool calls | `mcp: hydrata_mcp/server.py:186-188`; tests `mcp: tests/test_server.py:250`, `:270`, `:287`, `:303` |
| A connect-flow token whose `exp` has passed is answered `401` with `error="invalid_token"`; `exp` is read without verifying the signature, only to give the client a correct `401`. A token with no readable `exp` is left to Hydrata | `mcp: hydrata_mcp/server.py:87-101` and `:210-212`; tests `mcp: tests/test_connect_token.py:138`, `:148`, `:158` |

### What the server records (TASK-3961)

"Holds no identity" means the server never decides who a caller is. It does write one JSON line to stdout (journald, then Loki) for every authenticated `tools/call`, and the identity in that line is Hydrata's answer, not the server's.

| Claim | Evidence |
|---|---|
| One `mcp_tool_call` line per `tools/call`, whether the call succeeds, is refused or raises. A failure to write the line never fails the call. Requests refused with `401` before FastMCP runs are not recorded here (nginx counts them) | `mcp: hydrata_mcp/telemetry.py:550` (`ToolCallRecorder`), `mcp: hydrata_mcp/_common.py:242`; tests `mcp: tests/test_telemetry.py` (`exactly_one_line_per_call`, `unauthenticated_requests_are_not_recorded_here`, `telemetry_failure_never_fails_the_call`) |
| Who the caller is comes from Hydrata's `X-Hydrata-Actor` reply header (user id, username, grant, credential type, internal flag); the first reply that carries it wins. With no such reply the line carries only a fingerprint of the credential (an `hk_` key's id prefix, never its secret part; or a hash of a JWT `sub`) and `identity_source=credential` | `mcp: hydrata_mcp/telemetry.py:88`, `:111`, `:137`; `mcp: hydrata_mcp/client.py:197`; tests `identity_copied_from_actor_header`, `first_reply_wins`, `without_a_reply_falls_back_to_the_credential_fingerprint` |
| `X-Request-ID` (the inbound one, else a minted one) is forwarded on every call to Hydrata so the Django log row, the nginx line and this record join | `mcp: hydrata_mcp/client.py:163`; test `forwards_the_inbound_request_id_on_every_call` |
| The outcome is `ok`, `refused` (any 4xx, the kill-switch `503 AGENT_PAUSED`, a returned result with `http_status` >= 400), `invalid` (unknown tool, bad arguments, a refusal raised with no status, a call the client cancelled in under 290 s with `error_code=CANCELLED`) or `broken` (any other 5xx, timeout, connect error, an unexpected exception, a cancelled call that ran 290 s or more). Alerts key on `broken` only | `mcp: hydrata_mcp/telemetry.py:201`, `:211`, `:245`; test `outcome_is_four_way_not_the_exception_class` |
| Argument values are kept only in a separate `kind=sample` line, chosen by a sampler: every refused, invalid or broken call, the first ok call per tool per person per day, at most 50 per person per day. Strings over 1000 characters, lists over 50 items, GeoJSON geometries, base64 blobs, keys, tokens and presigned URLs are cut down or removed, and a sample never exceeds 16 KB | `mcp: hydrata_mcp/telemetry.py:376` (`Sampler`), `:510` (`build_sample`); tests `mcp: tests/test_arg_sample.py` |
| The call line holds no tool result, no argument value, no token and no presigned URL. The only argument-derived fields are integer `project_id`, `scenario_id` and `run_id`. Error text is clipped to 300 characters and scrubbed | `mcp: hydrata_mcp/telemetry.py:168`; test `never_contains_arguments_tokens_or_presigned_urls` |

## 2. How Hydrata verifies a connect-flow token (JWT)

When the one-click connect flow is on, Hydrata accepts an access token only if all of these hold:

| Check | Evidence | Test |
|---|---|---|
| Signed with RS256 only; a header with any other algorithm (including `none` and HS256) or without a `kid` is refused | `hydrata: apps/agents/oauth.py:50` and `:210-211` | `hydrata: tests/unit/test_agents/test_oauth_tokens.py:217` (`alg=none` and HS256 are 401) |
| Signature checked against the issuer's published keys (JWKS) | `hydrata: apps/agents/oauth.py:212` | `:209` (signed by another key is 401) |
| `iss` equals the configured issuer, and `aud` is, or is a list containing, the configured resource URL (exact string match); `exp`, `iss`, `aud` and `sub` are required | `hydrata: apps/agents/oauth.py:51` and `:213-214` (`jwt.decode(... audience=resource_url(), issuer=issuer(), ... options={'require': REQUIRED_CLAIMS})`) | wrong audience `:183`; slashless resource `:191`; missing audience `:195`; wrong issuer `:199` |
| Expired beyond a 60 second clock leeway is refused | `hydrata: apps/agents/oauth.py:52` (leeway) and `:213-214` | `:203` |
| A token that verifies must also map to a live grant for a live user: no grant, a revoked grant and an inactive user are all `401` | `hydrata: apps/agents/oauth.py:221-269` | `:226`, `:230` (revoked grant is 401 on the next call), `:239` |
| Revocation is read on every call (there is no token or grant cache), so revoking a grant takes effect before the token's own expiry | `hydrata: apps/agents/oauth.py:263-264`, `hydrata: apps/agents/middleware.py:508` | `:230` |

The audience the token must carry is the MCP resource URL, read from Hydrata's settings (`hydrata: apps/agents/oauth.py:78-79`, `resource_url()`).

## 3. Agent keys and grants

Agent keys and connect-flow tokens both map to an agent grant, so the level, spend limit, pause switch and activity log below apply to both.

| Claim | Evidence |
|---|---|
| A key is shown once at creation; Hydrata stores a SHA-256 hash, not the key, looks it up by prefix and compares in constant time | `hydrata: apps/agents/views.py:75-78` (shown once), `hydrata: apps/agents/models.py:45-46`, `:99-110`, `:122-134` |
| A grant is at one of three levels, read < draft < launch | `hydrata: apps/agents/models.py:49-59` and `:135-136` |
| A grant below the level an endpoint needs is refused `403 AGENT_LEVEL_INSUFFICIENT`; a draft-level token cannot start a run | `hydrata: apps/agents/middleware.py:365-381` and `:515`; test `hydrata: tests/unit/test_agents/test_oauth_tokens.py:245` |
| A grant can be revoked, one at a time or all at once | `hydrata: apps/agents/models.py:143-165` |
| A GeoNode OAuth token or HTTP Basic is not accepted through the MCP server | `mcp: hydrata_mcp/server.py:203`; tests `mcp: tests/test_server.py:3294`, `:3302` |

## 4. Spending, consent and the pause switch

| Claim | Evidence |
|---|---|
| Every grant has a monthly spend limit that defaults to $0 | `hydrata: apps/agents/models.py:64-65` and `:99` |
| A simulation run started with an agent credential that does not fit the limit is not started; it returns `CONSENT_REQUIRED` for the person to approve. The limit covers starting a run only: other compute an agent can trigger (scenario build, terrain preparation, retry) is not counted against it | `hydrata: apps/agents/consent.py:136-148`, called from the start-run view; `hydrata: apps/agents/middleware.py:88-90` |
| Consenting to a run's cost is a person's act in the browser: an agent credential cannot do it | `hydrata: apps/agents/middleware.py:117-122` (`v2-run-consent` is human-only) |
| An administrator switch can pause run launches, or all agent changes, for every agent; reads continue. A change can take up to 5 seconds to be seen | `hydrata: apps/agents/killswitch.py:133-145`, `hydrata: apps/agents/middleware.py:516-523` |
| Launches pause automatically when quoted spend in a rolling hour passes a threshold | `hydrata: apps/agents/killswitch.py:27-37` |

## 5. What the server returns

| Claim | Evidence |
|---|---|
| Presigned S3 links in the read tools' results are elided, by key name (`s3_*_url`) and by value shape (`X-Amz-Signature`, `X-Amz-Security-Token`); `presign_terrain_upload` is the one tool that returns an upload URL, because the caller asked for it. Results of write tools are not filtered (none returns such a link today) | `mcp: hydrata_mcp/server.py:551-608`; tests `mcp: tests/test_server.py:2197`, `:2227`, `:2265`, `:2372`, `:2517` |
| Text written by people under the listed fields (project and layer names, descriptions, logs) is wrapped in `<untrusted-data>` markers, a closing marker inside a value cannot break out, and a result carrying wrapped text leads with a note saying so. The wrapping can be switched off by the operator (`HYDRATA_MCP_DATA_ENVELOPE`) | `mcp: hydrata_mcp/server.py:266-310` (field lists), `:328-372`, `:505-527`; `mcp: hydrata_mcp/config.py:39`, `:60`; tests `mcp: tests/test_data_envelope.py:112`, `:115`, `:128`, `:146` |
| Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) are hints only | `mcp: hydrata_mcp/server.py:443-448` (comment) |

The `<untrusted-data>` wrapping reduces the chance that an agent follows instructions planted in a shared project's text; it is not a guarantee. What stands behind it is in sections 3 and 4: starting a run needs a launch-level grant and fits the spend limit or waits for a person's consent.

## 6. The activity log

Writes made with an agent credential that Hydrata has recognised (requests other than `GET`, `HEAD` and `OPTIONS`), including refused ones, are recorded against the grant (`hydrata: apps/agents/middleware.py:513-529`, `hydrata: apps/agents/activity.py:56`, `:69-71` and `:96-105`; model `hydrata: apps/agents/models.py:332`). A request with an unknown, revoked or invalid credential is refused before it can be recorded (`hydrata: apps/agents/middleware.py:505-510`), and recording is best effort: a failure to write the log does not fail the request (`hydrata: apps/agents/activity.py:98-102`). Reads are not recorded by this log. A grant that keeps being refused is summarised after a cap (60 in 10 minutes) rather than logged row by row (`hydrata: apps/agents/activity.py:108-110`).

## What is not claimed here

This page makes no claim about request rate limits, network placement, hosting or data retention: none is enforced by code in this repository or in the `apps/agents` package.
