# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased] — feat/v1.53.0_release

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
