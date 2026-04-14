"""
Integration test fixtures.

Connects to the remote MCP server exactly as Claude Desktop does —
via streamable-http with x-api-key header.

Usage:
    export CATCHALL_API_KEY=your_key
    pytest tests/integration/ -v -s
"""

from __future__ import annotations

import json
import os

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVER_URL = os.getenv(
    "MCP_SERVER_URL",
    "https://catchall-mcp.newscatcherapi.com/mcp",
)
API_KEY = os.getenv("CATCHALL_API_KEY", "")


@pytest.fixture()
async def mcp():
    """
    Per-test MCP ClientSession.
    Connects to the remote server with x-api-key header — same as Desktop.
    """
    print(f"\n🔗 Running tests against: {SERVER_URL}\n")
    headers = {"x-api-key": API_KEY} if API_KEY else {}
    try:
        async with streamablehttp_client(SERVER_URL, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    except* RuntimeError as eg:
        # Suppress the anyio cancel-scope teardown bug in pytest-asyncio.
        # All actual test assertions have already run at this point.
        non_cancel_scope = [
            e for e in eg.exceptions
            if "cancel scope" not in str(e).lower()
        ]
        if non_cancel_scope:
            raise eg


def call_result_text(result) -> str:
    """Extract raw text and always print it — visible with pytest -s."""
    assert result.content, "Tool returned no content"
    text = result.content[0].text
    print(f"\n--- MCP response ---\n{text}\n--------------------")
    return text


def call_result_json(result) -> dict:
    """Extract, print, and parse JSON from a CallToolResult."""
    text = call_result_text(result)
    assert not text.startswith("Error:"), f"Tool returned error: {text}"
    return json.loads(text)
