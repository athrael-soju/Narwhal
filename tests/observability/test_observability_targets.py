"""Regression tests for Prometheus target generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.observability.targets import scrape_targets
from tools.observability import make_targets


def fleet(*engines: EngineSpec, backend: str = "vllm") -> FleetConfig:
    return FleetConfig(
        model="stub",
        slo=SLO(ttft_s=1.0, tpot_s=0.02),
        engines=list(engines),
        backend=backend,
    )


class TargetGenerationTests(unittest.TestCase):
    def test_writes_router_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "targets"
            contract = make_targets.write_targets("http://localhost:8000", output)

            self.assertEqual(contract.router, "localhost:8000")
            self.assertEqual(contract.engine_targets_url, "http://localhost:8000/narwhal/targets")
            self.assertEqual(
                json.loads((output / "router.json").read_text()),
                [{"targets": ["localhost:8000"]}],
            )
            self.assertEqual((output / "router.json").stat().st_mode & 0o777, 0o644)
            self.assertFalse((output / "engines.json").exists())

    def test_rejects_router_urls_without_an_explicit_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit host and port"):
            make_targets.metrics_authority("http://router.internal")

    def test_rejects_https_until_prometheus_tls_is_configured(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires http"):
            make_targets.metrics_authority("https://router.internal:8443")


class EngineTargetTests(unittest.TestCase):
    def test_each_engine_is_one_target_group_with_its_iid_and_backend(self) -> None:
        cfg = fleet(
            EngineSpec("e0", "http://[fd00::10]:8002"),
            EngineSpec("e1", "http://127.0.0.2:8002/"),
            backend="sglang",
        )
        self.assertEqual(
            scrape_targets(cfg),
            [
                {
                    "targets": ["[fd00::10]:8002"],
                    "labels": {
                        "iid": "e0",
                        "backend": "sglang",
                        "__scheme__": "http",
                        "__metrics_path__": "/metrics",
                    },
                },
                {
                    "targets": ["127.0.0.2:8002"],
                    "labels": {
                        "iid": "e1",
                        "backend": "sglang",
                        "__scheme__": "http",
                        "__metrics_path__": "/metrics",
                    },
                },
            ],
        )

    def test_targets_keep_the_engine_scheme_and_path_without_credentials(self) -> None:
        cfg = fleet(EngineSpec("e0", "https://user:secret@engine.internal:8443/n1"))
        [group] = scrape_targets(cfg)
        self.assertEqual(group["targets"], ["engine.internal:8443"])
        self.assertEqual(group["labels"]["__scheme__"], "https")
        self.assertEqual(group["labels"]["__metrics_path__"], "/n1/metrics")
        self.assertNotIn("secret", json.dumps(group))
