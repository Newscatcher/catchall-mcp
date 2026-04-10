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
    # Monitor tools
    "create_monitor",
    "list_monitors",
    "pull_monitor_results",
    "list_monitor_jobs",
    "disable_monitor",
    "enable_monitor",
    "update_monitor",
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
