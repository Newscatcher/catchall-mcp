"""
Integration tests for the v1.5.3 surface (projects, datasets, entities,
validate, webhook resources).

These are deliberately SAFE: they only call read/list endpoints and check
client-side validation errors, so they do not create paid or recurring
resources on every CI run. Full create -> delete lifecycles for these families
are exercised out-of-band by the real-API verification harness.
"""

from __future__ import annotations

import pytest

from conftest import call_result_json, call_result_text


@pytest.mark.asyncio
class TestValidateQuery:
    async def test_returns_status(self, mcp):
        result = await mcp.call_tool(
            "validate_query",
            {"query": "Tesla layoffs in 2024 with employee counts affected"},
        )
        data = call_result_json(result)
        assert data.get("status") in {"critical", "needs_work", "good"}
        assert "confidence" in data


@pytest.mark.asyncio
class TestListSurfaces:
    async def test_list_projects(self, mcp):
        data = call_result_json(await mcp.call_tool("list_projects", {"page": 1, "page_size": 5}))
        assert "projects" in data

    async def test_list_datasets(self, mcp):
        data = call_result_json(await mcp.call_tool("list_datasets", {"page": 1, "page_size": 5}))
        assert "datasets" in data

    async def test_list_entities(self, mcp):
        data = call_result_json(await mcp.call_tool("list_entities", {"page": 1, "page_size": 5}))
        assert "entities" in data


@pytest.mark.asyncio
class TestEnumValidation:
    """Client-side enum validation should fail fast with a clear message."""

    async def test_invalid_ownership(self, mcp):
        text = call_result_text(await mcp.call_tool("list_projects", {"ownership": "everyone"}))
        assert text.startswith("Error:")
        assert "ownership" in text.lower()

    async def test_invalid_webhook_type(self, mcp):
        text = call_result_text(await mcp.call_tool(
            "create_webhook", {"name": "x", "url": "https://example.com/h", "type": "pigeon"}))
        assert text.startswith("Error:")
        assert "type" in text.lower()

    async def test_invalid_resource_type(self, mcp):
        text = call_result_text(await mcp.call_tool(
            "assign_webhook_resource",
            {"webhook_id": "x", "resource_type": "widget", "resource_id": "y"}))
        assert text.startswith("Error:")
        assert "resource_type" in text.lower()

    async def test_invalid_entity_type(self, mcp):
        text = call_result_text(await mcp.call_tool(
            "create_entity", {"name": "X", "entity_type": "robot"}))
        assert text.startswith("Error:")
        assert "entity_type" in text.lower()


@pytest.mark.asyncio
class TestNotFoundSurfaces:
    FAKE_ID = "00000000-0000-0000-0000-000000000000"

    async def test_get_unknown_project_returns_error(self, mcp):
        text = call_result_text(await mcp.call_tool("get_project", {"project_id": self.FAKE_ID}))
        assert text.startswith("Error:")

    async def test_get_unknown_dataset_returns_error(self, mcp):
        text = call_result_text(await mcp.call_tool("get_dataset", {"dataset_id": self.FAKE_ID}))
        assert text.startswith("Error:")

    async def test_get_unknown_entity_returns_error(self, mcp):
        text = call_result_text(await mcp.call_tool("get_entity", {"entity_id": self.FAKE_ID}))
        assert text.startswith("Error:")
