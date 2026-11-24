"""Join and leave engines through the control service, and restore the baseline membership."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import httpx

from tests.fleet_control.test_engine_actions import FLEET, NOW, EngineCase, FakeRouter, hook

NEW = {
    "iid": "e9",
    "url": "http://engine-9.invalid:8000",
    "role": "decode",
    "attestation_url": "http://engine-9.invalid:8000/v1/attestation",
}


class MemberRouter(FakeRouter):
    """Add the membership routes to the fake router.

    A join of an engine named `refused`, or of one in `refuse_join`, answers 409, and a join of
    an engine named `invalid` answers 422 with a validation `detail`. With `slow_leave` set, a
    leave answers 202 and the engine leaves on the second membership read after it. With
    `stuck_leave` set, a leave answers 202 and the drain passes its deadline. `leave_status`
    replaces a leave's answer, and `membership_status` the membership read's.
    """

    def __init__(self, stopped: Path) -> None:
        super().__init__(stopped)
        self.members = {row["iid"]: dict(row) for row in json.loads(FLEET.read_text())["engines"]}
        self.generation = 0
        self.slow_leave = False
        self._leaving: dict[str, int] = {}
        self.on_change: Any = None
        self.stuck_leave = False
        self.refuse_join: set[str] = set()
        self.leave_status: int | None = None
        self.membership_status: int | None = None
        # While set, the router refuses connections until the router_restart hook touches it.
        self.down_until: Path | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.down_until is not None and not self.down_until.exists():
            raise httpx.ConnectError("connection refused", request=request)
        if path == "/ready":
            return httpx.Response(200, json={"status": "ready", "reason": ""})
        if not path.startswith("/narwhal/engines"):
            return super().handle(request)
        body: Any = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        if self.on_change is not None and request.method != "GET":
            self.on_change()
        if request.method == "GET":
            if self.membership_status is not None:
                return httpx.Response(self.membership_status, json={"detail": "router error"})
            for iid in list(self._leaving):
                self._leaving[iid] -= 1
                if self._leaving[iid] <= 0:
                    del self._leaving[iid]
                    self._remove(iid)
            return httpx.Response(200, json=self.document())
        if request.method == "POST":
            if "${" in json.dumps(body):
                return httpx.Response(422, json=self.document("cannot reference variables"))
            if body["iid"] == "invalid":
                return httpx.Response(422, json={"detail": [{"msg": "Field required"}]})
            if body["iid"] == "refused" or body["iid"] in self.refuse_join:
                return httpx.Response(409, json=self.document("attestation differs"))
            self.members[body["iid"]] = body
            self.roles[body["iid"]] = body["role"]
            self.records[body["iid"]] = "active"
            self.generation += 1
            return httpx.Response(200, json=self.document())
        iid = path.rsplit("/", 1)[1]
        if iid not in self.members:
            return httpx.Response(404, json=self.document(f"unknown engine: {iid}"))
        if self.leave_status is not None:
            return httpx.Response(self.leave_status, json=self.document("router control fenced"))
        if self.stuck_leave:
            self.records[iid] = "deadline_exceeded"
            return httpx.Response(202, json=self.document())
        if self.slow_leave:
            self.records[iid] = "drained"
            self._leaving[iid] = 2
            return httpx.Response(202, json=self.document())
        self._remove(iid)
        return httpx.Response(200, json=self.document())

    def _remove(self, iid: str) -> None:
        del self.members[iid]
        self.roles.pop(iid, None)
        self.records.pop(iid, None)
        self.generation += 1

    def document(self, error: str = "") -> dict[str, Any]:
        return {
            "schema": "narwhal.membership",
            "schema_version": 1,
            "generation": self.generation,
            "engines": list(self.members.values()),
            "leaving": sorted(self._leaving),
            "error": error,
            "join": None,
        }


class MembershipActionTests(EngineCase):
    router_class: ClassVar[type[FakeRouter]] = MemberRouter
    router: MemberRouter

    async def changes(self) -> dict[str, Any]:
        assert self.service.session is not None
        return await self.service.changes(self.service.session)

    async def test_join_is_an_exclusive_recorded_action(self) -> None:
        await self.start_session()
        seen = []
        self.router.on_change = lambda: seen.append(self.service.status()["in_progress"])
        response = await self.client.post("/api/engines/join", json=NEW)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(seen, ["engine.join"])
        entry = self.last_action()
        self.assertEqual(entry["action"], "engine.join")
        self.assertEqual(entry["params"], {"engine": "e9", "spec": NEW})
        self.assertEqual(entry["result"]["generation"], {"before": 0, "after": 1})
        self.assertEqual(entry["result"]["router"]["status"], 200)
        self.assertTrue(entry["result"]["after"]["known"])
        self.assertEqual((await self.changes())["joined"], ["e9"])
        listed = (await self.client.get("/api/engines")).json()
        self.assertEqual(listed["generation"], 1)
        self.assertIn("e9", listed["engines"])

    async def test_a_refused_join_relays_the_router_status_and_reason(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/engines/join", json={**NEW, "iid": "refused"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"],
            "router refused join of refused with HTTP 409: attestation differs",
        )
        self.assertEqual(self.last_action()["outcome"], "failed")
        self.assertNotIn("joined", await self.changes())
        for body in ([], {"url": "http://x"}):
            response = await self.client.post("/api/engines/join", json=body)
            self.assertEqual(response.status_code, 422)

    async def test_leave_waits_for_removal_and_records_the_engine_entry(self) -> None:
        await self.start_session()
        self.router.slow_leave = True
        response = await self.client.post("/api/engines/e1/leave", json={"deadline_s": 30})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["router"]["status"], 202)
        self.assertTrue(result["removal"]["removed"])
        self.assertEqual(result["spec"]["url"], "http://engine-1.invalid:8000")
        deletes = [(m, p) for m, p, _ in self.router.requests if m == "DELETE"]
        self.assertEqual(deletes, [("DELETE", "/narwhal/engines/e1")])
        self.assertEqual((await self.changes())["left"], {"e1": result["spec"]})

    async def test_engine_actions_follow_the_router_membership(self) -> None:
        await self.start_session()
        await self.client.post("/api/engines/join", json=NEW)
        response = await self.client.post("/api/engines/e9/drain")
        self.assertEqual(response.status_code, 200, response.text)
        await self.client.post("/api/engines/e9/readmit")
        await self.client.post("/api/engines/e1/leave")
        response = await self.client.post("/api/engines/e1/drain")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "engine 'e1' is not in the router's membership")
        response = await self.client.post("/api/engines/e1/leave")
        self.assertEqual(response.status_code, 404)

    async def test_restore_returns_engines_that_left_and_removes_engines_that_joined(self) -> None:
        await self.start_session()
        await self.client.post("/api/engines/join", json=NEW)
        await self.client.post("/api/engines/e1/drain")
        await self.client.post("/api/engines/e1/leave")
        self.assertEqual(
            await self.changes(),
            {
                "configuration": False,
                "engines": {},
                "joined": ["e9"],
                "left": {"e1": self.last_action()["result"]["spec"]},
            },
        )
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        steps = response.json()["result"]["steps"]
        self.assertEqual(
            [(step["engine"], step["action"]) for step in steps],
            [("e1", "start"), ("e1", "join"), ("e9", "leave")],
        )
        self.assertTrue(all(step["seq"] is not None for step in steps))
        self.assertEqual(sorted(self.router.members), ["e0", "e1", "e2", "e3", "e4", "e5"])
        self.assertEqual(self.router.members["e1"]["url"], "http://engine-1.invalid:8000")
        self.assertEqual(await self.changes(), {"configuration": False, "engines": {}})
        self.assertIn(
            "start e1 http://engine-1.invalid:8000",
            self.record()["actions"][-4]["result"]["hook"]["tail"],
        )

    async def test_restore_skips_membership_steps_that_are_already_undone(self) -> None:
        await self.start_session()
        await self.client.post("/api/engines/join", json=NEW)
        await self.client.post("/api/engines/e1/leave")
        # The router lost the joined engine and regained e1 outside the session.
        self.router._remove("e9")
        self.router.members["e1"] = {"iid": "e1", "url": "http://engine-1.invalid:8000"}
        self.assertEqual(await self.changes(), {"configuration": False, "engines": {}})
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"]["steps"], [])

    async def test_a_leave_past_its_drain_deadline_stops_waiting_and_restore_returns_it(
        self,
    ) -> None:
        await self.start_session()
        self.router.stuck_leave = True
        response = await self.client.post("/api/engines/e1/leave", json={"deadline_s": 30})
        self.assertEqual(response.status_code, 504, response.text)
        self.assertIn("resident work after its drain deadline", response.json()["detail"])
        # The router removes the engine once its resident work ends.
        self.router._remove("e1")
        self.assertEqual(list((await self.changes())["left"]), ["e1"])
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("e1", self.router.members)

    async def test_changes_follow_the_router_membership_across_a_cold_restart(self) -> None:
        await self.start_session()
        await self.client.post("/api/engines/join", json=NEW)
        await self.client.post("/api/engines/e1/leave")

        async def restarted(session: Any) -> dict[str, Any]:
            return {}

        await self.service.act("config.cold_restart", {}, restarted, exclusive=True)
        # A router that resumes keeps the membership it had.
        changes = await self.changes()
        self.assertEqual(changes["joined"], ["e9"])
        self.assertEqual(list(changes["left"]), ["e1"])

    async def test_restore_leaves_engine_changes_of_non_members_to_the_membership_steps(
        self,
    ) -> None:
        await self.start_session()
        await self.client.post("/api/engines/join", json=NEW)
        await self.client.post("/api/engines/e9/pause")
        await self.client.post("/api/engines/e1/pause")
        await self.client.post("/api/engines/e1/leave")
        # The router lost the joined engine outside the session.
        self.router._remove("e9")
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        steps = [(step["engine"], step["action"]) for step in response.json()["result"]["steps"]]
        self.assertEqual(steps, [("e1", "start"), ("e1", "join")])

    async def test_an_unreadable_membership_fails_the_action_without_the_baseline(self) -> None:
        await self.start_session()
        self.router.membership_status = 500
        response = await self.client.post("/api/engines/e1/stop")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("answered no membership document", response.json()["detail"])
        self.assertFalse((self.stopped / "e1").exists())

    async def test_router_refusals_relay_their_status_and_reason(self) -> None:
        await self.start_session()
        response = await self.client.post("/api/engines/join", json={**NEW, "iid": "invalid"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Field required", response.json()["detail"])
        self.router.leave_status = 503
        response = await self.client.post("/api/engines/e1/leave")
        self.assertEqual(response.status_code, 503)
        self.assertIn("router control fenced", response.json()["detail"])

    async def test_leave_is_exclusive(self) -> None:
        await self.start_session()
        seen = []
        self.router.on_change = lambda: seen.append(self.service.status()["in_progress"])
        response = await self.client.post("/api/engines/e1/leave")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(seen, ["engine.leave"])

    async def test_a_failed_restore_records_its_completed_steps_and_can_be_retried(self) -> None:
        await self.start_session()
        await self.client.post("/api/engines/e1/leave")
        self.router.refuse_join = {"e1"}
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 409, response.text)
        restore = next(
            entry for entry in self.record()["actions"] if entry["action"] == "config.restore"
        )
        steps = [(step["engine"], step["action"]) for step in restore["result"]["steps"]]
        self.assertEqual(steps, [("e1", "start")])
        self.router.refuse_join = set()
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("e1", self.router.members)

    async def test_restore_waits_for_a_started_member_to_return_to_service(self) -> None:
        await self.start_session()
        await self.client.post("/api/engines/e1/stop")
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        start = next(
            entry for entry in self.record()["actions"] if entry["action"] == "engine.start"
        )
        self.assertTrue(start["result"]["service"]["in_service"])

    def reference_baseline(self, iid: str, name: str) -> None:
        assert self.service.session is not None
        for engine in self.service.session.baseline["engines"]:
            if engine["iid"] == iid:
                engine["url"] = "${" + name + "}"

    async def test_restore_returns_a_rejoined_engine_with_its_baseline_entry(self) -> None:
        await self.start_session()
        await self.client.post("/api/engines/e1/leave")
        moved = {**self.router.document()["engines"][0], "iid": "e1"}
        moved["url"] = "http://elsewhere.invalid:9999"
        await self.client.post("/api/engines/join", json=moved)
        await self.client.post("/api/engines/e1/leave")
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.router.members["e1"]["url"], "http://engine-1.invalid:8000")

    async def test_restore_resolves_a_baseline_reference_from_its_environment(self) -> None:
        await self.start_session()
        self.reference_baseline("e1", "NARWHAL_TEST_E1_URL")
        # The engine left before the session.
        self.router._remove("e1")
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("does not resolve", response.json()["detail"])
        nested = {**self.service._env, "NARWHAL_TEST_E1_URL": "${NARWHAL_OTHER}"}
        self.service._env = nested
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("does not resolve", response.json()["detail"])
        self.service._env = {**self.service._env, "NARWHAL_TEST_E1_URL": "http://engine-1.test:9"}
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.router.members["e1"]["url"], "http://engine-1.test:9")
        start = next(
            entry for entry in self.record()["actions"] if entry["action"] == "engine.start"
        )
        self.assertIn("http://engine-1.test:9", start["result"]["hook"]["tail"])


class RouterRestartRestoreTests(EngineCase):
    router_class: ClassVar[type[FakeRouter]] = MemberRouter
    router: MemberRouter
    hooks: ClassVar[dict[str, Any]] = {
        **EngineCase.hooks,
        "router_restart": hook(
            "router_restart",
            "import os, pathlib; pathlib.Path(os.environ['STOPPED'], 'router-up').touch()",
        ),
    }

    async def test_restore_restarts_a_router_that_does_not_answer(self) -> None:
        await self.start_session()
        session = self.service.session
        assert session is not None
        session.apply_configuration(
            NOW.isoformat(), "overlay", {**session.configurations[0]["document"], "_x": 1}
        )
        self.router.down_until = self.stopped / "router-up"
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(result["hook"]["exit_code"], 0)
        self.assertTrue(result["changes"]["configuration"])

    async def test_a_restore_whose_router_restart_fails_keeps_its_changes(self) -> None:
        await self.start_session()
        session = self.service.session
        assert session is not None
        session.apply_configuration(
            NOW.isoformat(), "overlay", {**session.configurations[0]["document"], "_x": 1}
        )
        self.service.config.hooks["router_restart"] = hook("router_restart", "raise SystemExit(3)")
        response = await self.client.post("/api/config/restore")
        self.assertEqual(response.status_code, 502, response.text)
        self.assertTrue(response.json()["action"]["result"]["changes"]["configuration"])
