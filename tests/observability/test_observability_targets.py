"""Regression tests for Prometheus target generation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.config import FleetConfig
from tests.fixtures import ROOT
from tools.observability import make_targets


class TargetGenerationTests(unittest.TestCase):
    def test_environment_targets_match_the_loaded_fleet(self) -> None:
        raw = json.loads((ROOT / "tests/data/fleet.json").read_text())
        env = {}
        for index, engine in enumerate(raw["engines"]):
            name = f"TEST_NODE_{index}_URL"
            engine["url"] = "${" + name + "}"
            env[name] = f"http://[fd00::{index + 1}]:8002"
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, env):
            fleet = Path(temporary) / "fleet.json"
            fleet.write_text(json.dumps(raw))
            cfg = FleetConfig.load(fleet)
            contract = make_targets.load_contract(fleet, "http://localhost:8000")
        self.assertEqual(
            contract.engines,
            tuple(
                (engine.iid, make_targets.metrics_authority(engine.url)) for engine in cfg.engines
            ),
        )

    def test_missing_endpoint_variable_does_not_write_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            fleet = root / "fleet.json"
            fleet.write_text(json.dumps({"engines": [{"iid": "e0", "url": "${TEST_URL}"}]}))
            with self.assertRaisesRegex(ValueError, "TEST_URL is unset or empty"):
                make_targets.write_targets(fleet, "http://localhost:8000", root / "targets")
            self.assertFalse((root / "targets").exists())

    def test_writes_router_and_engine_discovery_from_one_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fleet = root / "fleet.json"
            fleet.write_text(
                json.dumps(
                    {
                        "engines": [
                            {"iid": "e0", "url": "http://[fd00::10]:8002"},
                            {"iid": "e1", "url": "http://127.0.0.2:8002"},
                        ]
                    }
                )
            )
            output = root / "targets"
            contract = make_targets.write_targets(
                fleet,
                "http://localhost:8000",
                output,
            )

            self.assertEqual(contract.router, "localhost:8000")
            self.assertEqual(
                contract.engines,
                (("e0", "[fd00::10]:8002"), ("e1", "127.0.0.2:8002")),
            )
            self.assertEqual(
                json.loads((output / "router.json").read_text()),
                [{"targets": ["localhost:8000"]}],
            )
            self.assertEqual(
                json.loads((output / "engines.json").read_text()),
                [
                    {"targets": ["[fd00::10]:8002"], "labels": {"iid": "e0"}},
                    {"targets": ["127.0.0.2:8002"], "labels": {"iid": "e1"}},
                ],
            )
            self.assertEqual((output / "router.json").stat().st_mode & 0o777, 0o644)
            self.assertEqual((output / "engines.json").stat().st_mode & 0o777, 0o644)

    def test_rejects_router_urls_without_an_explicit_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit host and port"):
            make_targets.metrics_authority("http://router.internal")

    def test_rejects_https_until_prometheus_tls_is_configured(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires http"):
            make_targets.metrics_authority("https://router.internal:8443")

    def test_rejects_engine_urls_with_a_metrics_path(self) -> None:
        fleet = {"engines": [{"iid": "e0", "url": "http://engine:8002/metrics"}]}
        with self.assertRaisesRegex(ValueError, "HTTP origin"):
            make_targets.build_targets(fleet, "http://router:8000")

    def test_rejects_duplicate_engine_identities(self) -> None:
        fleet = {
            "engines": [
                {"iid": "e0", "url": "http://engine-a:8002"},
                {"iid": "e0", "url": "http://engine-b:8002"},
            ]
        }
        with self.assertRaisesRegex(ValueError, "appears more than once"):
            make_targets.build_targets(fleet, "http://router:8000")
