import asyncio
import unittest

import httpx

from live_pocs.app import create_app
from live_tests.test_service import RecordingBackend, settings


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = RecordingBackend()
        self.settings = settings()
        self.app = create_app(self.settings, lambda configured: self.backend)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://fixture"
        )
        self.headers = {
            "x-poc-backend-key": self.settings.backend_key,
            "x-poc-tenant-id": "customer-a",
        }

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)

    async def submit(self, **changes):
        body = {"session_id": "same-session", "prompt": "synthetic", "new_session": True, **changes}
        return await self.client.post("/runs", json=body, headers=self.headers)

    async def test_health_is_configuration_only_and_never_invokes_inference(self):
        response = await self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "configured")
        self.assertFalse(response.json()["inference_verified"])
        self.assertFalse(self.backend.calls)

    async def test_direct_unauthenticated_access_is_rejected(self):
        for headers in ({}, {"x-poc-backend-key": "invalid", "x-poc-tenant-id": "customer-a"}):
            with self.subTest(headers=headers):
                response = await self.client.post(
                    "/runs", json={"session_id": "s", "prompt": "test", "new_session": True},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 401)
        self.assertFalse(self.backend.calls)

    async def test_unknown_derived_tenant_is_rejected(self):
        response = await self.client.post(
            "/runs", json={"session_id": "s", "prompt": "test", "new_session": True},
            headers={**self.headers, "x-poc-tenant-id": "other"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.backend.calls)

    async def test_extra_tenant_provider_and_endpoint_fields_are_forbidden(self):
        for extra in (
            {"tenant_id": "customer-b"},
            {"provider": "other"},
            {"endpoint": "https://example.invalid"},
            {"credential": "SECRET_SHOULD_NOT_BE_ECHOED"},
        ):
            with self.subTest(extra=extra):
                response = await self.submit(**extra)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"error": {"code": "invalid_request"}})
                self.assertNotIn("SECRET_SHOULD_NOT_BE_ECHOED", response.text)
        self.assertFalse(self.backend.calls)

    async def test_submit_returns_fast_job_and_correlated_result(self):
        self.backend.delay = 0.02
        response = await self.submit()
        self.assertEqual(response.status_code, 202)
        run = response.json()
        self.assertEqual(run["status"], "queued")
        self.assertEqual(run["tenant_id"], "customer-a")
        self.assertEqual(run["session_id"], "same-session")
        for _ in range(100):
            status = await self.client.get("/runs/" + run["run_id"], headers=self.headers)
            if status.json()["status"] == "succeeded":
                break
            await asyncio.sleep(0.002)
        else:
            self.fail("Synthetic run did not finish")
        self.assertEqual(status.json()["run_id"], run["run_id"])
        self.assertEqual(status.json()["turn"], 1)

    async def test_another_tenant_cannot_read_or_cancel_a_run(self):
        response = await self.submit()
        path = "/runs/" + response.json()["run_id"]
        headers = {**self.headers, "x-poc-tenant-id": "customer-b"}
        self.assertEqual((await self.client.get(path, headers=headers)).status_code, 404)
        self.assertEqual((await self.client.delete(path, headers=headers)).status_code, 404)

    async def test_unknown_model_alias_fails_before_accepting_a_job(self):
        response = await self.submit(model_alias="unapproved-model")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "model_not_allowed")
        self.assertFalse(self.backend.calls)

    async def test_live_disabled_surface_is_explicit(self):
        self.app.state.service.settings = settings(live_enabled=False)
        response = await self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "live_inference_disabled")
        self.assertFalse(self.backend.calls)
