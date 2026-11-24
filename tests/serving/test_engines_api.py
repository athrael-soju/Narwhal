"""Check the membership routes: list, join and leave."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.runtime import state as handoff_state
from narwhal.runtime.lifecycle import leave
from narwhal.runtime.lifecycle.join import JoinOutcome
from narwhal.serving import engines_api
from narwhal.serving.app import create_app
from narwhal.serving.schemas import MembershipOut
from narwhal.types import Request
from tests.fixtures import fleet

NEW = {
    "iid": "e6",
    "url": "http://engine-6.invalid:8000",
    "role": "decode",
    "attestation_url": "http://engine-6.invalid:8000/v1/attestation",
}


class EnginesApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.cfg = fleet(root, engines=("e0", "e1", "e3"))
        self.cfg.state_path = root / "handoff.json"
        app = create_app(self.cfg)
        self.router = app.state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        )
        self.addAsyncCleanup(self.client.aclose)
        capture = AsyncMock(
            side_effect=lambda cfg, engines, **kwargs: (dict.fromkeys(engines, 100.0), {})
        )
        patcher = patch.object(leave, "capture_process_identities", capture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def document(self, response, status):
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        MembershipOut.model_validate(body)
        self.assertEqual((body["schema"], body["schema_version"]), ("narwhal.membership", 1))
        return body

    async def test_get_lists_the_snapshot_and_its_generation(self):
        body = self.document(await self.client.get("/narwhal/engines"), 200)
        self.assertEqual(body["generation"], 0)
        self.assertEqual([row["iid"] for row in body["engines"]], ["e0", "e1", "e3"])
        self.assertEqual(body["engines"][0]["role"], "prefill")
        self.assertEqual(body["leaving"], [])
        self.assertIsNone(body["join"])
        self.router.standby = True
        self.document(await self.client.get("/narwhal/engines"), 200)

    async def test_post_joins_a_validated_engine(self):
        async def joined(router, spec, *, transport=None):
            router.apply_membership(router.membership.joined(spec))
            return JoinOutcome(spec.iid, True, 1, checks=["health"])

        with patch.object(engines_api, "join_engine", AsyncMock(side_effect=joined)) as join:
            body = self.document(await self.client.post("/narwhal/engines", json=NEW), 200)
        spec = join.await_args.args[1]
        self.assertEqual((spec.iid, spec.url, spec.role.value), ("e6", NEW["url"], "decode"))
        self.assertEqual(body["generation"], 1)
        self.assertEqual(
            body["join"], {"iid": "e6", "joined": True, "checks": ["health"], "failures": []}
        )
        self.assertIn("e6", [row["iid"] for row in body["engines"]])
        saved = handoff_state.load(self.cfg.state_path)
        self.assertEqual(saved["membership"]["generation"], 1)

    async def test_post_refusals_and_invalid_specs(self):
        refused = JoinOutcome("e6", False, 0, failures=["attestation: engine_version differs"])
        with patch.object(engines_api, "join_engine", AsyncMock(return_value=refused)):
            body = self.document(await self.client.post("/narwhal/engines", json=NEW), 409)
        self.assertEqual(body["error"], "attestation: engine_version differs")
        self.assertFalse(body["join"]["joined"])
        invalid = {
            "unknown engine key 'color'": {**NEW, "color": 1},
            "engine is missing 'url'": {"iid": "e6"},
            "engine.role": {**NEW, "role": "both"},
            "engine.pin must be a boolean": {**NEW, "pin": "yes"},
            "url must be an http or https URL with a host": {**NEW, "url": "ftp://engine"},
            "iid must be nonempty": {**NEW, "iid": ""},
            "hold only letters": {**NEW, "iid": "a/b"},
            "cannot reference environment variables": {**NEW, "url": "${NARWHAL_URL}"},
            "one engine entry object": [NEW],
        }
        with patch.object(engines_api, "join_engine", AsyncMock()) as join:
            for reason, payload in invalid.items():
                with self.subTest(payload=payload):
                    response = await self.client.post("/narwhal/engines", json=payload)
                    self.assertIn(reason, self.document(response, 422)["error"])
            response = await self.client.post(
                "/narwhal/engines", content=b"{", headers={"content-type": "application/json"}
            )
            self.assertIn("must be JSON", self.document(response, 422)["error"])
        join.assert_not_awaited()

    async def test_a_join_under_whole_wave_answers_409(self):
        self.cfg.engine_restart_policy = "whole_wave"
        body = self.document(await self.client.post("/narwhal/engines", json=NEW), 409)
        self.assertIn("whole_wave", body["error"])
        self.assertFalse(body["join"]["joined"])

    def test_the_openapi_document_describes_the_join_body_and_its_422(self):
        operation = create_app(self.cfg).openapi()["paths"]["/narwhal/engines"]["post"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(
            set(schema["properties"]),
            {"iid", "url", "role", "pin", "attestation_url", "shared_device"},
        )
        self.assertEqual(schema["additionalProperties"], False)
        for status in ("409", "422", "503"):
            ref = operation["responses"][status]["content"]["application/json"]["schema"]
            self.assertEqual(ref, {"$ref": "#/components/schemas/MembershipOut"})
        document = create_app(self.cfg).openapi()
        refs = re.findall(r'"\$ref": "#/components/schemas/([^"]+)"', json.dumps(operation))
        self.assertTrue(refs)
        self.assertEqual(set(refs) - set(document["components"]["schemas"]), set())

    async def test_delete_drains_then_removes_the_engine(self):
        self.router.monitor.dispatched("e1", Request("r", 64))
        body = self.document(await self.client.delete("/narwhal/engines/e1"), 202)
        self.assertEqual(body["leaving"], ["e1"])
        self.assertEqual(body["generation"], 0)
        self.router.monitor.finished("e1", "r")
        leave.complete_leaves(self.router)
        body = self.document(await self.client.get("/narwhal/engines"), 200)
        self.assertEqual(body["generation"], 1)
        self.assertEqual(body["leaving"], [])
        self.assertNotIn("e1", [row["iid"] for row in body["engines"]])

    async def test_a_repeated_delete_while_draining_answers_202(self):
        self.router.monitor.dispatched("e1", Request("r", 64))
        self.document(await self.client.delete("/narwhal/engines/e1"), 202)
        body = self.document(await self.client.delete("/narwhal/engines/e1"), 202)
        self.assertEqual(body["leaving"], ["e1"])
        saved = handoff_state.load(self.cfg.state_path)
        records = {row["iid"]: row for row in saved["lifecycle"]["records"]}
        self.assertTrue(records["e1"]["leaving"])

    async def test_a_delete_whose_identity_capture_fails_answers_503(self):
        self.router.monitor.dispatched("e1", Request("r", 64))
        failed = AsyncMock(return_value=({}, {"e1": "process identity unreadable"}))
        with patch.object(leave, "capture_process_identities", failed):
            body = self.document(await self.client.delete("/narwhal/engines/e1"), 503)
        self.assertIn("process identity unreadable", body["error"])

    async def test_a_router_fenced_during_a_leave_answers_503(self):
        self.router.monitor.dispatched("e1", Request("r", 64))

        async def fenced(cfg, engines, **kwargs):
            self.router.standby = True
            return dict.fromkeys(engines, 100.0), {}

        with patch.object(leave, "capture_process_identities", AsyncMock(side_effect=fenced)):
            body = self.document(await self.client.delete("/narwhal/engines/e1"), 503)
        self.assertEqual(body["error"], "router control was fenced during the leave")
        self.assertIn("e1", self.router.membership)

    async def test_a_standby_answers_503_before_it_checks_the_deadline(self):
        self.router.standby = True
        response = await self.client.delete("/narwhal/engines/e1", params={"deadline_s": 0})
        self.assertEqual(self.document(response, 503)["error"], engines_api.NOT_CONTROLLING)

    async def test_a_failed_handoff_write_is_logged(self):
        self.cfg.state_path.mkdir()
        with self.assertLogs("narwhal.state", level="WARNING") as logs:
            self.document(await self.client.delete("/narwhal/engines/e1"), 200)
        self.assertIn("handoff write", logs.output[0])

    async def test_delete_of_an_idle_engine_removes_it_at_once(self):
        body = self.document(
            await self.client.delete("/narwhal/engines/e1", params={"deadline_s": 30}), 200
        )
        self.assertEqual(body["generation"], 1)
        self.assertEqual(handoff_state.load(self.cfg.state_path)["engines"], ["e0", "e3"])

    async def test_delete_errors(self):
        self.document(await self.client.delete("/narwhal/engines/e9"), 404)
        body = self.document(
            await self.client.delete("/narwhal/engines/e1", params={"deadline_s": 0}), 409
        )
        self.assertIn("deadline_s must be positive", body["error"])
        self.router.scheduler.min_decode = 2
        body = self.document(await self.client.delete("/narwhal/engines/e1"), 409)
        self.assertIn("min_prefill + min_decode", body["error"])

    async def test_a_join_fenced_during_validation_answers_503(self):
        async def fenced(router, spec, *, transport=None):
            router.standby = True
            return JoinOutcome(spec.iid, False, 0, failures=["router control was fenced"])

        with patch.object(engines_api, "join_engine", AsyncMock(side_effect=fenced)):
            body = self.document(await self.client.post("/narwhal/engines", json=NEW), 503)
        self.assertEqual(body["error"], "router control was fenced")

    async def test_mutations_answer_503_unless_the_router_controls_the_fleet(self):
        self.router.standby = True
        with patch.object(engines_api, "join_engine", AsyncMock()) as join:
            body = self.document(await self.client.post("/narwhal/engines", json=NEW), 503)
            self.assertEqual(body["error"], engines_api.NOT_CONTROLLING)
            self.document(await self.client.delete("/narwhal/engines/e1"), 503)
        join.assert_not_awaited()
        self.assertEqual(self.router.membership.generation, 0)
        self.assertEqual(self.router.lifecycle.records, {})


if __name__ == "__main__":
    unittest.main()
