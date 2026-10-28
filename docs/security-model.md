# Security model

This page says what `hydrata-mcp-server` does with a caller's credential and with the data it returns. Every statement links to the code or the test that backs it. A statement with no citation is not made.

**Read at:** `hydrata-mcp-server` commit `075bd4192e1f384492b3d445e1d137e72b86d78c` (the server code last changed at `56364ea5d82ac510903ef339eb054d2bbb9d047f`) and `Hydrata/hydrata` (private) commit `45f2e1da390e4f998a5ea997fb1c1a363b701720` (`apps/agents` and `tests/unit/test_agents/test_oauth_tokens.py` last changed at `4b000aad7fc6bf285732a4cacc11e0a8653caac1`). Line numbers are for those commits. `hydrata:` paths are in the private Hydrata application repository; `mcp:` paths are in this repository.

## 1. Who verifies the credential

The MCP server holds no identity and no key store. It does two things with an `Authorization` header: it refuses a request whose header is not an accepted shape, and it forwards the accepted header, unchanged, to the Hydrata REST API. Hydrata verifies it.

| Claim | Evidence |
|---|---|
| The server checks only the shape of the credential: `Bearer` plus a token starting `hk_` (agent key), or, when the connect flow is configured, `Bearer` plus a three-part JWT-shaped token | `mcp: hydrata_mcp/server.py:54-78` |
| The server never validates a key or token itself; Django does, on every call | `mcp: hydrata_mcp/server.py:37-40` and `:66-72` (docstrings), `mcp: hydrata_mcp/server.py:113-155` |
| The accepted header is forwarded to the REST API as the caller sent it (only `Authorization` is picked out of the inbound headers) | `mcp: hydrata_mcp/client.py:127-147`; tests `mcp: tests/test_server.py:3284`, `mcp: tests/test_connect_token.py:55` |
| A request with no credential, HTTP Basic, a non-`hk_` non-JWT bearer token (such as a GeoNode OAuth token), or two `Authorization` headers is answered `401` (`-32001`, `WWW-Authenticate: Bearer realm="hydrata.com"`) before any tool runs, and is never forwarded | `mcp: hydrata_mcp/server.py:192-234`; tests `mcp: tests/test_server.py:3294`, `:3302`, `:3311`, `:3319`, `:3328` |
| This applies to every JSON-RPC method (`initialize`, `tools/list`, `ping`, `tools/call`), not only tool calls | `mcp: hydrata_mcp/server.py:186-188`; tests `mcp: tests/test_server.py:250`, `:270`, `:287`, `:303` |
| A connect-flow token whose `exp` has passed is answered `401` with `error="invalid_token"`; `exp` is read without verifying the signature, only to give the client a correct `401`. A token with no readable `exp` is left to Django | `mcp: hydrata_mcp/server.py:87-101` and `:210-212`; tests `mcp: tests/test_connect_token.py:138`, `:148`, `:158` |

Because the server forwards the credential, what protects a request is the verification in Hydrata, below.

## 2. How Hydrata verifies a connect-flow token (JWT)

When the one-click connect flow is on, Hydrata accepts an access token only if all of these hold:

| Check | Evidence | Test |
|---|---|---|
| Signed with RS256 only; a header with any other algorithm (including `none` and HS256) or without a `kid` is refused | `hydrata: apps/agents/oauth.py:50` and `:210-211` | `hydrata: tests/unit/test_agents/test_oauth_tokens.py:217` (`alg=none` and HS256 are 401) |
| Signature checked against the issuer's published keys (JWKS) | `hydrata: apps/agents/oauth.py:212` | `:209` (signed by another key is 401) |
| `iss` equals the configured issuer and `aud` equals the configured resource URL, as an exact string; `exp`, `iss`, `aud` and `sub` are required | `hydrata: apps/agents/oauth.py:51` and `:213-214` (`jwt.decode(... audience=resource_url(), issuer=issuer(), ... options={'require': REQUIRED_CLAIMS})`) | wrong audience `:183`; slashless resource `:191`; missing audience `:195`; wrong issuer `:199` |
| Expired beyond a 60 second clock leeway is refused | `hydrata: apps/agents/oauth.py:52` | `:203` |
| A token that verifies must also map to a live grant for a live user: no grant, a revoked grant and an inactive user are all `401` | `hydrata: apps/agents/oauth.py:221-269` | `:226`, `:230` (revoked grant is 401 on the next call), `:239` |
| Revocation is read on every call, so revoking a grant takes effect before the token's own expiry | `hydrata: apps/agents/oauth.py:5-10` and `:255-256` (docstrings) | `:230` |

The audience the token must carry is the MCP resource URL, set in Hydrata's settings (`hydrata: apps/agents/oauth.py:17-19`).

## 3. Agent keys

| Claim | Evidence |
|---|---|
| A key is shown once at creation; Hydrata stores a SHA-256 hash, not the key, and compares in constant time | `hydrata: apps/agents/models.py:45-46`, `:99-110` ("The raw key exists only here"), `:122-134` |
| A key belongs to a named grant at one of three levels, read < draft < launch | `hydrata: apps/agents/models.py:49-59` and `:135-136` |
| A grant below the level an endpoint needs is refused `403 AGENT_LEVEL_INSUFFICIENT`; a draft-level token cannot start a run | `hydrata: apps/agents/middleware.py:365-381` and `:515`; test `hydrata: tests/unit/test_agents/test_oauth_tokens.py:245` |
| A grant can be revoked, one at a time or all at once | `hydrata: apps/agents/models.py:143-165` |
| A GeoNode OAuth token or HTTP Basic is not accepted through the MCP server | `mcp: hydrata_mcp/server.py:41-45`; tests `mcp: tests/test_server.py:3294`, `:3302` |

## 4. Spending, consent and the pause switch

| Claim | Evidence |
|---|---|
| A launch grant has a monthly spend limit that defaults to $0 | `hydrata: apps/agents/models.py:64-65` and `:99` |
| A run that does not fit the limit is not started; it returns `CONSENT_REQUIRED` for the person to approve | `hydrata: apps/agents/consent.py:55` and `:136-148` |
| Consenting to a run's cost is a person's act in the browser: an agent credential cannot do it | `hydrata: apps/agents/middleware.py:117-122` (`v2-run-consent` is human-only) |
| An administrator switch can pause run launches, or all agent changes, for every agent; reads continue | `hydrata: apps/agents/killswitch.py:17`, `:63`, `:77-89` |

## 5. What the server returns

| Claim | Evidence |
|---|---|
| Presigned S3 links in results are elided, by key name (`s3_*_url`) and by value shape (`X-Amz-Signature`, `X-Amz-Security-Token`); `presign_terrain_upload` is the one tool that returns an upload URL, because the caller asked for it | `mcp: hydrata_mcp/server.py:551-608`; tests `mcp: tests/test_server.py:2197`, `:2227`, `:2265`, `:2372`, `:2517` |
| Text written by people (project and layer names, descriptions, logs) is wrapped in `<untrusted-data>` markers, a closing marker inside a value cannot break out, and a result carrying wrapped text leads with a note saying so | `mcp: hydrata_mcp/server.py:266-310`; tests `mcp: tests/test_data_envelope.py:112`, `:115`, `:128`, `:146` |
| Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) are hints only | `mcp: hydrata_mcp/server.py:443-448` (comment) |

The `<untrusted-data>` wrapping reduces the chance that an agent follows instructions planted in a shared project's text; it is not a guarantee. What stands behind it is in section 4: a launch needs a launch-level grant and a spend limit, and consent to cost is human-only.

## 6. The activity log

Writes made with an agent credential (requests other than `GET`, `HEAD` and `OPTIONS`), including refused ones, are recorded against the grant (`hydrata: apps/agents/middleware.py:513-529`, `hydrata: apps/agents/activity.py:56`, `:69-71` and `:96-105`; model `hydrata: apps/agents/models.py:332`). Reads are not recorded by this log. A grant that keeps being refused is summarised after a cap rather than logged row by row (`hydrata: apps/agents/activity.py:108-110`).

## What is not claimed here

This page makes no claim about rate limits, network placement, hosting or data retention: none is enforced by code in this repository or in the `apps/agents` package.
