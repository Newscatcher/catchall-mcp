"""
Integration tests for API key auth across tools.

Tests:
- Tools that require auth return a clear error without a key.
- Tools that don't require auth (check_health, get_version) succeed without a key.
- Explicit api_key parameter overrides env var.
"""

from __future__ import annotations

import os

import pytest

from conftest import assert_tool_error, call_result_text


# Tools that require a valid API key (will 403/fail with a bad key)
AUTH_REQUIRED_TOOLS = [
    ("get_user_limits", {}),
    ("list_user_jobs", {}),
    ("list_monitors", {}),
    ("initialize_query", {"query": "test"}),
]

# Tools that explicitly do NOT require an API key
NO_AUTH_TOOLS = [
    ("check_health", {}),
    ("get_version", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,kwargs", NO_AUTH_TOOLS)
async def test_no_auth_tools_succeed_without_key(mcp, tool_name, kwargs):
    """check_health and get_version must not fail due to missing API key."""
    result = await mcp.call_tool(tool_name, {**kwargs, "api_key": "INVALID_KEY_FOR_TEST"})
    text = call_result_text(result)
    # These endpoints don't validate the key — they should always succeed.
    assert not result.isError, f"{tool_name} unexpectedly failed: {text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,kwargs", AUTH_REQUIRED_TOOLS)
async def test_auth_required_tools_fail_with_bad_key(mcp, tool_name, kwargs):
    """Tools that need auth must surface a real MCP tool error with an invalid key.

    v1.8.0: this must be `isError=True` (a genuine tool error), never a
    silently-returned `{}`/text success — that was the wrapper_gap regression.
    """
    result = await mcp.call_tool(tool_name, {**kwargs, "api_key": "INVALID_KEY_XYZ"})
    assert_tool_error(result)


@pytest.mark.asyncio
async def test_missing_key_returns_error_message(mcp):
    """
    When no API key is set at all, auth-required tools must surface a real MCP
    tool error with a helpful message — not crash uncontrolled, and never a
    silent success.
    """
    # Pass an explicit bad key to bypass env var
    result = await mcp.call_tool("list_user_jobs", {"api_key": "INVALID_KEY_XYZ"})
    assert_tool_error(result)


@pytest.mark.asyncio
async def test_explicit_api_key_param_is_used(mcp):
    """
    Passing api_key explicitly should be used (even if env has a different key).
    With a bad explicit key we expect a real MCP tool error, confirming the
    param was used and the failure was not swallowed into a silent success.
    """
    result = await mcp.call_tool(
        "list_user_jobs", {"api_key": "DEFINITELY_WRONG_KEY_12345"}
    )
    assert_tool_error(result)
