"""
MCP Server for Newscatcher CatchAll API

This server provides tools to interact with the Newscatcher CatchAll API.
Users can provide their API key via (in order of precedence):
1. URL query parameter: ?apiKey=YOUR_KEY (recommended for Claude Web, Claude Desktop)
2. The api_key parameter in each tool call
3. The CATCHALL_API_KEY environment variable
"""

import contextvars
import json
import os
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_request

# Context variable to store the API key from URL for the current session
session_api_key: contextvars.ContextVar[str] = contextvars.ContextVar("session_api_key", default="")

# API Configuration
API_BASE_URL = "https://catchall.newscatcherapi.com"


class ApiKeyMiddleware(Middleware):
    """Middleware to extract API key from URL query parameters.

    This allows users to pass their API key once in the connection URL:
    https://your-server.fastmcp.app/mcp?apiKey=YOUR_KEY

    The key is then used for all subsequent tool calls without
    needing to pass it in every request.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        """Extract API key from HTTP request query params before tool execution."""
        try:
            request = get_http_request()
            api_key = request.query_params.get("apiKey", "")
            if api_key:
                session_api_key.set(api_key)
        except Exception:
            # Not running in HTTP context (e.g., stdio), skip
            pass
        return await call_next(context)


# Create the FastMCP server
mcp = FastMCP(
    "Newscatcher CatchAll API",
    instructions="""This server allows you to search the web using natural language queries via the Newscatcher CatchAll API.

IMPORTANT: You need a CatchAll API key to use these tools. Get one at https://platform.newscatcherapi.com/

## Core workflow: Jobs (submit -> poll -> pull)
1. Use submit_query to submit your web search query (only `query` is required; the system auto-selects validators, enrichments, and dates)
2. Optionally use initialize_query first to preview suggested validators/enrichments before submitting
3. Use get_job_status to poll for completion (status: submitted -> analyzing -> fetching -> clustering -> enriching -> completed)
   IMPORTANT: Jobs take several minutes to process. Wait at least 30 seconds between status checks. Do NOT poll more frequently.
4. Use pull_results to retrieve the clustered web results (partial results available before completion)
5. Use continue_job to expand results beyond the initial limit if needed

## Understanding `limit` vs `page_size` — IMPORTANT
These two parameters serve completely different purposes:
- `limit` (submit_query, continue_job): Controls how many records the system PROCESSES. Users pay per record, so limit controls cost. Start with a low limit (e.g. 10-50) to preview results cheaply, then use continue_job with a higher new_limit if more are needed.
- `page_size` (pull_results, list_user_jobs): Controls how many records are RETURNED per API call. This is free pagination — it does not affect cost or processing. If a job has 244 total records, use page/page_size to iterate through ALL of them across multiple pull_results calls (e.g. page=1, page=2, page=3 with page_size=100).
To get all records from a completed job, check total_pages in the pull_results response and iterate through every page. Do NOT use continue_job just to see more records that already exist — use pagination instead.

## Monitors workflow (explore -> refine -> automate)
1. Submit and refine a job until results match your needs
2. Use create_monitor with the completed job's ID to schedule recurring runs
3. Use list_monitors, pull_monitor_results, list_monitor_jobs to manage and view results
4. Use enable_monitor / disable_monitor / update_monitor to control monitors""",
)

# Add middleware to extract API key from URL query parameters
mcp.add_middleware(ApiKeyMiddleware())


def get_api_key(api_key: str = "") -> str:
    """Get API key from parameter, URL session, or environment variable.

    Priority order:
    1. api_key parameter (explicit in tool call)
    2. session_api_key (from URL query param ?apiKey=XXX)
    3. CATCHALL_API_KEY environment variable
    """
    # Check explicit parameter first
    if api_key:
        return api_key

    # Check session key from URL
    url_key = session_api_key.get("")
    if url_key:
        return url_key

    # Fall back to environment variable
    env_key = os.environ.get("CATCHALL_API_KEY", "")
    if env_key:
        return env_key

    raise ValueError(
        "API key is required. Provide it via: "
        "1) URL parameter ?apiKey=YOUR_KEY, "
        "2) api_key tool parameter, or "
        "3) CATCHALL_API_KEY environment variable."
    )


async def make_api_request(
    api_key: str,
    method: str,
    path: str,
    json_data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make an API request to CatchAll API."""
    key = get_api_key(api_key)

    headers = {
        "x-api-key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=60.0) as client:
        response = await client.request(
            method=method,
            url=path,
            headers=headers,
            json=json_data,
            params=params,
        )

        if response.status_code >= 400:
            try:
                error_data = response.json()
                if isinstance(error_data, dict):
                    if "detail" in error_data:
                        detail = error_data["detail"]
                        if isinstance(detail, dict) and "detail" in detail:
                            error_msg = detail["detail"]
                        else:
                            error_msg = str(detail)
                    else:
                        error_msg = json.dumps(error_data)
                else:
                    error_msg = str(error_data)
            except Exception:
                error_msg = response.text or f"HTTP {response.status_code}"

            raise ValueError(f"API Error ({response.status_code}): {error_msg}")

        return response.json()


# ---------------------------------------------------------------------------
# Job tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def submit_query(
    query: str,
    api_key: str = "",
    context: str = "",
    limit: int = 0,
    start_date: str = "",
    end_date: str = "",
    validators: list[dict[str, str]] | None = None,
    enrichments: list[dict[str, str]] | None = None,
    schema: str = "",
) -> str:
    """
    Submit a natural language query to search the web.

    The system will fetch, validate, cluster, and summarize relevant results.
    Returns a job_id that you'll use to check status and retrieve results.

    Only `query` is required. When submitted with just a query, the system
    automatically selects appropriate validators, enrichments, and date ranges.

    Args:
        query: Natural language query to search the web (e.g., 'Find all M&A deals in tech sector last 7 days')
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.
        context: Additional context to refine the query (e.g., 'Focus on deals over $1B')
        limit: Maximum number of records the system will process (controls cost — users pay per record). 0 means no limit (exhaustive). Start low (e.g. 10-50) to preview results cheaply, then use continue_job to expand if needed. This is NOT pagination — use page_size in pull_results to paginate through processed records for free.
        start_date: Start of date range in ISO 8601 format (e.g., '2026-01-30T00:00:00Z'). Limits which articles are searched.
        end_date: End of date range in ISO 8601 format. Limits which articles are searched.
        validators: List of boolean validators to filter results. Each is a dict with 'name', 'description', and 'type' (always 'boolean'). Example: [{"name": "is_merger", "description": "Article is about a merger or acquisition", "type": "boolean"}]
        enrichments: List of enrichments to extract from results. Each is a dict with 'name', 'description', and 'type' (one of: text, number, date, option, url, company). Example: [{"name": "deal_value", "description": "Estimated deal value in USD", "type": "number"}]
        schema: Output schema specification.

    Returns:
        JSON with job_id to use for checking status and getting results
    """
    try:
        body: dict[str, Any] = {"query": query}
        if context:
            body["context"] = context
        if limit > 0:
            body["limit"] = limit
        if start_date:
            body["start_date"] = start_date
        if end_date:
            body["end_date"] = end_date
        if validators:
            body["validators"] = validators
        if enrichments:
            body["enrichments"] = enrichments
        if schema:
            body["schema"] = schema

        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/submit",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def initialize_query(
    query: str,
    api_key: str = "",
    context: str = "",
) -> str:
    """
    Preview suggested validators and enrichments before submitting a job.

    Use this to see what the system would auto-select for your query.
    You can then adjust the suggestions and pass them to submit_query.
    Skip this if you trust the defaults and go straight to submit_query.

    Args:
        query: Natural language query to preview (e.g., 'AI chip export restrictions')
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.
        context: Additional context to refine suggestions.

    Returns:
        JSON with suggested validators, enrichments, start_date, end_date, and date_modification_message
    """
    try:
        body: dict[str, Any] = {"query": query}
        if context:
            body["context"] = context

        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/initialize",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_job_status(job_id: str, api_key: str = "") -> str:
    """
    Check the status of a submitted job.

    Call this after submit_query to see if your job is ready.
    Status progression: submitted -> analyzing -> fetching -> clustering -> enriching -> completed

    IMPORTANT: Jobs take several minutes to process. Wait at least 30 seconds
    between status checks. Do NOT call this tool in a tight loop.

    You don't need to wait for completion to pull results. Partial results are
    available early — call pull_results after ~2 minutes, then poll status
    every 30-60 seconds and pull again for fresher results.

    Args:
        job_id: The job ID returned from submit_query
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.

    Returns:
        JSON with current job status, steps, and progress information
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/status/{job_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def pull_results(job_id: str, api_key: str = "", page: int = 1, page_size: int = 100) -> str:
    """
    Retrieve the results of a job.

    Can be called before completion for partial results, or after completion
    for the full set. Returns clustered, validated, and enriched web results.

    Pagination is free and does not cost credits. If the response shows
    total_pages > 1, iterate through all pages to get every record.
    For example, a job with 244 records at page_size=100 has 3 pages —
    call this tool 3 times with page=1, page=2, page=3.

    Args:
        job_id: The job ID returned from submit_query
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.
        page: Page number for pagination (default: 1). Use total_pages from the response to iterate through all results.
        page_size: Number of records returned per page (default: 100, max: 100). This is free pagination, not a billing limit.

    Returns:
        JSON with clustered web results, summaries, metadata, page, page_size, and total_pages
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/pull/{job_id}",
            params={"page": page, "page_size": page_size},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def continue_job(job_id: str, new_limit: int, api_key: str = "") -> str:
    """
    Expand a job by processing more records beyond the initial limit.

    This increases the number of records the system processes (which costs
    additional credits). Only use this when the user wants MORE data processed,
    not when paginating through existing results — use pull_results with
    page/page_size for free pagination instead.

    The new_limit must be greater than the previous limit.

    Args:
        job_id: The job ID to continue processing
        new_limit: New record processing limit (must exceed the previous limit). This controls cost — users pay per record.
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.

    Returns:
        JSON with job_id, previous_limit, new_limit, and status
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/continue",
            json_data={"job_id": job_id, "new_limit": new_limit},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_user_jobs(api_key: str = "", page: int = 1, page_size: int = 100) -> str:
    """
    List all jobs submitted by you.

    Returns your job history with IDs, queries, statuses, and timestamps.

    Args:
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.
        page: Page number for pagination (default: 1)
        page_size: Number of results per page (default: 100)

    Returns:
        JSON with list of your submitted jobs
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/jobs/user",
            params={"page": page, "page_size": page_size},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


# ---------------------------------------------------------------------------
# Monitor tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_monitor(
    reference_job_id: str,
    schedule: str,
    api_key: str = "",
    webhook_url: str = "",
    webhook_method: str = "POST",
    webhook_headers: dict[str, str] | None = None,
    webhook_params: dict[str, str] | None = None,
    webhook_auth: list[str] | None = None,
) -> str:
    """
    Create a recurring monitor from a completed job.

    Monitors re-run a job's query on a schedule. Use the explore -> refine -> automate
    pattern: submit a job, refine until results match, then create a monitor.

    The schedule is defined in natural language (e.g., 'every day at 9 AM EST').
    Always include a timezone.

    Args:
        reference_job_id: ID of a completed job to use as the template
        schedule: Natural language schedule (e.g., 'every day at 9 AM EST', 'every Monday at 8 AM UTC', 'every 6 hours')
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.
        webhook_url: Optional webhook URL to receive results on each run
        webhook_method: Webhook HTTP method: 'POST' (default) or 'PUT'
        webhook_headers: Optional dict of custom HTTP headers for the webhook
        webhook_params: Optional dict of query string parameters for the webhook
        webhook_auth: Optional basic auth as [username, password]

    Returns:
        JSON with monitor_id and status
    """
    try:
        body: dict[str, Any] = {
            "reference_job_id": reference_job_id,
            "schedule": schedule,
        }

        if webhook_url:
            webhook: dict[str, Any] = {"url": webhook_url, "method": webhook_method}
            if webhook_headers:
                webhook["headers"] = webhook_headers
            if webhook_params:
                webhook["params"] = webhook_params
            if webhook_auth:
                webhook["auth"] = webhook_auth
            body["webhook"] = webhook

        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/monitors/create",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_monitors(api_key: str = "") -> str:
    """
    List all your monitors.

    Returns all monitors with their schedule, status, reference query, and webhook config.

    Args:
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.

    Returns:
        JSON with total_monitors and list of monitors
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/monitors/",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def pull_monitor_results(monitor_id: str, api_key: str = "") -> str:
    """
    Retrieve the latest results from a monitor.

    Returns the most recent run's results including run_info, records, and all_records.

    Args:
        monitor_id: The monitor ID to pull results from
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.

    Returns:
        JSON with monitor_id, cron_expression, reference_job, run_info, records, and all_records
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/monitors/pull/{monitor_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_monitor_jobs(monitor_id: str, api_key: str = "", sort: str = "asc") -> str:
    """
    List all jobs spawned by a monitor.

    Returns the history of scheduled runs for a monitor.

    Args:
        monitor_id: The monitor ID to list jobs for
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.
        sort: Sort order by start_date: 'asc' (default) or 'desc'

    Returns:
        JSON with list of jobs including job_id, start_date, end_date
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/monitors/{monitor_id}/jobs",
            params={"sort": sort},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def disable_monitor(monitor_id: str, api_key: str = "") -> str:
    """
    Disable a monitor to stop its scheduled runs.

    The monitor can be re-enabled later with enable_monitor.

    Args:
        monitor_id: The monitor ID to disable
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.

    Returns:
        Confirmation that the monitor was disabled
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path=f"/catchAll/monitors/{monitor_id}/disable",
        )
        return json.dumps(result, indent=2) if result else "Monitor disabled successfully."
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def enable_monitor(monitor_id: str, api_key: str = "") -> str:
    """
    Enable a previously disabled monitor to resume its scheduled runs.

    Args:
        monitor_id: The monitor ID to enable
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.

    Returns:
        Confirmation that the monitor was enabled
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path=f"/catchAll/monitors/{monitor_id}/enable",
        )
        return json.dumps(result, indent=2) if result else "Monitor enabled successfully."
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def update_monitor(
    monitor_id: str,
    api_key: str = "",
    webhook_url: str = "",
    webhook_method: str = "POST",
    webhook_headers: dict[str, str] | None = None,
    webhook_params: dict[str, str] | None = None,
    webhook_auth: list[str] | None = None,
) -> str:
    """
    Update a monitor's webhook configuration.

    Args:
        monitor_id: The monitor ID to update
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.
        webhook_url: New webhook URL
        webhook_method: Webhook HTTP method: 'POST' (default) or 'PUT'
        webhook_headers: Optional dict of custom HTTP headers for the webhook
        webhook_params: Optional dict of query string parameters for the webhook
        webhook_auth: Optional basic auth as [username, password]

    Returns:
        JSON with monitor_id and status
    """
    try:
        body: dict[str, Any] = {}

        if webhook_url:
            webhook: dict[str, Any] = {"url": webhook_url, "method": webhook_method}
            if webhook_headers:
                webhook["headers"] = webhook_headers
            if webhook_params:
                webhook["params"] = webhook_params
            if webhook_auth:
                webhook["auth"] = webhook_auth
            body["webhook"] = webhook

        result = await make_api_request(
            api_key=api_key,
            method="PATCH",
            path=f"/catchAll/monitors/{monitor_id}",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


if __name__ == "__main__":
    mcp.run()
