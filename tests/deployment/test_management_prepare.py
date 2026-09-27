"""Bind local dev preparation to content and ownership without creating a fleet."""

import asyncio
import copy
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from narwhal.deployment.management_access import InspectionAccess
from narwhal.deployment.management_records import OperationError, canonical
from narwhal.deployment.management_registry import ManagementRegistry
from narwhal.dev import management_prepare as prepare
from narwhal.dev import management_settings as settings
from narwhal.dev import template


class LocalPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text("{}")
        self.gguf = self.model / "synthetic.gguf"
        self.gguf.write_bytes(b"model-content")
        self.spec = template.default_template()
        self.spec["model"].update(
            filename=self.gguf.name,
            sha256=hashlib.sha256(self.gguf.read_bytes()).hexdigest(),
            tokenizer_sha256={},
        )
        self.recipe = self.root / "recipe.json"
        self.recipe.write_text(json.dumps(self.spec))
        self.recipe.chmod(0o600)
        self.settings = self.root / "settings.json"
        self.settings.write_text(
            json.dumps(
                {
                    "schema": "narwhal.local-dev-settings",
                    "schema_version": 1,
                    "init": {
                        "model_path": str(self.gguf),
                        "model_dir": str(self.model),
                        "gpu_uuid": "GPU-test",
                        "fabric_interface": "lo",
                    },
                }
            )
        )
        self.settings.chmod(0o600)
        self.instance = self.root / "instance"
        self.registry = ManagementRegistry.model_validate_json(
            json.dumps(
                {
                    "schema": "narwhal.management-registry",
                    "schema_version": 1,
                    "registry_id": "7a112b98-ec55-4efa-9cf1-496b959f1070",
                    "state_dir": str(self.root / "state"),
                    "targets": [
                        {
                            "id": "dev",
                            "kind": "dev",
                            "working_directory": str(self.root),
                            "artifact_root": str(self.root / "artifacts"),
                            "fleet_file": None,
                            "instance_dir": str(self.instance),
                            "adapter": {"id": "local-dev-v1", "settings_path": str(self.settings)},
                            "capabilities": ["inspect", "measure", "mutate"],
                            "actions": ["dev_init", "dev_up", "dev_verify", "dev_down"],
                            "recipes": [{"id": "small", "kind": "dev", "path": str(self.recipe)}],
                        }
                    ],
                }
            )
        )
        self.target = self.registry.targets[0]
        self.context = SimpleNamespace(
            assert_current=lambda: None,
            access=InspectionAccess(self.registry),
            target=self.target,
            deadline=time.monotonic() + 60,
        )
        self.observed = {
            "gpu": {"name": "Synthetic NVIDIA GPU", "uuid": "GPU-test", "total_mib": 8192},
            "interface": "lo",
            "address": "127.0.0.1",
            "runtime_packages": self.spec["runtime"]["expected_packages"],
        }
        self.probe = patch.object(
            prepare,
            "_observe",
            return_value=(self.observed, {"gpu_used_mib": 0, "gpu_free_mib": 8192}),
        )
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def snapshot(self, action="dev_init"):
        return prepare.snapshot(
            self.context,
            self.target,
            action,
            {"recipe_id": "small"} if action == "dev_init" else {},
        )

    def initialize_files(self):
        documents = template.render_documents(
            self.instance,
            spec=self.spec,
            model_dir=self.model,
            model_path=self.gguf,
            fabric_interface="lo",
            address="127.0.0.1",
            gpu=self.observed["gpu"],
        )
        self.instance.mkdir(mode=0o700)
        for name, value in documents.items():
            path = self.instance / name
            path.write_text(json.dumps(value))
            path.chmod(0o600)

    def test_init_preparation_leaves_target_absent_and_binds_physical_resources(self):
        prepared = self.snapshot()
        self.assertFalse(self.instance.exists())
        self.assertEqual(prepared.stages[0]["operation"], "dev.init")
        self.assertEqual(prepared.stages[0]["timeout_ms"], 300000)
        canonical(prepared.identity)
        resources = prepared.identity["resources"]
        self.assertTrue(any(row.endswith(":gpu:GPU-test") for row in resources))
        self.assertTrue(any(row.endswith(":tcp:39000") for row in resources))
        self.assertTrue(any(row.endswith(":tcp:39999") for row in resources))
        self.assertFalse(any("127.0.0.1" in row for row in resources))
        runtime = json.loads(prepared.inputs["runtime_identity"])
        self.assertEqual(runtime["execution"]["template"], self.spec)
        self.assertNotIn("--template", runtime["execution"]["arguments"])

    def test_model_replacement_changes_or_rejects_prepared_identity(self):
        self.snapshot()
        self.gguf.write_bytes(b"changed-model")
        with self.assertRaises(OperationError) as caught:
            self.snapshot()
        self.assertEqual(caught.exception.code, "prerequisite_failed")
        self.assertFalse(self.instance.exists())

    def test_model_cache_symlinks_are_hashed_and_fifos_rejected(self):
        alias = self.root / "cached.gguf"
        alias.symlink_to(self.gguf)
        self.assertEqual(
            prepare.file_identity(alias, self.context)["sha256"], self.spec["model"]["sha256"]
        )
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(OperationError):
            prepare.file_identity(fifo, self.context)

    def test_change_during_file_hashing_is_rejected(self):
        calls = 0

        def changing():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.gguf.write_bytes(b"changed-while-reading")

        self.context.assert_current = changing
        with self.assertRaises(OperationError) as caught:
            prepare.file_identity(self.gguf, self.context)
        self.assertEqual(caught.exception.code, "stale_plan")

    def test_down_keeps_working_after_model_and_template_are_removed(self):
        self.initialize_files()
        self.gguf.unlink()
        (self.instance / "template.json").unlink()
        with patch.object(prepare, "_observe", side_effect=AssertionError("GPU probe during down")):
            prepared = self.snapshot("dev_down")
        self.assertEqual(prepared.stages[0]["timeout_ms"], 60000)
        self.assertNotIn("model_identity", prepared.inputs)
        self.assertIn("cleanup_selection", prepared.inputs)

    def test_registered_instance_symlink_and_outside_run_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        self.instance.symlink_to(outside)
        with self.assertRaises(OperationError):
            self.snapshot()
        self.instance.unlink()
        self.initialize_files()
        (self.instance / "lifecycle.json").write_text(
            json.dumps({"run": str(outside), "phase": "launched", "processes": []})
        )
        (self.instance / "lifecycle.json").chmod(0o600)
        with self.assertRaises(OperationError):
            self.snapshot("dev_down")

    def test_ownership_snapshot_detects_a_changed_live_generation(self):
        self.initialize_files()
        run = self.instance / "run-fixture"
        run.mkdir(mode=0o700)
        identity = {"pid": 1234, "boot_id": "fixture", "start_ticks": 1}
        state = {
            "run": str(run),
            "phase": "launched",
            "processes": [{"name": "router", "identity": identity}],
        }
        (self.instance / "lifecycle.json").write_text(json.dumps(state))
        (self.instance / "lifecycle.json").chmod(0o600)
        with patch.object(prepare.native_engine, "process_identity", return_value=identity):
            first = self.snapshot("dev_down")
        with patch.object(
            prepare.native_engine, "process_identity", return_value={**identity, "start_ticks": 2}
        ):
            second = self.snapshot("dev_down")
        self.assertNotEqual(canonical(first.identity), canonical(second.identity))

    def test_settings_reject_execution_escapes_and_nonfinite_budgets(self):
        for extra in (
            {"runtime_python": sys.executable},
            {"command": ["sh"]},
            {"budgets": {"dev_up": {"timeout_ms": 0}}},
            {"init": {"gpu_memory_utilization": float("inf")}},
            {"init": {"model_path": "../model"}},
        ):
            self.settings.write_text(
                json.dumps({"schema": "narwhal.local-dev-settings", "schema_version": 1, **extra})
            )
            with self.assertRaises(OperationError):
                settings.load(self.target)
        for version in (True, 2):
            self.settings.write_text(
                json.dumps({"schema": "narwhal.local-dev-settings", "schema_version": version})
            )
            with self.assertRaises(OperationError):
                settings.load(self.target)
        for name in ("PYTHONPATH", "LD_LIBRARY_PATH"):
            spec = copy.deepcopy(self.spec)
            spec["runtime"]["environment"][name] = "/tmp/arbitrary"
            with self.assertRaises(OperationError):
                settings.validate_recipe(spec)

    def test_partial_action_budgets_preserve_action_timeout_defaults(self):
        for action, timeout in (("dev_up", 3_600_000), ("dev_down", 60_000)):
            for override in ({}, {"term_grace_ms": 1000}):
                with self.subTest(action=action, override=override):
                    self.settings.write_text(
                        json.dumps(
                            {
                                "schema": "narwhal.local-dev-settings",
                                "schema_version": 1,
                                "budgets": {action: override},
                            }
                        )
                    )
                    budget = settings.load(self.target).budget(action)
                    self.assertEqual(budget.timeout_ms, timeout)
                    self.assertEqual(budget.term_grace_ms, override.get("term_grace_ms", 10_000))
                    self.assertEqual(budget.kill_grace_ms, 5000)
                    self.assertEqual(budget.reconcile_ms, 30_000)
            self.settings.write_text(
                json.dumps(
                    {
                        "schema": "narwhal.local-dev-settings",
                        "schema_version": 1,
                        "budgets": {action: {"timeout_ms": 123_000}},
                    }
                )
            )
            self.assertEqual(settings.load(self.target).budget(action).timeout_ms, 123_000)

    def test_expected_absent_refuses_reuse_before_reading_an_existing_instance(self):
        self.instance.mkdir()
        with self.assertRaisesRegex(ValueError, "remain absent"):
            template.materialize(self.instance, expected_absent=True)

    def test_packaged_settings_schema_matches_current_model(self):
        path = Path(settings.__file__).with_name("local-dev-settings-v1.schema.json")
        shipped = json.loads(path.read_text())
        current = settings.DevSettings.model_json_schema(by_alias=True)
        for key in ("$schema", "$id", "required"):
            shipped.pop(key, None)
            current.pop(key, None)
        self.assertEqual(shipped, current)

    def test_verify_binds_active_fleet_and_profiles_and_observes_idle_before_probes(self):
        self.initialize_files()
        run = self.instance / "run-fixture"
        run.mkdir(mode=0o700)
        identity = {"pid": 1234, "boot_id": "fixture", "start_ticks": 1}
        state = {
            "run": str(run),
            "phase": "launched",
            "processes": [{"name": "router", "identity": identity}],
        }
        fleet = json.loads((self.instance / "fleet.json").read_bytes())
        fleet["profiles"]["path"] = str(run / "profiles.json")
        for path, document in (
            (self.instance / "lifecycle.json", state),
            (run / "fleet.json", fleet),
            (run / "profiles.json", {"profiles": []}),
        ):
            path.write_text(json.dumps(document))
            path.chmod(0o600)
        idle = {"observed_at": "2026-09-27T00:00:00Z", "admission": {"inflight": 0}}
        with (
            patch.object(prepare.native_engine, "process_identity", return_value=identity),
            patch.object(prepare, "_router_idle", return_value=idle),
        ):
            prepared = self.snapshot("dev_verify")
        self.assertEqual(prepared.observations["router_idle"], idle)
        self.assertNotIn("router_idle", prepared.identity)
        execution = json.loads(prepared.inputs["runtime_identity"])["execution"]
        self.assertIn(str(run / "fleet.json"), execution["input_hashes"])
        self.assertIn(str(run / "profiles.json"), execution["input_hashes"])
        plan = {"payload": {"binding": {"source": {}}, "action": "dev_verify", "parameters": {}}}
        for path in (run / "fleet.json", run / "profiles.json"):
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with (
                patch.object(prepare, "manifest", return_value=SimpleNamespace(source={})),
                patch.object(prepare, "execution_config", return_value=execution),
                patch.object(
                    prepare, "snapshot", side_effect=AssertionError("Probe ran after input changed")
                ),
                self.assertRaises(OperationError) as caught,
            ):
                prepare.check(self.context, self.target, plan)
            self.assertEqual(caught.exception.code, "stale_plan")
            path.write_bytes(original)
        with (
            patch.object(prepare.native_engine, "process_identity", return_value=identity),
            patch.object(prepare, "_router_idle", side_effect=OperationError("fleet_busy", "busy")),
            patch.object(
                prepare, "_model", side_effect=AssertionError("Model probe ran before idle check")
            ),
            self.assertRaises(OperationError) as caught,
        ):
            self.snapshot("dev_verify")
        self.assertEqual(caught.exception.code, "fleet_busy")


class RouterIdleTests(unittest.TestCase):
    def setUp(self):
        self.document = {
            "schema": "narwhal.state",
            "schema_version": 1,
            "admission": {"inflight": 0, "queued": 0, "waiting_prefill": 0, "waiting_decode": 0},
            "serving": {"http_retained": 0},
            "resident": {"n1": {"prefill": 0, "decode": 0}, "n2": {"prefill": 0, "decode": 0}},
            "ha": {"standby": False, "epoch": 0},
        }
        self.context = SimpleNamespace(assert_current=lambda: None, deadline=time.monotonic() + 60)
        self.config = {
            "ports": {"router": 18000},
            "router_url": "http://127.0.0.1:18000",
            "engine_count": 2,
        }
        self.ownership = {"processes": [{"name": "router", "observed_state": "present"}]}
        self.fleet = {"engines": [{"iid": "n1"}, {"iid": "n2"}]}
        self.requests = []
        self.options = []

    def request(self, document=None, *, status=200, stream=None):
        payload = json.dumps(self.document if document is None else document).encode()

        def handler(request):
            self.requests.append(request)
            return httpx.Response(
                status,
                headers={"Location": "http://127.0.0.1:19000/redirect"},
                stream=stream if stream is not None else httpx.ByteStream(payload),
            )

        original = httpx.AsyncClient

        def client(**kwargs):
            self.options.append(kwargs)
            return original(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(prepare.httpx, "AsyncClient", side_effect=client):
            return prepare._router_idle(self.context, self.config, self.ownership, self.fleet)

    def test_idle_observation_uses_only_recorded_loopback_without_proxies_or_redirects(self):
        result = self.request()
        self.assertEqual(result["resident"], self.document["resident"])
        self.assertEqual(str(self.requests[0].url), "http://127.0.0.1:18000/narwhal/state")
        self.assertFalse(self.options[0]["trust_env"])
        self.assertFalse(self.options[0]["follow_redirects"])
        self.assertLessEqual(self.options[0]["timeout"], 5)
        self.assertIn("observed_at", result)

    def test_each_nonzero_admission_and_resident_counter_rejects_verification(self):
        paths = [("admission", name) for name in self.document["admission"]]
        paths += [
            ("serving", "http_retained"),
            ("resident", "n1", "prefill"),
            ("resident", "n2", "decode"),
        ]
        for path in paths:
            document = copy.deepcopy(self.document)
            selected = document
            for name in path[:-1]:
                selected = selected[name]
            selected[path[-1]] = 1
            with self.subTest(path=path), self.assertRaises(OperationError) as caught:
                self.request(document)
            self.assertEqual(caught.exception.code, "fleet_busy")

    def test_missing_malformed_and_ha_evidence_fail_closed(self):
        documents = []
        for invalid in (None, True, -1, 0.5):
            value = copy.deepcopy(self.document)
            value["admission"]["inflight"] = invalid
            documents.append(value)
        for missing in ("ha", "resident", "admission", "serving"):
            value = copy.deepcopy(self.document)
            del value[missing]
            documents.append(value)
        value = copy.deepcopy(self.document)
        del value["resident"]["n2"]
        documents.append(value)
        documents.extend(
            {**self.document, "ha": ha}
            for ha in (
                {"standby": True, "epoch": 0},
                {"standby": False, "epoch": 1},
                {"standby": False, "epoch": True},
            )
        )
        documents.append({**self.document, "schema_version": 2})
        for document in documents:
            with self.subTest(document=document), self.assertRaises(OperationError) as caught:
                self.request(document)
            self.assertEqual(caught.exception.code, "prerequisite_failed")

    def test_redirect_wrong_origin_and_oversized_evidence_are_rejected(self):
        with self.assertRaises(OperationError):
            self.request(status=302)
        self.assertEqual(len(self.requests), 1)
        with self.assertRaises(OperationError):
            self.request(stream=httpx.ByteStream(b"x" * (prepare.MAX_ROUTER_IDLE_BYTES + 1)))
        self.config["router_url"] = "http://127.0.0.1:18001"
        previous = len(self.requests)
        with self.assertRaises(OperationError):
            self.request()
        self.assertEqual(len(self.requests), previous)

    def test_slow_stream_has_an_overall_deadline(self):
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    await asyncio.sleep(0.01)
                    yield b" "

        self.context.deadline = time.monotonic() + 0.04
        started = time.monotonic()
        with self.assertRaises(OperationError) as caught:
            self.request(stream=SlowStream())
        self.assertEqual(caught.exception.code, "prerequisite_failed")
        self.assertLess(time.monotonic() - started, 0.5)


class PortReadinessTests(unittest.TestCase):
    def test_stopped_listener_time_wait_allows_restart_but_live_bindings_do_not(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            port = server.getsockname()[1]
            server.listen()
            with self.assertRaises(ValueError):
                template._check_free_ports({port}, "127.0.0.1")
            with socket.create_connection(("127.0.0.1", port)) as client:
                connection, _ = server.accept()
                with connection:
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")
        # The accepted server endpoint closed first and retains TCP TIME_WAIT.
        template._check_free_ports({port}, "127.0.0.1")
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            with self.assertRaises(ValueError):
                template._check_free_ports({bound.getsockname()[1]}, "127.0.0.1")
