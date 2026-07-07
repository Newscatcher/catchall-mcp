import base64
import inspect
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
import validators


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

    async def test_make_api_request_empty_body_returns_empty_dict(self) -> None:
        """API returns 200 with empty body (e.g. list endpoints with no items)."""

        class EmptyDummyResponse:
            status_code = 200
            text = ""

            def json(self):
                import json as _json
                raise _json.JSONDecodeError("Empty body", "", 0)

        class EmptyClientFactory:
            def __call__(self, *args, **kwargs):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def request(self, *args, **kwargs):
                return EmptyDummyResponse()

        with patch("server.httpx.AsyncClient", side_effect=EmptyClientFactory()):
            result = await server.make_api_request(
                api_key="key",
                method="GET",
                path="/catchAll/monitors",
                require_auth=True,
            )
        self.assertEqual(result, {})


class ValidationHelperTests(unittest.TestCase):
    def test_coerce_definition_list(self) -> None:
        parsed = validators.coerce_definition_list(
            '[{"name":"is_event","description":"true if event"}]',
            "validators",
        )
        self.assertEqual(parsed, [{"name": "is_event", "description": "true if event"}])
        self.assertIsNone(validators.coerce_definition_list("", "validators"))
        self.assertIsNone(validators.coerce_definition_list(None, "validators"))

        with self.assertRaises(ValueError):
            validators.coerce_definition_list("{bad-json", "validators")
        with self.assertRaises(ValueError):
            validators.coerce_definition_list('{"name":"not-an-array"}', "validators")

    def test_validate_page_params(self) -> None:
        validators.validate_page_params(1, 1000)
        with self.assertRaises(ValueError):
            validators.validate_page_params(0, 1)
        with self.assertRaises(ValueError):
            validators.validate_page_params(1, 0)
        with self.assertRaises(ValueError):
            validators.validate_page_params(1, 1001)

    def test_validate_sort(self) -> None:
        self.assertEqual(validators.validate_sort("asc"), "asc")
        self.assertEqual(validators.validate_sort("desc"), "desc")
        with self.assertRaises(ValueError):
            validators.validate_sort("latest")

    def test_validate_new_limit(self) -> None:
        validators.validate_new_limit(1)
        with self.assertRaises(ValueError):
            validators.validate_new_limit(0)

    def test_validate_monitor_limit(self) -> None:
        validators.validate_monitor_limit(10)
        with self.assertRaises(ValueError):
            validators.validate_monitor_limit(9)

    def test_validate_http_method(self) -> None:
        # v1.5.3: webhook delivery accepts the full HttpMethod enum (the live API
        # even has a GET webhook), not just POST/PUT.
        self.assertEqual(validators.validate_http_method("post"), "POST")
        self.assertEqual(validators.validate_http_method("PUT"), "PUT")
        self.assertEqual(validators.validate_http_method("get"), "GET")
        self.assertEqual(validators.validate_http_method("patch"), "PATCH")
        with self.assertRaises(ValueError):
            validators.validate_http_method("TRACE")

    def test_validate_choice(self) -> None:
        self.assertEqual(validators.validate_choice("job", validators.MAPPABLE_RESOURCE_TYPES, "rt"), "job")
        self.assertEqual(validators.validate_choice("dataset", validators.PROJECT_RESOURCE_TYPES, "rt"), "dataset")
        self.assertEqual(validators.validate_choice("own", validators.OWNERSHIP_VALUES, "ownership"), "own")
        with self.assertRaises(ValueError):
            validators.validate_choice("nope", validators.WEBHOOK_TYPES, "type")

    def test_coerce_csv_file_content(self) -> None:
        # Raw CSV text passes through as utf-8 bytes.
        csv_text = "name,description\nAcme,maker of anvils\n"
        self.assertEqual(validators.coerce_csv_file_content(csv_text), csv_text.encode("utf-8"))

        # Standard base64-encoded CSV is decoded.
        encoded = base64.b64encode(csv_text.encode("utf-8")).decode("ascii")
        self.assertEqual(validators.coerce_csv_file_content(encoded), csv_text.encode("utf-8"))

        # Empty / non-CSV / non-base64 input fails fast.
        with self.assertRaises(ValueError):
            validators.coerce_csv_file_content("")
        with self.assertRaises(ValueError):
            validators.coerce_csv_file_content("   ")
        with self.assertRaises(ValueError):
            validators.coerce_csv_file_content("not-base64-and-not-csv!!!")
        with self.assertRaises(ValueError):
            validators.coerce_csv_file_content(base64.b64encode(b"").decode("ascii"))

    def test_coerce_csv_file_content_size_cap(self) -> None:
        # Hard 10 MB cap on decoded inline CSV content (memory-DoS guard).
        cap = validators.MAX_CSV_BYTES
        header = "name,description\n"

        # Raw CSV exactly at the cap is accepted.
        at_cap = header + "a" * (cap - len(header))
        self.assertEqual(len(at_cap.encode("utf-8")), cap)
        self.assertEqual(validators.coerce_csv_file_content(at_cap), at_cap.encode("utf-8"))

        # Raw CSV one byte over the cap is rejected with a clear message.
        over_cap = at_cap + "a"
        with self.assertRaises(ValueError) as ctx:
            validators.coerce_csv_file_content(over_cap)
        self.assertIn("10 MB", str(ctx.exception))

        # Base64 that decodes to exactly the cap is accepted.
        at_cap_bytes = at_cap.encode("utf-8")
        encoded_at_cap = base64.b64encode(at_cap_bytes).decode("ascii")
        self.assertEqual(validators.coerce_csv_file_content(encoded_at_cap), at_cap_bytes)

        # Base64 that decodes to one byte over the cap is rejected.
        encoded_over_cap = base64.b64encode(at_cap_bytes + b"a").decode("ascii")
        with self.assertRaises(ValueError) as ctx:
            validators.coerce_csv_file_content(encoded_over_cap)
        self.assertIn("10 MB", str(ctx.exception))

        # A grossly oversized string is rejected by the cheap pre-decode
        # length guard (base64 inflates ~4/3, so > _MAX_CSV_B64_CHARS can
        # never decode under the cap).
        with self.assertRaises(ValueError) as ctx:
            validators.coerce_csv_file_content("A" * (validators._MAX_CSV_B64_CHARS + 1))
        self.assertIn("10 MB", str(ctx.exception))

    def test_validate_webhook_auth_object(self) -> None:
        # v1.5.3 auth is an object (bearer / api_key / basic), not a [user, pass] list.
        bearer = {"type": "bearer", "token": "abc"}
        self.assertEqual(validators.validate_webhook_auth(bearer), bearer)
        api_key = {"type": "api_key", "header": "X-API-Key", "value": "k"}
        self.assertEqual(validators.validate_webhook_auth(api_key), api_key)
        basic = {"type": "basic", "username": "u", "password": "p"}
        self.assertEqual(validators.validate_webhook_auth(basic), basic)
        self.assertIsNone(validators.validate_webhook_auth(None))
        with self.assertRaises(ValueError):
            validators.validate_webhook_auth({"type": "oauth", "token": "x"})
        with self.assertRaises(ValueError):
            validators.validate_webhook_auth({"type": "bearer"})  # missing token
        with self.assertRaises(ValueError):
            validators.validate_webhook_auth({"type": "basic", "username": "u"})  # missing password

    def test_validate_validator_definitions(self) -> None:
        normalized = validators.validate_validator_definitions(
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
            validators.validate_validator_definitions([{"name": "", "description": "d"}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            validators.validate_validator_definitions([{"name": "ok", "description": ""}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            validators.validate_validator_definitions(  # type: ignore[list-item]
                [{"name": "ok", "description": "d", "type": "number"}]
            )

    def test_validate_enrichment_definitions(self) -> None:
        normalized = validators.validate_enrichment_definitions(
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
            validators.validate_enrichment_definitions([{"name": "", "description": "d", "type": "text"}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            validators.validate_enrichment_definitions([{"name": "ok", "description": "", "type": "text"}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            validators.validate_enrichment_definitions([{"name": "ok", "description": "d"}])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            validators.validate_enrichment_definitions(  # type: ignore[list-item]
                [{"name": "ok", "description": "d", "type": "boolean"}]
            )


def _unwrap(tool_func):
    """Return the underlying async function from a FastMCP FunctionTool wrapper."""
    return tool_func.fn if hasattr(tool_func, "fn") else tool_func


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
            result = await _unwrap(tool_func)(**tool_kwargs)

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
                server.continue_job,
                {"job_id": "job-1"},
                "POST",
                "/catchAll/continue",
                {"job_id": "job-1"},
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
                {"reference_job_id": "job-1", "schedule": "every day at 9 AM UTC", "backfill": True},
                None,
                True,
            ),
            (
                server.list_monitors,
                {},
                "GET",
                "/catchAll/monitors/",
                None,
                {"page": 1, "page_size": 100},
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
                server.enable_monitor,
                {"monitor_id": "mon-1", "backfill": False},
                "POST",
                "/catchAll/monitors/mon-1/enable",
                {"backfill": False},
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
                {"monitor_id": "mon-1", "webhook_ids": ["wh-1", "wh-2"]},
                "PATCH",
                "/catchAll/monitors/mon-1",
                {"webhook_ids": ["wh-1", "wh-2"]},
                None,
                True,
            ),
            (
                server.update_monitor,
                {"monitor_id": "mon-1", "webhook_ids": []},
                "PATCH",
                "/catchAll/monitors/mon-1",
                {"webhook_ids": []},
                None,
                True,
            ),
            (
                server.update_monitor,
                {"monitor_id": "mon-1", "limit": 10},
                "PATCH",
                "/catchAll/monitors/mon-1",
                {"limit": 10},
                None,
                True,
            ),
            (
                server.create_monitor,
                {"reference_job_id": "job-1", "schedule": "every day at 9 AM EST",
                 "timezone": "America/New_York", "webhook_ids": ["wh-1"], "backfill": False},
                "POST",
                "/catchAll/monitors/create",
                {"reference_job_id": "job-1", "schedule": "every day at 9 AM EST",
                 "backfill": False, "timezone": "America/New_York", "webhook_ids": ["wh-1"]},
                None,
                True,
            ),
            (
                server.delete_job,
                {"job_id": "job-1"},
                "DELETE",
                "/catchAll/jobs/job-1",
                None,
                None,
                True,
            ),
            (
                server.validate_query,
                {"query": "tesla layoffs 2024"},
                "POST",
                "/catchAll/validate",
                {"query": "tesla layoffs 2024"},
                None,
                True,
            ),
            (
                server.delete_monitor,
                {"monitor_id": "mon-1"},
                "DELETE",
                "/catchAll/monitors/mon-1",
                None,
                None,
                True,
            ),
            (
                server.get_monitor_status,
                {"monitor_id": "mon-1"},
                "GET",
                "/catchAll/monitors/mon-1/status",
                None,
                None,
                True,
            ),
            (
                server.create_project,
                {"name": "Acme"},
                "POST",
                "/catchAll/projects/",
                {"name": "Acme"},
                None,
                True,
            ),
            (
                server.create_webhook,
                {"name": "wh", "url": "https://example.com/h", "type": "slack",
                 "delivery_mode": "per_record"},
                "POST",
                "/catchAll/webhooks",
                {"name": "wh", "url": "https://example.com/h", "method": "POST",
                 "type": "slack", "delivery_mode": "per_record"},
                None,
                True,
            ),
            (
                server.assign_webhook_resource,
                {"webhook_id": "wh-1", "resource_type": "job", "resource_id": "job-1"},
                "POST",
                "/catchAll/webhooks/wh-1/resources",
                {"resource_type": "job", "resource_id": "job-1"},
                None,
                True,
            ),
            (
                server.trigger_webhook,
                {"webhook_id": "wh-1", "resource_type": "job", "resource_id": "job-1"},
                "POST",
                "/catchAll/webhook/trigger/job/job-1",
                None,
                {"webhook_id": "wh-1"},
                True,
            ),
            (
                server.trigger_webhook,
                {"webhook_id": "wh-1", "resource_type": "monitor", "resource_id": "mon-1",
                 "job_id": "job-9"},
                "POST",
                "/catchAll/webhook/trigger/monitor/mon-1",
                None,
                {"webhook_id": "wh-1", "job_id": "job-9"},
                True,
            ),
            (
                server.create_entity,
                {"name": "Stripe", "entity_type": "company"},
                "POST",
                "/catchAll/entities/",
                {"name": "Stripe", "entity_type": "company"},
                None,
                True,
            ),
            (
                server.create_dataset,
                {"name": "ds"},
                "POST",
                "/catchAll/datasets/",
                {"name": "ds"},
                None,
                True,
            ),
            (
                server.add_dataset_entities,
                {"dataset_id": "ds-1", "entity_ids": ["e1", "e2"]},
                "POST",
                "/catchAll/datasets/ds-1/entities",
                {"entity_ids": ["e1", "e2"]},
                None,
                True,
            ),
            (
                server.get_user_limits,
                {},
                "POST",
                "/catchAll/user/limits",
                None,
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
            with self.subTest(tool=_unwrap(case[0]).__name__):
                await self._assert_tool_call(*case)

    async def test_submit_query_normalizes_validator_type(self) -> None:
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"job_id": "job-1"}
            result = await _unwrap(server.submit_query)(
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
            result = await _unwrap(server.submit_query)(
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

    async def test_submit_query_mode_included_when_provided(self) -> None:
        for mode_value in ("lite", "base"):
            with self.subTest(mode=mode_value):
                with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
                    mock_api.return_value = {"job_id": "job-3"}
                    await _unwrap(server.submit_query)(query="test", mode=mode_value)
                called = mock_api.await_args.kwargs
                self.assertEqual(called["json_data"]["mode"], mode_value)

    async def test_submit_query_mode_omitted_when_empty(self) -> None:
        with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = {"job_id": "job-4"}
            await _unwrap(server.submit_query)(query="test")
        called = mock_api.await_args.kwargs
        self.assertNotIn("mode", called["json_data"])

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
                    "limit": 9,
                },
                "limit must be >= 10.",
            ),
            (
                server.create_webhook,
                {"name": "wh", "url": "https://example.com/h", "type": "carrier-pigeon"},
                "type must be one of: custom, generic, slack, teams.",
            ),
            (
                server.create_webhook,
                {"name": "wh", "url": "https://example.com/h", "method": "TRACE"},
                "method must be one of: DELETE, GET, PATCH, POST, PUT.",
            ),
            (
                server.assign_webhook_resource,
                {"webhook_id": "wh-1", "resource_type": "widget", "resource_id": "r-1"},
                "resource_type must be one of: job, monitor, monitor_group.",
            ),
            (
                server.trigger_webhook,
                {"webhook_id": "wh-1", "resource_type": "widget", "resource_id": "r-1"},
                "resource_type must be one of: job, monitor, monitor_group.",
            ),
            (
                server.add_project_resources,
                {"project_id": "p-1", "resources": [{"resource_type": "widget", "resource_id": "r-1"}]},
                "resources[0].resource_type must be one of: dataset, job, monitor, monitor_group.",
            ),
            (
                server.list_user_jobs,
                {"ownership": "everyone"},
                "ownership must be one of: all, own, shared.",
            ),
            (
                server.update_monitor,
                {"monitor_id": "mon-1", "limit": 9},
                "limit must be >= 10.",
            ),
            (
                server.list_monitors,
                {"page": 0},
                "page must be >= 1.",
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
                "enrichments[0].type must be one of: company, date, number, option, text, url.",
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
                server.submit_query,
                {"query": "acquisitions", "mode": "fast"},
                "mode must be 'lite' or 'base'.",
            ),
        ]

        for tool_func, kwargs, expected_error in invalid_calls:
            fn = _unwrap(tool_func)
            with self.subTest(tool=fn.__name__, kwargs=kwargs):
                with patch("server.make_api_request", new_callable=AsyncMock) as mock_api:
                    result = await fn(**kwargs)
                self.assertEqual(result, f"Error: {expected_error}")
                mock_api.assert_not_awaited()

    async def test_validate_query_has_no_context_param(self) -> None:
        """v1.6.1 removed `context` from CheckQueryQualityRequestDto — the tool
        signature must no longer advertise it."""
        params = inspect.signature(_unwrap(server.validate_query)).parameters
        self.assertNotIn("context", params)
        self.assertEqual(set(params), {"query", "api_key"})


CSV_TEXT = "name,description\nAcme,maker of anvils\nGlobex,evil conglomerate\n"


class CsvUploadToolTests(unittest.IsolatedAsyncioTestCase):
    """Tools for the v1.6.1 CSV dataset upload endpoints."""

    async def test_create_dataset_from_csv_full_request(self) -> None:
        payload = {
            "dataset_id": "ds-1",
            "dataset_name": "anvils",
            "entities_created": 2,
            "validation_report": {
                "total_rows": 2,
                "valid_rows": 2,
                "skipped_count": 0,
                "skipped_rows": [],
            },
        }
        with patch("server.make_api_upload", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = payload
            result = await _unwrap(server.create_dataset_from_csv)(
                name="anvils",
                file=CSV_TEXT,
                description="suppliers",
                project_id="proj-1",
            )

        self.assertEqual(result, json.dumps(payload, indent=2))
        mock_upload.assert_awaited_once()
        called = mock_upload.await_args.kwargs
        self.assertEqual(called["path"], "/catchAll/datasets/upload")
        self.assertEqual(called["file_bytes"], CSV_TEXT.encode("utf-8"))
        self.assertEqual(
            called["data"],
            {"name": "anvils", "description": "suppliers", "project_id": "proj-1"},
        )

    async def test_create_dataset_from_csv_omits_empty_optionals(self) -> None:
        with patch("server.make_api_upload", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = {"dataset_id": "ds-1"}
            await _unwrap(server.create_dataset_from_csv)(name="anvils", file=CSV_TEXT)

        called = mock_upload.await_args.kwargs
        self.assertEqual(called["data"], {"name": "anvils"})

    async def test_create_dataset_from_csv_accepts_base64(self) -> None:
        encoded = base64.b64encode(CSV_TEXT.encode("utf-8")).decode("ascii")
        with patch("server.make_api_upload", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = {"dataset_id": "ds-1"}
            await _unwrap(server.create_dataset_from_csv)(name="anvils", file=encoded)

        called = mock_upload.await_args.kwargs
        self.assertEqual(called["file_bytes"], CSV_TEXT.encode("utf-8"))

    async def test_append_csv_to_dataset_request(self) -> None:
        payload = {
            "dataset_id": "ds-1",
            "entities_created": 1,
            "validation_report": {
                "total_rows": 2,
                "valid_rows": 1,
                "skipped_count": 1,
                "skipped_rows": [{"row": 2, "reason": "duplicate name"}],
            },
        }
        with patch("server.make_api_upload", new_callable=AsyncMock) as mock_upload:
            mock_upload.return_value = payload
            result = await _unwrap(server.append_csv_to_dataset)(
                dataset_id="ds-1", file=CSV_TEXT
            )

        self.assertEqual(result, json.dumps(payload, indent=2))
        called = mock_upload.await_args.kwargs
        self.assertEqual(called["path"], "/catchAll/datasets/ds-1/upload")
        self.assertEqual(called["file_bytes"], CSV_TEXT.encode("utf-8"))
        # Body_uploadCSVToDataset only has `file` — no extra form fields.
        self.assertNotIn("data", called)

    async def test_upload_tools_validate_input_before_calling_api(self) -> None:
        invalid_calls = [
            (
                server.create_dataset_from_csv,
                {"name": "", "file": CSV_TEXT},
                "Error: name is required.",
            ),
            (
                server.create_dataset_from_csv,
                {"name": "anvils", "file": ""},
                "Error: file is required: pass the CSV content as raw text or base64.",
            ),
            (
                server.append_csv_to_dataset,
                {"dataset_id": "", "file": CSV_TEXT},
                "Error: dataset_id is required.",
            ),
            (
                server.append_csv_to_dataset,
                {"dataset_id": "ds-1", "file": "not-base64-and-not-csv!!!"},
                "Error: file must be raw CSV text or standard base64-encoded CSV content.",
            ),
        ]
        for tool_func, kwargs, expected_error in invalid_calls:
            fn = _unwrap(tool_func)
            with self.subTest(tool=fn.__name__, kwargs=kwargs):
                with patch("server.make_api_upload", new_callable=AsyncMock) as mock_upload:
                    result = await fn(**kwargs)
                self.assertEqual(result, expected_error)
                mock_upload.assert_not_awaited()


class UploadRequestTests(unittest.IsolatedAsyncioTestCase):
    """make_api_upload sends authenticated multipart requests."""

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

    async def test_make_api_upload_requires_key(self) -> None:
        with patch("server.httpx.AsyncClient") as mock_client:
            with self.assertRaises(ValueError):
                await server.make_api_upload(
                    api_key="",
                    path="/catchAll/datasets/upload",
                    file_bytes=b"name\nAcme\n",
                    data={"name": "ds"},
                )
        mock_client.assert_not_called()

    async def test_make_api_upload_sends_multipart(self) -> None:
        class UploadDummyClient:
            def __init__(self) -> None:
                self.post_kwargs: dict | None = None

            def __call__(self, *args, **kwargs):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, path, headers=None, files=None, data=None):
                self.post_kwargs = {
                    "path": path,
                    "headers": headers,
                    "files": files,
                    "data": data,
                }
                return DummyResponse({"dataset_id": "ds-1"})

        client = UploadDummyClient()
        with patch("server.httpx.AsyncClient", side_effect=client):
            result = await server.make_api_upload(
                api_key="key",
                path="/catchAll/datasets/upload",
                file_bytes=b"name\nAcme\n",
                data={"name": "ds"},
            )

        self.assertEqual(result, {"dataset_id": "ds-1"})
        assert client.post_kwargs is not None
        self.assertEqual(client.post_kwargs["path"], "/catchAll/datasets/upload")
        self.assertEqual(client.post_kwargs["headers"]["x-api-key"], "key")
        # No manual Content-Type: httpx must set the multipart boundary itself.
        self.assertNotIn("Content-Type", client.post_kwargs["headers"])
        self.assertEqual(
            client.post_kwargs["files"], {"file": ("upload.csv", b"name\nAcme\n", "text/csv")}
        )
        self.assertEqual(client.post_kwargs["data"], {"name": "ds"})


if __name__ == "__main__":
    unittest.main()
