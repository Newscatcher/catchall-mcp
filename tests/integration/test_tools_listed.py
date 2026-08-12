"""
Integration test: verify the MCP server advertises the expected tools.

This is a fast smoke test — doesn't call any real API endpoints,
just checks the server's tool manifest.
"""

from __future__ import annotations

import pytest


EXPECTED_TOOLS = {
    # Job tools
    "submit_query",
    "initialize_query",
    "get_job_status",
    "pull_results",
    "continue_job",
    "list_user_jobs",
    "delete_job",
    "validate_query",
    # Monitor tools
    "create_monitor",
    "list_monitors",
    "pull_monitor_results",
    "list_monitor_jobs",
    "disable_monitor",
    "enable_monitor",
    "update_monitor",
    "delete_monitor",
    "get_monitor_status",
    # Webhook tools
    "list_webhooks",
    "create_webhook",
    "get_webhook",
    "update_webhook",
    "delete_webhook",
    "test_webhook",
    "assign_webhook_resource",
    "list_webhook_resources",
    "remove_webhook_resource",
    "list_resource_webhooks",
    "get_webhook_history",
    "trigger_webhook",
    # Project tools
    "create_project",
    "list_projects",
    "get_project",
    "update_project",
    "delete_project",
    "get_project_overview",
    "add_project_resources",
    "list_project_resources",
    "remove_project_resource",
    # Dataset tools
    "create_dataset",
    "list_datasets",
    "get_dataset",
    "update_dataset",
    "delete_dataset",
    "add_dataset_entities",
    "remove_dataset_entities",
    "list_dataset_entities",
    "get_dataset_status",
    # CSV upload tools (v1.6.1): accept inline CSV content (raw text or
    # base64), never a server-side file path — safe for a hosted server.
    "create_dataset_from_csv",
    "append_csv_to_dataset",
    # CSV download tools (v1.6.3)
    "pull_job_csv",
    "pull_monitor_csv",
    # Entity tools
    "create_entity",
    "list_entities",
    "create_entities_batch",
    "get_entity",
    "update_entity",
    "delete_entity",
    # Meta tools
    "check_health",
    "get_version",
    "get_user_limits",
}


@pytest.mark.asyncio
async def test_all_expected_tools_registered(mcp):
    result = await mcp.list_tools()
    registered = {t.name for t in result.tools}
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"Missing tools: {missing}"


@pytest.mark.asyncio
async def test_tool_count(mcp):
    result = await mcp.list_tools()
    assert len(result.tools) >= len(EXPECTED_TOOLS)


@pytest.mark.asyncio
async def test_each_tool_has_description(mcp):
    result = await mcp.list_tools()
    for tool in result.tools:
        assert tool.description, f"Tool '{tool.name}' has no description"


@pytest.mark.asyncio
async def test_submit_query_has_required_params(mcp):
    result = await mcp.list_tools()
    tool = next((t for t in result.tools if t.name == "submit_query"), None)
    assert tool is not None, "submit_query not found"
    schema = tool.inputSchema
    assert "query" in schema.get("properties", {}), "submit_query missing 'query' param"
    assert "query" in schema.get("required", []), "submit_query 'query' should be required"


@pytest.mark.asyncio
async def test_validate_query_has_no_context_param(mcp):
    """v1.6.1 removed `context` from CheckQueryQualityRequestDto."""
    result = await mcp.list_tools()
    tool = next((t for t in result.tools if t.name == "validate_query"), None)
    assert tool is not None, "validate_query not found"
    props = tool.inputSchema.get("properties", {})
    assert "context" not in props, "validate_query still advertises the removed 'context' param"
    assert "query" in props


@pytest.mark.asyncio
async def test_csv_upload_tool_schemas(mcp):
    """v1.6.1 CSV upload tools expose the documented multipart fields."""
    result = await mcp.list_tools()
    tools = {t.name: t for t in result.tools}

    create = tools.get("create_dataset_from_csv")
    assert create is not None, "create_dataset_from_csv not found"
    props = create.inputSchema.get("properties", {})
    required = create.inputSchema.get("required", [])
    assert {"name", "file", "description", "project_id"} <= set(props)
    assert "name" in required and "file" in required

    append = tools.get("append_csv_to_dataset")
    assert append is not None, "append_csv_to_dataset not found"
    props = append.inputSchema.get("properties", {})
    required = append.inputSchema.get("required", [])
    assert {"dataset_id", "file"} <= set(props)
    assert "dataset_id" in required and "file" in required


@pytest.mark.asyncio
async def test_entity_tools_have_external_entity_id(mcp):
    """v1.6.3: create_entity and update_entity must expose external_entity_id."""
    result = await mcp.list_tools()
    tools = {t.name: t for t in result.tools}

    for tool_name in ("create_entity", "update_entity"):
        tool = tools.get(tool_name)
        assert tool is not None, f"{tool_name} not found"
        props = tool.inputSchema.get("properties", {})
        assert "external_entity_id" in props, (
            f"{tool_name} missing 'external_entity_id' parameter (required by 1.6.3)"
        )


@pytest.mark.asyncio
async def test_trigger_webhook_tool_schema(mcp):
    """trigger_webhook (POST /catchAll/webhook/trigger/{resource_type}/{resource_id})
    must be registered with the documented required/optional parameters."""
    result = await mcp.list_tools()
    tool = next((t for t in result.tools if t.name == "trigger_webhook"), None)
    assert tool is not None, "trigger_webhook not found"
    props = tool.inputSchema.get("properties", {})
    required = tool.inputSchema.get("required", [])
    assert {"webhook_id", "resource_type", "resource_id", "job_id"} <= set(props)
    assert {"webhook_id", "resource_type", "resource_id"} <= set(required)
    assert "job_id" not in required, "trigger_webhook 'job_id' must stay optional"


@pytest.mark.asyncio
async def test_list_user_jobs_has_mode_filter(mcp):
    """GET /catchAll/jobs/user gained a `mode` (base|lite) query filter — the
    tool must advertise an optional `mode` parameter."""
    result = await mcp.list_tools()
    tool = next((t for t in result.tools if t.name == "list_user_jobs"), None)
    assert tool is not None, "list_user_jobs not found"
    props = tool.inputSchema.get("properties", {})
    required = tool.inputSchema.get("required", [])
    assert "mode" in props, "list_user_jobs missing 'mode' parameter"
    assert "mode" not in required, "list_user_jobs 'mode' must stay optional"


@pytest.mark.asyncio
async def test_create_webhook_has_project_id(mcp):
    """POST /catchAll/webhooks accepts an optional `project_id` to attach the
    webhook to a project on creation."""
    result = await mcp.list_tools()
    tool = next((t for t in result.tools if t.name == "create_webhook"), None)
    assert tool is not None, "create_webhook not found"
    props = tool.inputSchema.get("properties", {})
    required = tool.inputSchema.get("required", [])
    assert "project_id" in props, "create_webhook missing 'project_id' parameter"
    assert "project_id" not in required, "create_webhook 'project_id' must stay optional"


@pytest.mark.asyncio
async def test_get_webhook_history_supports_webhook_id_mode(mcp):
    """GET /catchAll/webhook-history supports querying by `webhook_id` as an
    alternative to `resource_type` + `resource_id` (exactly one of the two
    modes per call), so none of the three can be schema-required."""
    result = await mcp.list_tools()
    tool = next((t for t in result.tools if t.name == "get_webhook_history"), None)
    assert tool is not None, "get_webhook_history not found"
    props = tool.inputSchema.get("properties", {})
    required = tool.inputSchema.get("required", [])
    assert {"webhook_id", "resource_type", "resource_id"} <= set(props)
    for param in ("webhook_id", "resource_type", "resource_id"):
        assert param not in required, (
            f"get_webhook_history '{param}' must be optional (mode chosen per call)"
        )


@pytest.mark.asyncio
async def test_project_resource_tools_accept_webhook_type(mcp):
    """resource_type='webhook' is a valid project resource type; the client-side
    allow-list must not reject it before the API is called. Uses a bogus project
    ID so validation failure vs API error is distinguishable without creating
    anything."""
    result = await mcp.call_tool(
        "add_project_resources",
        {
            "project_id": "00000000-0000-0000-0000-000000000000",
            "resources": [
                {
                    "resource_type": "webhook",
                    "resource_id": "00000000-0000-0000-0000-000000000000",
                }
            ],
        },
    )
    text = result.content[0].text
    assert "resource_type must be one of" not in text, (
        f"client-side allow-list still rejects resource_type='webhook': {text}"
    )


@pytest.mark.asyncio
async def test_csv_download_tool_schemas(mcp):
    """v1.6.3: pull_job_csv and pull_monitor_csv must be present with correct required params."""
    result = await mcp.list_tools()
    tools = {t.name: t for t in result.tools}

    job_csv = tools.get("pull_job_csv")
    assert job_csv is not None, "pull_job_csv not found"
    props = job_csv.inputSchema.get("properties", {})
    required = job_csv.inputSchema.get("required", [])
    assert "job_id" in props, "pull_job_csv missing 'job_id'"
    assert "job_id" in required, "pull_job_csv 'job_id' should be required"

    mon_csv = tools.get("pull_monitor_csv")
    assert mon_csv is not None, "pull_monitor_csv not found"
    props = mon_csv.inputSchema.get("properties", {})
    required = mon_csv.inputSchema.get("required", [])
    assert "monitor_id" in props, "pull_monitor_csv missing 'monitor_id'"
    assert "monitor_id" in required, "pull_monitor_csv 'monitor_id' should be required"
