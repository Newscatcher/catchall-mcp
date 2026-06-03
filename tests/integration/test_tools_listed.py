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
    "create_dataset_from_csv",
    "get_dataset",
    "update_dataset",
    "delete_dataset",
    "add_dataset_entities",
    "remove_dataset_entities",
    "list_dataset_entities",
    "get_dataset_status",
    "append_dataset_csv",
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
