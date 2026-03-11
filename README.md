# Newscatcher CatchAll MCP Server

MCP server for the NewsCatcher CatchAll Web Search API.

## Tool To Endpoint Mapping

| MCP Tool | Method | Endpoint |
| --- | --- | --- |
| `initialize_query` | `POST` | `/catchAll/initialize` |
| `submit_query` | `POST` | `/catchAll/submit` |
| `continue_job` | `POST` | `/catchAll/continue` |
| `list_user_jobs` | `GET` | `/catchAll/jobs/user` |
| `get_job_status` | `GET` | `/catchAll/status/{job_id}` |
| `pull_results` | `GET` | `/catchAll/pull/{job_id}` |
| `create_monitor` | `POST` | `/catchAll/monitors/create` |
| `update_monitor` | `PATCH` | `/catchAll/monitors/{monitor_id}` |
| `list_monitors` | `GET` | `/catchAll/monitors` |
| `list_monitor_jobs` | `GET` | `/catchAll/monitors/{monitor_id}/jobs` |
| `pull_monitor_results` | `GET` | `/catchAll/monitors/pull/{monitor_id}` |
| `enable_monitor` | `POST` | `/catchAll/monitors/{monitor_id}/enable` |
| `disable_monitor` | `POST` | `/catchAll/monitors/{monitor_id}/disable` |
| `check_health` | `GET` | `/health` |
| `get_version` | `GET` | `/version` |

## Authentication

API key precedence (highest to lowest):

1. `api_key` tool parameter
2. URL query parameter `?apiKey=...`
3. `CATCHALL_API_KEY` environment variable

`check_health` and `get_version` do not require API key auth.

## Core Workflow (Jobs)

1. Optional: call `initialize_query` to preview validators/enrichments/date window.
2. `initialize_query` is preview-only (it does not create a job) and suggestions are non-deterministic.
3. Submit with `submit_query` (`query` required). You can send only `query`; omitted optional fields are auto-selected/generated.
4. Optional fields are independent: provide any subset (for example, custom `validators` only), omitted ones are still auto-generated.
5. `start_date`/`end_date` filter web page discovery dates, not event dates in extracted content.
6. For event-time accuracy, use event-focused validators/enrichments and verify `event_date` in pulled results.
7. Poll `get_job_status`: first check after ~1-2 minutes, then every 30-60 seconds, stop on `completed` or `failed`.
8. Pull with `pull_results`; partial data appears during `enriching`.
9. Paginate while `page < total_pages` to retrieve all available records.
10. Use `continue_job` only to process more records (cost-affecting). It applies only to jobs originally submitted with `limit`.
11. `continue_job.new_limit` is optional; if omitted, API defaults to your plan maximum.
12. `page/page_size/total_pages` represent already-available records; use `progress_validated < candidate_records` to detect if more records may still appear.

## Limit vs Page Size

- `limit` (`submit_query`, `continue_job`) controls how many records are processed and therefore affects cost.
- `page_size` (`pull_results`, `list_user_jobs`) controls pagination only and does not affect processing cost.
- `pull_results.page_size` default is `100`.
- `page_size` range is `1..1000`.
- `pull_results` response includes `error` (failed jobs) and `limit` (applied job limit).

## API-Enforced Monitor Constraints

- `create_monitor.backfill=true`: reference job `end_date` must be within the last 7 days.
- `create_monitor.backfill=false`: reference job age constraint does not apply.
- Monitor minimum schedule frequency depends on plan.
- `create_monitor` supports optional `limit` (minimum `10`) and `backfill` (default `true`).
- `enable_monitor` supports optional `backfill`.
- `update_monitor` supports webhook updates and optional run `limit` updates.
- `list_monitors` supports pagination via `page` and `page_size`, and returns `total`, `page`, `page_size`, `total_pages`, `monitors`.

## Enrichment Output Notes

- `enrichment.enrichment_confidence` is always present.
- Company enrichments are structured objects with:
  - `source_text`
  - `confidence`
  - `metadata.name`
  - `metadata.domain_url`
  - `metadata.domain_url_confidence`

## Error Handling

Tools return:

- Pretty JSON string on success.
- `"Error: ..."` for validation/API errors.
- `"Unexpected error: ..."` for unhandled exceptions.

## Running

Install dependencies:

```bash
pip install -r requirements.txt
```

Run over stdio:

```bash
python server.py
```

Run over HTTP (if `fastmcp` CLI is available):

```bash
fastmcp run server.py:mcp --transport streamable-http --host 0.0.0.0 --port 8000
```
