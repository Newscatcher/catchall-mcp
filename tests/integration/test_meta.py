"""
Integration tests for meta tools: check_health and get_version.
These tools don't require an API key and always work against the live API.
"""

from __future__ import annotations

import pytest
from conftest import call_result_json, call_result_text


@pytest.mark.asyncio
class TestCheckHealth:
    async def test_returns_healthy(self, mcp):
        result = await mcp.call_tool("check_health", {})
        data = call_result_json(result)
        assert "healthy" in data, f"Expected 'healthy' in response, got: {data}"
        assert data["healthy"] is True

    async def test_no_api_key_still_works(self, mcp):
        result = await mcp.call_tool("check_health", {})
        text = call_result_text(result)
        assert not text.startswith("Error:"), f"Unexpected error: {text}"

    async def test_explicit_empty_api_key(self, mcp):
        result = await mcp.call_tool("check_health", {"api_key": ""})
        data = call_result_json(result)
        assert "healthy" in data


@pytest.mark.asyncio
class TestGetVersion:
    async def test_returns_version_field(self, mcp):
        result = await mcp.call_tool("get_version", {})
        data = call_result_json(result)
        assert "version" in data, f"Expected 'version' in response, got: {data}"

    async def test_no_api_key_still_works(self, mcp):
        result = await mcp.call_tool("get_version", {})
        text = call_result_text(result)
        assert not text.startswith("Error:"), f"Unexpected error: {text}"
