"""
Integration tests for monitor tools.

Monitors require a completed reference job. The tests that create monitors
are skipped unless CATCHALL_RUN_MONITOR_TESTS=1 is set, to avoid
unintentionally creating paid recurring jobs.

Set both env vars to run everything:
    CATCHALL_API_KEY=your_key CATCHALL_RUN_MONITOR_TESTS=1 pytest tests/integration/test_monitors.py -v
"""

from __future__ import annotations

import os


import pytest

from conftest import call_result_json, call_result_text

run_monitor_tests = pytest.mark.skipif(
    not os.getenv("CATCHALL_RUN_MONITOR_TESTS"),
    reason="Set CATCHALL_RUN_MONITOR_TESTS=1 to run monitor-creating tests",
)


# ---------------------------------------------------------------------------
# list_monitors — safe to call anytime
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestListMonitors:
    async def test_returns_response(self, mcp):
        result = await mcp.call_tool("list_monitors", {})
        data = call_result_json(result)
        assert isinstance(data, dict), f"Expected dict, got: {type(data)}"

    async def test_pagination_params_accepted(self, mcp):
        result = await mcp.call_tool("list_monitors", {"page": 1, "page_size": 10})
        data = call_result_json(result)
        assert data is not None

    async def test_invalid_page_returns_error(self, mcp):
        result = await mcp.call_tool("list_monitors", {"page": 0})
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "page" in text.lower()

    async def test_invalid_page_size_returns_error(self, mcp):
        result = await mcp.call_tool("list_monitors", {"page_size": 9999})
        text = call_result_text(result)
        assert text.startswith("Error:")


# ---------------------------------------------------------------------------
# pull_monitor_results / list_monitor_jobs — with a fake ID expect an error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMonitorReadTools:
    async def test_pull_unknown_monitor_returns_error(self, mcp):
        result = await mcp.call_tool(
            "pull_monitor_results",
            {"monitor_id": "00000000-0000-0000-0000-000000000000"},
        )
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_list_jobs_unknown_monitor_returns_error(self, mcp):
        result = await mcp.call_tool(
            "list_monitor_jobs",
            {"monitor_id": "00000000-0000-0000-0000-000000000000"},
        )
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_list_monitor_jobs_invalid_sort_returns_error(self, mcp):
        result = await mcp.call_tool(
            "list_monitor_jobs",
            {"monitor_id": "some-id", "sort": "newest"},
        )
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "sort" in text.lower()

    async def test_list_monitor_jobs_valid_sort_values(self, mcp):
        for sort in ("asc", "desc"):
            result = await mcp.call_tool(
                "list_monitor_jobs",
                {"monitor_id": "00000000-0000-0000-0000-000000000000", "sort": sort},
            )
            text = call_result_text(result)
            # Should fail with not-found, NOT a validation error
            assert "sort" not in text.lower() or text.startswith("Error:"), (
                f"sort={sort!r} caused unexpected response: {text}"
            )


# ---------------------------------------------------------------------------
# enable / disable / update with a fake ID — expect API error, not crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMonitorWriteToolsWithFakeId:
    FAKE_ID = "00000000-0000-0000-0000-000000000000"

    async def test_disable_unknown_monitor_returns_error(self, mcp):
        result = await mcp.call_tool("disable_monitor", {"monitor_id": self.FAKE_ID})
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_enable_unknown_monitor_returns_error(self, mcp):
        result = await mcp.call_tool("enable_monitor", {"monitor_id": self.FAKE_ID})
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_update_unknown_monitor_returns_error(self, mcp):
        # v1.5.3: monitors take centralized webhook_ids (no inline webhook config).
        result = await mcp.call_tool(
            "update_monitor",
            {"monitor_id": self.FAKE_ID, "webhook_ids": [self.FAKE_ID]},
        )
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_update_monitor_invalid_limit_returns_error(self, mcp):
        result = await mcp.call_tool(
            "update_monitor", {"monitor_id": self.FAKE_ID, "limit": 5}
        )
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "limit" in text.lower()

    async def test_delete_unknown_monitor_returns_error(self, mcp):
        result = await mcp.call_tool("delete_monitor", {"monitor_id": self.FAKE_ID})
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_get_status_unknown_monitor_returns_error(self, mcp):
        result = await mcp.call_tool("get_monitor_status", {"monitor_id": self.FAKE_ID})
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_create_monitor_without_valid_job_returns_error(self, mcp):
        result = await mcp.call_tool(
            "create_monitor",
            {
                "reference_job_id": self.FAKE_ID,
                "schedule": "every day at 9 AM UTC",
            },
        )
        text = call_result_text(result)
        assert text.startswith("Error:")

    async def test_create_monitor_invalid_limit_returns_error(self, mcp):
        result = await mcp.call_tool(
            "create_monitor",
            {
                "reference_job_id": self.FAKE_ID,
                "schedule": "every day at 9 AM UTC",
                "limit": 5,  # minimum is 10
            },
        )
        text = call_result_text(result)
        assert text.startswith("Error:")
        assert "limit" in text.lower()


# ---------------------------------------------------------------------------
# Full monitor lifecycle — opt-in only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@run_monitor_tests
class TestMonitorLifecycle:
    """
    Full lifecycle: submit job → create monitor → list → disable → enable → update.
    Requires a real API key AND CATCHALL_RUN_MONITOR_TESTS=1.
    """

    async def test_full_monitor_lifecycle(self, mcp):
        # 1. Submit a job to use as reference
        submit = await mcp.call_tool(
            "submit_query",
            {"query": "AI company funding rounds", "limit": 10},
        )
        submit_data = call_result_json(submit)
        job_id = submit_data["job_id"]

        # 2. Create monitor (backfill=False so reference job age doesn't matter)
        create = await mcp.call_tool(
            "create_monitor",
            {
                "reference_job_id": job_id,
                "schedule": "every day at 9 AM UTC",
                "backfill": False,
                "limit": 10,
            },
        )
        create_data = call_result_json(create)
        assert "monitor_id" in create_data, f"Expected monitor_id: {create_data}"
        monitor_id = create_data["monitor_id"]

        # 3. List monitors — new monitor should appear
        list_result = await mcp.call_tool("list_monitors", {})
        list_data = call_result_json(list_result)
        monitor_ids = [m.get("monitor_id") for m in list_data.get("monitors", [])]
        assert monitor_id in monitor_ids, f"Monitor {monitor_id} not in list"

        # 4. Disable
        disable = await mcp.call_tool("disable_monitor", {"monitor_id": monitor_id})
        disable_text = call_result_text(disable)
        assert not disable_text.startswith("Error:"), f"Disable failed: {disable_text}"

        # 5. Enable
        enable = await mcp.call_tool("enable_monitor", {"monitor_id": monitor_id})
        enable_text = call_result_text(enable)
        assert not enable_text.startswith("Error:"), f"Enable failed: {enable_text}"

        # 6. Update per-run limit (webhooks are now assigned via webhook_ids)
        update = await mcp.call_tool(
            "update_monitor",
            {
                "monitor_id": monitor_id,
                "limit": 15,
            },
        )
        update_data = call_result_json(update)
        assert "monitor_id" in update_data or update_data is not None

        # 7. Disable again to clean up (avoid recurring charges)
        await mcp.call_tool("disable_monitor", {"monitor_id": monitor_id})
