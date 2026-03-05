import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


def _install_test_stubs() -> None:
    """Install lightweight stubs when runtime deps are unavailable."""
    try:
        import httpx  # noqa: F401
    except ModuleNotFoundError:
        httpx_module = types.ModuleType("httpx")

        class AsyncClient:  # pragma: no cover - only used in fallback envs
            def __init__(self, *args, **kwargs):
                raise RuntimeError("httpx.AsyncClient stub should be patched in tests.")

        httpx_module.AsyncClient = AsyncClient
        sys.modules["httpx"] = httpx_module

    try:
        import fastmcp  # noqa: F401
    except ModuleNotFoundError:
        fastmcp_module = types.ModuleType("fastmcp")
        middleware_module = types.ModuleType("fastmcp.server.middleware")
        dependencies_module = types.ModuleType("fastmcp.server.dependencies")
        server_module = types.ModuleType("fastmcp.server")

        class FastMCP:  # pragma: no cover - only used in fallback envs
            def __init__(self, *args, **kwargs):
                pass

            def add_middleware(self, *args, **kwargs):
                pass

            def tool(self):
                def decorator(func):
                    return func

                return decorator

            def run(self):
                pass

        class Middleware:  # pragma: no cover - only used in fallback envs
            pass

        class MiddlewareContext:  # pragma: no cover - only used in fallback envs
            pass

        def get_http_request():  # pragma: no cover - only used in fallback envs
            raise RuntimeError("No HTTP request context in tests.")

        fastmcp_module.FastMCP = FastMCP
        middleware_module.Middleware = Middleware
        middleware_module.MiddlewareContext = MiddlewareContext
        dependencies_module.get_http_request = get_http_request

        sys.modules["fastmcp"] = fastmcp_module
        sys.modules["fastmcp.server"] = server_module
        sys.modules["fastmcp.server.middleware"] = middleware_module
        sys.modules["fastmcp.server.dependencies"] = dependencies_module


_install_test_stubs()

import server


class DummyResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict:
        return self._payload


class DummyAsyncClient:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._response = DummyResponse(payload, status_code)
        self.request_kwargs: dict | None = None
        self.client_kwargs: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method: str, url: str, headers: dict, json=None, params=None):
        self.request_kwargs = {
            "method": method,
            "url": url,
            "headers": headers,
            "json": json,
            "params": params,
        }
        return self._response


class AsyncClientFactory:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.client = DummyAsyncClient(payload, status_code)

    def __call__(self, *args, **kwargs):
        self.client.client_kwargs = kwargs
        return self.client


class ApiKeyPrecedenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.get("CATCHALL_API_KEY")
        self._token = server.session_api_key.set("")
        os.environ.pop("CATCHALL_API_KEY", None)

    def tearDown(self) -> None:
        server.session_api_key.reset(self._token)
        if self._old_env is None:
            os.environ.pop("CATCHALL_API_KEY", None)
        else:
            os.environ["CATCHALL_API_KEY"] = self._old_env

    def test_get_api_key_precedence(self) -> None:
        os.environ["CATCHALL_API_KEY"] = "env_key"
        session_token = server.session_api_key.set("session_key")
        try:
            self.assertEqual(server.get_api_key("explicit_key"), "explicit_key")
            self.assertEqual(server.get_api_key(""), "session_key")
        finally:
            server.session_api_key.reset(session_token)

        self.assertEqual(server.get_api_key(""), "env_key")

    def test_get_api_key_missing_raises(self) -> None:
        with self.assertRaises(ValueError):
            server.get_api_key("")


class ApiRequestAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._old_env = os.environ.get("CATCHALL_API_KEY")
        self._token = server.session_api_key.set("")
        os.environ.pop("CATCHALL_API_KEY", None)

    async def asyncTearDown(self) -> None:
        server.session_api_key.reset(self._token)
        if self._old_env is None:
            os.environ.pop("CATCHALL_API_KEY", None)
        else:
            os.environ["CATCHALL_API_KEY"] = self._old_env

    async def test_make_api_request_without_auth_sends_no_key(self) -> None:
        factory = AsyncClientFactory({"healthy": True})
        with patch("server.httpx.AsyncClient", side_effect=factory):
            result = await server.make_api_request(
                api_key="",
                method="GET",
                path="/health",
                require_auth=False,
            )

        self.assertEqual(result, {"healthy": True})
        assert factory.client.request_kwargs is not None
        self.assertNotIn("x-api-key", factory.client.request_kwargs["headers"])

    async def test_make_api_request_without_auth_uses_optional_key_if_present(self) -> None:
        os.environ["CATCHALL_API_KEY"] = "env_key"
        factory = AsyncClientFactory({"version": "0.0.1"})
        with patch("server.httpx.AsyncClient", side_effect=factory):
            result = await server.make_api_request(
                api_key="",
                method="GET",
                path="/version",
                require_auth=False,
            )

        self.assertEqual(result, {"version": "0.0.1"})
        assert factory.client.request_kwargs is not None
        self.assertEqual(factory.client.request_kwargs["headers"]["x-api-key"], "env_key")

    async def test_make_api_request_with_auth_requires_key(self) -> None:
        with patch("server.httpx.AsyncClient") as mock_client:
            with self.assertRaises(ValueError):
                await server.make_api_request(
                    api_key="",
                    method="GET",
                    path="/catchAll/jobs/user",
                    require_auth=True,
                )
        mock_client.assert_not_called()


class ValidationHelperTests(unittest.TestCase):
    def test_coerce_definition_list(self) -> None:
        parsed = server.coerce_definition_list(
            '[{"name":"is_event","description":"true if event"}]',
            "validators",
        )
        self.assertEqual(parsed, [{"name": "is_event", "description": "true if event"}])
        self.assertIsNone(server.coerce_definition_list("", "validators"))
        self.assertIsNone(server.coerce_definition_list(None, "validators"))

        with self.assertRaises(ValueError):
            server.coerce_definition_list("{bad-json", "validators")
        with self.assertRaises(ValueError):
            server.coerce_definition_list('{"name":"not-an-array"}', "validators")

    def test_validate_page_params(self) -> None:
        server.validate_page_params(1, 1000)
        with self.assertRaises(ValueError):
            server.validate_page_params(0, 1)
        with self.assertRaises(ValueError):
            server.validate_page_params(1, 0)
        with self.assertRaises(ValueError):
            server.validate_page_params(1, 1001)

    def test_validate_sort(self) -> None:
        self.assertEqual(server.validate_sort("asc"), "asc")
        self.assertEqual(server.validate_sort("desc"), "desc")
        with self.assertRaises(ValueError):
            server.validate_sort("latest")

    def test_validate_new_limit(self) -> None:
        server.validate_new_limit(1)
        with self.assertRaises(ValueError):
            server.validate_new_limit(0)

    def test_validate_webhook_method_and_auth(self) -> None:
        self.assertEqual(server.validate_webhook_method("post"), "POST")
        self.assertEqual(server.validate_webhook_method("PUT"), "PUT")
        with self.assertRaises(ValueError):
            server.validate_webhook_method("PATCH")

        server.validate_webhook_auth(["user", "pass"])
        with self.assertRaises(ValueError):
            server.validate_webhook_auth(["user"])
        with self.assertRaises(ValueError):
            server.validate_webhook_auth(["user", ""])

    def test_build_webhook_payload_requires_url_for_extras(self) -> None:
        with self.assertRaises(ValueError):
            server.build_webhook_payload(
                webhook_url="",
                webhook_method="POST",
                webhook_headers={"x-test": "1"},
                webhook_params=None,
                webhook_auth=None,
            )

        webhook = server.build_webhook_payload(
            webhook_url="https://example.com/hook",
            webhook_method="put",
            webhook_headers={"Authorization": "Bearer token"},
            webhook_params={"team": "qa"},
            webhook_auth=["user", "pass"],
        )
        self.assertEqual(
            webhook,
            {
                "url": "https://example.com/hook",
                "method": "PUT",
                "headers": {"Authorization": "Bearer token"},
                "params": {"team": "qa"},
                "auth": ["user", "pass"],
            },
        )

    def test_validate_validator_definitions(self) -> None:
        normalized = server.validate_validator_definitions(
            [
                {
                    "name": "is_acquisition_event",
                    "description": "true if page describes an acquisition",
                }
            ]
        )
        self.assertEqual(
            normalized,
            [
                {
                    "name": "is_acquisition_event",
                    "description": "true if page describes an acquisition",
                    "type": "boolean",
                }
            ],
        )

        with self.assertRaises(ValueError):
            server.validate_validator_definitions([{"name": "", "description": "d"}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            server.validate_validator_definitions([{"name": "ok", "description": ""}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            server.validate_validator_definitions(  # type: ignore[list-item]
                [{"name": "ok", "description": "d", "type": "number"}]
            )

    def test_validate_enrichment_definitions(self) -> None:
        normalized = server.validate_enrichment_definitions(
            [
                {
                    "name": "acquiring_company",
                    "description": "Extract acquiring company",
                    "type": "company",
                }
            ]
        )
        self.assertEqual(
            normalized,
            [
                {
                    "name": "acquiring_company",
                    "description": "Extract acquiring company",
                    "type": "company",
                }
            ],
        )

        with self.assertRaises(ValueError):
            server.validate_enrichment_definitions([{"name": "", "description": "d", "type": "text"}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            server.validate_enrichment_definitions([{"name": "ok", "description": "", "type": "text"}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            server.validate_enrichment_definitions([{"name": "ok", "description": "d"}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            server.validate_enrichment_definitions(  # type: ignore[list-item]
                [{"name": "ok", "description": "d", "type": "boolean"}]
            )


class ToolBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_tool_call(
        self,
        tool_func,
        tool_kwargs: dict,
        expected_method: str,
        expected_path: str,
        expected_json=None,
        expected_params=None,
        expected_require_auth: bool = True,
    ) -> None:
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"ok": True}
            result = await tool_func(**tool_kwargs)

        self.assertEqual(result, json.dumps({"ok": True}, indent=2))
        mock_api.assert_awaited_once()
        called = mock_api.await_args.kwargs
        self.assertEqual(called["method"], expected_method)
        self.assertEqual(called["path"], expected_path)
        self.assertEqual(called.get("json_data"), expected_json)
        self.assertEqual(called.get("params"), expected_params)
        self.assertEqual(called.get("require_auth", True), expected_require_auth)

    async def test_tool_request_mapping(self) -> None:
        cases = [
            (
                server.initialize_query,
                {"query": "acquisitions"},
                "POST",
                "/catchAll/initialize",
                {"query": "acquisitions"},
                None,
                True,
            ),
            (
                server.submit_query,
                {"query": "acquisitions"},
                "POST",
                "/catchAll/submit",
                {"query": "acquisitions"},
                None,
                True,
            ),
            (
                server.get_job_status,
                {"job_id": "job-1"},
                "GET",
                "/catchAll/status/job-1",
                None,
                None,
                True,
            ),
            (
                server.pull_results,
                {"job_id": "job-1", "page": 2, "page_size": 50},
                "GET",
                "/catchAll/pull/job-1",
                None,
                {"page": 2, "page_size": 50},
                True,
            ),
            (
                server.continue_job,
                {"job_id": "job-1", "new_limit": 10},
                "POST",
                "/catchAll/continue",
                {"job_id": "job-1", "new_limit": 10},
                None,
                True,
            ),
            (
                server.list_user_jobs,
                {"page": 3, "page_size": 25},
                "GET",
                "/catchAll/jobs/user",
                None,
                {"page": 3, "page_size": 25},
                True,
            ),
            (
                server.create_monitor,
                {"reference_job_id": "job-1", "schedule": "every day at 9 AM UTC"},
                "POST",
                "/catchAll/monitors/create",
                {"reference_job_id": "job-1", "schedule": "every day at 9 AM UTC"},
                None,
                True,
            ),
            (
                server.list_monitors,
                {},
                "GET",
                "/catchAll/monitors",
                None,
                None,
                True,
            ),
            (
                server.pull_monitor_results,
                {"monitor_id": "mon-1"},
                "GET",
                "/catchAll/monitors/pull/mon-1",
                None,
                None,
                True,
            ),
            (
                server.list_monitor_jobs,
                {"monitor_id": "mon-1", "sort": "desc"},
                "GET",
                "/catchAll/monitors/mon-1/jobs",
                None,
                {"sort": "desc"},
                True,
            ),
            (
                server.enable_monitor,
                {"monitor_id": "mon-1"},
                "POST",
                "/catchAll/monitors/mon-1/enable",
                None,
                None,
                True,
            ),
            (
                server.disable_monitor,
                {"monitor_id": "mon-1"},
                "POST",
                "/catchAll/monitors/mon-1/disable",
                None,
                None,
                True,
            ),
            (
                server.update_monitor,
                {"monitor_id": "mon-1", "webhook_url": "https://example.com/webhook"},
                "PATCH",
                "/catchAll/monitors/mon-1",
                {"webhook": {"url": "https://example.com/webhook", "method": "POST"}},
                None,
                True,
            ),
            (
                server.check_health,
                {},
                "GET",
                "/health",
                None,
                None,
                False,
            ),
            (
                server.get_version,
                {},
                "GET",
                "/version",
                None,
                None,
                False,
            ),
        ]

        for case in cases:
            with self.subTest(tool=case[0].__name__):
                await self._assert_tool_call(*case)

    async def test_submit_query_normalizes_validator_type(self) -> None:
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"job_id": "job-1"}
            result = await server.submit_query(
                query="acquisitions",
                validators=[
                    {
                        "name": "is_acquisition_event",
                        "description": "true if page describes an acquisition",
                    }
                ],
                enrichments=[
                    {
                        "name": "acquiring_company",
                        "description": "Extract acquiring company",
                        "type": "company",
                    }
                ],
            )

        self.assertEqual(result, json.dumps({"job_id": "job-1"}, indent=2))
        called = mock_api.await_args.kwargs
        self.assertEqual(
            called["json_data"]["validators"],
            [
                {
                    "name": "is_acquisition_event",
                    "description": "true if page describes an acquisition",
                    "type": "boolean",
                }
            ],
        )
        self.assertEqual(
            called["json_data"]["enrichments"],
            [
                {
                    "name": "acquiring_company",
                    "description": "Extract acquiring company",
                    "type": "company",
                }
            ],
        )

    async def test_submit_query_accepts_stringified_definitions(self) -> None:
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"job_id": "job-2"}
            result = await server.submit_query(
                query="incidents",
                validators='[{"name":"is_incident","description":"true if incident"}]',
                enrichments='[{"name":"incident_type","description":"type of incident","type":"option"}]',
            )

        self.assertEqual(result, json.dumps({"job_id": "job-2"}, indent=2))
        called = mock_api.await_args.kwargs
        self.assertEqual(
            called["json_data"]["validators"],
            [
                {
                    "name": "is_incident",
                    "description": "true if incident",
                    "type": "boolean",
                }
            ],
        )
        self.assertEqual(
            called["json_data"]["enrichments"],
            [
                {
                    "name": "incident_type",
                    "description": "type of incident",
                    "type": "option",
                }
            ],
        )

    async def test_tool_validations_fail_early(self) -> None:
        invalid_calls = [
            (server.pull_results, {"job_id": "job-1", "page": 0}, "page must be >= 1."),
            (
                server.list_user_jobs,
                {"page": 1, "page_size": 1001},
                "page_size must be between 1 and 1000.",
            ),
            (
                server.continue_job,
                {"job_id": "job-1", "new_limit": 0},
                "new_limit must be >= 1.",
            ),
            (
                server.list_monitor_jobs,
                {"monitor_id": "mon-1", "sort": "latest"},
                "sort must be either 'asc' or 'desc'.",
            ),
            (
                server.create_monitor,
                {
                    "reference_job_id": "job-1",
                    "schedule": "every day at 9 AM UTC",
                    "webhook_headers": {"Authorization": "Bearer token"},
                },
                "webhook_url is required when providing webhook_method, webhook_headers, webhook_params, or webhook_auth.",
            ),
            (
                server.create_monitor,
                {
                    "reference_job_id": "job-1",
                    "schedule": "every day at 9 AM UTC",
                    "webhook_url": "https://example.com",
                    "webhook_method": "PATCH",
                },
                "webhook_method must be 'POST' or 'PUT'.",
            ),
            (
                server.create_monitor,
                {
                    "reference_job_id": "job-1",
                    "schedule": "every day at 9 AM UTC",
                    "webhook_url": "https://example.com",
                    "webhook_auth": ["only-user"],
                },
                "webhook_auth must contain exactly two values: [username, password].",
            ),
            (
                server.submit_query,
                {
                    "query": "acquisitions",
                    "validators": [{"name": "invalid", "description": "test", "type": "number"}],
                },
                "validators[0].type must be 'boolean'.",
            ),
            (
                server.submit_query,
                {
                    "query": "acquisitions",
                    "validators": "{bad-json",
                },
                "validators must be valid JSON array when provided as string.",
            ),
            (
                server.submit_query,
                {
                    "query": "acquisitions",
                    "enrichments": [{"name": "deal_value", "description": "Extract value", "type": "boolean"}],
                },
                "enrichments[0].type must be one of: company, date, dict, number, option, text, url.",
            ),
            (
                server.submit_query,
                {
                    "query": "acquisitions",
                    "enrichments": '{"name":"not-array"}',
                },
                "enrichments must be a JSON array when provided.",
            ),
            (
                server.update_monitor,
                {"monitor_id": "mon-1", "webhook_params": {"k": "v"}},
                "webhook_url is required when providing webhook_method, webhook_headers, webhook_params, or webhook_auth.",
            ),
        ]

        for tool_func, kwargs, expected_error in invalid_calls:
            with self.subTest(tool=tool_func.__name__, kwargs=kwargs):
                with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
                    result = await tool_func(**kwargs)
                self.assertEqual(result, f"Error: {expected_error}")
                mock_api.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
