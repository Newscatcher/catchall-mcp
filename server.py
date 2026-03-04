"""
MCP Server for Newscatcher CatchAll API

This server provides tools to interact with the Newscatcher CatchAll API.
Users can provide their API key via (in order of precedence):
1. The api_key parameter in each tool call
2. URL query parameter: ?apiKey=YOUR_KEY (recommended for Claude Web, Claude Desktop)
3. The CATCHALL_API_KEY environment variable
"""

from __future__ import annotations

import contextvars
import json
import os
from typing import Any, Literal
from typing_extensions import TypedDict

import httpx
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_request

# Context variable to store the API key from URL for the current session
session_api_key: contextvars.ContextVar[str] = contextvars.ContextVar("session_api_key", default="")

# API Configuration
API_BASE_URL = "https://catchall.newscatcherapi.com"
ENRICHMENT_TYPES = {"text", "number", "date", "option", "url", "dict", "company"}


class ValidatorDefinition(TypedDict):
    """Schema for a custom validator."""

    name: str
    description: str
    type: Literal["boolean"]


class EnrichmentDefinition(TypedDict):
    """Schema for a custom enrichment."""

    name: str
    description: str
    type: Literal["text", "number", "date", "option", "url", "dict", "company"]


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

IMPORTANT: Most tools require a CatchAll API key. Get one at https://platform.newscatcherapi.com/
Exceptions: `check_health` and `get_version` do not require an API key.

## Core workflow: Jobs (submit -> poll -> pull)
1. If you already have a `job_id`, skip submission and start with `get_job_status` / `pull_results`.
2. (Optional) Use `initialize_query` to preview validators, enrichments, and dates before submitting.
   It is preview-only: it does not create a job or start processing.
   IMPORTANT: Initialize suggestions are LLM-generated and not deterministic. If you want to reuse them, pass them explicitly to `submit_query`.
   Check `date_modification_message` for any date adjustments due to plan limits.
3. Use `submit_query` to create a new job (`query` is required).
   You can call it with only `query`; if `validators`, `enrichments`, `start_date`, and `end_date` are omitted, the API auto-selects/generates them.
   Optional fields are independent: you can provide any subset (for example, `validators` only) and omitted fields are still auto-selected/generated.
   IMPORTANT: `start_date`/`end_date` filter page discovery dates, not event dates in extracted content.
   If you care about when events happened, add event-time validators/enrichments and verify `event_date` in results.
   Use `limit` to control processed records and cost.
4. Use `get_job_status` to poll job progress.
   IMPORTANT: First check after ~1-2 minutes, then poll every 30-60 seconds.
   Status flow: submitted -> analyzing -> fetching -> clustering -> enriching -> completed/failed.
   Stop polling when status is `completed` or `failed`.
5. Use `pull_results` to retrieve output.
   Partial results are available during `enriching`; `progress_validated` shows validation progress.
   For full output after completion, start with `page=1` and continue while `page < total_pages`.
6. Use `continue_job` only when you need more records processed (cost-affecting).
   This only applies to jobs originally submitted with `limit`.
   If a job was submitted without `limit`, there is nothing to continue.
   `new_limit` must be greater than the previous limit. After continuing, repeat polling and pulling.

## Understanding `limit` vs `page_size` — IMPORTANT
These two parameters serve completely different purposes:
- `limit` (submit_query, continue_job): Controls how many records the system PROCESSES. Users pay per record, so limit controls cost. Start with a low limit (e.g. 10-50) to preview results cheaply, then use continue_job with a higher new_limit if more are needed.
- `page_size` (pull_results, list_user_jobs): Controls how many records are RETURNED per API call (max 1000). This is free pagination — it does not affect cost or processing. If a job has 244 total records, use page/page_size to iterate through ALL of them across multiple pull_results calls (e.g. page=1, page=2, page=3 with page_size=100).
To get all records from a completed job, check total_pages in the pull_results response and iterate through every page. Do NOT use continue_job just to see more records that already exist — use pagination instead.

## Monitors workflow (explore -> refine -> automate)
1. Submit and refine a job until results match your needs
2. Use create_monitor with the completed job's ID and a schedule string that includes timezone (for example, `every day at 9 AM EST`)
3. Use list_monitors, pull_monitor_results, list_monitor_jobs to manage and view results
4. Use enable_monitor / disable_monitor / update_monitor to control monitors

## Monitor constraints (API-enforced)
- Reference jobs for create_monitor must have `end_date` within the last 7 days
- Monitor schedules must have at least a 24-hour interval
- update_monitor only updates webhook configuration; schedule and reference job cannot be changed

## Meta tools
- check_health and get_version map to `/health` and `/version` and work without API key""",
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
        "1) api_key tool parameter, "
        "2) URL parameter ?apiKey=YOUR_KEY, or "
        "3) CATCHALL_API_KEY environment variable."
    )


def get_optional_api_key(api_key: str = "") -> str:
    """Get API key without requiring one."""
    if api_key:
        return api_key

    url_key = session_api_key.get("")
    if url_key:
        return url_key

    return os.environ.get("CATCHALL_API_KEY", "")


def validate_page_params(page: int, page_size: int, max_page_size: int = 1000) -> None:
    """Validate pagination parameters."""
    if page < 1:
        raise ValueError("page must be >= 1.")
    if page_size < 1 or page_size > max_page_size:
        raise ValueError(f"page_size must be between 1 and {max_page_size}.")


def validate_sort(sort: str) -> str:
    """Validate monitor jobs sort order."""
    if sort not in {"asc", "desc"}:
        raise ValueError("sort must be either 'asc' or 'desc'.")
    return sort


def validate_new_limit(new_limit: int) -> None:
    """Validate continue_job new_limit."""
    if new_limit < 1:
        raise ValueError("new_limit must be >= 1.")


def validate_validator_definitions(
    validators: list[ValidatorDefinition] | None,
) -> list[ValidatorDefinition] | None:
    """Validate and normalize custom validators."""
    if validators is None:
        return None

    normalized: list[ValidatorDefinition] = []
    for idx, validator in enumerate(validators):
        if not isinstance(validator, dict):
            raise ValueError(f"validators[{idx}] must be an object.")

        name = validator.get("name")
        description = validator.get("description")
        validator_type = validator.get("type", "boolean")

        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"validators[{idx}].name must be a non-empty string.")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"validators[{idx}].description must be a non-empty string.")
        if validator_type != "boolean":
            raise ValueError(f"validators[{idx}].type must be 'boolean'.")

        normalized.append(
            {
                "name": name.strip(),
                "description": description.strip(),
                "type": "boolean",
            }
        )

    return normalized


def validate_enrichment_definitions(
    enrichments: list[EnrichmentDefinition] | None,
) -> list[EnrichmentDefinition] | None:
    """Validate custom enrichments."""
    if enrichments is None:
        return None

    normalized: list[EnrichmentDefinition] = []
    for idx, enrichment in enumerate(enrichments):
        if not isinstance(enrichment, dict):
            raise ValueError(f"enrichments[{idx}] must be an object.")

        name = enrichment.get("name")
        description = enrichment.get("description")
        enrichment_type = enrichment.get("type")

        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"enrichments[{idx}].name must be a non-empty string.")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"enrichments[{idx}].description must be a non-empty string.")
        if not isinstance(enrichment_type, str) or enrichment_type not in ENRICHMENT_TYPES:
            allowed = ", ".join(sorted(ENRICHMENT_TYPES))
            raise ValueError(f"enrichments[{idx}].type must be one of: {allowed}.")

        normalized.append(
            {
                "name": name.strip(),
                "description": description.strip(),
                "type": enrichment_type,  # type: ignore[typeddict-item]
            }
        )

    return normalized


def validate_webhook_method(webhook_method: str) -> str:
    """Validate webhook HTTP method."""
    normalized = webhook_method.upper()
    if normalized not in {"POST", "PUT"}:
        raise ValueError("webhook_method must be 'POST' or 'PUT'.")
    return normalized


def validate_webhook_auth(webhook_auth: list[str] | None) -> None:
    """Validate webhook basic auth tuple."""
    if webhook_auth is None:
        return
    if len(webhook_auth) != 2:
        raise ValueError("webhook_auth must contain exactly two values: [username, password].")
    if not all(isinstance(item, str) and item for item in webhook_auth):
        raise ValueError("webhook_auth values must be non-empty strings.")


def build_webhook_payload(
    webhook_url: str,
    webhook_method: str,
    webhook_headers: dict[str, str] | None,
    webhook_params: dict[str, str] | None,
    webhook_auth: list[str] | None,
) -> dict[str, Any] | None:
    """Build and validate optional webhook payload."""
    has_webhook_extras = (
        webhook_headers is not None
        or webhook_params is not None
        or webhook_auth is not None
        or webhook_method.upper() != "POST"
    )

    if not webhook_url:
        if has_webhook_extras:
            raise ValueError(
                "webhook_url is required when providing webhook_method, "
                "webhook_headers, webhook_params, or webhook_auth."
            )
        return None

    normalized_method = validate_webhook_method(webhook_method)
    validate_webhook_auth(webhook_auth)

    webhook: dict[str, Any] = {"url": webhook_url, "method": normalized_method}
    if webhook_headers:
        webhook["headers"] = webhook_headers
    if webhook_params:
        webhook["params"] = webhook_params
    if webhook_auth:
        webhook["auth"] = webhook_auth

    return webhook


async def make_api_request(
    api_key: str,
    method: str,
    path: str,
    json_data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    require_auth: bool = True,
) -> dict[str, Any]:
    """Make an API request to CatchAll API."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    key = get_api_key(api_key) if require_auth else get_optional_api_key(api_key)
    if key:
        headers["x-api-key"] = key

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
    validators: list[ValidatorDefinition] | None = None,
    enrichments: list[EnrichmentDefinition] | None = None,
    schema: str = "",
) -> str:
    """
    Create a new CatchAll processing job from a natural-language query.

    Use when:
    - You need a new `job_id` for a new web search request.

    Do not use when:
    - You want status for an existing job (use `get_job_status`).
    - You want records for an existing job (use `pull_results`).

    Key rules:
    - `query` is required.
    - You can submit with only `query`; omitted optional fields (`validators`, `enrichments`, `start_date`, `end_date`) are auto-selected/generated by the API.
    - Optional fields are independent: you can pass any subset (for example, custom `validators` but no `enrichments`), and omitted fields are still auto-selected/generated.
    - `start_date` and `end_date` filter by web page discovery date, not event date.
    - Discovery dates and extracted event dates can differ. For event-time accuracy, use event-focused validators/enrichments and verify `event_date` in pulled results.
    - `end_date` must be after `start_date`.
    - Dates outside your plan lookback limits return API 400.
    - `limit` controls processed record count (cost-affecting). In this MCP, `limit <= 0` means the field is omitted and API defaults apply.
    - `schema` is a template string using placeholders (e.g., [ACQUIRER], [TARGET], [AMOUNT]); API generates `schema_based_summary`.
    - `validators[].type` must be `boolean` (if omitted, it defaults to `boolean`).
    - `enrichments[].type` supported values: text, number, date, option, url, dict, company.

    Basic examples:
    - validators:
      `[{"name":"is_acquisition_event","description":"true if page describes an acquisition","type":"boolean"}]`
    - enrichments:
      `[{"name":"acquiring_company","description":"Extract acquiring company","type":"company"},{"name":"deal_value","description":"Extract announced deal value","type":"number"}]`
    - schema:
      `"[ACQUIRER] acquired [TARGET] for [AMOUNT]"`

    Next step:
    - Poll `get_job_status` until completed/failed, then call `pull_results`.

    Args:
        query: Plain text search intent (required).
        api_key: CatchAll API key. Optional if provided via URL session or CATCHALL_API_KEY.
        context: Optional extra context to focus extraction.
        limit: Optional processing cap; affects cost.
        start_date: Optional ISO 8601 UTC start of search window.
        end_date: Optional ISO 8601 UTC end of search window.
        validators: Optional custom boolean validators (`name`, `description`, `type`).
        enrichments: Optional custom enrichments (`name`, `description`, `type`).
        schema: Optional summary template string.

    Returns:
        JSON string with `{"job_id":"<uuid>"}`.

    Common API errors:
        - 400: bad request or constraint violations (for example, date limits).
        - 403: missing or invalid API key.
        - 422: input validation errors.
    """
    try:
        normalized_validators = validate_validator_definitions(validators)
        normalized_enrichments = validate_enrichment_definitions(enrichments)

        body: dict[str, Any] = {"query": query}
        if context:
            body["context"] = context
        if limit > 0:
            body["limit"] = limit
        if start_date:
            body["start_date"] = start_date
        if end_date:
            body["end_date"] = end_date
        if normalized_validators:
            body["validators"] = normalized_validators
        if normalized_enrichments:
            body["enrichments"] = normalized_enrichments
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
    Preview suggested validators, enrichments, and date ranges before submitting.

    Use when:
    - You want to inspect/edit auto-generated validators/enrichments before submitting.
    - You want to preview date adjustments via `date_modification_message`.

    Do not use when:
    - You want to start processing immediately with final inputs (use `submit_query`).

    Key behavior:
    - Preview-only endpoint: does not create a job and does not start processing.
    - Suggestions are LLM-generated and not deterministic across calls.
    - To reuse suggestions, pass them explicitly to `submit_query`.

    Args:
        query: Natural language query to preview (required).
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.
        context: Optional context to refine suggestions.

    Returns:
        JSON string with `validators`, `enrichments`, `start_date`, `end_date`,
        and `date_modification_message` (messages may be empty if no date
        adjustments were needed).

    Common API errors:
        - 403: missing or invalid API key.
        - 422: input validation errors.
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
    Status progression: submitted -> analyzing -> fetching -> clustering -> enriching -> completed/failed

    IMPORTANT: Jobs take several minutes to process.
    First check after ~1-2 minutes, then poll every 30-60 seconds.
    Do NOT call this tool in a tight loop.
    Stop polling when status is `completed` or `failed`.

    You don't need to wait for completion to pull results. Partial results are
    available during `enriching` — call pull_results after ~2 minutes, then
    poll status every 30-60 seconds and pull again for fresher results.

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

    Args:
        job_id: The job ID returned from submit_query
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.
        page: Page number for pagination (default: 1). Use total_pages from the response to iterate through all results.
        page_size: Number of records returned per page (default: 100, max: 1000).

    Returns:
        JSON string with job output fields such as `status`, `all_records`,
        `candidate_records`, `valid_records`, `progress_validated`, `page`,
        `page_size`, and `total_pages`.
        Iterate pages while `page < total_pages` to fetch the full result set.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
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
    additional credits). Only use this when the user wants MORE data processed.

    This only applies to jobs originally submitted with `limit`.
    If a job was submitted without `limit`, there is nothing to continue.
    The new_limit must be greater than the previous limit.

    Args:
        job_id: The job ID to continue processing
        new_limit: New record processing limit (must exceed the previous limit).
        api_key: Your CatchAll API key. Optional if CATCHALL_API_KEY env var is set.

    Returns:
        JSON with job_id, previous_limit, new_limit, and status.
        After continuation is accepted, poll status again and pull results again.
    """
    try:
        validate_new_limit(new_limit)
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
        page_size: Number of results per page (default: 100, max: 1000)

    Returns:
        JSON with list of your submitted jobs
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
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
    Always include a timezone. API-enforced constraints apply:
    - Reference job end_date must be within the last 7 days
    - Minimum schedule frequency is every 24 hours

    Args:
        reference_job_id: ID of a completed job to use as the template
        schedule: Natural language schedule (e.g., 'every day at 9 AM EST', 'every Monday at 8 AM UTC', 'every 48 hours')
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

        webhook = build_webhook_payload(
            webhook_url=webhook_url,
            webhook_method=webhook_method,
            webhook_headers=webhook_headers,
            webhook_params=webhook_params,
            webhook_auth=webhook_auth,
        )
        if webhook:
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
            path="/catchAll/monitors",
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
        validated_sort = validate_sort(sort)
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/monitors/{monitor_id}/jobs",
            params={"sort": validated_sort},
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

    Note: schedule and reference_job_id cannot be modified through this endpoint.

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

        webhook = build_webhook_payload(
            webhook_url=webhook_url,
            webhook_method=webhook_method,
            webhook_headers=webhook_headers,
            webhook_params=webhook_params,
            webhook_auth=webhook_auth,
        )
        if webhook:
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


@mcp.tool()
async def check_health(api_key: str = "") -> str:
    """
    Check API health status.

    This tool maps to GET /health and does not require an API key.

    Args:
        api_key: Optional CatchAll API key.

    Returns:
        JSON with API health status
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/health",
            require_auth=False,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_version(api_key: str = "") -> str:
    """
    Get current API version.

    This tool maps to GET /version and does not require an API key.

    Args:
        api_key: Optional CatchAll API key.

    Returns:
        JSON with version information
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/version",
            require_auth=False,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


if __name__ == "__main__":
    mcp.run()
