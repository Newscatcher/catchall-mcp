# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.6.4] — 2026-07-07

### Added
- `trigger_webhook` tool — `POST /catchAll/webhook/trigger/{resource_type}/{resource_id}`.
  Manually triggers webhook delivery for a resource (job/monitor/monitor_group).
  Params: `webhook_id` (required, query), `resource_type` + `resource_id` (required,
  path), `job_id` (optional, query). Returns `success` and `message`
  ("Webhook trigger dispatched."); the dispatch is asynchronous — use
  `get_webhook_history` to see the delivery outcome. `resource_type` is validated
  client-side against job/monitor/monitor_group.

### Tests
- Unit request-mapping tests for `trigger_webhook` (minimal and with `job_id`) plus a
  fail-fast invalid-`resource_type` case in `tests/test_server.py`.
- Integration schema test `test_trigger_webhook_tool_schema` and `EXPECTED_TOOLS`
  updated in `tests/integration/test_tools_listed.py`; safe client-side enum test
  added to the integration surface suite.

---

## [1.6.3] — 2026-06-25

### Added
- `pull_job_csv` tool — `GET /catchAll/pull/{job_id}/csv`. Downloads a completed job's
  results as a CSV file. Prefer this over `pull_results` when the consumer needs
  spreadsheet/CSV format instead of paginated JSON.
- `pull_monitor_csv` tool — `GET /catchAll/monitors/pull/{monitor_id}/csv`. Downloads the
  most recent monitor run's results as a CSV file.
- `external_entity_id` parameter on `create_entity` and `update_entity` — optional
  customer-supplied identifier that links the entity to a record in an external system.

### Changed
- `make_api_request` gained a `return_text` flag: when `True`, the raw response body is
  returned as a string instead of being JSON-decoded (used by the two new CSV download
  tools; also sets `Accept: text/csv, text/plain, */*`).

### Tests
- Integration test `test_entity_tools_have_external_entity_id` — asserts both
  `create_entity` and `update_entity` expose the new `external_entity_id` parameter.
- Integration test `test_csv_download_tool_schemas` — asserts `pull_job_csv` and
  `pull_monitor_csv` are registered with the correct required parameters.
- `EXPECTED_TOOLS` set in `test_tools_listed.py` updated to include the two new tools.

---

## [1.6.1] — 2026-06-10

> **Note:** this entry re-adds CSV upload tools that were deliberately removed in
> `cd86a40` ("unsafe for a hosted server"). The unsafe part was the `file_path`
> argument (server-side filesystem reads). The new tools accept **inline content
> only** (raw CSV text or base64, hard 10 MB cap, auth required) — no filesystem
> access. Re-adding was reviewed and approved by the maintainer on 2026-06-10.


### Added
- `create_dataset_from_csv` tool — wraps `POST /catchAll/datasets/upload`
  (multipart). Params: `name` (required), `file` (required; raw CSV text or
  base64 — server-side file paths are not accepted), `description` (optional),
  `project_id` (optional, new in 1.6.1). Returns `dataset_id`, `dataset_name`,
  `entities_created`, `validation_report`.
- `append_csv_to_dataset` tool — wraps `POST /catchAll/datasets/{dataset_id}/upload`
  (multipart). Params: `dataset_id` (required), `file` (required). Returns
  `dataset_id`, `entities_created`, `validation_report`.
- `make_api_upload` helper for authenticated multipart/form-data uploads.
- `coerce_csv_file_content` validator: accepts raw CSV text or standard
  base64, rejects empty input and anything else. Inline CSV content is
  capped at a hard 10 MB (decoded): the raw string length is checked before
  any base64 decode (base64 inflates ~4/3) and the decoded size is checked
  after, so over-cap uploads fail fast with a clear error instead of
  buffering in memory.

### Changed
- `validate_query`: removed the stale optional `context` parameter. The 1.6.1
  API dropped it from `CheckQueryQualityRequestDto`; the tool now sends only
  `query` to `/catchAll/validate`. Live behavior is unchanged.

### Tests
- Unit tests for both upload tools (request mapping, optional-field omission,
  base64 input, fail-fast validation) and for `make_api_upload`
  (auth required, multipart shape).
- Size-cap tests for `coerce_csv_file_content`: at-cap accepted and over-cap
  rejected, for both raw CSV and base64 input, plus the cheap pre-decode
  length guard.
- Regression test that `validate_query` no longer advertises `context`.
- Tool-manifest integration test updated: the two new tools are now expected,
  plus input-schema checks for the new tools and `validate_query`.

---

## [1.5.3]

### Added
- Full sync to **CatchAll API v1.5.3** — server now exposes **57 tools** (up from 22)
- **Jobs**: `validate_query`, `delete_job`; `submit_query` gained `project_id`, `webhook_ids`, `schema`, `connected_dataset_ids`, `ed_score_min`; `list_user_jobs` gained `search`, `ownership`, `project_id`
- **Monitors**: `delete_monitor`, `get_monitor_status`; `create_monitor` now uses `webhook_ids`/`timezone`/`project_id`; `update_monitor` accepts `webhook_ids`/`limit`; `list_monitors` gained `search`, `ownership`, `project_id`
- **Webhooks**: `assign_webhook_resource`, `list_webhook_resources`, `remove_webhook_resource`, `list_resource_webhooks`, `get_webhook_history`; `create_webhook`/`update_webhook` gained `type`/`params`; `test_webhook` gained `payload`; `auth` is now an object (`bearer`/`api_key`/`basic`); `method` accepts the full HTTP-method enum
- **Projects** (9 new tools): `create_project`, `list_projects`, `get_project`, `update_project`, `delete_project`, `get_project_overview`, `add_project_resources`, `list_project_resources`, `remove_project_resource`
- **Datasets** (9 new tools): full CRUD + `add_dataset_entities`, `remove_dataset_entities`, `list_dataset_entities`, `get_dataset_status` — CSV upload intentionally omitted (see below)
- **Entities** (6 new tools): `create_entity`, `get_entity`, `update_entity`, `delete_entity`, `list_entities`, `create_entities_batch`
- Integration test suite (`tests/integration/`) — 64 passed, 1 skipped
- `tests/integration/test_v153_surface.py` — safe (no paid-resource) surface tests for validate, list surfaces, enum validation, and not-found handling
- `tests/integration/test_tools_listed.py` — asserts all 57 expected tools register
- Pinned runtime and test dependencies for reproducible builds

### Changed
- Monitor creation now uses centralized `webhook_ids` list (v1.5.2 inline `webhook` object removed)
- `tests/test_server.py` — updated stale v1.5.2 inline-webhook tests to match verified new contract; added mapping tests for all new tools
- `tests/integration/test_monitors.py` — dropped inline-webhook tests, added `delete_monitor`/`get_monitor_status` error tests

### Removed
- **CSV file-upload tools** (`POST /catchAll/datasets/upload` and `POST /catchAll/datasets/{dataset_id}/upload`) — deliberately omitted because the server runs as a hosted `streamable-http` MCP; a `file_path` argument would make the server read from its own filesystem (arbitrary local file read, useless to remote callers). Use `create_entity`/`create_entities_batch` + `add_dataset_entities` to populate datasets instead.

---

## [1.1.2] — Integration tests & auth hardening

### Added
- Scheduled CI run for production
- Full integration test suite (`tests/integration/`)
- `x-api-key` and `Authorization: Bearer` header support for FastMCP Gateway deployments
- Session-aware API key fallback for cross-task key lookup

### Changed
- Lite mode endpoint added
- Authentication resolution order documented and hardened
- Empty-body JSON error fixed; tests updated for `FunctionTool` wrapper
- `submit_query` and `continue_job` parameter documentation clarified (`limit` = billing cap, `page_size` = free pagination)

---

## [1.1.0] — Validators split & submit docs

### Added
- `initialize_query` preview tool (validators, enrichments, date range — no job created)
- `create_monitor`, `list_monitors`, `update_monitor`, `pull_monitor_results`, `list_monitor_jobs` (monitor family)
- `validators.py` extracted as standalone module

### Changed
- Submit docs and parameter descriptions significantly refined
- Stringified `submit_query` definitions now accepted
- Long-running result completeness semantics clarified

---

## [1.0.0] — BYOK & API key as parameter

### Added
- `api_key` tool parameter for bring-your-own-key (BYOK) usage
- `CATCHALL_API_KEY` environment variable support (renamed from `NEWSCATCHER_API_KEY`)
- API key resolution via URL query parameter (`?apiKey=`)

### Changed
- Terminology updated from "news" to "web search" throughout
- Refactored to use `api_key` as a first-class tool parameter

---

## [0.2.0] — Encoding & header auth

### Added
- `Authorization` header support in addition to URL query parameter
- Custom decompressing HTTP transport to fix UTF-8 encoding errors

### Changed
- `GET /catchAll` submit/continue/pull tools stabilised after rewrite revert

---

## [0.1.0] — Initial release

### Added
- Initial MCP server for the Newscatcher CatchAll API
- `submit_query`, `continue_job`, `pull_results`, `get_job_status`, `list_user_jobs`
- `check_health`, `get_version`, `get_user_limits`
- `create_webhook`, `get_webhook`, `update_webhook`, `delete_webhook`, `list_webhooks`, `test_webhook`
- OpenAPI schema compatibility for FastMCP cloud deployment
