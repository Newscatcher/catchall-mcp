# Newscatcher CatchAll MCP Server

MCP server for the NewsCatcher CatchAll Web Search API.

## Quick Start — Use Our Hosted Server

You don't need to clone or run this repo to use the MCP — NewsCatcher runs a hosted instance:

```
https://catchall-mcp.newscatcherapi.com/mcp?apiKey=YOUR_CATCHALL_API_KEY
```

Get a CatchAll API key at [platform.newscatcherapi.com](https://platform.newscatcherapi.com/), then connect:

```json
{
  "mcpServers": {
    "catchall": {
      "type": "http",
      "url": "https://catchall-mcp.newscatcherapi.com/mcp?apiKey=YOUR_CATCHALL_API_KEY"
    }
  }
}
```

Or via Claude Code CLI:

```bash
claude mcp add --transport http catchall "https://catchall-mcp.newscatcherapi.com/mcp?apiKey=YOUR_CATCHALL_API_KEY"
```

Full integration docs: https://www.newscatcherapi.com/docs/web-search-api/integrations/mcp

Prefer to run the server yourself (locally or self-hosted)? See [Running](#running) below.

## Tool To Endpoint Mapping

### Jobs

| MCP Tool | Method | Endpoint |
| --- | --- | --- |
| `initialize_query` | `POST` | `/catchAll/initialize` |
| `submit_query` | `POST` | `/catchAll/submit` |
| `validate_query` | `POST` | `/catchAll/validate` |
| `continue_job` | `POST` | `/catchAll/continue` |
| `list_user_jobs` | `GET` | `/catchAll/jobs/user` |
| `get_job_status` | `GET` | `/catchAll/status/{job_id}` |
| `pull_results` | `GET` | `/catchAll/pull/{job_id}` |
| `pull_job_csv` | `GET` | `/catchAll/pull/{job_id}/csv` |
| `delete_job` | `DELETE` | `/catchAll/jobs/{job_id}` |

### Monitors

| MCP Tool | Method | Endpoint |
| --- | --- | --- |
| `create_monitor` | `POST` | `/catchAll/monitors/create` |
| `update_monitor` | `PATCH` | `/catchAll/monitors/{monitor_id}` |
| `delete_monitor` | `DELETE` | `/catchAll/monitors/{monitor_id}` |
| `list_monitors` | `GET` | `/catchAll/monitors/` |
| `list_monitor_jobs` | `GET` | `/catchAll/monitors/{monitor_id}/jobs` |
| `get_monitor_status` | `GET` | `/catchAll/monitors/{monitor_id}/status` |
| `pull_monitor_results` | `GET` | `/catchAll/monitors/pull/{monitor_id}` |
| `pull_monitor_csv` | `GET` | `/catchAll/monitors/pull/{monitor_id}/csv` |
| `enable_monitor` | `POST` | `/catchAll/monitors/{monitor_id}/enable` |
| `disable_monitor` | `POST` | `/catchAll/monitors/{monitor_id}/disable` |

### Webhooks

| MCP Tool | Method | Endpoint |
| --- | --- | --- |
| `list_webhooks` | `GET` | `/catchAll/webhooks` |
| `create_webhook` | `POST` | `/catchAll/webhooks` |
| `get_webhook` | `GET` | `/catchAll/webhooks/{webhook_id}` |
| `update_webhook` | `PATCH` | `/catchAll/webhooks/{webhook_id}` |
| `delete_webhook` | `DELETE` | `/catchAll/webhooks/{webhook_id}` |
| `test_webhook` | `POST` | `/catchAll/webhooks/{webhook_id}/test` |
| `assign_webhook_resource` | `POST` | `/catchAll/webhooks/{webhook_id}/resources` |
| `list_webhook_resources` | `GET` | `/catchAll/webhooks/{webhook_id}/resources` |
| `remove_webhook_resource` | `DELETE` | `/catchAll/webhooks/{webhook_id}/resources/{resource_type}/{resource_id}` |
| `list_resource_webhooks` | `GET` | `/catchAll/resources/{resource_type}/{resource_id}/webhooks` |
| `get_webhook_history` | `GET` | `/catchAll/webhook-history` |
| `trigger_webhook` | `POST` | `/catchAll/webhook/trigger/{resource_type}/{resource_id}` |

### Projects

| MCP Tool | Method | Endpoint |
| --- | --- | --- |
| `create_project` | `POST` | `/catchAll/projects/` |
| `list_projects` | `GET` | `/catchAll/projects/` |
| `get_project` | `GET` | `/catchAll/projects/{project_id}` |
| `update_project` | `PATCH` | `/catchAll/projects/{project_id}` |
| `delete_project` | `DELETE` | `/catchAll/projects/{project_id}` |
| `get_project_overview` | `GET` | `/catchAll/projects/{project_id}/overview` |
| `add_project_resources` | `POST` | `/catchAll/projects/{project_id}/resources` |
| `list_project_resources` | `GET` | `/catchAll/projects/{project_id}/resources` |
| `remove_project_resource` | `DELETE` | `/catchAll/projects/{project_id}/resources/{resource_type}/{resource_id}` |

### Datasets

| MCP Tool | Method | Endpoint |
| --- | --- | --- |
| `create_dataset` | `POST` | `/catchAll/datasets/` |
| `list_datasets` | `GET` | `/catchAll/datasets/` |
| `get_dataset` | `GET` | `/catchAll/datasets/{dataset_id}` |
| `update_dataset` | `PATCH` | `/catchAll/datasets/{dataset_id}` |
| `delete_dataset` | `DELETE` | `/catchAll/datasets/{dataset_id}` |
| `add_dataset_entities` | `POST` | `/catchAll/datasets/{dataset_id}/entities` |
| `remove_dataset_entities` | `DELETE` | `/catchAll/datasets/{dataset_id}/entities` |
| `list_dataset_entities` | `POST` | `/catchAll/datasets/{dataset_id}/entities/list` |
| `get_dataset_status` | `GET` | `/catchAll/datasets/{dataset_id}/status` |
| `create_dataset_from_csv` | `POST` | `/catchAll/datasets/upload` |
| `append_csv_to_dataset` | `POST` | `/catchAll/datasets/{dataset_id}/upload` |

> **CSV uploads (v1.6.1):** `create_dataset_from_csv` and `append_csv_to_dataset` take
> the CSV **content** in the `file` parameter — raw CSV text or standard base64. They
> never read a path from the server's filesystem, so they stay safe on a remote/hosted
> MCP. Inline CSV content is capped at a hard 10 MB (after base64 decoding).
> `create_dataset_from_csv` also accepts the new optional `project_id` field.

### Entities

| MCP Tool | Method | Endpoint |
| --- | --- | --- |
| `create_entity` | `POST` | `/catchAll/entities/` |
| `list_entities` | `GET` | `/catchAll/entities/` |
| `create_entities_batch` | `POST` | `/catchAll/entities/batch` |
| `get_entity` | `GET` | `/catchAll/entities/{entity_id}` |
| `update_entity` | `PATCH` | `/catchAll/entities/{entity_id}` |
| `delete_entity` | `DELETE` | `/catchAll/entities/{entity_id}` |

> **`external_entity_id` (v1.6.3):** `create_entity` and `update_entity` accept an optional
> `external_entity_id` string — a customer-supplied identifier that links the entity to a
> record in an external system.

### User & Meta

| MCP Tool | Method | Endpoint |
| --- | --- | --- |
| `get_user_limits` | `POST` | `/catchAll/user/limits` |
| `check_health` | `GET` | `/health` |
| `get_version` | `GET` | `/version` |

## Authentication

API key precedence (highest to lowest):

1. `api_key` tool parameter
2. `x-api-key` request header
3. `Authorization: Bearer <key>` request header
4. URL query parameter `?apiKey=...`
5. `CATCHALL_API_KEY` environment variable

`check_health` and `get_version` do not require API key auth.

### Hosted deployment (FastMCP Gateway)

When deployed via fastmcp.app, a stateless gateway sits in front of the server. The gateway
forwards HTTP headers to the backend but **not** URL query parameters. Use the `x-api-key`
header or `CATCHALL_API_KEY` environment variable instead of `?apiKey=`.

**Claude Code / Cursor:**

```json
{
  "mcpServers": {
    "catchall": {
      "type": "http",
      "url": "https://YOUR-DEPLOYMENT.fastmcp.app/mcp",
      "headers": { "x-api-key": "YOUR_API_KEY" }
    }
  }
}
```

Or via CLI:

```bash
claude mcp add --transport http catchall "https://YOUR-DEPLOYMENT.fastmcp.app/mcp" \
  --header "x-api-key: YOUR_API_KEY"
```

**Direct server access** (no gateway): `?apiKey=YOUR_KEY` in the URL still works.

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

- `limit` (`submit_query`, `continue_job`) controls how many records are processed and therefore affects cost. If provided, must be >= 10. Omit to retrieve everything up to your plan's maximum.
- `page_size` (`pull_results`, `list_user_jobs`) controls pagination only and does not affect processing cost.
- `pull_results.page_size` default is `100`.
- `page_size` range is `1..1000`.
- `pull_results` response includes `error` (failed jobs) and `limit` (applied job limit).

## API-Enforced Monitor Constraints

- `create_monitor.backfill=true`: reference job `end_date` must be within the last 7 days.
- `create_monitor.backfill=false`: reference job age constraint does not apply.
- Monitor minimum schedule frequency depends on plan.
- `create_monitor` supports optional `limit` (minimum `10`), `backfill` (default `true`), `timezone`, `webhook_ids`, and `project_id`.
- Webhooks are centralized in v1.5.3: register them with `create_webhook`, then attach by ID via `create_monitor.webhook_ids` / `update_monitor.webhook_ids` (no inline webhook config).
- Monitors are only supported for `base` jobs (not `lite`).
- `enable_monitor` supports optional `backfill`.
- `update_monitor` updates `webhook_ids` and/or run `limit` (pass `webhook_ids=[]` to clear assignments).
- `list_monitors` supports pagination via `page` and `page_size` plus `search`, `ownership`, and `project_id` filters; it returns `total`, `page`, `page_size`, `total_pages`, `monitors`.

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
