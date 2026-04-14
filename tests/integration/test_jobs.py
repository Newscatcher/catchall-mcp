"""
Integration tests for job tools.

These tests make real API calls and require a valid CATCHALL_API_KEY.
Skip this file if you don't have a key:
    pytest tests/integration/ -v --ignore=tests/integration/test_jobs.py

Or mark with the env var guard at the top of each test.

Note: submit_query starts real processing jobs — tests use a small limit=10
to minimize cost and use initialize_query for preview-only calls.
"""

from __future__ import annotations

import json

import pytest

from conftest import call_result_json, call_result_text


# ---------------------------------------------------------------------------
# initialize_query (preview only — no job created, no cost)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestInitializeQuery:
    async def test_returns_validators_and_enrichments(self, mcp):
        result = await mcp.call_tool("initialize_query", {"query": "tech company acquisitions 2024"})
        data = call_result_json(result)
        assert "validators" in data, f"Expected 'validators' key: {data}"
        assert "enrichments" in data, f"Expected 'enrichments' key: {data}"

    async def test_returns_dates(self, mcp):
        result = await mcp.call_tool("initialize_query", {"query": "AI startup funding rounds"})
        data = call_result_json(result)
        assert "start_date" in data
        assert "end_date" in data

    async def test_with_context(self, mcp):
        result = await mcp.call_tool(
            "initialize_query",
            {
                "query": "biotech mergers",
                "context": "Focus on European companies with deal value above $100M",
            },
        )
        data = call_result_json(result)
        assert isinstance(data.get("validators"), list)
        assert isinstance(data.get("enrichments"), list)

    async def test_invalid_api_key_returns_error(self, mcp):
        result = await mcp.call_tool(
            "initialize_query", {"query": "test", "api_key": "INVALID"}
        )
        text = call_result_text(result)
        assert text.startswith("Error:")


# ---------------------------------------------------------------------------
# list_user_jobs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestListUserJobs:
    async def test_returns_list(self, mcp):
        result = await mcp.call_tool("list_user_jobs", {})
        data = call_result_json(result)
        # Response is a list OR a dict with a jobs/items key
        assert isinstance(data, (list, dict)), f"Unexpected type: {type(data)}"

    async def test_pagination_params_accepted(self, mcp):
        result = await mcp.call_tool("list_user_jobs", {"page": 1, "page_size": 10})
        data = call_result_json(result)
        assert data is not None

    async def test_invalid_page_returns_error(self, mcp):
        result = await mcp.call_tool("list_user_jobs", {"page": 0})
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "page" in text.lower()

    async def test_invalid_page_size_returns_error(self, mcp):
        result = await mcp.call_tool("list_user_jobs", {"page_size": 9999})
        text = call_result_text(result)
        assert text.startswith("Error:")


# ---------------------------------------------------------------------------
# submit_query + get_job_status + pull_results (full job lifecycle)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestJobLifecycle:
    async def test_submit_returns_job_id(self, mcp):
        result = await mcp.call_tool(
            "submit_query",
            {"query": "AI chip manufacturers news", "limit": 10},
        )
        data = call_result_json(result)
        assert "job_id" in data, f"Expected job_id in response: {data}"
        assert isinstance(data["job_id"], str)
        assert len(data["job_id"]) > 0

    async def test_get_job_status_for_submitted_job(self, mcp):
        # Submit a job first
        submit = await mcp.call_tool(
            "submit_query",
            {"query": "electric vehicle battery technology", "limit": 10},
        )
        submit_data = call_result_json(submit)
        job_id = submit_data["job_id"]

        # Check status
        result = await mcp.call_tool("get_job_status", {"job_id": job_id})
        data = call_result_json(result)
        assert "status" in data, f"Expected 'status' in response: {data}"
        assert data["status"] in {
            "submitted", "analyzing", "fetching", "clustering",
            "enriching", "completed", "failed",
        }, f"Unknown status: {data['status']}"

    async def test_pull_results_for_submitted_job(self, mcp):
        submit = await mcp.call_tool(
            "submit_query",
            {"query": "renewable energy investments", "limit": 10},
        )
        job_id = call_result_json(submit)["job_id"]

        result = await mcp.call_tool("pull_results", {"job_id": job_id})
        data = call_result_json(result)
        # Job may still be running — partial or empty results are OK
        assert "status" in data or "all_records" in data, (
            f"Unexpected pull_results response: {data}"
        )

    async def test_get_status_unknown_job_returns_error(self, mcp):
        result = await mcp.call_tool(
            "get_job_status", {"job_id": "00000000-0000-0000-0000-000000000000"}
        )
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_pull_results_unknown_job_returns_error(self, mcp):
        result = await mcp.call_tool(
            "pull_results", {"job_id": "00000000-0000-0000-0000-000000000000"}
        )
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_submit_with_validators_and_enrichments(self, mcp):
        result = await mcp.call_tool(
            "submit_query",
            {
                "query": "pharmaceutical company acquisitions",
                "limit": 10,
                "validators": [
                    {
                        "name": "is_acquisition",
                        "description": "true if the page describes a company acquisition",
                        "type": "boolean",
                    }
                ],
                "enrichments": [
                    {
                        "name": "acquiring_company",
                        "description": "Name of the acquiring company",
                        "type": "company",
                    },
                    {
                        "name": "deal_value",
                        "description": "Deal value in USD",
                        "type": "number",
                    },
                ],
            },
        )
        data = call_result_json(result)
        assert "job_id" in data

    async def test_submit_lite_mode(self, mcp):
        result = await mcp.call_tool(
            "submit_query",
            {"query": "fintech IPO news", "mode": "lite"},
        )
        data = call_result_json(result)
        assert "job_id" in data

    async def test_submit_invalid_mode_returns_error(self, mcp):
        result = await mcp.call_tool(
            "submit_query", {"query": "test", "mode": "turbo"}
        )
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "mode" in text.lower()

    async def test_pull_results_pagination_params(self, mcp):
        submit = await mcp.call_tool(
            "submit_query", {"query": "space exploration news", "limit": 10}
        )
        job_id = call_result_json(submit)["job_id"]

        result = await mcp.call_tool(
            "pull_results", {"job_id": job_id, "page": 1, "page_size": 10}
        )
        data = call_result_json(result)
        assert data is not None

    async def test_pull_results_invalid_page_returns_error(self, mcp):
        result = await mcp.call_tool(
            "pull_results", {"job_id": "any-id", "page": 0}
        )
        text = call_result_text(result)
        assert text.startswith("Error:")


# ---------------------------------------------------------------------------
# continue_job
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestContinueJob:
    async def test_continue_unknown_job_returns_error(self, mcp):
        result = await mcp.call_tool(
            "continue_job",
            {"job_id": "00000000-0000-0000-0000-000000000000", "new_limit": 15},
        )
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_continue_invalid_new_limit_returns_error(self, mcp):
        result = await mcp.call_tool(
            "continue_job", {"job_id": "some-id", "new_limit": 0}
        )
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "new_limit" in text.lower()


# ---------------------------------------------------------------------------
# get_user_limits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetUserLimits:
    async def test_returns_features(self, mcp):
        result = await mcp.call_tool("get_user_limits", {})
        data = call_result_json(result)
        assert "features" in data, f"Expected 'features' key: {data}"
        assert isinstance(data["features"], list)

    async def test_features_have_expected_shape(self, mcp):
        result = await mcp.call_tool("get_user_limits", {})
        data = call_result_json(result)
        for feature in data["features"]:
            assert "name" in feature, f"Feature missing 'name': {feature}"
            assert "value" in feature, f"Feature missing 'value': {feature}"
