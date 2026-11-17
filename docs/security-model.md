# Security model

This page says what `hydrata-mcp-server` does with a caller's credential and with the data it returns. Every statement links to the code or the test that backs it. A statement with no citation is not made.

**Read at:** `hydrata-mcp-server` commit `d600e822e0fbfd099ee70c9904c45c52a9e9e4ef` and `Hydrata/hydrata` (private) commit `25ee7168ea487a1a832327162b37ed8abb8d1cfc` (`apps/agents` and `tests/unit/test_agents/test_oauth_tokens.py` last changed at `5e5728529ad5ea29019974d5c5499072892cb938`). Line numbers are for those commits. `hydrata:` paths are in the private Hydrata application repository; `mcp:` paths are in this repository. Re-read for TASK-4272 (17-11-26); every cited line is re-checked by an independent reviewer before this page is published.

## 1. Who verifies the credential

The MCP server is an OAuth protected resource (a resource server, RFC 9728) in front of Hydrata's REST API, and the connect-flow tokens it accepts are issued for it: their audience is the MCP resource URL. It does not check a token's audience itself: it forwards any credential of an accepted shape, and Hydrata refuses a connect-flow token that was not issued for this MCP resource (section 2); any other kind of token is refused here (below). Because Hydrata's agent API also accepts these tokens directly (below), the MCP server and that API share one audience. It keeps no key store and no session, and it does not check a signature itself. It checks that a request carries exactly one `Authorization` header of an accepted shape, and it sends that header with each call it makes to Hydrata, where Django verifies it on every request (section 2). Django holds the keys, the grants and the revocations, so a credential is accepted or refused there, and a tool only ever acts as the person and grant that Django verified.

- **Connect-flow tokens** are issued for this MCP resource (`aud` = the MCP resource URL). Hydrata refuses a token that fails any check in section 2. Hydrata also accepts these tokens when they are presented directly to its agent API.
- **Agent keys** (`hk_…`) are Hydrata API credentials, not audience-bound tokens, and are valid against the REST API directly.
- **Requests that never reach Hydrata** (`initialize`, `tools/list`, `ping`) are answered after the shape check alone, so a caller with a well-formed but invalid credential can read the tool catalog. Reading project data, writing, or spending always goes through Hydrata's verification.

| Claim | Evidence |
|---|---|
| The server checks only the shape of the credential: `Bearer` plus a token starting `hk_` (agent key), or, when the connect flow is configured (`HYDRATA_MCP_RESOURCE_URL`), `Bearer` plus three dot-separated base64url segments (the signature segment may be empty) | `mcp: hydrata_mcp/server.py:55-79` (`_is_agent_key`, `_is_connect_token`) |
| The server never validates a key or a token's signature itself. Hydrata verifies the credential on every request the server makes to the REST API, which is every tool call; `initialize`, `tools/list` and `ping` are answered after the shape check alone | `mcp: hydrata_mcp/server.py:239-256`, `:292`; test `mcp: tests/test_connect_token.py:53` (a shape-valid JWT is forwarded, not checked); test `mcp: tests/test_app.py:92` (every catalog method is answered for a key-shaped credential) |
| The accepted `Authorization` header is sent to the REST API with its value unchanged. The server also relays the inbound `Mcp-Session-Id`, normalised to a UUID, as `X-Hydrata-Session`, and adds `X-Hydrata-Client`, `X-Request-ID` and, when configured, `Host` | `mcp: hydrata_mcp/client.py:30-55`, `:229-251`; tests `mcp: tests/test_app.py:108`, `:247`, `:161`, `mcp: tests/test_connect_token.py:53` |
| A request with no credential, HTTP Basic, a non-`hk_` non-JWT bearer token (such as a GeoNode OAuth token), or two `Authorization` headers is answered `401` (`-32001`, `WWW-Authenticate: Bearer realm="hydrata.com"`, plus `resource_metadata` when the connect flow is on) before any tool runs, and is never forwarded | `mcp: hydrata_mcp/server.py:119-125`, `:239-250`, `:257-276`; tests `mcp: tests/test_app.py:47`, `:493`, `:501`, `:510`, `:540`, `mcp: tests/test_connect_token.py:47`, `:68` |
| That refusal applies to every JSON-RPC method (`initialize`, `tools/list`, `ping`, `tools/call`), not only tool calls, with one exception for allowlisted catalog crawlers (below, TASK-4177) that never covers `tools/call` | `mcp: hydrata_mcp/server.py:218-222`; tests `mcp: tests/test_app.py:65`, `:82`, `:99`, `:553`, `mcp: tests/test_connect_token.py:89` |

### Token expiry and long calls (TASK-4204, TASK-4223)

A connect-flow access token is short-lived and a tool call keeps the token it started with. Three rules keep an expiry from looking like a revocation:

| Claim | Evidence |
|---|---|
| A connect-flow token whose `exp` has passed (however long ago) is answered `401` here with `error="invalid_token"`, so the client refreshes it. `exp` is read without verifying the signature, only to give the client a correct `401` and to time waits; it is never used to accept a request. A token with no readable numeric `exp` is left to Hydrata | `mcp: hydrata_mcp/server.py:88-101`, `:251-255`; `mcp: hydrata_mcp/client.py:75-86` (`bearer_exp`); tests `mcp: tests/test_connect_token.py:102`, `:139`, `:149`, `:160`, `mcp: tests/test_token_expired.py:166`, `:185` |
| Every wait inside a tool call (`get_run_status` with `wait_seconds`, the build and terrain-import polls) stops 20 seconds before the call's token expires and returns the tool's normal resumable result. If the token is already inside that margin when the wait starts, the tool does one read and its next step tells the agent NOT to call again straight away, but after the person's next message or a pause of about 30 seconds (`TOKEN_REFRESH_NEXT_STEP`). The last poll's HTTP request can still run up to its 30-second timeout past the cap. An agent key, which has no `exp`, is not capped | `mcp: hydrata_mcp/client.py:57-107` (`TOKEN_EXPIRY_MARGIN_SECONDS`, `token_wait_budget`), `:224` (request timeout); `mcp: hydrata_mcp/_helpers.py:145-162` (`TOKEN_REFRESH_NEXT_STEP`), `:170-199` (`_poll_until`); tests `mcp: tests/test_token_expired.py:91`, `:108`, `:192`, `:203`, `:212` |
| Hydrata answers a token that failed only on its age, within the last hour, on a grant that is still live, with `401 AGENT_TOKEN_EXPIRED`; anything else (older, another audience or issuer, a bad signature, a revoked grant, an inactive user) gets the `AGENT_KEY_INVALID` body byte for byte, so the holder of an old token learns nothing about the grant | `hydrata: apps/agents/oauth.py:56-64`, `:392-420` (`expired_on_live_grant`); `hydrata: apps/agents/middleware.py:401-405`, `:584-588`; tests `hydrata: tests/unit/test_agents/test_oauth_tokens.py:311`, `:331`, `:343`, `:353`, `:378`, `:385` |
| The server relays `AGENT_TOKEN_EXPIRED` as a resumable result (`outcome: token_expired`, `retryable: true`) that says nothing was revoked and never asks the person to reconnect; only Hydrata's `AGENT_KEY_INVALID` carries the revoked wording | `mcp: hydrata_mcp/client.py:110-127` (`token_expired_result`), `mcp: hydrata_mcp/_common.py:431-433`, `:457-471`; tests `mcp: tests/test_token_expired.py:233`, `:245`, `:265`, `:275` |

### Anonymous catalog discovery for MCP catalog crawlers (TASK-4177)

The one exception to "every request needs a credential". MCP catalogs (mcptry.com, Smithery) check a server by listing its tools without credentials; under the rule above they saw only a `401`. A request is now let through with no credential only when all of these hold:

- it carries **no** `Authorization` header at all (a request that carries one, even a blank or malformed one, follows the rules above unchanged, so a bad token still gets `401`);
- its `User-Agent` contains, ignoring case, one of the configured crawler substrings: `HYDRATA_MCP_DISCOVERY_USER_AGENTS`, comma-separated, default `mcptry.com indexer,SmitheryBot`; set to an empty value it turns the exception off;
- **every** JSON-RPC message in the body is one of `initialize`, `notifications/initialized`, `ping`, `tools/list`. A `tools/call`, any other method, or a batch holding one is answered with the same `401` and `WWW-Authenticate` challenge (including `resource_metadata`) as any anonymous request.

Why a `User-Agent` match, which anyone can send, is acceptable:

- **The catalog is public anyway.** The README lists every tool, and `tools/list` is answered from the statically registered tools without any call to Hydrata. A client that fakes a crawler `User-Agent` learns only that public list.
- **`tools/call` always needs a credential.** Reading project data, writing and spending are never reachable this way; they still go through the shape check here and Hydrata's verification.
- **Real clients keep the OAuth default (TASK-3987).** claude.ai, ChatGPT, Claude Code, Cursor and VS Code never send a crawler `User-Agent`, so their first `initialize` still gets the `401` that tells them the server needs sign-in. The exception is an allowlist, not a general anonymous `initialize`, for exactly this reason.

Anonymous discovery adds no telemetry line: `ToolCallRecorder` records `tools/call` only.

| Claim | Evidence |
|---|---|
| The exception needs no `Authorization` header, an allowlisted `User-Agent` (case-insensitive substring) and only discovery methods | `mcp: hydrata_mcp/server.py:105-116` (`DISCOVERY_METHODS`, `_is_discovery_crawler`), `:229-236` (`RequireAuth.__call__`); tests `mcp: tests/test_discovery.py` (`crawler_discovery_method_is_200`, `crawler_match_is_case_insensitive_substring`) |
| `tools/call`, other methods, a mixed batch and any `Authorization` header still get `401` | tests `mcp: tests/test_discovery.py` (`crawler_tools_call_is_401_with_todays_challenge`, `crawler_tools_call_challenge_names_the_resource_metadata`, `crawler_other_methods_are_401`, `crawler_batch_with_a_tools_call_is_401`, `crawler_with_any_authorization_header_follows_todays_rules`) |
| Any other `User-Agent` (including an empty one) gets `401` exactly as before | test `mcp: tests/test_discovery.py` (`default_user_agent_is_401_exactly_as_today`) |
| The allowlist comes from `HYDRATA_MCP_DISCOVERY_USER_AGENTS`; empty disables it | `mcp: hydrata_mcp/config.py` (`DEFAULT_DISCOVERY_USER_AGENTS`, `_discovery_user_agents`); tests `crawler_allowlist_env_override`, `crawler_allowlist_empty_disables`, `crawler_allowlist_default` |
| Listing the catalog makes no request to Hydrata, and discovery writes no telemetry line | tests `crawler_discovery_method_is_200` (no outbound request), `crawler_discovery_is_not_recorded_by_telemetry` |

### The Server Card (TASK-4177)

`GET <mcp-url>/server-card` (`https://hydrata.com/mcp/server-card`) serves a static MCP Server Card (SEP-2127) built from this repository's `server.json`: name, title, description, version, website, repository, icons and the remote endpoint. It holds no tools (SEP-2127 leaves them to `tools/list`), no credential and nothing per-user, and needs no credential (`RequireAuth` gates only `POST`, `mcp: hydrata_mcp/server.py:193`). Evidence: `mcp: hydrata_mcp/server.py:320-350` (`build_server_card`, `server_card`); tests `mcp: tests/test_discovery.py` (`TestServerCard`).

`GET <mcp-url>/icon.png` (`https://hydrata.com/mcp/icon.png`) serves the catalog icon, a static 512×512 PNG shipped in the package (`hydrata_mcp/assets/icon.png`), on the server's own origin. It needs no credential, carries nothing per-user, and is the same icon listed in `server.json` and `serverInfo.icons`. Evidence: `mcp: hydrata_mcp/server.py:357-375` (`icon`), `mcp: hydrata_mcp/_common.py` (`ICON`); tests `mcp: tests/test_discovery.py` (`TestIcon`).

### What the server records (TASK-3961)

"Keeps no session" means the server never decides who a caller is. It does write one JSON line to stdout (journald, then Loki) for every authenticated `tools/call`, and the identity in that line is Hydrata's answer, not the server's.

| Claim | Evidence |
|---|---|
| One `mcp_tool_call` line per `tools/call`, whether the call succeeds, is refused or raises. A failure to write the line never fails the call. Requests refused with `401` before FastMCP runs are not recorded here (nginx counts them) | `mcp: hydrata_mcp/telemetry.py:636` (`ToolCallRecorder`); tests `mcp: tests/test_telemetry.py:73` (`exactly_one_line_per_call`), `:328` (`unauthenticated_requests_are_not_recorded_here`), `:336` (`telemetry_failure_never_fails_the_call`) |
| Who the caller is comes from Hydrata's `X-Hydrata-Actor` reply header (user id, username, grant, credential type, internal flag); the first reply that carries it wins. With no such reply the line carries only a fingerprint of the credential (an `hk_` key's id prefix, never its secret part; or a hash of a JWT `sub`) and `identity_source=credential`. A call rejected before Django (wrong arguments) takes the user Django last verified for the same credential from a bounded in-process cache (`ActorCache`, 5000 entries, least recently used evicted: a lookup moves an entry to the end) keyed by the sha256 of the FULL bearer token, never the `sub` fingerprint (this server verifies no signature, so a forged token with a known `sub` must miss); the record then carries `identity_source=credential_cache`. Only the digest is held and it is never emitted. A Django reply without an actor header (revoked key, expired token) bypasses the cache and forgets the entry; a restart empties it | `mcp: hydrata_mcp/telemetry.py:82` (`CallState`), `:105` (`parse_actor`), `:128` (`note_reply`), `:157` (`credential_identity`), `:415` (`ActorCache`); `mcp: hydrata_mcp/client.py:280`; tests `mcp: tests/test_telemetry.py:181` (`identity_copied_from_actor_header`), `:219` (`first_reply_wins`), `:234` (`without_a_reply_falls_back_to_the_credential_fingerprint`), `:510`-`:605` (`credential_cache_*`: `carries_the_verified_user_onto_a_pre_django_failure`, `miss_before_any_verified_call`, `spoofed_jwt_is_a_miss`, `spoofed_key_with_the_same_prefix_is_a_miss`, `forgets_on_a_reply_without_an_actor`, `is_bounded_and_empty_after_restart`, `evicts_least_recently_used`, `never_emits_the_token_or_its_digest`, `sample_line_and_sampler_use_the_users_identity`) |
| `X-Request-ID` (the inbound one, else a minted one) is forwarded on every call to Hydrata so the Django log row, the nginx line and this record join | `mcp: hydrata_mcp/client.py:244`, `mcp: hydrata_mcp/telemetry.py:94-102`; test `mcp: tests/test_telemetry.py:259` (`forwards_the_inbound_request_id_on_every_call`) |
| The outcome is `ok`, `refused` (any 4xx, the kill-switch `503 AGENT_PAUSED`, a returned result with `http_status` >= 400), `invalid` (unknown tool, bad arguments, a refusal raised with no status, a call the client cancelled in under 120 s with `error_code=CANCELLED`) or `broken` (any other 5xx, timeout, connect error, an unexpected exception, a cancelled call that ran 120 s or more). Alerts key on `broken` only | `mcp: hydrata_mcp/telemetry.py:215` (`classify_status`), `:225` (`classify_exception`), `:259` (`classify_result`); test `mcp: tests/test_telemetry.py:85` (`outcome_is_four_way_not_the_exception_class`) |
| Argument values are kept only in a separate `kind=sample` line, chosen by a sampler: refused, invalid or broken calls and the first ok call per tool per person per day, at most 50 per person per day in all (refused, invalid and broken calls count toward the 50), and none for a new person once 5000 people have been sampled that day. `suggest_capability` is always sampled, outside the cap. Strings over 1000 characters, lists over 50 items, GeoJSON geometries, base64 blobs, keys, tokens and presigned URLs are cut down or removed, and a sample never exceeds 16 KB | `mcp: hydrata_mcp/telemetry.py:393-395` (`SAMPLE_MAX_IDENTITIES`, `ALWAYS_SAMPLED_TOOLS`), `:461` (`Sampler`), `:473-484` (`decide`), `:595` (`build_sample`); tests `mcp: tests/test_arg_sample.py` |
| The call line holds no tool result, no argument value, no token and no presigned URL. The only argument-derived fields are integer `project_id`, `scenario_id` and `run_id`. Error text is clipped to 300 characters and scrubbed | `mcp: hydrata_mcp/telemetry.py:188` (`scrub`), `:279` (`_int_arg`); test `mcp: tests/test_telemetry.py:303` (`never_contains_arguments_tokens_or_presigned_urls`) |

## 2. How Hydrata verifies a connect-flow token (JWT)

When the one-click connect flow is on, Hydrata accepts an access token only if all of these hold:

| Check | Evidence | Test |
|---|---|---|
| Signed with RS256 only; a header with any other algorithm (including `none` and HS256) or without a `kid` is refused | `hydrata: apps/agents/oauth.py:50` and `:221-222` | `hydrata: tests/unit/test_agents/test_oauth_tokens.py:217` (`alg=none` and HS256 are 401) |
| Signature checked against the issuer's published keys (JWKS) | `hydrata: apps/agents/oauth.py:223` (`_signing_key`) | `hydrata: tests/unit/test_agents/test_oauth_tokens.py:209` (signed by another key is 401) |
| `iss` equals the configured issuer, and `aud` is, or is a list containing, the configured resource URL (exact string match; PyJWT's default audience check, `strict_aud` not set: TASK-4071 recorded why); `exp`, `iss`, `aud` and `sub` are required | `hydrata: apps/agents/oauth.py:51` and `:224-225` (`jwt.decode(... audience=resource_url(), issuer=issuer(), ... options={'require': REQUIRED_CLAIMS, ...})`) | `hydrata: tests/unit/test_agents/test_oauth_tokens.py:183` (wrong audience), `:191` (slashless resource), `:195` (missing audience), `:199` (wrong issuer) |
| Expired beyond a 60 second clock leeway is refused (and answered as in section 1, Token expiry) | `hydrata: apps/agents/oauth.py:52` (leeway) and `:224-225` | `hydrata: tests/unit/test_agents/test_oauth_tokens.py:203`, `:401` (a token inside the leeway still works) |
| A token that verifies must also map to a live grant for a live user: no grant, a revoked grant and an inactive user are all `401` | `hydrata: apps/agents/oauth.py:232-289` (`grant_for_claims`, `_binding`: `revoked_at__isnull=True, user__is_active=True`) | `hydrata: tests/unit/test_agents/test_oauth_tokens.py:226`, `:230` (revoked grant is 401 on the next call), `:239` |
| Revocation is read on every call (there is no token or grant cache), so revoking a grant takes effect before the token's own expiry | `hydrata: apps/agents/oauth.py:283-289`, `:381` (`grant_for_token`); `hydrata: apps/agents/middleware.py:584` | `hydrata: tests/unit/test_agents/test_oauth_tokens.py:230` |

The audience the token must carry is the MCP resource URL, read from Hydrata's settings (`hydrata: apps/agents/oauth.py:85-86`, `resource_url()`).

## 3. Agent keys and grants

Agent keys and connect-flow tokens both map to an agent grant, so the level, spend limit, pause switch, person-only acts and activity log below apply to both.

| Claim | Evidence |
|---|---|
| A key is shown once at creation; Hydrata stores a SHA-256 hash, not the key, looks it up by prefix and compares in constant time | `hydrata: apps/agents/views.py:78-81` (shown once), `hydrata: apps/agents/models.py:45-46`, `:99-109`, `:122-132` |
| A grant is at one of three levels, read < draft < launch | `hydrata: apps/agents/models.py:50-59` and `:135-136` |
| A grant below the level an endpoint needs is refused `403 AGENT_LEVEL_INSUFFICIENT`; a draft-level token cannot start a run | `hydrata: apps/agents/middleware.py:419-436` and `:602-603`; test `hydrata: tests/unit/test_agents/test_oauth_tokens.py:245` |
| A grant can be revoked, one at a time or all at once | `hydrata: apps/agents/models.py:143-165` |
| A GeoNode OAuth token or HTTP Basic is not accepted through the MCP server | `mcp: hydrata_mcp/server.py:244-250`; tests `mcp: tests/test_app.py:493`, `:501` |

## 4. Spending, consent and the pause switch

| Claim | Evidence |
|---|---|
| Every grant has a monthly spend limit that defaults to $0 | `hydrata: apps/agents/models.py:64-65` and `:99` |
| A simulation run started with an agent credential that does not fit the limit is not started; it returns `CONSENT_REQUIRED` for the person to approve. The limit covers starting a run only: other compute an agent can trigger (scenario build, terrain preparation, retry) is not counted against it | `hydrata: apps/agents/consent.py:137-149` (`check_monthly_limit`), `:169` (`consent_required`), called from the start-run view at `hydrata: apps/gn_anuga/api_v2.py:6498` (`check_monthly_limit`) and `:6593` (`consent_required`) |
| Consenting to a run's cost is a person's act in the browser: an agent credential cannot do it | `hydrata: apps/agents/middleware.py:118-123` (`v2-run-consent` is human-only) and `:332-334` |
| An administrator switch can pause run launches, or all agent changes, for every agent; reads continue. A change can take up to 5 seconds to be seen | `hydrata: apps/agents/killswitch.py:61`, `:133-141`; `hydrata: apps/agents/middleware.py:604-613` |
| Launches pause automatically when quoted spend in a rolling hour passes a threshold | `hydrata: apps/agents/killswitch.py:27-37`, `:213` (`auto_trip_if_over`) |

## 5. What only a person can do: input review and publishing

An agent can write a project's inputs, but nothing it wrote is built or run until a person has looked at it, and nothing it made becomes public until a person publishes it.

| Claim | Evidence |
|---|---|
| A build (and a retry) is refused `409 UNREVIEWED_INPUTS` while any attached input an agent wrote is unreviewed; a run is refused `409 UNREVIEWED_INPUTS`, then `409 RUN_INVALIDATED` when the built package is not what the person reviewed. A person's later edit re-opens the review; an input that cannot be read stays unreviewed | `hydrata: apps/gn_anuga/input_review.py:1-40` (rules), `:335` (`unreviewed_refusal`), `:344` (`run_refusal`); called at `hydrata: apps/gn_anuga/api_v2.py:438`, `:4185`, `:5882-5888`; tests `hydrata: tests/unit/test_gn_anuga/test_input_review.py:392`, `:450` |
| Reviewing inputs is the person's act in the browser: an agent credential's review write is refused `403 AGENT_CANNOT_REVIEW_INPUTS` (and logged) before the view runs, at every level | `hydrata: apps/agents/middleware.py:125-131`, `:332-334`, `:439-440`, `:448-449`; test `hydrata: tests/unit/test_gn_anuga/test_input_review.py:197` |
| The server relays those refusals as a result keyed on `error_code`, with the link to the review screen; it never waits inside a call for the person to review | `mcp: hydrata_mcp/_helpers.py:294-353` (`_input_review_hand_back`); tests `mcp: tests/test_unreviewed_relay.py:97`, `:113`, `:136`, `:145` |
| A project an agent credential creates starts unpublished: only its owner and members can read it, and to anyone else it is the same `404` as a project that does not exist | created unpublished: `hydrata: apps/gn_anuga/api_v2.py:1040-1047`; the same `404`: `hydrata: apps/gn_anuga/models/project.py:159` (`publicly_readable_q`), `hydrata: apps/gn_anuga/api_v2.py:809` (`viewable_project_or_404`); tests `hydrata: tests/unit/test_gn_anuga/test_unpublished_projects.py:108`, `:132`, `:147` |
| Publishing a project is the person's act in the browser session: an agent credential is refused at every level (`hydrata: apps/agents/middleware.py:184-187`, `:375-376`) and the view refuses anything but the session on its own | `hydrata: apps/gn_anuga/api_v2.py:1053-1091` (`publish`); tests `hydrata: tests/unit/test_gn_anuga/test_unpublished_projects.py:368`, `:381`, `:393`, `:399` |

## 6. What the server returns

| Claim | Evidence |
|---|---|
| Presigned S3 links in the read tools' results are elided, by key name (`s3_*_url`) and by value shape (`X-Amz-Signature`, `X-Amz-Security-Token`); `presign_terrain_upload` is the one tool that returns an upload URL, because the caller asked for it | `mcp: hydrata_mcp/_helpers.py:57-68`, `:109-128` (`_elide_presigned`); tests `mcp: tests/test_presigned_elision.py:32`, `:62`, `:85`, `:146`, `:223` |
| Text written by people under the listed fields (project and layer names, descriptions, logs, attribute values, an observation exclusion's reason) is wrapped in `<untrusted-data>` markers, a closing marker inside a value cannot break out, and a result carrying wrapped text leads with a note saying so. The wrapping is applied where the agent reads the result (the registered tool). The operator can switch it off (`HYDRATA_MCP_DATA_ENVELOPE`) | `mcp: hydrata_mcp/_common.py:80-129` (field lists), `:132-177`, `:470-471`; `mcp: hydrata_mcp/calibration.py` (`_reason_as_data`, TASK-4071); `mcp: hydrata_mcp/config.py:54`, `:81`; tests `mcp: tests/test_data_envelope.py:119`, `:122`, `:141`, `:146`, `mcp: tests/test_exclusion_reason_envelope.py` |
| Tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) are hints only | `mcp: hydrata_mcp/_common.py:338-343` (comment) |

The `<untrusted-data>` wrapping reduces the chance that an agent follows instructions planted in a shared project's text; it is not a guarantee. What stands behind it is in sections 3 to 5: starting a run needs a launch-level grant and fits the spend limit or waits for a person's consent, and nothing an agent wrote is built or run before a person reviews it.

## 7. The activity log

Writes made with an agent credential that Hydrata has recognised (requests other than `GET`, `HEAD` and `OPTIONS`), including refused ones, are recorded against the grant (`hydrata: apps/agents/middleware.py:598-603`, `:646-650`; model `hydrata: apps/agents/models.py:394`). Writes made with a password (HTTP Basic) by a signed-in script are recorded too (`hydrata: apps/agents/middleware.py:573-580`). A request with an unknown, revoked or invalid credential is refused before it can be recorded (`hydrata: apps/agents/middleware.py:584-588`), and recording is best effort: a failure to write the log does not fail the request (`hydrata: apps/agents/activity.py:97-103`). Reads are not recorded by this log (`hydrata: apps/agents/activity.py:9`). A grant that keeps being refused is summarised after a cap (60 in 10 minutes) rather than logged row by row (`hydrata: apps/agents/activity.py:65-66`, `:160-182`).

## What is not claimed here

This page makes no claim about request rate limits, network placement, hosting or data retention: none is enforced by code in this repository or in the `apps/agents` package.
