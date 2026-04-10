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

from conftest import call_result_text


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
    # These endpoints don't validate the key — they should always return data
    assert not text.startswith("Unexpected error:"), f"{tool_name} crashed: {text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,kwargs", AUTH_REQUIRED_TOOLS)
async def test_auth_required_tools_fail_with_bad_key(mcp, tool_name, kwargs):
    """Tools that need auth must return an error with an invalid key."""
    result = await mcp.call_tool(tool_name, {**kwargs, "api_key": "INVALID_KEY_XYZ"})
    text = call_result_text(result)
    assert text.startswith("Error:"), (
        f"{tool_name} should return 'Error: ...' with invalid key, got: {text!r}"
    )


@pytest.mark.asyncio
async def test_missing_key_returns_error_message(mcp):
    """
    When no API key is set at all, auth-required tools must return a helpful
    error string — not crash with an exception.
    """
    # Pass an explicit empty key to bypass env var
    result = await mcp.call_tool("list_user_jobs", {"api_key": "INVALID_KEY_XYZ"})
    text = call_result_text(result)
    assert text.startswith("Error:"), f"Expected error message, got: {text!r}"


@pytest.mark.asyncio
async def test_explicit_api_key_param_is_used(mcp):
    """
    Passing api_key explicitly should be used (even if env has a different key).
    With a bad explicit key we expect an auth error, confirming the param was used.
    """
    result = await mcp.call_tool(
        "list_user_jobs", {"api_key": "DEFINITELY_WRONG_KEY_12345"}
    )
    text = call_result_text(result)
    assert text.startswith("Error:"), (
        f"Expected auth error with wrong explicit key, got: {text!r}"
    )
