"""Regression tests for Prometheus target generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from narwhal.backends import load as load_backend
from narwhal.config import SLO, EngineSpec, FleetConfig
from narwhal.observability.stack import make_targets
from narwhal.observability.targets import scrape_targets


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
            contract = make_targets.write_targets(["http://localhost:8000"], None, output)

            self.assertEqual(contract.routers, ("localhost:8000",))
            self.assertEqual(contract.engine_targets_url, "http://localhost:8000/narwhal/targets")
            self.assertEqual(
                json.loads((output / "router.json").read_text()),
                [{"targets": ["localhost:8000"]}],
            )
            self.assertEqual((output / "router.json").stat().st_mode & 0o777, 0o644)
            self.assertFalse((output / "engines.json").exists())

    def test_writes_every_router_of_a_pair_and_reads_engines_through_the_load_balancer(self):
        for urls in (
            ["http://router-a:8000", "http://router-b:8000"],
            ["http://router-a:8000,http://router-b:8000"],
            [" http://router-a:8000 , http://router-b:8000 ,"],
        ):
            with self.subTest(urls=urls), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "targets"
                contract = make_targets.write_targets(urls, "http://balancer:8000", output)
                self.assertEqual(contract.routers, ("router-a:8000", "router-b:8000"))
                self.assertEqual(
                    contract.engine_targets_url, "http://balancer:8000/narwhal/targets"
                )
                self.assertEqual(
                    json.loads((output / "router.json").read_text()),
                    [{"targets": ["router-a:8000", "router-b:8000"]}],
                )

    def test_a_pair_requires_the_origin_that_reaches_its_active_router(self) -> None:
        with self.assertRaisesRegex(ValueError, "router pair requires --targets-url"):
            make_targets.load_contract(["http://router-a:8000", "http://router-b:8000"])

    def test_rejects_a_missing_or_repeated_router(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one router origin"):
            make_targets.load_contract([" , "])
        with self.assertRaisesRegex(ValueError, "repeat a router"):
            make_targets.load_contract(
                ["http://router-a:8000", "http://router-a:8000/"], "http://balancer:8000"
            )

    def test_the_targets_origin_is_not_one_of_the_routers(self) -> None:
        for routers in (["http://router-a:8000", "http://router-b:8000"], ["http://router-a:8000"]):
            with (
                self.subTest(routers=routers),
                self.assertRaisesRegex(ValueError, "NARWHAL_TARGETS_URL names router router-a"),
            ):
                make_targets.load_contract(routers, "http://router-a:8000/")

    def test_one_router_may_serve_its_engine_targets_from_another_origin(self) -> None:
        contract = make_targets.load_contract(["http://router-a:8000"], "http://balancer:8000")
        self.assertEqual(contract.targets, "balancer:8000")

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
            scrape_targets(cfg, cfg.engines),
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

    def test_the_scrape_path_is_the_backends_metrics_route(self) -> None:
        class Elsewhere(type(load_backend("vllm").metrics)):  # type: ignore[misc]
            path = "/prometheus/metrics"

        backend = replace(load_backend("vllm"), metrics=Elsewhere())
        with patch("narwhal.observability.targets.load_backend", return_value=backend):
            cfg = fleet(EngineSpec("e0", "http://127.0.0.2:8002/n1"))
            [group] = scrape_targets(cfg, cfg.engines)
        self.assertEqual(group["labels"]["__metrics_path__"], "/n1/prometheus/metrics")

    def test_targets_keep_the_engine_scheme_and_path_without_credentials(self) -> None:
        cfg = fleet(EngineSpec("e0", "https://user:secret@engine.internal:8443/n1"))
        [group] = scrape_targets(cfg, cfg.engines)
        self.assertEqual(group["targets"], ["engine.internal:8443"])
        self.assertEqual(group["labels"]["__scheme__"], "https")
        self.assertEqual(group["labels"]["__metrics_path__"], "/n1/metrics")
        self.assertNotIn("secret", json.dumps(group))
