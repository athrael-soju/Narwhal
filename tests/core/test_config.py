"""Check fleet parsing, cross-field limits and credential references."""

import copy
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal.config import FleetConfig
from narwhal.config.serialization import document
from narwhal.diagnostics import check
from narwhal.serving.policy import ServingPolicy
from tests.fixtures import ROOT


class ConfigTests(unittest.TestCase):
    """Each invalid field is checked against a valid fleet configuration."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "fleet.json"
        self.raw = json.loads((ROOT / "tests/data/fleet.json").read_text())

    def load(self, raw):
        """Write one candidate config through the public loader."""
        self.path.write_text(json.dumps(raw))
        return FleetConfig.load(self.path)

    def changed(self, path, value):
        """Return a copied fleet document with one nested value replaced."""
        raw = copy.deepcopy(self.raw)
        target = raw
        for key in path[:-1]:
            target = target[key] if isinstance(target, list) else target.setdefault(key, {})
        target[path[-1]] = value
        return raw

    def test_save_load_preserves_effective_configuration(self):
        """Serialization preserves policies, optional fields and credential references."""
        for source in ("tests/data/fleet.json", "config/fleet.example.json"):
            with self.subTest(source=source):
                cfg = FleetConfig.load(ROOT / source)
                cfg.serving = ServingPolicy(max_attempts=2, handoff_timeout_s=5)
                cfg.save(self.path)
                restored = FleetConfig.load(self.path)
                self.assertEqual(restored, cfg)
                self.assertEqual(json.loads(self.path.read_text()), document(cfg))
                self.assertGreater(restored.control_connections, 0)

    def test_packaged_example_matches_the_canonical_document(self):
        """The package resource retains the canonical bytes across archive builds."""
        packaged = ROOT / "src/narwhal/fleet.example.json"
        canonical = ROOT / "config/fleet.example.json"
        if (ROOT / ".git").exists() and not (ROOT / "PKG-INFO").exists():
            self.assertTrue(packaged.is_symlink())
            self.assertEqual(packaged.resolve(), canonical.resolve())
        self.assertEqual(packaged.read_bytes(), canonical.read_bytes())

    def test_endpoint_environment_references_resolve_before_serialization(self):
        """Serving and saved fleet documents receive resolved URLs, including IPv6."""
        raw = self.changed(("engines", 0, "url"), "${TEST_ENGINE_URL}")
        raw["engines"][0]["attestation_url"] = "${TEST_ATTESTATION_URL}"
        with patch.dict(
            os.environ,
            {
                "TEST_ENGINE_URL": "http://[::1]:8002/",
                "TEST_ATTESTATION_URL": "http://[::1]:8010/v1/attestation",
            },
            clear=True,
        ):
            cfg = self.load(raw)
        self.assertEqual(cfg.engines[0].url, "http://[::1]:8002")
        self.assertEqual(cfg.engines[0].attestation_url, "http://[::1]:8010/v1/attestation")
        self.assertEqual(cfg.engines[1].url, self.raw["engines"][1]["url"])
        cfg.save(self.path)
        self.assertNotIn("${", self.path.read_text())
        self.assertEqual(FleetConfig.load(self.path), cfg)

    def test_endpoint_environment_errors_are_aggregated(self):
        """Missing and blank endpoint variables identify every affected field."""
        raw = self.changed(("engines", 0, "url"), "${TEST_ENGINE_URL}")
        raw["engines"][0]["attestation_url"] = "${TEST_ATTESTATION_URL}"
        for env in ({}, {"TEST_ENGINE_URL": "", "TEST_ATTESTATION_URL": "  "}):
            with (
                self.subTest(env=env),
                patch.dict(os.environ, env, clear=True),
                self.assertRaises(ValueError) as caught,
            ):
                self.load(raw)
            message = str(caught.exception)
            for expected in (
                "engines[0].url",
                "engines[0].attestation_url",
                "TEST_ENGINE_URL",
                "TEST_ATTESTATION_URL",
                "unset or empty",
            ):
                self.assertIn(expected, message)

    def test_endpoint_references_reject_shell_expressions_and_nested_references(self):
        """Only a whole-value variable name is expanded, without shell evaluation."""
        for value in ("http://${TEST_HOST}:8000", "${TEST_URL:-http://fallback:8000}", "${}"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "whole-value"):
                self.load(self.changed(("engines", 0, "url"), value))
        with (
            patch.dict(os.environ, {"TEST_URL": "${OTHER_URL}"}),
            self.assertRaisesRegex(ValueError, "contains another reference"),
        ):
            self.load(self.changed(("engines", 0, "url"), "${TEST_URL}"))

    def test_environment_expansion_is_limited_to_endpoint_fields(self):
        """Model identifiers and credential variable names retain their literal values."""
        raw = self.changed(("model",), "${TEST_MODEL}")
        raw["engine"]["engine_api_key_env"] = "TEST_ENGINE_KEY"
        with patch.dict(os.environ, {"TEST_MODEL": "expanded", "TEST_ENGINE_KEY": "secret"}):
            cfg = self.load(raw)
        self.assertEqual(cfg.model, "${TEST_MODEL}")
        self.assertEqual(cfg.engine_api_key_env, "TEST_ENGINE_KEY")

    def test_missing_required_and_unknown_fields_are_aggregated(self):
        """The parser reports independent top-level mistakes together."""
        raw = {**self.raw, "surprise": 1}
        del raw["model"]
        del raw["slo"]
        with self.assertRaises(ValueError) as caught:
            self.load(raw)
        for field in ("surprise", "model", "slo"):
            self.assertIn(field, str(caught.exception))

    def test_exact_json_types_and_finite_numbers(self):
        """Boolean coercion and nonfinite values are rejected at the input boundary."""
        for path, value in (
            (("model",), 1),
            (("controller", "advisory"), 1),
            (("serving", "max_connections"), True),
            (("controller", "monitor_interval_s"), "1"),
            (("controller", "monitor_interval_s"), float("nan")),
            (("controller", "monitor_interval_s"), float("inf")),
            (("engine", "decode_read_timeout_s"), -0.5),
            (("engines",), {}),
            (("slo",), []),
            (("controller", "thresholds"), []),
            (("hardware",), []),
            (("engine_contract",), []),
            (("profiles",), []),
            (("profiles", "path"), 1),
            (("recovery", "state_path"), 1),
            (("engines", 0, "iid"), 1),
            (("engines", 0, "url"), 1),
            (("engines", 0, "role"), 1),
            (("engines", 0, "attestation_url"), 1),
            (("engine_contract", "vllm_version"), 1),
        ):
            with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                self.load(self.changed(path, value))

    def test_nested_unknown_fields_and_invalid_engine_rows(self):
        """Nested sections and engine rows keep strict field contracts."""
        for path in (
            ("controller",),
            ("controller", "thresholds"),
            ("controller", "reactive"),
            ("serving",),
            ("engine",),
            ("recovery",),
            ("recovery", "health"),
            ("profiles",),
            ("hardware",),
            ("engine_contract",),
        ):
            raw = copy.deepcopy(self.raw)
            target = raw
            for key in path:
                target = target.setdefault(key, {})
            target["surprise"] = 1
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "surprise"):
                self.load(raw)
        for engine in (None, {}, {"iid": "x", "url": "http://x", "pin": 1}):
            with self.subTest(engine=engine), self.assertRaises(ValueError):
                self.load({**self.raw, "engines": [engine]})

    def test_positive_limits_reject_zero(self):
        """Each time budget and required count rejects its zero boundary."""
        cfg = self.load(self.raw)
        fields = (
            "monitor_interval_s",
            "tokenize_timeout_s",
            "pool_timeout_s",
            "connect_timeout_s",
            "health_timeout_s",
            "request_timeout_s",
            "prefill_timeout_s",
            "chars_per_token",
            "first_token_timeout_s",
            "monitor_failure_limit",
            "eject_after",
            "readmit_every",
            "liveness_misses",
            "max_connections",
            "flip_history",
            "reactive_window_s",
            "reactive_utilization",
            "reactive_demand_floor",
            "reactive_step_s",
            "reactive_confirmations",
            "reactive_min_arrivals",
            "reactive_evidence_min_arrivals",
            "health_min_samples",
            "health_probation_windows",
            "health_evict_windows",
            "health_recovery_windows",
        )
        for field in fields:
            label = field.replace("reactive_", "reactive.")
            if field != "health_timeout_s":
                label = label.replace("health_", "health.")
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, re.escape(label)):
                replace(cfg, **{field: 0}).validate()

    def test_decode_read_timeout_zero_disables(self):
        """decode_read_timeout_s accepts zero as its disabled bound."""
        self.load(self.changed(("engine", "decode_read_timeout_s"), 0))
        cfg = self.load(self.raw)
        replace(cfg, decode_read_timeout_s=0.0).validate()

    def test_cross_field_constraints_name_the_conflict(self):
        """Derived windows, role floors and health cadence have explicit constraints."""
        cfg = self.load(self.raw)
        for changes, message in (
            ({"reactive_evidence_span_s": 121}, "evidence_span_s"),
            ({"reactive_evidence_max_span_s": 121}, "evidence_max_span_s"),
            ({"reactive_utilization": 1.1}, "utilization"),
            ({"reactive_decode_correction_alpha": 0}, "decode_correction_alpha"),
            (
                {"reactive_decode_correction_min": 2, "reactive_decode_correction_max": 1},
                "decode_correction_max",
            ),
            ({"health_min_samples": 1000}, "nominal"),
            ({"health_evict_windows": 1, "health_probation_windows": 2}, "evict_windows"),
            ({"min_prefill": 6}, "permits zero decode"),
            ({"min_prefill": 5, "min_decode": 2}, "exceeds the fleet size"),
            ({"engine_restart_policy": "whole_wave", "liveness_every": 0}, "liveness_every"),
            ({"engine_restart_policy": "whole_wave", "engine_contract": None}, "engine_contract"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                replace(cfg, **changes).validate()

    def test_engine_credentials_resolve_at_use_and_select_auth_posture(self):
        """Fleet JSON stores the credential's environment variable name and omits its value."""
        cfg = self.load(self.raw)
        cfg.engine_api_key_env = "NARWHAL_TEST_ENGINE_KEY"
        with patch.dict("os.environ", {"NARWHAL_TEST_ENGINE_KEY": "synthetic-key"}):
            self.assertEqual(cfg.engine_auth_mode(), "engine-credential")
            cfg.save(self.path)
            self.assertNotIn("synthetic-key", self.path.read_text())
        cfg.engine_api_key_env = ""
        self.assertEqual(cfg.engine_auth_mode(), "boundary")

    def test_optional_limits_reject_negative_values(self):
        """Disabled limits accept zero while negative values identify the invalid setting."""
        cfg = self.load(self.raw)
        for name in (
            "control_connections",
            "admission_margin",
            "failure_quarantine_s",
            "graceful_timeout_s",
            "reactive_demand_rise_tolerance",
            "health_probation_penalty_s",
            "health_relative_band",
        ):
            label = name.replace("reactive_", "reactive.").replace("health_", "health.")
            with self.subTest(name=name):
                replace(cfg, **{name: 0}).validate()
                with self.assertRaisesRegex(ValueError, re.escape(label)):
                    replace(cfg, **{name: -1}).validate()
        for name in ("shrink", "cooldown_s", "dwell_s", "flip_resident_guard"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                replace(cfg, thresholds=replace(cfg.thresholds, **{name: -1})).validate()

    def test_policy_boundaries_preserve_hysteresis_and_evidence(self):
        """Invalid thresholds and correction settings fail before controller construction."""
        cfg = self.load(self.raw)
        for changes, message in (
            ({"thresholds": replace(cfg.thresholds, panic_ratio=0.5)}, "panic_ratio"),
            ({"thresholds": replace(cfg.thresholds, shrink=2)}, "shrink"),
            ({"reactive_movement_margin": 1}, "movement_margin"),
            ({"reactive_movement_margin": -1}, "movement_margin"),
            ({"reactive_decode_correction_min": 0}, "decode_correction_min"),
            ({"reactive_decode_correction_min_samples": 0}, "decode_correction_min_samples"),
            ({"health_drift_band": 1}, "drift_band"),
            ({"health_window_s": 0}, "window_s"),
            ({"admission": "unknown"}, "admission"),
            ({"advisory": 1}, "advisory"),
            ({"engine_restart_policy": "unknown"}, "engine_restart_policy"),
            ({"connector": "unknown"}, "connector"),
            ({"dialect": "unknown"}, "dialect"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                replace(cfg, **changes).validate()

    def test_role_floors_require_positive_integers_and_allow_one_engine(self):
        """Single-engine configurations accept min_prefill=1 and min_decode=1."""
        cfg = self.load(self.raw)
        for field in ("min_prefill", "min_decode"):
            for value in (True, "1", 0):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, field),
                ):
                    replace(cfg, **{field: value}).validate()
        replace(cfg, engines=cfg.engines[:1], min_prefill=1, min_decode=1).validate()

    def test_engine_and_hardware_contracts_reject_unsafe_bindings(self):
        """Handshake, digest, architecture and TP constraints identify each invalid field."""
        cfg = self.load(self.raw)
        for name, value in (
            ("vllm_version", ""),
            ("connector", ""),
            ("enforce_handshake_compat", False),
            ("image_digest", "latest"),
            ("nixl_connector_version", -1),
            ("kv_heads", -1),
            ("head_size", -1),
            ("hidden_layers", -1),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                replace(
                    cfg, engine_contract=replace(cfg.engine_contract, **{name: value})
                ).validate()
        for changes, message in (
            ({"accelerator": ""}, "accelerator"),
            ({"accelerators_per_engine": 0}, "accelerators_per_engine"),
            ({"tensor_parallel": 0}, "tensor_parallel"),
            ({"tensor_parallel": 99}, "tensor_parallel"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                replace(cfg, hardware=replace(cfg.hardware, **changes)).validate()
        raw = copy.deepcopy(self.raw)
        raw["hardware"]["accelerator"] = 1
        with self.assertRaisesRegex(ValueError, "accelerator"):
            self.load(raw)
        for field in ("cross_layers_blocks", "hybrid_kv_cache_manager"):
            raw = copy.deepcopy(self.raw)
            raw["engine_contract"][field] = 1
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                self.load(raw)

    def test_profile_validation_requires_usable_limits(self):
        """Profile error policy requires explicit usable limits."""
        cfg = self.load(self.raw)
        for name in ("max_decode_fit_mape", "max_decode_cv_mape"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name):
                replace(
                    cfg,
                    profile_validation=replace(cfg.profile_validation, **{name: 0}),
                ).validate()
        with self.assertRaisesRegex(ValueError, "exceeds controller.reactive.movement_margin"):
            replace(
                cfg,
                profile_validation=replace(cfg.profile_validation, max_decode_fit_mape=0.99),
            ).validate()

    def test_serving_policy_rejects_mistyped_and_contradictory_limits(self):
        """Queue and retry policies require bounded resources and a valid handoff age."""
        policy = ServingPolicy()
        for changes, message in (
            ({"queue_capacity": True}, "queue_capacity"),
            ({"decode_concurrency": -1}, "decode_concurrency"),
            ({"max_attempts": 4}, "max_attempts"),
            ({"max_request_bytes": 0}, "max_request_bytes"),
            ({"max_response_bytes": True}, "max_response_bytes"),
            ({"queue_timeout_s": float("inf")}, "queue_timeout_s"),
            ({"retry_replenish": "1"}, "retry_replenish"),
            ({"queue_capacity": 1}, "queue_capacity requires"),
            ({"retry_base_s": 0}, "retry delays"),
            ({"retry_cap_s": 0.01}, "retry delays"),
            ({"retry_replenish": 1.01}, "retry_replenish"),
            ({"max_attempts": 2}, "handoff_timeout_s"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                replace(policy, **changes).validate()
        with self.assertRaisesRegex(ValueError, "max_attempts"):
            self.load({**self.raw, "serving": {"max_attempts": 4}})
        with self.assertRaisesRegex(ValueError, "max_attempts"):
            replace(self.load(self.raw), serving=replace(policy, max_attempts=4)).validate()

    def test_check_cli_uses_native_fleet_config(self):
        """Preflight loads the native fleet document and rejects retired source selectors."""
        with patch.object(check, "run", new=AsyncMock(return_value=0)) as run:
            self.assertEqual(check.main(["--fleet", str(ROOT / "tests/data/fleet.json")]), 0)
        self.assertEqual(run.await_args.args[0].model, "test-model")

        for option in ("--preset", "--from-fleet-json", "--write"):
            with (
                self.subTest(option=option),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                check.main([option, "fixture"])
            self.assertEqual(caught.exception.code, 2)
