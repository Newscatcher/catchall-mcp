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
    DATASET_SORT_BY,
    DATASET_STATUSES,
    DELIVERY_MODES,
    ED_ASSOCIATION_TYPES,
    ENTITY_SORT_BY,
    ENTITY_STATUSES,
    ENTITY_TYPES,
    MAPPABLE_RESOURCE_TYPES,
    OWNERSHIP_VALUES,
    PROJECT_RESOURCE_TYPES,
    SORT_ORDERS,
    WEBHOOK_TYPES,
    EnrichmentDefinition,
    ValidatorDefinition,
    coerce_csv_file_content,
    coerce_definition_list,
    validate_choice,
    validate_enrichment_definitions,
    validate_http_method,
    validate_mode,
    validate_limit,
    validate_new_limit,
    validate_page_params,
    validate_sort,
    validate_validator_definitions,
    validate_webhook_auth,
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
    return_text: bool = False,
) -> dict[str, Any] | str:
    """Make an API request to CatchAll API.

    When ``return_text=True`` the raw response body is returned as a string
    instead of being JSON-decoded (use for CSV/text download endpoints).
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if return_text:
        headers["Accept"] = "text/csv, text/plain, */*"

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

        if return_text:
            return response.text

        try:
            return response.json()
        except json.JSONDecodeError:
            if response.text and response.text.strip():
                raise ValueError(f"API returned non-JSON response: {response.text[:500]}")
            return {}


async def make_api_upload(
    api_key: str,
    path: str,
    file_bytes: bytes,
    data: dict[str, Any] | None = None,
    filename: str = "upload.csv",
) -> dict[str, Any]:
    """POST a multipart/form-data CSV upload to the CatchAll API.

    httpx sets the multipart Content-Type (with boundary) itself, so unlike
    `make_api_request` no Content-Type header is set here.
    """
    headers = {"Accept": "application/json"}
    key = get_api_key(api_key)
    headers["x-api-key"] = key

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=120.0) as client:
        response = await client.post(
            path,
            headers=headers,
            files={"file": (filename, file_bytes, "text/csv")},
            data=data,
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
    limit: int | None = None,
    start_date: str = "",
    end_date: str = "",
    validators: list[ValidatorDefinition] | str | None = None,
    enrichments: list[EnrichmentDefinition] | str | None = None,
    mode: str = "",
    project_id: str = "",
    webhook_ids: list[str] | None = None,
    schema: str = "",
    connected_dataset_ids: list[str] | None = None,
    ed_score_min: int | None = None,
    ed_association_type: str = "",
    fetch_all_watchlist_news: bool = False,
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
    - When `connected_dataset_ids` is set, the `query` must describe the **topic or event type only**
      (e.g. "M&A activity", "regulatory filings", "executive changes"). Do NOT write things like
      "for my companies", "for the selected list of companies", or "news about my watchlist" — the
      entity filtering is applied automatically by the connected dataset. Mentioning companies in
      the query when a dataset is attached is redundant and degrades retrieval quality.
    - When `connected_dataset_ids` is set, entity-relevance validators (e.g.
      `company_is_primary_subject`) are generated automatically by the API. Do NOT add them
      manually to `validators` — they are redundant and may conflict with the auto-generated ones.
      Only pass validators that describe the **event or topic**, not entity filtering.
    - `start_date` and `end_date` filter by web page discovery date, not event date.
    - Discovery dates and extracted event dates can differ. For event-time accuracy, use event-focused validators/enrichments and verify `event_date` in pulled results.
    - `end_date` must be after `start_date`.
    - Dates outside your plan lookback limits return API 400.
    - `limit` controls processed record count (cost-affecting). Omit it to retrieve everything
      up to your plan's maximum. If provided, must be >= 10.
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
            If a company dataset will be attached, note that entity-relevance
            validators (e.g. `company_is_primary_subject`) will be auto-generated —
            do not ask for them here. Do not mention things like "company list will be attached".
        limit: Optional processing cap (minimum 10); affects cost. Omit to retrieve everything
            up to your plan's maximum.
        start_date: Optional ISO 8601 UTC start of search window.
        end_date: Optional ISO 8601 UTC end of search window.
        validators: Optional custom boolean validators (`name`, `description`, `type`), as array or JSON-string array.
            When `connected_dataset_ids` is set, do NOT include entity-relevance validators such as
            `company_is_primary_subject` — the API generates those automatically. Only add validators
            that describe the event or topic (e.g. `is_acquisition_event`).
        enrichments: Optional custom enrichments (`name`, `description`, `type`), as array or JSON-string array.
        mode: Optional job processing mode: `"lite"` (faster, lower cost, less detail) or `"base"` (default,
            full extraction). If omitted, the API defaults to `"base"`.
        project_id: Optional project ID to associate this job with.
        webhook_ids: Optional list of webhook IDs to notify when the job completes (max 5 per job).
            Use `list_webhooks` / `create_webhook` to get IDs.
        schema: Optional advanced custom JSON schema string that overrides the default extraction
            schema. Use `initialize_query` to discover a suitable schema.
        connected_dataset_ids: Optional list of dataset IDs whose entities narrow the retrieval
            scope. When set: (1) entity filtering is applied automatically — do NOT mention the
            company list or watchlist in `query`; (2) entity-relevance validators such as
            `company_is_primary_subject` are generated automatically — do NOT add them to
            `validators`. `ed_score_min` defaults to 2 if not provided.
        ed_score_min: Optional minimum entity-domain relevance score (1-10). Only relevant when
            `connected_dataset_ids` is set.
        ed_association_type: Optional filter on how strongly a watchlist entity must appear in
            each event. Only relevant when `connected_dataset_ids` is set.
            - `"event_associated"`: keep only events where the entity is a **direct actor** (default when connected_dataset_ids is set).
            - `"mention"`: keep all even where the entity is **merely referenced**.
        fetch_all_watchlist_news: When `True`, retrieves **all** news for connected watchlist
            entities without applying topic filtering from `query`. Requires
            `connected_dataset_ids` to be set. Default: `False`.

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
        if limit is not None:
            validate_limit(limit)
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
        if project_id:
            body["project_id"] = project_id
        if webhook_ids:
            body["webhook_ids"] = webhook_ids
        if schema:
            body["schema"] = schema
        if connected_dataset_ids:
            body["connected_dataset_ids"] = connected_dataset_ids
        if ed_score_min is not None:
            body["ed_score_min"] = ed_score_min
        if ed_association_type:
            body["ed_association_type"] = validate_choice(
                ed_association_type, ED_ASSOCIATION_TYPES, "ed_association_type"
            )
        if fetch_all_watchlist_news:
            body["fetch_all_watchlist_news"] = True

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
    fetch_all_watchlist_news: bool = False,
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
        query: Natural language query to preview (required). If you plan to attach a
            company dataset via `connected_dataset_ids` in the subsequent `submit_query`,
            do NOT reference the company list here — entity filtering is applied
            automatically by the dataset, not by the query text.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        context: Optional guidance on what to prioritize so suggested validators,
            enrichments, and dates align with your target data points. If a company
            dataset will be attached in `submit_query`, note that entity-relevance
            validators (e.g. `company_is_primary_subject`) will be auto-generated —
            do not ask for them here. Do not mention things like "company list will be attached".
            Focus on the event or topic only.
        fetch_all_watchlist_news: When `True`, signals that the subsequent job will
            retrieve all news for connected watchlist entities without topic filtering.
            Pass this when you intend to use `fetch_all_watchlist_news=True` in
            `submit_query` so the previewed validators/enrichments are generated
            accordingly. Requires `connected_dataset_ids` to be set in `submit_query`.
            Default: `False`.

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
        if fetch_all_watchlist_news:
            body["fetch_all_watchlist_news"] = True

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
async def pull_job_csv(job_id: str, api_key: str = "") -> str:
    """
    Download a job's results as a CSV file.

    Use when:
    - You want the full job output as a CSV for offline analysis or export.
    - Prefer this over `pull_results` when the consumer needs spreadsheet/CSV format.

    Args:
        job_id: The job ID to download as CSV.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        CSV text with all job result records.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: job not found or no results available yet.
    """
    try:
        return await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/pull/{job_id}/csv",
            return_text=True,
        )
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
async def list_user_jobs(
    api_key: str = "",
    page: int = 1,
    page_size: int = 100,
    search: str = "",
    ownership: str = "",
    project_id: str = "",
) -> str:
    """
    List all jobs submitted by you.

    Returns your job history with IDs, queries, statuses, and timestamps.

    Args:
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1)
        page_size: Number of results per page (default: 100, max: 1000)
        search: Optional text filter on the job query.
        ownership: Optional ownership filter: 'all', 'own', or 'shared'.
        project_id: Optional filter to jobs belonging to a specific project.

    Returns:
        JSON with list of your submitted jobs. Each job includes `mode`
        (`"lite"` or `"base"`) and `user_key` identifying the API key owner.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        if ownership:
            params["ownership"] = validate_choice(ownership, OWNERSHIP_VALUES, "ownership")
        if project_id:
            params["project_id"] = project_id
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/jobs/user",
            params=params,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def delete_job(job_id: str, api_key: str = "") -> str:
    """
    Permanently delete a job and its results.

    Use when:
    - You want to remove a job you no longer need from your account.

    Args:
        job_id: The job ID to delete.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `success`, `message`, and `job_id`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: job not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="DELETE",
            path=f"/catchAll/jobs/{job_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def validate_query(query: str, api_key: str = "") -> str:
    """
    Check the quality of a query before submitting a job ("Check Query Quality").

    Use when:
    - You want quick feedback on whether a query is well-formed for CatchAll
      before spending credits on a job.
    - You want concrete suggestions to improve a vague or overly broad query.

    Do not use when:
    - You want to preview auto-generated validators/enrichments (use `initialize_query`).
    - You want to actually run a search (use `submit_query`).

    Args:
        query: The natural-language query to assess (required).
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with:
        - `status`: overall quality — one of `critical`, `needs_work`, `good`.
        - `title`: brief assessment headline.
        - `description`: 2-3 sentence assessment.
        - `issues`: list of issue codes (for example `too_vague`, `too_short`,
          `missing_event_type`, `wrong_timeframe`).
        - `suggestions`: list of `{issue, message, example}` improvement tips.
        - `confidence`: number — confidence in the assessment.

    Common API errors:
        - 403: missing or invalid API key.
        - 422: input validation errors.
    """
    try:
        # v1.6.1: CheckQueryQualityRequestDto only accepts `query` — the optional
        # `context` field was removed from the API request schema.
        body: dict[str, Any] = {"query": query}
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/validate",
            json_data=body,
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
    timezone: str = "",
    webhook_ids: list[str] | None = None,
    limit: int | None = None,
    backfill: bool = True,
    project_id: str = "",
) -> str:
    """
    Create a recurring monitor from a completed job.

    Monitors re-run a job's query on a schedule. Use the explore -> refine -> automate
    pattern: submit a job, refine until results match, then create a monitor.

    The schedule is defined in natural language (e.g., 'every day at 9 AM EST').
    Always include a timezone (in the schedule text or via the `timezone` arg).
    API-enforced constraints apply:
    - If `backfill=true`, reference job end_date must be within the last 7 days
    - If `backfill=false`, reference job age does not matter
    - Minimum schedule frequency depends on your plan

    Webhooks are now centralized: register them with `create_webhook`, then pass
    their IDs here via `webhook_ids` (there is no inline webhook config anymore).

    Args:
        reference_job_id: ID of a completed job to use as the template
        schedule: Natural language schedule (e.g., 'every day at 9 AM EST', 'every Monday at 8 AM UTC', 'every 48 hours')
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        timezone: Optional IANA timezone for the schedule (e.g. 'America/New_York').
            Defaults to UTC. A timezone written into the schedule text overrides this.
        webhook_ids: Optional list of webhook IDs to notify on each run completion (max 5).
        limit: Optional max records per run (minimum 10). If omitted, API uses plan default.
        backfill: Optional gap-fill toggle before first run (default true).
        project_id: Optional project ID to associate this monitor with.

    Returns:
        JSON with monitor_id and status.
    """
    try:
        body: dict[str, Any] = {
            "reference_job_id": reference_job_id,
            "schedule": schedule,
            "backfill": backfill,
        }
        if timezone:
            body["timezone"] = timezone
        if webhook_ids:
            body["webhook_ids"] = webhook_ids
        if limit is not None:
            validate_limit(limit)
            body["limit"] = limit
        if project_id:
            body["project_id"] = project_id

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
async def list_monitors(
    api_key: str = "",
    page: int = 1,
    page_size: int = 100,
    search: str = "",
    ownership: str = "",
    project_id: str = "",
) -> str:
    """
    List all your monitors.

    Returns all monitors with their schedule, status, reference query, and webhook config.

    Args:
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100, max: 1000).
        search: Optional text filter on the monitor query.
        ownership: Optional ownership filter: 'all', 'own', or 'shared'.
        project_id: Optional filter to monitors belonging to a specific project.

    Returns:
        JSON with total, page, page_size, total_pages, and monitors.
        Each monitor includes `user_key` identifying the API key owner.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        if ownership:
            params["ownership"] = validate_choice(ownership, OWNERSHIP_VALUES, "ownership")
        if project_id:
            params["project_id"] = project_id
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/monitors/",
            params=params,
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
async def pull_monitor_csv(monitor_id: str, api_key: str = "") -> str:
    """
    Download the latest monitor run's results as a CSV file.

    Use when:
    - You want the most recent monitor run output as a CSV for offline analysis or export.
    - Prefer this over `pull_monitor_results` when the consumer needs spreadsheet/CSV format.

    Args:
        monitor_id: The monitor ID to download results for.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        CSV text with all records from the latest monitor run.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: monitor not found or no results available yet.
    """
    try:
        return await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/monitors/pull/{monitor_id}/csv",
            return_text=True,
        )
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
    webhook_ids: list[str] | None = None,
    limit: int | None = None,
) -> str:
    """
    Update a monitor's webhook assignments and per-run limit.

    Note: schedule and reference_job_id cannot be modified through this endpoint.
    Webhooks are centralized — pass webhook IDs (from `create_webhook`/`list_webhooks`).

    Args:
        monitor_id: The monitor ID to update
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        webhook_ids: Optional list of webhook IDs to assign to this monitor.
            Pass an empty list `[]` to clear all webhook assignments.
        limit: Optional updated maximum records per run (minimum 10).

    Returns:
        JSON with monitor_id and status.
    """
    try:
        body: dict[str, Any] = {}
        if webhook_ids is not None:
            body["webhook_ids"] = webhook_ids
        if limit is not None:
            validate_limit(limit)
            body["limit"] = limit

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
async def delete_monitor(monitor_id: str, api_key: str = "") -> str:
    """
    Permanently delete a monitor and stop its scheduled runs.

    Use when:
    - You want to remove a monitor entirely (use `disable_monitor` to only pause it).

    Args:
        monitor_id: The monitor ID to delete.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `success`, `message`, and `monitor_id`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: monitor not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="DELETE",
            path=f"/catchAll/monitors/{monitor_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_monitor_status(monitor_id: str, api_key: str = "") -> str:
    """
    Get the status history of a monitor.

    Use when:
    - You want to see the timeline of a monitor's state changes (e.g. active,
      disabled, errored) and any related details.

    Args:
        monitor_id: The monitor ID to inspect.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `success`, `message`, `monitor_id`, `total_statuses`, and a
        `statuses` list. Each status item has `id`, `status`, `created_at`, and
        optional `additional_information`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: monitor not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/monitors/{monitor_id}/status",
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
    type: str = "",
    method: str = "POST",
    delivery_mode: str | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    auth: dict[str, Any] | None = None,
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
        type: Optional webhook target type: 'generic' (default), 'slack', 'teams', or 'custom'.
            'slack'/'teams' send pre-formatted payloads; 'generic'/'custom' send the raw result payload.
        method: HTTP method for delivery (default 'POST'). One of GET, POST, PUT, PATCH, DELETE.
        delivery_mode: Optional delivery mode: 'full' (default, whole result set in one call)
            or 'per_record' (one call per article).
        headers: Optional dict of custom HTTP headers to include in deliveries.
        params: Optional dict of query string parameters appended to the webhook URL.
        auth: Optional auth object forwarded with each delivery. One of:
            - {"type": "bearer", "token": "..."}
            - {"type": "api_key", "header": "X-API-Key", "value": "..."}
            - {"type": "basic", "username": "...", "password": "..."}
        formatter_config: Optional custom payload transformation config dict.

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
        body: dict[str, Any] = {"name": name, "url": url, "method": validate_http_method(method)}
        if type:
            body["type"] = validate_choice(type, WEBHOOK_TYPES, "type")
        if delivery_mode is not None:
            body["delivery_mode"] = validate_choice(delivery_mode, DELIVERY_MODES, "delivery_mode")
        if headers is not None:
            body["headers"] = headers
        if params is not None:
            body["params"] = params
        if auth is not None:
            body["auth"] = validate_webhook_auth(auth)
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
    type: str | None = None,
    method: str | None = None,
    delivery_mode: str | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    auth: dict[str, Any] | None = None,
    formatter_config: dict[str, Any] | None = None,
    is_active: bool | None = None,
) -> str:
    """
    Update an existing webhook's configuration.

    Use when:
    - You want to change a webhook's URL, method, headers, or other settings.
    - You want to enable or disable a webhook (set `is_active`).
    - Only the fields you provide are updated; omitted fields remain unchanged.

    Args:
        webhook_id: The webhook ID to update.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        name: Updated webhook name.
        url: Updated target URL.
        type: Updated webhook type: 'generic', 'slack', 'teams', or 'custom'.
        method: Updated HTTP method: one of GET, POST, PUT, PATCH, DELETE.
        delivery_mode: Updated delivery mode: 'full' or 'per_record'.
        headers: Updated dict of custom HTTP headers.
        params: Updated dict of query string parameters.
        auth: Updated auth object. One of:
            - {"type": "bearer", "token": "..."}
            - {"type": "api_key", "header": "X-API-Key", "value": "..."}
            - {"type": "basic", "username": "...", "password": "..."}
        formatter_config: Updated formatter configuration dict.
        is_active: Set to false to disable the webhook (stop deliveries), true to re-enable it.

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
        if type is not None:
            body["type"] = validate_choice(type, WEBHOOK_TYPES, "type")
        if method is not None:
            body["method"] = validate_http_method(method)
        if delivery_mode is not None:
            body["delivery_mode"] = validate_choice(delivery_mode, DELIVERY_MODES, "delivery_mode")
        if headers is not None:
            body["headers"] = headers
        if params is not None:
            body["params"] = params
        if auth is not None:
            body["auth"] = validate_webhook_auth(auth)
        if formatter_config is not None:
            body["formatter_config"] = formatter_config
        if is_active is not None:
            body["is_active"] = is_active
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
async def test_webhook(webhook_id: str, api_key: str = "", payload: dict[str, Any] | None = None) -> str:
    """
    Send a test delivery to a webhook endpoint.

    Use when:
    - You want to verify a webhook URL is reachable and correctly configured
      before attaching it to a monitor or job.

    Args:
        webhook_id: The webhook ID to test.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        payload: Optional custom JSON object to send as the test body. If omitted,
            the API sends a default sample payload.

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
        body: dict[str, Any] | None = {"payload": payload} if payload is not None else None
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path=f"/catchAll/webhooks/{webhook_id}/test",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def assign_webhook_resource(
    webhook_id: str,
    resource_type: str,
    resource_id: str,
    api_key: str = "",
) -> str:
    """
    Map a resource (job, monitor, or monitor_group) to a webhook.

    Use when:
    - You want a webhook to fire for a specific job or monitor's deliveries.

    Args:
        webhook_id: The webhook ID to attach the resource to.
        resource_type: Resource type: 'job', 'monitor', or 'monitor_group'.
        resource_id: The ID of the job/monitor/monitor_group to map.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `success`, `message`, `already_existed`, and a `mapping` object
        (id, webhook_id, resource_type, resource_id, assigned_at).

    Common API errors:
        - 403: missing or invalid API key.
        - 404: webhook or resource not found.
        - 422: invalid resource_type.
    """
    try:
        validate_choice(resource_type, MAPPABLE_RESOURCE_TYPES, "resource_type")
        body = {"resource_type": resource_type, "resource_id": resource_id}
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path=f"/catchAll/webhooks/{webhook_id}/resources",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_webhook_resources(
    webhook_id: str,
    api_key: str = "",
    resource_type: str = "",
    page: int = 1,
    page_size: int = 100,
) -> str:
    """
    List the resources mapped to a webhook.

    Use when:
    - You want to see which jobs/monitors a webhook is attached to.

    Args:
        webhook_id: The webhook ID whose resource mappings you want.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        resource_type: Optional filter: 'job', 'monitor', or 'monitor_group'.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100, max: 1000).

    Returns:
        JSON with `total`, `page`, `page_size`, `total_pages`, and a `resources`
        list of mapping objects (id, webhook_id, resource_type, resource_id, assigned_at).

    Common API errors:
        - 403: missing or invalid API key.
        - 404: webhook not found.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if resource_type:
            params["resource_type"] = validate_choice(resource_type, MAPPABLE_RESOURCE_TYPES, "resource_type")
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/webhooks/{webhook_id}/resources",
            params=params,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def remove_webhook_resource(
    webhook_id: str,
    resource_type: str,
    resource_id: str,
    api_key: str = "",
) -> str:
    """
    Unmap a resource from a webhook.

    Use when:
    - You want to stop a webhook from firing for a specific job or monitor.

    Args:
        webhook_id: The webhook ID to detach the resource from.
        resource_type: Resource type: 'job', 'monitor', or 'monitor_group'.
        resource_id: The ID of the mapped job/monitor/monitor_group.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON. On success the API returns an empty object `{}` with HTTP 200.
        A missing mapping returns 404, surfaced here as an error string.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: mapping not found.
    """
    try:
        validate_choice(resource_type, MAPPABLE_RESOURCE_TYPES, "resource_type")
        result = await make_api_request(
            api_key=api_key,
            method="DELETE",
            path=f"/catchAll/webhooks/{webhook_id}/resources/{resource_type}/{resource_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_resource_webhooks(
    resource_type: str,
    resource_id: str,
    api_key: str = "",
    is_active: bool | None = None,
    page: int = 1,
    page_size: int = 100,
) -> str:
    """
    List the webhooks mapped to a specific resource (job/monitor/monitor_group).

    Use when:
    - You have a job or monitor ID and want to know which webhooks will fire for it.

    Args:
        resource_type: Resource type: 'job', 'monitor', or 'monitor_group'.
        resource_id: The ID of the job/monitor/monitor_group.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        is_active: Optional filter — only active (true) or inactive (false) webhooks.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100, max: 1000).

    Returns:
        JSON with pagination fields and the webhooks mapped to this resource
        (each a WebhookOutputData; see get_webhook for its fields).

    Common API errors:
        - 403: missing or invalid API key.
        - 422: invalid resource_type.
    """
    try:
        validate_choice(resource_type, MAPPABLE_RESOURCE_TYPES, "resource_type")
        validate_page_params(page, page_size, max_page_size=1000)
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if is_active is not None:
            params["is_active"] = is_active
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/resources/{resource_type}/{resource_id}/webhooks",
            params=params,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_webhook_history(
    resource_type: str,
    resource_id: str,
    api_key: str = "",
    page: int = 1,
    page_size: int = 50,
) -> str:
    """
    Get the webhook delivery history for a resource (job/monitor/monitor_group).

    Use when:
    - You want to see past webhook delivery attempts and their outcomes for a
      specific job or monitor.

    Args:
        resource_type: Resource type: 'job', 'monitor', or 'monitor_group'.
        resource_id: The ID of the job/monitor/monitor_group.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 50, max: 500).

    Returns:
        JSON with the delivery history records for the resource.

    Common API errors:
        - 403: missing or invalid API key.
        - 422: invalid resource_type.
    """
    try:
        validate_choice(resource_type, MAPPABLE_RESOURCE_TYPES, "resource_type")
        validate_page_params(page, page_size, max_page_size=500)
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/webhook-history",
            params={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "page": page,
                "page_size": page_size,
            },
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


# ---------------------------------------------------------------------------
# Dataset tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_dataset(
    name: str,
    api_key: str = "",
    description: str = "",
    entity_ids: list[str] | None = None,
    project_id: str = "",
) -> str:
    """
    Create a new dataset.

    Datasets are collections of entities (companies/people). Connect a dataset to
    a job via `submit_query(connected_dataset_ids=[...])` to narrow retrieval scope.

    Args:
        name: Human-readable dataset name (required).
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        description: Optional dataset description.
        entity_ids: Optional list of existing entity IDs to seed the dataset with.
        project_id: Optional project ID to associate this dataset with.

    Returns:
        JSON dataset object: `id`, `organization_id`, `name`, `description`,
        `entity_count`, `entity_status_breakdown`, `health_score`,
        `health_breakdown`, `latest_status`, `created_by_user_id`,
        `sharing_info`, `created_at`, `updated_at`.

    Common API errors:
        - 403: missing or invalid API key.
        - 422: input validation errors.
    """
    try:
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        if entity_ids:
            body["entity_ids"] = entity_ids
        if project_id:
            body["project_id"] = project_id
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/datasets/",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_datasets(
    api_key: str = "",
    page: int = 1,
    page_size: int = 100,
    search: str = "",
    latest_status: str = "",
    sort_by: str = "",
    sort_order: str = "",
    ownership: str = "",
    project_id: str = "",
) -> str:
    """
    List your datasets.

    Args:
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100, max: 1000).
        search: Optional text filter on the dataset name.
        latest_status: Optional status filter: 'pending', 'enriching', 'ready', or 'failed'.
        sort_by: Optional sort field: 'name', 'created_at', or 'status'.
        sort_order: Optional sort direction: 'asc' or 'desc'.
        ownership: Optional ownership filter: 'all', 'own', or 'shared'.
        project_id: Optional filter to datasets belonging to a specific project.

    Returns:
        JSON with `datasets` (list of dataset objects), `total`, `page`, `page_size`.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        if latest_status:
            params["latest_status"] = validate_choice(latest_status, DATASET_STATUSES, "latest_status")
        if sort_by:
            params["sort_by"] = validate_choice(sort_by, DATASET_SORT_BY, "sort_by")
        if sort_order:
            params["sort_order"] = validate_choice(sort_order, SORT_ORDERS, "sort_order")
        if ownership:
            params["ownership"] = validate_choice(ownership, OWNERSHIP_VALUES, "ownership")
        if project_id:
            params["project_id"] = project_id
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/datasets/",
            params=params,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_dataset(dataset_id: str, api_key: str = "") -> str:
    """
    Get a single dataset's details.

    Args:
        dataset_id: The dataset ID to retrieve.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON dataset object (see `create_dataset` for fields), including
        `entity_count`, `entity_status_breakdown`, `health_score`, and `latest_status`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: dataset not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/datasets/{dataset_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def update_dataset(
    dataset_id: str,
    api_key: str = "",
    name: str | None = None,
    description: str | None = None,
) -> str:
    """
    Update a dataset's name and/or description.

    Args:
        dataset_id: The dataset ID to update.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        name: Optional new dataset name.
        description: Optional new dataset description.

    Returns:
        JSON of the updated dataset object.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: dataset not found.
        - 422: input validation errors.
    """
    try:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        result = await make_api_request(
            api_key=api_key,
            method="PATCH",
            path=f"/catchAll/datasets/{dataset_id}",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def delete_dataset(dataset_id: str, api_key: str = "") -> str:
    """
    Permanently delete a dataset.

    The entities the dataset referenced are not deleted; only the dataset and its
    entity associations are removed.

    Args:
        dataset_id: The dataset ID to delete.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON. On success the API returns an empty object `{}` with HTTP 200.
        A missing dataset returns 404, surfaced here as an error string.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: dataset not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="DELETE",
            path=f"/catchAll/datasets/{dataset_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def add_dataset_entities(dataset_id: str, entity_ids: list[str], api_key: str = "") -> str:
    """
    Add existing entities to a dataset.

    Args:
        dataset_id: The dataset ID to add entities to.
        entity_ids: List of entity IDs to add (required).
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `dataset_id` and `affected_count` (number of entities added).

    Common API errors:
        - 403: missing or invalid API key.
        - 404: dataset not found.
        - 422: input validation errors.
    """
    try:
        if not entity_ids:
            raise ValueError("entity_ids must be a non-empty list.")
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path=f"/catchAll/datasets/{dataset_id}/entities",
            json_data={"entity_ids": entity_ids},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def remove_dataset_entities(dataset_id: str, entity_ids: list[str], api_key: str = "") -> str:
    """
    Remove entities from a dataset (the entities themselves are not deleted).

    Args:
        dataset_id: The dataset ID to remove entities from.
        entity_ids: List of entity IDs to remove (required).
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `dataset_id` and `affected_count` (number of entities removed).

    Common API errors:
        - 403: missing or invalid API key.
        - 404: dataset not found.
    """
    try:
        if not entity_ids:
            raise ValueError("entity_ids must be a non-empty list.")
        result = await make_api_request(
            api_key=api_key,
            method="DELETE",
            path=f"/catchAll/datasets/{dataset_id}/entities",
            json_data={"entity_ids": entity_ids},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_dataset_entities(
    dataset_id: str,
    api_key: str = "",
    page: int = 1,
    page_size: int = 100,
    search: str = "",
    status: str = "",
    entity_type: str = "",
    sort_by: str = "",
    sort_order: str = "",
) -> str:
    """
    List the entities contained in a dataset.

    Args:
        dataset_id: The dataset ID whose entities you want.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100).
        search: Optional text filter on entity name.
        status: Optional status filter: 'pending', 'enriching', 'ready', or 'failed'.
        entity_type: Optional type filter: 'company' or 'person'.
        sort_by: Optional sort field: 'created_at', 'name', or 'status'.
        sort_order: Optional sort direction: 'asc' or 'desc'.

    Returns:
        JSON with `entities` (list of entity summaries: `id`, `name`,
        `entity_type`, `status`, `description`, `attributes`), `total`, `page`,
        `page_size`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: dataset not found.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        body: dict[str, Any] = {"page": page, "page_size": page_size}
        if search:
            body["search"] = search
        if status:
            body["status"] = validate_choice(status, ENTITY_STATUSES, "status")
        if entity_type:
            body["entity_type"] = validate_choice(entity_type, ENTITY_TYPES, "entity_type")
        if sort_by:
            body["sort_by"] = validate_choice(sort_by, ENTITY_SORT_BY, "sort_by")
        if sort_order:
            body["sort_order"] = validate_choice(sort_order, SORT_ORDERS, "sort_order")
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path=f"/catchAll/datasets/{dataset_id}/entities/list",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_dataset_status(dataset_id: str, api_key: str = "") -> str:
    """
    Get the status history of a dataset (e.g. its enrichment progress over time).

    Args:
        dataset_id: The dataset ID to inspect.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `dataset_id` and `history` — a list of status entries, each
        with `status`, `additional_information`, and `created_at`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: dataset not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/datasets/{dataset_id}/status",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def create_dataset_from_csv(
    name: str,
    file: str,
    api_key: str = "",
    description: str = "",
    project_id: str = "",
) -> str:
    """
    Create a new dataset by uploading a CSV file.

    The CSV must have at least a `name` column. For meaningful entity
    enrichment each row should also include a `domain` column or a
    `description` column (or both) — a row with only a name is accepted but
    produces lower-quality enrichment. Additional columns are mapped to entity
    attributes. Max file size is plan-dependent. To add CSV rows to an
    existing dataset, use `append_csv_to_dataset` instead.

    Args:
        name: Human-readable dataset name (required).
        file: CSV content (required) — raw CSV text or standard base64-encoded
            CSV, capped at 10 MB after decoding. Server-side file paths are
            not accepted.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        description: Optional dataset description.
        project_id: Optional project ID to associate this dataset with (new in 1.6.1).

    Returns:
        JSON with:
        - `dataset_id`: unique identifier of the created dataset.
        - `dataset_name`: name of the created dataset.
        - `entities_created`: number of entities created from the CSV.
        - `validation_report`: `{total_rows, valid_rows, skipped_count,
          skipped_rows}` summary of CSV processing.

    Common API errors:
        - 400: file is not a valid CSV.
        - 403: missing or invalid API key.
        - 422: CSV parsing error or missing required columns.
    """
    try:
        if not name or not name.strip():
            raise ValueError("name is required.")
        file_bytes = coerce_csv_file_content(file)
        data: dict[str, Any] = {"name": name}
        if description:
            data["description"] = description
        if project_id:
            data["project_id"] = project_id
        result = await make_api_upload(
            api_key=api_key,
            path="/catchAll/datasets/upload",
            file_bytes=file_bytes,
            data=data,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def append_csv_to_dataset(dataset_id: str, file: str, api_key: str = "") -> str:
    """
    Append entities from a CSV file to an existing dataset.

    Parses the CSV and appends its entities to the dataset. Each row must
    have a `name` column; include a `domain` or `description` column (or both)
    for meaningful enrichment. Duplicate rows (by name) are skipped. To create
    a new dataset from a CSV, use `create_dataset_from_csv` instead.

    Args:
        dataset_id: The dataset ID to append entities to (required).
        file: CSV content (required) — raw CSV text or standard base64-encoded
            CSV, capped at 10 MB after decoding. Server-side file paths are
            not accepted.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with:
        - `dataset_id`: ID of the dataset that was updated.
        - `entities_created`: number of new entities created from the CSV.
        - `validation_report`: `{total_rows, valid_rows, skipped_count,
          skipped_rows}` summary of CSV processing.

    Common API errors:
        - 400: file is not a valid CSV.
        - 403: missing or invalid API key, or dataset does not belong to this user.
        - 404: dataset not found.
        - 422: CSV parsing error.
    """
    try:
        if not dataset_id or not dataset_id.strip():
            raise ValueError("dataset_id is required.")
        file_bytes = coerce_csv_file_content(file)
        result = await make_api_upload(
            api_key=api_key,
            path=f"/catchAll/datasets/{dataset_id}/upload",
            file_bytes=file_bytes,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


# ---------------------------------------------------------------------------
# Entity tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_entity(
    name: str,
    api_key: str = "",
    entity_type: str = "",
    description: str = "",
    external_entity_id: str | None = None,
    additional_attributes: dict[str, Any] | None = None,
) -> str:
    """
    Create a single entity (a company or person).

    ``name`` is required plus at least one identifying
    field: either ``description`` or ``additional_attributes.company_attributes.domain``.

    Args:
        name: Entity name (required).
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        entity_type: Optional entity type: 'company' (default) or 'person'.
        description: Optional description of the entity.
        external_entity_id: Optional customer-supplied identifier linking this entity to
            an external system's record (new in 1.6.3).
        additional_attributes: Optional structured attributes. For companies, use
            `{"company_attributes": {"alternative_names": [...], "domain": "...",
            "key_persons": [...], "description": "..."}}`.

    Returns:
        JSON with `id` and `status` (e.g. 'pending' while enrichment runs).

    Common API errors:
        - 403: missing or invalid API key.
        - 422: input validation errors.
    """
    try:
        body: dict[str, Any] = {"name": name}
        if entity_type:
            body["entity_type"] = validate_choice(entity_type, ENTITY_TYPES, "entity_type")
        if description:
            body["description"] = description
        if external_entity_id is not None:
            body["external_entity_id"] = external_entity_id
        if additional_attributes is not None:
            body["additional_attributes"] = additional_attributes
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/entities/",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_entities(
    api_key: str = "",
    page: int = 1,
    page_size: int = 100,
    search: str = "",
    status: str = "",
    entity_type: str = "",
    sort_by: str = "",
    sort_order: str = "",
) -> str:
    """
    List your entities.

    Args:
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100, max: 1000).
        search: Optional text filter on entity name.
        status: Optional status filter: 'pending', 'enriching', 'ready', or 'failed'.
        entity_type: Optional type filter: 'company' or 'person'.
        sort_by: Optional sort field: 'created_at', 'name', or 'status'.
        sort_order: Optional sort direction: 'asc' or 'desc'.

    Returns:
        JSON with `entities` (list of full entity objects), `total`, `page`, `page_size`.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        if status:
            params["status"] = validate_choice(status, ENTITY_STATUSES, "status")
        if entity_type:
            params["entity_type"] = validate_choice(entity_type, ENTITY_TYPES, "entity_type")
        if sort_by:
            params["sort_by"] = validate_choice(sort_by, ENTITY_SORT_BY, "sort_by")
        if sort_order:
            params["sort_order"] = validate_choice(sort_order, SORT_ORDERS, "sort_order")
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/entities/",
            params=params,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def create_entities_batch(
    entities: list[dict[str, Any]] | str,
    api_key: str = "",
) -> str:
    """
    Create multiple entities in one call.

    Args:
        entities: A list of entity objects. Each object requires a ``name``
            plus one identifying field for good enrichment: either a top-level
            ``"description"`` or ``"additional_attributes": {"company_attributes": {"domain": "..."}}``.
            Also accepts optional ``entity_type`` ('company'/'person').
            May also be passed as a JSON-string array.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `entities` (list of `{id, status}`) and `count`.

    Common API errors:
        - 403: missing or invalid API key.
        - 422: input validation errors.
    """
    try:
        parsed = coerce_definition_list(entities, "entities")
        if not parsed:
            raise ValueError("entities must be a non-empty array.")
        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise ValueError(f"entities[{idx}] must be an object.")
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"entities[{idx}].name must be a non-empty string.")
            entry: dict[str, Any] = {"name": name}
            if item.get("entity_type"):
                entry["entity_type"] = validate_choice(
                    item["entity_type"], ENTITY_TYPES, f"entities[{idx}].entity_type"
                )
            if item.get("description"):
                entry["description"] = item["description"]
            if item.get("additional_attributes") is not None:
                entry["additional_attributes"] = item["additional_attributes"]
            normalized.append(entry)
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/entities/batch",
            json_data={"entities": normalized},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_entity(entity_id: str, api_key: str = "") -> str:
    """
    Get a single entity's details.

    Args:
        entity_id: The entity ID to retrieve.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON entity object: `id`, `entity_type`, `organization_id`, `name`,
        `description`, `additional_attributes`, `status`, `created_by_user_id`,
        `created_at`, `updated_at`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: entity not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/entities/{entity_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def update_entity(
    entity_id: str,
    api_key: str = "",
    name: str | None = None,
    description: str | None = None,
    external_entity_id: str | None = None,
    additional_attributes: dict[str, Any] | None = None,
) -> str:
    """
    Update an entity's name, description, external_entity_id, and/or attributes.

    Args:
        entity_id: The entity ID to update.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        name: Optional new entity name.
        description: Optional new description.
        external_entity_id: Optional customer-supplied identifier linking this entity to
            an external system's record (new in 1.6.3).
        additional_attributes: Optional updated structured attributes
            (see `create_entity` for the company_attributes shape).

    Returns:
        JSON of the updated entity object.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: entity not found.
        - 422: input validation errors.
    """
    try:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if external_entity_id is not None:
            body["external_entity_id"] = external_entity_id
        if additional_attributes is not None:
            body["additional_attributes"] = additional_attributes
        result = await make_api_request(
            api_key=api_key,
            method="PATCH",
            path=f"/catchAll/entities/{entity_id}",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def delete_entity(entity_id: str, api_key: str = "") -> str:
    """
    Permanently delete an entity.

    Args:
        entity_id: The entity ID to delete.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON. On success the API returns an empty object `{}` with HTTP 200.
        A missing entity returns 404, surfaced here as an error string.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: entity not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="DELETE",
            path=f"/catchAll/entities/{entity_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


# ---------------------------------------------------------------------------
# Project tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_project(name: str, api_key: str = "", description: str = "") -> str:
    """
    Create a new project.

    Projects group related resources (jobs, monitors, datasets, monitor_groups)
    so you can organize work and filter listings by `project_id`.

    Args:
        name: Human-readable project name (required).
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        description: Optional project description.

    Returns:
        JSON with `success`, `message`, `project_id`, and `name`.
        Save `project_id` to attach resources or pass to `submit_query`/`create_monitor`.

    Common API errors:
        - 403: missing or invalid API key.
        - 422: input validation errors.
    """
    try:
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path="/catchAll/projects/",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_projects(
    api_key: str = "",
    page: int = 1,
    page_size: int = 100,
    search: str = "",
    ownership: str = "",
) -> str:
    """
    List your projects.

    Args:
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100, max: 1000).
        search: Optional text filter on the project name.
        ownership: Optional ownership filter: 'all', 'own', or 'shared'.

    Returns:
        JSON with `total`, `page`, `page_size`, `total_pages`, and a `projects`
        list. Each project has `project_id`, `name`, `description`,
        `resources_count`, `created_at`, `updated_at`, and `sharing_info`.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        if ownership:
            params["ownership"] = validate_choice(ownership, OWNERSHIP_VALUES, "ownership")
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path="/catchAll/projects/",
            params=params,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_project(project_id: str, api_key: str = "") -> str:
    """
    Get a single project's details.

    Args:
        project_id: The project ID to retrieve.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `success`, `message`, `project_id`, `name`, `description`,
        `resources_count`, `created_at`, `updated_at`, and `sharing_info`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: project not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/projects/{project_id}",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def update_project(
    project_id: str,
    api_key: str = "",
    name: str | None = None,
    description: str | None = None,
) -> str:
    """
    Update a project's name and/or description.

    Only the fields you provide are changed.

    Args:
        project_id: The project ID to update.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        name: Optional new project name.
        description: Optional new project description.

    Returns:
        JSON with `success`, `message`, and `project_id`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: project not found.
        - 422: input validation errors.
    """
    try:
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        result = await make_api_request(
            api_key=api_key,
            method="PATCH",
            path=f"/catchAll/projects/{project_id}",
            json_data=body,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def delete_project(project_id: str, api_key: str = "", delete_resources: bool = False) -> str:
    """
    Delete a project.

    By default the project's resources (jobs, monitors, etc.) are detached but
    kept. Set `delete_resources=true` to also delete the contained resources.

    Args:
        project_id: The project ID to delete.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        delete_resources: If true, also delete the project's resources (default false).

    Returns:
        JSON with `success`, `message`, `project_id`, and `deleted_resources`
        (a map of resource_type -> count deleted).

    Common API errors:
        - 403: missing or invalid API key.
        - 404: project not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="DELETE",
            path=f"/catchAll/projects/{project_id}",
            params={"delete_resources": delete_resources},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def get_project_overview(project_id: str, api_key: str = "") -> str:
    """
    Get a project's resource overview (counts grouped by resource type and status).

    Args:
        project_id: The project ID to summarize.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `project_id` and `overview` — a nested map of
        resource_type -> {status -> count}.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: project not found.
    """
    try:
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/projects/{project_id}/overview",
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def add_project_resources(
    project_id: str,
    resources: list[dict[str, str]] | str,
    api_key: str = "",
) -> str:
    """
    Add one or more resources to a project.

    Args:
        project_id: The project ID to add resources to.
        resources: A list of resource objects, each `{"resource_type": ..., "resource_id": ...}`.
            `resource_type` is one of: 'job', 'monitor', 'dataset', 'monitor_group'.
            May also be passed as a JSON-string array for client compatibility.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `success`, `message`, and a `results` list. Each result has
        `resource_type`, `resource_id`, `success`, `message`, and `already_exists`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: project not found.
        - 422: invalid resource entry.
    """
    try:
        parsed = coerce_definition_list(resources, "resources")
        if not parsed:
            raise ValueError("resources must be a non-empty array.")
        normalized: list[dict[str, str]] = []
        for idx, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise ValueError(f"resources[{idx}] must be an object.")
            rtype = item.get("resource_type")
            rid = item.get("resource_id")
            if not isinstance(rtype, str):
                raise ValueError(f"resources[{idx}].resource_type is required.")
            validate_choice(rtype, PROJECT_RESOURCE_TYPES, f"resources[{idx}].resource_type")
            if not isinstance(rid, str) or not rid:
                raise ValueError(f"resources[{idx}].resource_id must be a non-empty string.")
            normalized.append({"resource_type": rtype, "resource_id": rid})
        result = await make_api_request(
            api_key=api_key,
            method="POST",
            path=f"/catchAll/projects/{project_id}/resources",
            json_data={"resources": normalized},
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def list_project_resources(
    project_id: str,
    api_key: str = "",
    resource_type: str = "",
    page: int = 1,
    page_size: int = 100,
) -> str:
    """
    List the resources contained in a project.

    Args:
        project_id: The project ID whose resources you want.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.
        resource_type: Optional filter: 'job', 'monitor', 'dataset', or 'monitor_group'.
        page: Page number for pagination (default: 1).
        page_size: Number of results per page (default: 100, max: 1000).

    Returns:
        JSON with `total`, `page`, `page_size`, `total_pages`, and a `resources`
        list. Each item has `resource_type`, `resource_id`, `name`, `created_at`,
        and `metadata`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: project not found.
    """
    try:
        validate_page_params(page, page_size, max_page_size=1000)
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if resource_type:
            params["resource_type"] = validate_choice(resource_type, PROJECT_RESOURCE_TYPES, "resource_type")
        result = await make_api_request(
            api_key=api_key,
            method="GET",
            path=f"/catchAll/projects/{project_id}/resources",
            params=params,
        )
        return json.dumps(result, indent=2)
    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


@mcp.tool()
async def remove_project_resource(
    project_id: str,
    resource_type: str,
    resource_id: str,
    api_key: str = "",
) -> str:
    """
    Remove a single resource from a project.

    Args:
        project_id: The project ID to remove the resource from.
        resource_type: Resource type: 'job', 'monitor', 'dataset', or 'monitor_group'.
        resource_id: The ID of the resource to remove.
        api_key: CatchAll API key. Optional if provided via x-api-key header or CATCHALL_API_KEY env var.

    Returns:
        JSON with `success` and `message`.

    Common API errors:
        - 403: missing or invalid API key.
        - 404: project or resource mapping not found.
    """
    try:
        validate_choice(resource_type, PROJECT_RESOURCE_TYPES, "resource_type")
        result = await make_api_request(
            api_key=api_key,
            method="DELETE",
            path=f"/catchAll/projects/{project_id}/resources/{resource_type}/{resource_id}",
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
