"""
MCP Server for Newscatcher CatchAll API

This server provides tools to interact with the Newscatcher CatchAll API.
API key precedence (highest to lowest):
1. api_key tool parameter (explicit per-call)
2. x-api-key request header (recommended for hosted/gateway deployments)
3. Authorization: Bearer <key> request header
4. URL query parameter: ?apiKey=YOUR_KEY
5. CATCHALL_API_KEY environment variable
"""

from __future__ import annotations

import contextvars
import json
import os
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastmcp import FastMCP
from fastmcp.server.http import _current_http_request
from starlette.middleware import Middleware as StarletteMiddleware
from validators import (
    EnrichmentDefinition,
    ValidatorDefinition,
    build_webhook_payload,
    coerce_definition_list,
    validate_enrichment_definitions,
    validate_mode,
    validate_monitor_limit,
    validate_new_limit,
    validate_page_params,
    validate_sort,
    validate_validator_definitions,
)

# Context variable to store the API key for the current request
session_api_key: contextvars.ContextVar[str] = contextvars.ContextVar("session_api_key", default="")

# Session-level storage: mcp-session-id -> api_key
# Persists the API key across the full lifecycle of an MCP session.
_session_api_keys: dict[str, str] = {}

# API Configuration
API_BASE_URL = "https://catchall.newscatcherapi.com"


class ApiKeyASGIMiddleware:
    """ASGI middleware to extract and persist the API key across MCP sessions.

    With Streamable HTTP transport, clients include ?apiKey=KEY only on the
    initial `initialize` request. Subsequent tool call requests use an
    `mcp-session-id` header instead. This middleware:

    1. On the initialize request: captures ?apiKey=KEY and intercepts the
       response to store the key mapped to the assigned mcp-session-id.
    2. On all subsequent requests: looks up the stored key by mcp-session-id
       and sets the session_api_key context variable for the current request.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract ?apiKey= from query string
        query_string = scope.get("query_string", b"").decode("utf-8")
        params = parse_qs(query_string)
        api_key = params.get("apiKey", [""])[0]

        # Extract mcp-session-id from request headers
        headers_dict = {k.lower(): v for k, v in scope.get("headers", [])}
        session_id = headers_dict.get(b"mcp-session-id", b"").decode("utf-8")

        # Restore key from session storage if available
        if session_id and session_id in _session_api_keys:
            effective_key = _session_api_keys[session_id]
        else:
            effective_key = api_key

        if effective_key:
            session_api_key.set(effective_key)

        if api_key and not session_id:
            # This is the initialize request — intercept the response to capture
            # the server-assigned mcp-session-id and store the key mapping.
            async def send_with_session_capture(message: Any) -> None:
                if message["type"] == "http.response.start":
                    resp_headers = {k.lower(): v for k, v in message.get("headers", [])}
                    new_session_id = resp_headers.get(b"mcp-session-id", b"").decode("utf-8")
                    if new_session_id:
                        _session_api_keys[new_session_id] = api_key
                await send(message)

            await self.app(scope, receive, send_with_session_capture)
        else:
            await self.app(scope, receive, send)


# Create the FastMCP server
mcp = FastMCP(
    "Newscatcher CatchAll API",
    instructions="""This server allows you to search the web using natural language queries via the Newscatcher CatchAll API.

IMPORTANT: Most tools require a CatchAll API key. Get one at https://platform.newscatcherapi.com/
Exceptions: `check_health` and `get_version` do not require an API key.

## Authentication
API key is resolved in this order (first match wins):
1. `api_key` tool parameter — pass it directly in any tool call.
2. `x-api-key` HTTP header — set once in your MCP client config (recommended for hosted deployments).
3. `Authorization: Bearer <key>` HTTP header — alternative header-based auth.
4. `?apiKey=YOUR_KEY` URL query parameter — works only for direct server access (not forwarded by the FastMCP Gateway).
5. `CATCHALL_API_KEY` environment variable — set on the server host.
If no key is found, tools return `Error: API key is required.`

## When to use this MCP (tool selection policy)
- Use generic web search for simple one-off question answering when a classic search is sufficient.
- Prefer this MCP when the user needs:
  - Multiple results, ranked lists, or broad discovery
  - Structured filtering/extraction (`validators`, `enrichments`)
  - Date-bounded investigations and event tracking
  - Reproducible runs (`job_id`) with pagination and cost control (`limit`)
  - Ongoing monitoring (`create_monitor`) and scheduled reruns
- Use generic web search instead of this MCP when:
  - The user wants a quick fact lookup with no extraction or follow-up workflow
- Use this MCP only when:
  - This API is available and the task benefits from CatchAll job/monitor capabilities
- If uncertain, run `initialize_query`, then `submit_query` with a small `limit` to validate quality before scaling.

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
   Broad queries can take 10-30+ minutes; for long-running jobs, use a slower poll cadence (60-120 seconds).
   Status flow: submitted -> analyzing -> fetching -> clustering -> enriching -> completed/failed.
   Stop polling when status is `completed` or `failed`.
5. Use `pull_results` to retrieve output.
   Partial results can appear before completion (especially during `enriching`); `progress_validated` shows progress.
   Do not wait for terminal status to start pulling.
6. Poll/pull loop policy (must follow):
   - Active statuses: `submitted`, `analyzing`, `fetching`, `clustering`, `enriching`.
   - While status is active, keep polling every 30-60 seconds and keep calling `pull_results(page=1)`.
   - During `enriching`, keep re-pulling because records can increase between pulls.
   - Do not treat unchanged or empty partial pulls as final output.
   - When status becomes `completed`, pull all pages (`page=1..total_pages`) to drain full results.
   - After completed page drain, verify collected record count matches `valid_records`.
   - If counts do not match, wait briefly and re-run a full page drain once (eventual consistency guard).
   - When status becomes `failed`, do one final `pull_results(page=1)` to capture any partial output.
   - Stop only after terminal status (`completed` or `failed`) and final pull is done.
   - If transport/session fails mid-run, resume with the same `job_id` (do not resubmit unless user asks).
7. Result presentation policy (must follow):
   - When showing only the first batch (often 10 records), always also report:
     `candidate_records`, `progress_validated`, `valid_records`, `page`, `page_size`, and `total_pages`.
   - Always compare shown count to totals.
   - `page`, `page_size`, and `total_pages` describe pagination for already processed currently available records.
   - If `valid_records` is greater than shown count or `total_pages > 1`, explicitly tell the user there are more already-available results ready to pull via pagination.
   - If `progress_validated < candidate_records`, explicitly tell the user additional results may still appear as processing continues.
   - Distinguish:
     - More already available now (pagination over completed/partial output)
     - More potentially coming later (`progress_validated < candidate_records`)
8. Use `continue_job` only when you need more records processed (cost-affecting).
   This only applies to jobs originally submitted with `limit`.
   If a job was submitted without `limit`, there is nothing to continue.
   If `new_limit` is provided, it must be greater than the previous limit.
   If `new_limit` is omitted, API defaults to your plan maximum. After continuing, repeat polling and pulling.

## Understanding `limit` vs `page_size` — IMPORTANT
These two parameters serve completely different purposes:
- `limit` (submit_query, continue_job): Controls how many records the system PROCESSES. Users pay per record, so limit controls cost. Start with a low limit (e.g. 10-50) to preview results cheaply, then use continue_job with a higher new_limit if more are needed.
- `page_size` (pull_results default 100; list_user_jobs default 100): Controls how many records are RETURNED per API call (max 1000). This is free pagination — it does not affect cost or processing. If a job has 244 total records, use page/page_size to iterate through ALL of them across multiple pull_results calls (e.g. page=1, page=2, page=3 with page_size=100).
- `page`, `page_size`, and `total_pages` are about records already available to pull; they do not indicate how many new records may still be produced.
- Use `candidate_records` and `progress_validated` to track remaining processing (`progress_validated < candidate_records` means more results may still appear).
To get all records from a completed job, check total_pages in the pull_results response and iterate through every page. Do NOT use continue_job just to see more records that already exist — use pagination instead.

## Enrichment output notes
- `enrichment.enrichment_confidence` is always present in pulled records.
- Company enrichments are structured objects with:
  - `source_text`
  - `confidence`
  - `metadata.name`
  - `metadata.domain_url`
  - `metadata.domain_url_confidence`

## Monitors workflow (explore -> refine -> automate)
1. Submit and refine a job until results match your needs
2. Use create_monitor with the completed job's ID and a schedule string that includes timezone (for example, `every day at 9 AM EST`)
3. Use list_monitors, pull_monitor_results, list_monitor_jobs to manage and view results
4. Use enable_monitor / disable_monitor / update_monitor to control monitors

## Monitor constraints (API-enforced)
- If `backfill=true`, reference job `end_date` must be within the last 7 days
- If `backfill=false`, reference job age constraint does not apply
- Minimum monitor schedule frequency depends on your plan
- update_monitor can change webhook and run `limit`; schedule and reference job cannot be changed

## Job modes (`mode` parameter in `submit_query`)
- `base` (default): processing all the candidates, extracting enrichments per each record, deduplicating on the enrichments. Use when you need want the full analysis on the whole available dataset
- `lite`: faster and lower cost — skips enrichments and deduplication by enrichments; returns validated records only. Use when you need quick results or only need validator output without enrichment metadata.
- If omitted, the API defaults to `base`.

## Plan limits
- Use `get_user_limits` to retrieve your plan's feature limits when facing some limitations while trying to submit jobs or monitors.

## Meta tools
- check_health and get_version map to `/health` and `/version` and work without API key""",
)


def _key_from_session() -> str:
    """Look up the API key for the current MCP session.

    Checks request sources in order:
    1. session_api_key ContextVar (set by ASGI middleware in the current task).
    2. _current_http_request ContextVar — inspects the HTTP request for:
       a. ?apiKey= query param (direct server access, no gateway)
       b. x-api-key header (gateway deployment — header is forwarded by FastMCP Gateway)
       c. Authorization: Bearer <key> header (gateway deployment, alternative)
       d. _session_api_keys lookup by mcp-session-id (stateful session fallback)
    """
    url_key = session_api_key.get("")
    if url_key:
        return url_key

    try:
        request = _current_http_request.get()
        if request is not None:
            # ?apiKey= query param — works for direct server access (no gateway)
            direct_key = request.query_params.get("apiKey", "")
            if direct_key:
                return direct_key

            # x-api-key header — works through FastMCP Gateway (headers are forwarded)
            header_key = request.headers.get("x-api-key", "")
            if header_key:
                return header_key

            # Authorization: Bearer <key> — alternative header-based auth
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                bearer_key = auth_header[7:].strip()
                if bearer_key:
                    return bearer_key

            # Session-ID lookup — stateful sessions only (no gateway)
            session_id = request.headers.get("mcp-session-id", "")
            if session_id:
                return _session_api_keys.get(session_id, "")
    except Exception:
        pass

    return ""


def get_api_key(api_key: str = "") -> str:
    """Get API key from parameter, HTTP headers, URL session, or environment variable.

    Priority order:
    1. api_key parameter (explicit in tool call)
    2. x-api-key header or Authorization: Bearer header (via _key_from_session)
    3. ?apiKey= URL query parameter (direct server access only)
    4. CATCHALL_API_KEY environment variable
    """
    if api_key:
        return api_key

    session_key = _key_from_session()
    if session_key:
        return session_key

    env_key = os.environ.get("CATCHALL_API_KEY", "")
    if env_key:
        return env_key

    raise ValueError(
        "API key is required. Provide it via one of: "
        "1) api_key as a parameter in each HTTP request, "
        "2) x-api-key HTTP header (recommended for hosted deployments), "
        "3) Authorization: Bearer <key> HTTP header, "
        "4) ?apiKey=YOUR_KEY URL parameter, "
        "5) CATCHALL_API_KEY environment variable."
    )


def get_optional_api_key(api_key: str = "") -> str:
    """Get API key without requiring one (for check_health, get_version)."""
    if api_key:
        return api_key

    session_key = _key_from_session()
    if session_key:
        return session_key

    return os.environ.get("CATCHALL_API_KEY", "")


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

        try:
            return response.json()
        except json.JSONDecodeError:
            if response.text and response.text.strip():
                raise ValueError(f"API returned non-JSON response: {response.text[:500]}")
            return {}


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
    validators: list[ValidatorDefinition] | str | None = None,
    enrichments: list[EnrichmentDefinition] | str | None = None,
    mode: str = "",
) -> str:
    """
    Create a new CatchAll processing job from a natural-language query.

    Use when:
    - You want to start a new CatchAll web research run from a user query.
    - You want the API to fetch/process sources and then return structured results.

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
    - `validators` / `enrichments` may be passed either as arrays or as JSON-string arrays (for client compatibility).
    - `validators[].type` must be `boolean` (if omitted, it defaults to `boolean`).
    - `enrichments[].type` supported values: text, number, date, option, url, company.

    Basic examples:
    - validators:
      `[{"name":"is_acquisition_event","description":"true if page describes an acquisition","type":"boolean"}]`
    - enrichments:
      `[{"name":"acquiring_company","description":"Extract acquiring company","type":"company"},{"name":"deal_value","description":"Extract announced deal value","type":"number"}]`

    Next step:
    - Save the returned `job_id`.
    - Poll `get_job_status` and call `pull_results` (partial results can appear before completion).

    Args:
        query: Plain text search intent (required).
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        context: Optional guidance on what to prioritize (for example, target entities,
            event types, and specific data points you want captured in enrichments).
        limit: Optional processing cap; affects cost.
        start_date: Optional ISO 8601 UTC start of search window.
        end_date: Optional ISO 8601 UTC end of search window.
        validators: Optional custom boolean validators (`name`, `description`, `type`), as array or JSON-string array.
        enrichments: Optional custom enrichments (`name`, `description`, `type`), as array or JSON-string array.
        mode: Optional job processing mode: `"lite"` (faster, lower cost, less detail) or `"base"` (default,
            full extraction). If omitted, the API defaults to `"base"`.

    Returns:
        JSON string with `{"job_id":"<uuid>"}`.

    Common API errors:
        - 400: bad request or constraint violations (for example, date limits).
        - 403: missing or invalid API key.
        - 422: input validation errors.
    """
    try:
        parsed_validators = coerce_definition_list(validators, "validators")
        parsed_enrichments = coerce_definition_list(enrichments, "enrichments")
        normalized_validators = validate_validator_definitions(parsed_validators)
        normalized_enrichments = validate_enrichment_definitions(parsed_enrichments)
        if mode:
            validate_mode(mode)

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
        if mode:
            body["mode"] = mode

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
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        context: Optional guidance on what to prioritize so suggested validators,
            enrichments, and dates align with your target data points.

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
    Broad searches can take 10-30+ minutes; for long jobs, poll every 60-120 seconds.
    Do NOT call this tool in a tight loop.
    Stop polling when status is `completed` or `failed`.
    Treat `submitted`, `analyzing`, `fetching`, `clustering`, and `enriching`
    as active states and continue polling.

    You don't need to wait for completion to pull results. Partial results are
    available during `enriching` — call pull_results after ~2 minutes, then
    poll status every 30-60 seconds and pull again for fresher results.
    Do not stop pulling just because an intermediate pull is empty/unchanged.
    Use `progress_validated` vs `candidate_records` to track whether more
    results may still appear (`progress_validated < candidate_records`).
    If transport/session fails, resume using the same `job_id`.

    Args:
        job_id: The job ID returned from submit_query
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

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
    While job status is active, call this repeatedly (typically page=1) to
    refresh partial output. When job reaches completed, iterate all pages.
    If job fails, call once more to capture any partial output.

    Args:
        job_id: The job ID returned from submit_query
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1). Use total_pages from the response to iterate through all results.
        page_size: Number of records returned per page (default: 100, max: 1000).

    Returns:
        JSON string with job output fields such as `status`, `all_records`,
        `error`, `limit`, `mode`, `candidate_records`, `valid_records`,
        `progress_validated`, `page`, `page_size`, and `total_pages`.
        `mode` reflects the processing mode used (`"lite"` or `"base"`).
        Stop only after terminal status (`completed` or `failed`) and the
        final pull is done.
        Always iterate all pages while `page < total_pages` to fetch the full
        currently available result set.
        `page`, `page_size`, and `total_pages` only describe currently
        available records, not future records that may still be produced.
        Do not treat a single partial pull as final unless status is terminal.
        After terminal `completed`, verify collected records across pages match
        `valid_records`; if not, wait briefly and re-pull all pages once.
        When presenting only a sample batch (for example 10 records), always
        report `candidate_records`, `progress_validated`, `valid_records`, and
        pagination fields so users know:
        - whether more results are already available via pagination
        - whether more results may still appear (`progress_validated < candidate_records`)
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
async def continue_job(job_id: str, new_limit: int | None = None, api_key: str = "") -> str:
    """
    Expand a job by processing more records beyond the initial limit.

    This increases the number of records the system processes (which costs
    additional credits). Only use this when the user wants MORE data processed.

    This only applies to jobs originally submitted with `limit`.
    If a job was submitted without `limit`, there is nothing to continue.
    The new_limit must be greater than the previous limit when provided.
    If omitted, API defaults to your plan maximum.

    Args:
        job_id: The job ID to continue processing
        new_limit: Optional new record processing limit (must exceed the previous limit if provided).
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with job_id, previous_limit, new_limit, and status.
        After continuation is accepted, poll status again and pull results again.
    """
    try:
        body: dict[str, Any] = {"job_id": job_id}
        if new_limit is not None:
            validate_new_limit(new_limit)
            body["new_limit"] = new_limit
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/continue",
            json_data=body,
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
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1)
        page_size: Number of results per page (default: 100, max: 1000)

    Returns:
        JSON with list of your submitted jobs. Each job includes `mode`
        (`"lite"` or `"base"`) and `user_key` identifying the API key owner.
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
    limit: int | None = None,
    backfill: bool = True,
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
    - If `backfill=true`, reference job end_date must be within the last 7 days
    - If `backfill=false`, reference job age does not matter
    - Minimum schedule frequency depends on your plan

    Args:
        reference_job_id: ID of a completed job to use as the template
        schedule: Natural language schedule (e.g., 'every day at 9 AM EST', 'every Monday at 8 AM UTC', 'every 48 hours')
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        limit: Optional max records per run (minimum 10). If omitted, API uses plan default.
        backfill: Optional gap-fill toggle before first run (default true).
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
            "backfill": backfill,
        }
        if limit is not None:
            validate_monitor_limit(limit)
            body["limit"] = limit

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
async def list_monitors(api_key: str = "", page: int = 1, page_size: int = 100) -> str:
    """
    List all your monitors.

    Returns all monitors with their schedule, status, reference query, and webhook config.

    Args:
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100, max: 1000).

    Returns:
        JSON with total, page, page_size, total_pages, and monitors.
        Each monitor includes `user_key` identifying the API key owner.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/monitors/",
            params={"page": page, "page_size": page_size},
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
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

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
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
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
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

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
async def enable_monitor(monitor_id: str, api_key: str = "", backfill: bool | None = None) -> str:
    """
    Enable a previously disabled monitor to resume its scheduled runs.

    Args:
        monitor_id: The monitor ID to enable
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        backfill: Optional backfill behavior for resume.

    Returns:
        Confirmation that the monitor was enabled
    """
    try:
        body: dict[str, Any] | None = None
        if backfill is not None:
            body = {"backfill": backfill}
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path=f"/catchAll/monitors/{monitor_id}/enable",
            json_data=body,
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
    limit: int | None = None,
    webhook_url: str = "",
    webhook_method: str = "POST",
    webhook_headers: dict[str, str] | None = None,
    webhook_params: dict[str, str] | None = None,
    webhook_auth: list[str] | None = None,
) -> str:
    """
    Update a monitor's webhook configuration and per-run limit.

    Note: schedule and reference_job_id cannot be modified through this endpoint.

    Args:
        monitor_id: The monitor ID to update
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        limit: Optional updated maximum records per run (minimum 10).
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
        if limit is not None:
            validate_monitor_limit(limit)
            body["limit"] = limit

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


# ---------------------------------------------------------------------------
# Webhook tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_webhooks(api_key: str = "", page: int = 1, page_size: int = 100) -> str:
    """
    List all your webhooks.

    Use when:
    - You want to see all webhook endpoints configured in your account.
    - You need to find a webhook_id to pass to monitors (via webhook_ids) or jobs.

    Args:
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100, max: 1000).

    Returns:
        JSON with total, page, page_size, total_pages, and a webhooks list.
        Each item is a WebhookOutputData object: id, name, url, type, method,
        delivery_mode, headers, params, formatter_config, is_active,
        organization_id, created_by_user_id, created_at, updated_at.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/webhooks",
            params={"page": page, "page_size": page_size},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def create_webhook(
    name: str,
    url: str,
    api_key: str = "",
    method: str = "POST",
    headers: dict[str, str] | None = None,
    auth: list[str] | None = None,
    delivery_mode: str | None = None,
    formatter_config: dict[str, Any] | None = None,
) -> str:
    """
    Create a new webhook endpoint.

    Use when:
    - You want to register a URL to receive job or monitor result deliveries.
    - You need a webhook_id to attach to a monitor (via webhook_ids) or a job submission.

    Args:
        name: Human-readable name for the webhook (required).
        url: Target URL that will receive webhook deliveries (required).
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        method: HTTP method for delivery: 'POST' (default) or 'PUT'.
        headers: Optional dict of custom HTTP headers to include in deliveries.
        auth: Optional basic auth as [username, password].
        delivery_mode: Optional delivery mode: 'full' (default) or 'per_record'.
        formatter_config: Optional formatter configuration dict.

    Returns:
        JSON with `success`, `message`, and a `webhook` object — the new id is at
        `webhook.id` (NOT at the top level). The webhook object is a
        WebhookOutputData: id, name, url, type, delivery_mode, method, headers,
        params, formatter_config, is_active, organization_id, created_by_user_id,
        created_at, updated_at.

    Common API errors:
        - 400: bad request or invalid parameters.
        - 403: missing or invalid API key.
        - 422: input validation errors.
    """
    try:
        body: dict[str, Any] = {"name": name, "url": url, "method": method}
        if headers is not None:
            body["headers"] = headers
        if auth is not None:
            body["auth"] = auth
        if delivery_mode is not None:
            body["delivery_mode"] = delivery_mode
        if formatter_config is not None:
            body["formatter_config"] = formatter_config
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/webhooks",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_webhook(webhook_id: str, api_key: str = "") -> str:
    """
    Retrieve the full configuration of a specific webhook.

    Use when:
    - You want to inspect a webhook's URL, method, headers, or status by its ID.

    Args:
        webhook_id: The webhook ID to retrieve.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `success`, `message`, and a `webhook` object (the full
        WebhookOutputData: id, name, url, type, delivery_mode, method, headers,
        params, formatter_config, is_active, organization_id, created_by_user_id,
        created_at, updated_at).

    Common API errors:
        - 403: missing or invalid API key.
        - 404: webhook not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/webhooks/{webhook_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def update_webhook(
    webhook_id: str,
    api_key: str = "",
    name: str | None = None,
    url: str | None = None,
    method: str | None = None,
    headers: dict[str, str] | None = None,
    auth: list[str] | None = None,
    delivery_mode: str | None = None,
    formatter_config: dict[str, Any] | None = None,
) -> str:
    """
    Update an existing webhook's configuration.

    Use when:
    - You want to change a webhook's URL, method, headers, or other settings.
    - Only the fields you provide are updated; omitted fields remain unchanged.

    Args:
        webhook_id: The webhook ID to update.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        name: Updated webhook name.
        url: Updated target URL.
        method: Updated HTTP method: 'POST' or 'PUT'.
        headers: Updated dict of custom HTTP headers.
        auth: Updated basic auth as [username, password].
        delivery_mode: Updated delivery mode: 'full' or 'per_record'.
        formatter_config: Updated formatter configuration dict.

    Returns:
        JSON with `success`, `message`, and the updated `webhook` object
        (a WebhookOutputData; see get_webhook for its fields).

    Common API errors:
        - 403: missing or invalid API key.
        - 404: webhook not found.
        - 422: input validation errors.
    """
    try:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if url is not None:
            body["url"] = url
        if method is not None:
            body["method"] = method
        if headers is not None:
            body["headers"] = headers
        if auth is not None:
            body["auth"] = auth
        if delivery_mode is not None:
            body["delivery_mode"] = delivery_mode
        if formatter_config is not None:
            body["formatter_config"] = formatter_config
        result = await make_api_request(
            api_key=api_key,
            method="PATCH",
            path=f"/catchAll/webhooks/{webhook_id}",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def delete_webhook(webhook_id: str, api_key: str = "") -> str:
    """
    Permanently delete a webhook endpoint.

    Use when:
    - You want to remove a webhook from your account.

    Args:
        webhook_id: The webhook ID to delete.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON. On success the API returns an empty object `{}` with HTTP 200
        (there is no success/webhook_id/message body). A missing webhook
        returns 404, surfaced here as an error string.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: webhook not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="DELETE",
            path=f"/catchAll/webhooks/{webhook_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def test_webhook(webhook_id: str, api_key: str = "") -> str:
    """
    Send a test delivery to a webhook endpoint.

    Use when:
    - You want to verify a webhook URL is reachable and correctly configured
      before attaching it to a monitor or job.

    Args:
        webhook_id: The webhook ID to test.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `success`, `message`, `http_status_code` (the status the
        target URL returned to the test delivery), and `response_body`. If the
        target returns a non-2xx status the call is reported as an error string
        that includes that upstream status.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: webhook not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path=f"/catchAll/webhooks/{webhook_id}/test",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_user_limits(api_key: str = "") -> str:
    """
    Retrieve plan features and current usage limits for your API key.

    Use when:
    - You want to know how many records/jobs/monitors your plan allows.
    - You want to check current usage against plan limits before running a large job.

    Args:
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `features` — a list of billing features with usage, each containing:
        `name`, `code`, `value_type`, `value` (plan limit), and `current_usage`.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/user/limits",
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


# Patch mcp.http_app to always inject ApiKeyASGIMiddleware, regardless of how the
# server is invoked (uvicorn server:app, fastmcp run server.py:mcp, python server.py, etc.)
_original_http_app = mcp.http_app


def _http_app_with_api_key_middleware(*args: Any, middleware: list | None = None, **kwargs: Any) -> Any:
    mw = [StarletteMiddleware(ApiKeyASGIMiddleware)]
    if middleware:
        mw = mw + list(middleware)
    return _original_http_app(*args, middleware=mw, **kwargs)


mcp.http_app = _http_app_with_api_key_middleware  # type: ignore[method-assign]

# Module-level ASGI app for deployment via `uvicorn server:app`
app = mcp.http_app()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
