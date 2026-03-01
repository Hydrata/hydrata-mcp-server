"""Hydrata MCP Server — 9 hand-crafted tools for ANUGA flood simulation."""

import json
from contextlib import asynccontextmanager
from typing import Annotated

from fastmcp import FastMCP

from .client import HydrataClient
from .config import Config

config = Config.from_env()
client = HydrataClient(config)


@asynccontextmanager
async def lifespan(server):
    """Close the httpx connection pool on shutdown."""
    yield
    await client.close()


mcp = FastMCP(
    "Hydrata",
    instructions=(
        "Hydrata is a geospatial hydraulic modeling platform. Use these tools to "
        "manage ANUGA flood simulation projects, scenarios, and runs. "
        "A typical workflow is: list_projects → get_scenario → start_simulation → "
        "poll get_run_status until complete → get_run for results."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Tool 1: list_projects
# ---------------------------------------------------------------------------
@mcp.tool
async def list_projects(
    page: Annotated[int, "Page number (default 1)"] = 1,
    page_size: Annotated[int, "Results per page, max 100 (default 100)"] = 100,
) -> str:
    """List ANUGA simulation projects accessible to the authenticated user.

    Returns a paginated list of projects with their names, projections,
    and base map references.
    """
    data = await client.get("/projects/", params={"page": page, "page_size": page_size})
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 2: get_project
# ---------------------------------------------------------------------------
@mcp.tool
async def get_project(
    project_id: Annotated[int, "The project ID"],
) -> str:
    """Get details of a specific ANUGA project including its scenarios.

    Returns the project name, projection (EPSG code), base map ID,
    and configuration.
    """
    data = await client.get(f"/projects/{project_id}/")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 3: get_scenario
# ---------------------------------------------------------------------------
@mcp.tool
async def get_scenario(
    project_id: Annotated[int, "The project ID"],
    scenario_id: Annotated[int, "The scenario ID"],
) -> str:
    """Get scenario details including its current status and latest run.

    The status field is computed from the latest run and will be one of:
    created, building, built, queued, computing, processing, complete,
    error, or cancelled. A scenario must be in 'built' status before
    it can be run.
    """
    data = await client.get(f"/projects/{project_id}/scenarios/{scenario_id}/")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 4: start_simulation
# ---------------------------------------------------------------------------
@mcp.tool
async def start_simulation(
    scenario_id: Annotated[int, "The scenario ID to run"],
    compute_backend: Annotated[
        str,
        "Compute backend: 'local' (Celery), 'ec2' (dedicated instance), "
        "or 'batch' (AWS Batch spot, cheapest). Default: 'local'",
    ] = "local",
) -> str:
    """Start a flood simulation run for a built scenario.

    The scenario must be in 'built' status. Returns 202 with the new run.
    The run transitions through: built → queued → computing → processing → complete.

    After starting, poll get_run_status to track progress. Returns 409
    if the scenario is not in the correct state.
    """
    body, status_code = await client.post(
        f"/scenarios/{scenario_id}/run/",
        json={"compute_backend": compute_backend},
    )
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 5: get_run_status
# ---------------------------------------------------------------------------
@mcp.tool
async def get_run_status(
    run_id: Annotated[int, "The run ID"],
) -> str:
    """Lightweight status check for a simulation run (fast, <50ms).

    Use this for polling instead of get_run. Returns only: id, status,
    progress_pct (0-100), eta_seconds, error_message, and compute_backend.

    Poll every 5-10 seconds. Terminal states: complete, error, cancelled.
    """
    data = await client.get(f"/runs/{run_id}/status/")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 6: get_run
# ---------------------------------------------------------------------------
@mcp.tool
async def get_run(
    run_id: Annotated[int, "The run ID"],
) -> str:
    """Get full details of a simulation run including timing and results.

    Returns the complete run record: status, progress, timing (start/end
    timestamps, duration), compute details (backend, instance type, cost),
    mesh info, error messages, and result log. Use get_run_status for
    lightweight polling; use this for final results.
    """
    data = await client.get(f"/runs/{run_id}/")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Tool 7: cancel_run
# ---------------------------------------------------------------------------
@mcp.tool
async def cancel_run(
    run_id: Annotated[int, "The run ID to cancel"],
) -> str:
    """Cancel an in-flight simulation run.

    Works on runs in built, queued, or computing status. Cleans up
    compute resources (terminates EC2 instance, Celery task, or Batch job).

    Returns 409 if the run is already in a terminal state
    (complete, cancelled, or error).
    """
    body, status_code = await client.post(f"/runs/{run_id}/cancel/")
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 8: retry_run
# ---------------------------------------------------------------------------
@mcp.tool
async def retry_run(
    run_id: Annotated[int, "The run ID to retry"],
) -> str:
    """Retry a failed simulation run.

    Resets an errored run back to 'created' status and triggers a new
    package build. The same run ID is reused. Only valid when
    status is 'error'. Returns 409 for any other state.
    """
    body, status_code = await client.post(f"/runs/{run_id}/retry/")
    if not isinstance(body, dict):
        body = {"response": body}
    body["http_status"] = status_code
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------
# Tool 9: list_runs
# ---------------------------------------------------------------------------
@mcp.tool
async def list_runs(
    project_id: Annotated[int, "The project ID"],
    status_filter: Annotated[
        str | None,
        "Filter by status: created, building, built, queued, computing, "
        "processing, complete, error, cancelled. Omit for all.",
    ] = None,
    page: Annotated[int, "Page number (default 1)"] = 1,
    page_size: Annotated[int, "Results per page, max 100 (default 100)"] = 100,
) -> str:
    """List all simulation runs across all scenarios in a project.

    Returns a paginated list of runs. Optionally filter by status
    to find active, completed, or failed runs.
    """
    params: dict = {"page": page, "page_size": page_size}
    if status_filter:
        params["status"] = status_filter
    data = await client.get(f"/projects/{project_id}/runs/", params=params)
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# ASGI app factory + CLI entry point
# ---------------------------------------------------------------------------
def create_app():
    """Create ASGI application for uvicorn."""
    return mcp.http_app(path="/", stateless_http=True)


# Module-level ASGI app for `uvicorn hydrata_mcp.server:app`
app = create_app()


def main():
    """CLI entry point: `hydrata-mcp`."""
    mcp.run(transport="http", host=config.host, port=config.port)


if __name__ == "__main__":
    main()
