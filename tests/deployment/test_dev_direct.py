import copy
import hashlib
import json
import random
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from narwhal.backends import load
from narwhal.backends.llamacpp.dev import PROBE_OUTPUT_TOKENS, LlamacppDirect
from narwhal.deployment.direct_start import DirectEngine, DirectStart, NominalFigures
from narwhal.dev import direct, lifecycle, template
from narwhal.dev.template import _check_free_ports, _read_template
from narwhal.profiling.store import ProfileStore

ROOT = Path(__file__).resolve().parents[2]
MODEL_BYTES = b"simulated model file"
VERSION = "version: 1.0.0-dev (build 12365, commit ae93877c8)\nbuilt with a compiler\n"


class Simulated(DirectStart):
    """Starts the simulated engine in place of an engine binary."""

    crash = False

    def check(self, binary, runtime):
        return "simulated-1"

    def environment(self, binary):
        return {"PYTHONPATH": str(ROOT)}

    def command(self, binary, engine):
        if self.crash:
            return [str(binary), "-c", "raise SystemExit(3)"]
        return [
            str(binary),
            "-m",
            "tools.measurement.simulated_engine",
            "--backend",
            "llamacpp",
            "--iid",
            f"engine-{engine.port}",
            "--port",
            str(engine.port),
            "--token-interval",
            "0.001",
            "--prefill-seconds",
            "0",
            "--state-dir",
            str(engine.shared_dir),
        ]

    def probe(self, served_name):
        return "/v1/completions", {"model": served_name, "prompt": "probe", "max_tokens": 1}

    def figures(self, reply, runtime):
        return NominalFigures(0.0001, 0.002, runtime["context_tokens"] * runtime["slots"], 2)


def free_port_base(count: int) -> int:
    for _ in range(50):
        base = random.randrange(20000, 50000)
        try:
            _check_free_ports(set(range(base, base + count + 2)), "127.0.0.1")
        except ValueError:
            continue
        return base
    raise AssertionError("no free port range")


class DirectInstance(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.model = self.folder / "model.gguf"
        self.model.write_bytes(MODEL_BYTES)
        self.spec = copy.deepcopy(_read_template("direct-qwen35-08b-v1.json"))
        self.spec["model"]["sha256"] = hashlib.sha256(MODEL_BYTES).hexdigest()
        self.root = self.folder / "instance"
        backend = replace(load("llamacpp"), direct=Simulated)
        started = patch.object(direct, "load_backend", return_value=backend)
        started.start()
        self.addCleanup(started.stop)

    def init(self, **changes):
        settings = {
            "template": self.spec,
            "engine_binary": Path(sys.executable),
            "model_path": self.model,
            "engine_count": 2,
            "port_base": free_port_base(2),
            **changes,
        }
        return template.materialize(self.root, **settings)


class DirectInitTests(DirectInstance):
    def test_init_writes_an_instance_whose_engines_start_directly(self):
        self.init(engine_count=4, port_base=(base := free_port_base(4)))
        instance = lifecycle.instance(self.root)
        self.assertEqual(
            {name: instance[name] for name in ("start", "backend", "engine_count", "accelerated")},
            {"start": "direct", "backend": "llamacpp", "engine_count": 4, "accelerated": True},
        )
        self.assertEqual(instance["ports"], {"router": base, "engine_first": base + 1})
        self.assertEqual(instance["engine_version"], "simulated-1")
        fleet = json.loads((self.root / "fleet.json").read_text())
        self.assertEqual(
            [(e["url"], e["role"]) for e in fleet["engines"]],
            [
                (f"http://127.0.0.1:{base + 1 + i}", "prefill" if i < 2 else "decode")
                for i in range(4)
            ],
        )
        self.assertTrue(all("attestation_url" not in e for e in fleet["engines"]))
        self.assertEqual(fleet["engine"]["backend"], "llamacpp")

    def test_init_checks_the_engine_binary_the_model_file_and_host_memory(self):
        for changes, message in (
            ({"engine_binary": None}, "requires --engine-binary and --model"),
            ({"engine_binary": self.model}, "engine binary must name an executable file"),
            ({"model_path": self.folder / "absent.gguf"}, "model path must name the file"),
            ({"model_path": Path(__file__)}, "SHA-256 differs from the pinned model revision"),
            ({"engine_count": 9}, "requires 2 to 8 engines"),
            ({"gpu_uuid": "GPU-1"}, "this template takes no --gpu"),
        ):
            with self.subTest(changes=list(changes)), self.assertRaisesRegex(ValueError, message):
                self.init(**changes)
        with (
            patch.object(direct, "_available_mib", return_value=100),
            self.assertRaisesRegex(ValueError, "100 MiB free, 2400 MiB required"),
        ):
            self.init()
        self.assertFalse(self.root.exists())

    def test_a_repeated_init_keeps_the_instance_and_names_each_conflict(self):
        self.init(port_base=(base := free_port_base(2)))
        before = (self.root / "instance.json").read_text()
        self.assertEqual(template.materialize(self.root), self.root)
        self.init(port_base=base)
        with self.assertRaisesRegex(
            ValueError, "conflicts with --engine-count, --port-base, --no-accelerator"
        ):
            self.init(engine_count=4, port_base=base + 10, accelerated=False)
        self.assertEqual((self.root / "instance.json").read_text(), before)

    def test_a_template_that_launches_engines_takes_no_direct_setting(self):
        with self.assertRaisesRegex(ValueError, "takes no --engine-binary or --no-accelerator"):
            template.materialize(self.root, engine_binary=Path(sys.executable))


class DirectLifecycleTests(DirectInstance):
    def test_up_verify_status_and_down_run_the_fleet(self):
        self.init()
        launched = lifecycle.up(self.root)
        self.addCleanup(self.stop)
        self.assertEqual(launched["status"], "launched")
        run = Path(launched["run"])
        store = ProfileStore(run / "profiles.json")
        profiles = [store.get(iid) for iid in ("n1", "n2")]
        self.assertTrue(all(profile.nominal for profile in profiles))
        self.assertEqual(
            (profiles[0].ttft_b, profiles[0].tpot_intercept, profiles[0].decode_max_requests),
            (0.0001, 0.002, 2),
        )
        self.assertEqual(lifecycle.status(self.root)["status"], "launched")
        verified = lifecycle.verify(self.root)
        self.assertEqual(verified["status"], "ready")
        self.assertTrue(verified["reply"].strip())
        status = lifecycle.status(self.root)
        self.assertEqual((status["status"], status["problems"]), ("ready", []))
        self.assertEqual(sorted(status["processes"]), ["engine-1", "engine-2", "router"])
        with self.assertRaisesRegex(ValueError, "run dev down before starting"):
            lifecycle.up(self.root)
        self.assertEqual(lifecycle.down(self.root)["status"], "stopped")
        instance = lifecycle.instance(self.root)
        ports = instance["ports"]
        _check_free_ports({ports["router"], ports["engine_first"]}, "127.0.0.1")

    def stop(self):
        if json.loads((self.root / "lifecycle.json").read_text())["phase"] != "stopped":
            lifecycle.down(self.root)

    def test_an_engine_that_exits_fails_up_and_leaves_nothing_running(self):
        self.init()
        with (
            patch.object(Simulated, "crash", True),
            self.assertRaisesRegex(ValueError, "process exited before .* became healthy"),
        ):
            lifecycle.up(self.root)
        state = json.loads((self.root / "lifecycle.json").read_text())
        self.assertEqual(state["phase"], "stopped")
        self.assertIn("became healthy", state["failure"]["message"])
        self.assertEqual(lifecycle.status(self.root)["status"], "stopped")

    def test_a_changed_engine_binary_or_model_file_stops_up(self):
        self.init()
        with (
            patch.object(Simulated, "check", return_value="simulated-2"),
            self.assertRaisesRegex(ValueError, "engine binary changed after dev init"),
        ):
            lifecycle.up(self.root)
        self.model.write_bytes(b"another file")
        with self.assertRaisesRegex(ValueError, "SHA-256 differs"):
            lifecycle.up(self.root)


class LlamacppDirectTests(unittest.TestCase):
    def setUp(self):
        self.start = LlamacppDirect()
        self.runtime = {"build": 12365, "context_tokens": 4096, "slots": 2, "extra_args": ["-v"]}
        self.binary = Path("/opt/engine/bin/server")

    def engine(self, **changes) -> DirectEngine:
        settings = {
            "model": Path("/models/m.gguf"),
            "served_name": "m",
            "host": "127.0.0.1",
            "port": 18101,
            "runtime": self.runtime,
            "shared_dir": Path("/run/state"),
            "accelerated": True,
            "threads": 4,
        }
        return DirectEngine(**{**settings, **changes})

    def test_the_command_serves_the_slots_from_one_shared_state_directory(self):
        command = self.start.command(self.binary, self.engine())
        options = dict(zip(command[1::2], command[2::2], strict=False))
        self.assertEqual(command[0], str(self.binary))
        self.assertEqual(
            {name: options[name] for name in ("--ctx-size", "--parallel", "--slot-save-path")},
            {"--ctx-size": "8192", "--parallel": "2", "--slot-save-path": "/run/state"},
        )
        self.assertEqual((options["--alias"], options["--port"]), ("m", "18101"))
        self.assertEqual(command[-1], "-v")
        self.assertNotEqual(options["--n-gpu-layers"], "0")
        cpu = self.start.command(self.binary, self.engine(accelerated=False))
        self.assertEqual(cpu[cpu.index("--n-gpu-layers") + 1], "0")

    def test_the_binary_must_be_the_pinned_build(self):
        def reply(stderr: str, code: int = 0):
            return patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], code, "", stderr)
            )

        with reply(VERSION) as run:
            self.assertEqual(self.start.check(self.binary, self.runtime), "b12365-ae93877c8")
        self.assertIn(str(self.binary.parent), run.call_args.kwargs["env"]["LD_LIBRARY_PATH"])
        with (
            reply(VERSION),
            self.assertRaisesRegex(ValueError, "requires llama.cpp build 1; found"),
        ):
            self.start.check(self.binary, {**self.runtime, "build": 1})
        for stderr, code in (("usage", 0), (VERSION, 1)):
            with reply(stderr, code), self.assertRaisesRegex(ValueError, "reported no llama.cpp"):
                self.start.check(self.binary, self.runtime)
        with (
            patch.object(subprocess, "run", side_effect=OSError("exec format error")),
            self.assertRaisesRegex(ValueError, "--version failed: exec format error"),
        ):
            self.start.check(self.binary, self.runtime)

    def test_the_figures_come_from_the_probe_reply_timings(self):
        endpoint, body = self.start.probe("m")
        self.assertEqual((endpoint, body["max_tokens"]), ("/v1/completions", PROBE_OUTPUT_TOKENS))
        self.assertIs(body["cache_prompt"], False)
        timings = {"prompt_per_token_ms": 5.0, "predicted_per_token_ms": 12.5}
        self.assertEqual(
            self.start.figures({"timings": timings}, self.runtime),
            NominalFigures(0.005, 0.0125, 8192, 2),
        )
        for reply in ({}, {"timings": {**timings, "prompt_per_token_ms": 0}}, {"timings": []}):
            with self.assertRaisesRegex(ValueError, "no positive timings"):
                self.start.figures(reply, self.runtime)
