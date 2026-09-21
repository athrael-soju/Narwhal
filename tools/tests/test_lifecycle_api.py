"""Check lifecycle HTTP controls, identity capture and validation fencing."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.runtime.lifecycle import ValidationOutcome
from narwhal.serving import app as serving_app
from tools.tests.fixtures import fleet


class LifecycleApiTests(unittest.IsolatedAsyncioTestCase):
    """Exercise drain and readmission with failed validation and fleet-control changes."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        cfg = fleet(root)
        cfg.state_path = root / "handoff.json"
        app = serving_app.create_app(cfg)
        self.router = app.state.router
        self.addAsyncCleanup(self.router.engines.aclose)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def drain(self, *, wave=False):
        """Capture the pre-restart identity through the real HTTP drain route."""
        names = ["e0", "e3"] if wave else ["e0"]
        with patch.object(
            serving_app,
            "capture_process_identities",
            new=AsyncMock(return_value=(dict.fromkeys(names, 100), {})),
        ) as capture:
            response = await self.client.post(
                "/narwhal/lifecycle/drain", json={"wave": True} if wave else {"engines": names}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(capture.call_args.args[1], names)
        self.assertTrue(self.router.cfg.state_path.is_file())

    async def test_standby_rejects_both_mutations_before_engine_calls(self):
        """A standby returns 503 for drain and readmission and 200 for lifecycle state."""
        self.router.standby = True
        with patch.object(serving_app, "capture_process_identities", new=AsyncMock()) as capture:
            for action in ("drain", "readmit"):
                response = await self.client.post(
                    f"/narwhal/lifecycle/{action}", json={"engines": ["e0"]}
                )
                self.assertEqual(response.status_code, 503)
            capture.assert_not_awaited()
        self.assertEqual(self.router.lifecycle.records, {})
        response = await self.client.get("/narwhal/lifecycle")
        self.assertEqual(response.status_code, 200)

    async def test_invalid_engine_sets_preserve_placement(self):
        """Malformed lifecycle selections return conflicts before identity validation."""
        for action, payload in (
            ("drain", {}),
            ("drain", {"engines": ["unknown"]}),
            ("readmit", {}),
            ("readmit", {"engines": ["e0", "e3"]}),
            ("readmit", {"engines": ["e0"]}),
            ("readmit", {"wave": True}),
        ):
            with self.subTest(action=action, payload=payload):
                response = await self.client.post(f"/narwhal/lifecycle/{action}", json=payload)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.router.scheduler.draining, set())

    async def test_capture_failure_retains_drain_and_reports_engine_error(self):
        """Unavailable process identity keeps the engine held and returns its capture error."""
        with patch.object(
            serving_app,
            "capture_process_identities",
            new=AsyncMock(return_value=({}, {"e0": "metrics unavailable"})),
        ):
            response = await self.client.post("/narwhal/lifecycle/drain", json={"engines": ["e0"]})
        self.assertEqual(response.status_code, 503)
        self.assertIn("metrics unavailable", response.text)
        self.assertIn("e0", self.router.scheduler.draining)
        self.assertIsNone(self.router.lifecycle.records["e0"].old_process_start)

    async def test_fencing_during_capture_retains_captured_identity_and_hold(self):
        """Ownership loss after capture persists the hold before returning unavailable."""

        async def capture(*args, **kwargs):
            self.router.standby = True
            return {"e0": 100}, {}

        with patch.object(serving_app, "capture_process_identities", side_effect=capture):
            response = await self.client.post("/narwhal/lifecycle/drain", json={"engines": ["e0"]})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.router.lifecycle.records["e0"].old_process_start, 100)
        self.assertIn("e0", self.router.scheduler.draining)

    async def test_validated_restart_releases_single_engine(self):
        """Complete validation replaces the captured process identity and releases placement."""
        await self.drain()
        with patch.object(
            serving_app,
            "validate_readmission",
            new=AsyncMock(return_value=ValidationOutcome(starts={"e0": 101})),
        ) as validate:
            response = await self.client.post(
                "/narwhal/lifecycle/readmit", json={"engines": ["e0"]}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(validate.call_args.kwargs["wave"])
        self.assertEqual(self.router.lifecycle.process_starts["e0"], 101)
        self.assertEqual(self.router.scheduler.draining, set())

    async def test_wave_requires_complete_set_and_releases_members_together(self):
        """An active wave rejects individual and partial readmission before atomic release."""
        await self.drain(wave=True)
        for payload in ({"engines": ["e0"]}, {"engines": ["e0"], "wave": True}):
            response = await self.client.post("/narwhal/lifecycle/readmit", json=payload)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.router.scheduler.draining, {"e0", "e3"})
        with patch.object(
            serving_app,
            "validate_readmission",
            new=AsyncMock(return_value=ValidationOutcome(starts={"e0": 101, "e3": 102})),
        ) as validate:
            response = await self.client.post("/narwhal/lifecycle/readmit", json={"wave": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(validate.call_args.kwargs["wave"])
        self.assertEqual(self.router.scheduler.draining, set())
        self.assertEqual(self.router.lifecycle.wave_id, "")

    async def test_failed_or_incomplete_validation_retains_hold(self):
        """Validation failures and missing process identities keep the candidate blocked."""
        await self.drain()
        for outcome in (
            ValidationOutcome(failures={"e0": ["fabric probe failed"]}),
            ValidationOutcome(),
        ):
            with patch.object(
                serving_app, "validate_readmission", new=AsyncMock(return_value=outcome)
            ):
                response = await self.client.post(
                    "/narwhal/lifecycle/readmit", json={"engines": ["e0"]}
                )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.router.lifecycle.records["e0"].state, "blocked")
            self.assertIn("e0", self.router.scheduler.draining)

    async def test_validator_exception_becomes_persisted_failure(self):
        """A validator exception becomes a blocked record carrying the exception type."""
        await self.drain()
        with (
            patch.object(
                serving_app, "validate_readmission", new=AsyncMock(side_effect=ValueError("probe"))
            ),
            self.assertLogs("narwhal", level="ERROR"),
        ):
            response = await self.client.post(
                "/narwhal/lifecycle/readmit", json={"engines": ["e0"]}
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.router.lifecycle.records["e0"].state, "blocked")
        self.assertIn("ValueError: probe", self.router.lifecycle.records["e0"].error)

    async def test_fencing_during_validation_overrides_passing_evidence(self):
        """Ownership loss keeps a successfully probed engine held for the active router."""
        await self.drain()

        async def validate(*args, **kwargs):
            self.router.standby = True
            return ValidationOutcome(starts={"e0": 101})

        with patch.object(serving_app, "validate_readmission", side_effect=validate):
            response = await self.client.post(
                "/narwhal/lifecycle/readmit", json={"engines": ["e0"]}
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.router.lifecycle.records["e0"].state, "blocked")
        self.assertIn("e0", self.router.scheduler.draining)
