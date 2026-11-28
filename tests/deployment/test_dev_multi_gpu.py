"""Run each engine of a dev fleet on its own GPU, outside any shared_device group."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.backends.vllm import dev as vllm_dev
from narwhal.config import FleetConfig
from narwhal.config.loading import read_engine_spec
from narwhal.deployment.engine_launch import selected_launch
from narwhal.deployment.launch_engine.plan import option
from narwhal.dev import launch, lifecycle, template
from narwhal.dev.cli import main
from narwhal.runtime.lifecycle import leave
from narwhal.runtime.lifecycle.join import join_refusal
from narwhal.runtime.lifecycle.leave import begin_leave, leave_refusal
from narwhal.serving.app import create_app
from narwhal.serving.control_token import ROUTER_TOKEN_ENV, token_problem
from tests import CONTROL_HEADERS, CONTROL_TOKEN
from tests.runtime import test_join

ROOT = Path(__file__).resolve().parents[2]
GPUS = ["GPU-aaaa", "GPU-bbbb", "GPU-cccc", "GPU-dddd"]


def rows(names=None):
    return [{"name": (names or {}).get(uuid, "NVIDIA Test GPU"), "uuid": uuid} for uuid in GPUS]


class Instance(unittest.TestCase):
    """A small vLLM template and a host with four GPUs."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "instance"
        self.model = self.parent / "model"
        self.model.mkdir()
        (self.model / "config.json").write_text("{}")
        self.gguf = self.model / "synthetic.gguf"
        self.gguf.write_bytes(b"synthetic")
        self.spec = template.default_template()
        self.spec["model"].update(
            filename=self.gguf.name,
            sha256=hashlib.sha256(b"synthetic").hexdigest(),
            tokenizer_sha256={},
        )
        self.spec["runtime"].pop("gguf_plugin_python_sha256", None)
        self.memory = {uuid: {"total_mib": 8192, "used_mib": 0} for uuid in GPUS}
        self.names: dict[str, str] = {}

    @contextlib.contextmanager
    def host(self):
        with (
            patch.object(template, "_gpu_rows", side_effect=lambda: rows(self.names)),
            patch.object(template, "gpu_memory", side_effect=lambda uuid: self.memory[uuid]),
            patch.object(template, "_check_packages"),
            patch.object(vllm_dev, "check_imports"),
            patch.object(template, "_address", return_value="127.0.0.1"),
            patch.object(template, "_check_free_ports"),
        ):
            yield

    def initialize(self, **overrides):
        with self.host():
            return template.materialize(
                self.root,
                model_dir=self.model,
                model_path=self.gguf,
                fabric_interface="lo",
                template=self.spec,
                **overrides,
            )

    def cli(self, *flags):
        spec = self.parent / "template.json"
        spec.write_text(json.dumps(self.spec))
        with (
            self.host(),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = main(
                [
                    "dev",
                    "init",
                    "--instance",
                    str(self.root),
                    "--model",
                    str(self.gguf),
                    "--model-dir",
                    str(self.model),
                    "--interface",
                    "lo",
                    # A reused instance keeps its saved template.
                    *([] if self.root.exists() else ["--template", str(spec)]),
                    *flags,
                ]
            )
        return code, output.getvalue(), errors.getvalue()


class MultiGpuInitTests(Instance):
    def test_each_engine_runs_on_its_own_gpu_without_a_shared_group(self):
        self.initialize(gpu_uuids=GPUS, engine_count=3)
        document = lifecycle.read(self.root / "engine-launch.json")
        launch = document["engines"]
        self.assertEqual(
            [launch[f"engine-{n}"]["gpu_ids"] for n in (1, 2, 3)], [[uuid] for uuid in GPUS[:3]]
        )
        self.assertEqual(
            [
                selected_launch(document, f"engine-{n}", {})["environment"]["CUDA_VISIBLE_DEVICES"]
                for n in (1, 2, 3)
            ],
            GPUS[:3],
        )
        self.assertFalse(any("shared_device" in record for record in launch.values()))
        fleet = lifecycle.read(self.root / "fleet.json")
        self.assertFalse(any("shared_device" in engine for engine in fleet["engines"]))
        self.assertEqual(
            [engine["role"] for engine in fleet["engines"]], ["prefill", "decode", "decode"]
        )
        self.assertEqual(fleet["hardware"]["accelerator"], "NVIDIA Test GPU")
        self.assertEqual(FleetConfig.load(self.root / "fleet.json").engines[0].shared_device, None)
        recorded = lifecycle.instance(self.root)
        self.assertEqual(recorded["gpu_uuids"], GPUS[:3])
        self.assertNotIn("gpu_uuid", recorded)
        self.assertEqual(template.instance_gpus(recorded), GPUS[:3])

    def test_the_memory_settings_apply_on_each_gpu(self):
        # Three engines of 2867 MiB exceed the 7168 MiB of one GPU, and fit one per GPU.
        self.initialize(gpu_uuids=GPUS[:3], engine_count=3)
        saved = lifecycle.read(self.root / "template.json")["allocation"]
        self.assertEqual(
            (saved["gpu_memory_utilization"], saved["device_allowance"]), (0.35, 0.875)
        )
        runtime = lifecycle.read(self.root / "engine-launch.json")["engines"]["engine-1"]["runtime"]
        self.assertEqual(option(runtime["extra_args"], "--gpu-memory-utilization"), "0.35")
        shutil.rmtree(self.root)
        # Each GPU needs free memory for its engine's fraction and the reserve.
        self.memory["GPU-bbbb"] = {"total_mib": 8192, "used_mib": 4800}
        self.initialize(gpu_uuids=GPUS[:2])
        shutil.rmtree(self.root)
        self.memory["GPU-bbbb"] = {"total_mib": 8192, "used_mib": 5000}
        with self.assertRaisesRegex(
            ValueError, "GPU GPU-bbbb VRAM reserve failed: 3192 MiB free, 3379 MiB required"
        ):
            self.initialize(gpu_uuids=GPUS[:2])
        self.assertFalse(self.root.exists())
        self.memory["GPU-bbbb"] = {"total_mib": 8192, "used_mib": 0}
        with self.assertRaisesRegex(ValueError, "an engine of 8000 MiB exceeds the 7168 MiB"):
            self.initialize(gpu_uuids=GPUS[:2], engine_memory_mib=8000, device_memory_mib=7168)
        # The smallest GPU sets the fractions.
        self.memory = {uuid: {"total_mib": 16384, "used_mib": 0} for uuid in GPUS}
        self.memory["GPU-bbbb"] = {"total_mib": 8192, "used_mib": 0}
        self.initialize(gpu_uuids=GPUS[:2])
        saved = lifecycle.read(self.root / "template.json")["allocation"]
        self.assertEqual(saved["gpu_memory_utilization"], 0.35)

    def test_the_gpu_list_must_name_a_distinct_gpu_for_every_engine(self):
        for overrides, message in (
            ({"gpu_uuids": GPUS[:1]}, "2 engines need 2 GPUs; --gpus names 1"),
            (
                {"gpu_uuids": GPUS[:3], "engine_count": 4},
                "4 engines need 4 GPUs; --gpus names 3",
            ),
            ({"gpu_uuids": ["GPU-aaaa", "GPU-aaaa"]}, "names GPU-aaaa more than once"),
            ({"gpu_uuids": ["GPU-aaaa", "0"]}, "start with GPU-"),
            ({"gpu_uuids": ["GPU-aaaa", "GPU-eeee"]}, "nvidia-smi lists no GPU GPU-eeee"),
            # The GPUs past the engine count are checked too.
            (
                {"gpu_uuids": ["GPU-aaaa", "GPU-bbbb", "GPU-eeee"]},
                "nvidia-smi lists no GPU GPU-eeee",
            ),
            ({"gpu_uuids": GPUS[:2], "gpu_uuid": "GPU-aaaa"}, "--gpu or a GPU per engine"),
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                self.initialize(**overrides)
        self.names = {"GPU-bbbb": "NVIDIA Other GPU"}
        with self.assertRaisesRegex(ValueError, "one product"):
            self.initialize(gpu_uuids=GPUS[:2])
        self.spec["gpu"]["product"] = "NVIDIA Pinned GPU"
        self.names = {}
        with self.assertRaisesRegex(ValueError, "template requires NVIDIA Pinned GPU"):
            self.initialize(gpu_uuids=GPUS[:2])
        self.assertFalse(self.root.exists())

    def test_the_cli_takes_a_comma_separated_gpu_list(self):
        code, output, errors = self.cli("--gpus", ",".join(GPUS), "--engine-count", "3")
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "initialized")
        self.assertEqual(lifecycle.instance(self.root)["gpu_uuids"], GPUS[:3])
        # The same list reuses the instance; another list, or one shared GPU, conflicts.
        code, output, _ = self.cli("--gpus", ",".join(GPUS))
        self.assertEqual((code, json.loads(output)["status"]), (0, "reused"))
        for flags, detail in (
            (("--gpus", "GPU-aaaa,GPU-cccc,GPU-bbbb"), "conflicts with --gpus"),
            (("--gpu", "GPU-aaaa"), "conflicts with --gpu"),
            # Reuse checks the whole list as a fresh init does.
            (("--gpus", ",".join([*GPUS, "GPU-eeee"])), "nvidia-smi lists no GPU GPU-eeee"),
            (("--gpus", ",".join([*GPUS[:3], "GPU-aaaa"])), "names GPU-aaaa more than once"),
        ):
            with self.subTest(flags=flags):
                code, output, errors = self.cli(*flags)
                self.assertEqual((code, output), (2, ""))
                self.assertIn(detail, errors)

    def test_the_cli_refuses_an_empty_entry_and_both_gpu_options(self):
        for flags in (("--gpus", "GPU-aaaa,,GPU-bbbb"), ("--gpu", "GPU-a", "--gpus", "GPU-b")):
            with self.subTest(flags=flags), self.assertRaises(SystemExit) as exit:
                self.cli(*flags)
            self.assertEqual(exit.exception.code, 2)
        self.assertFalse(self.root.exists())

    def test_a_shared_instance_conflicts_with_a_gpu_list(self):
        self.initialize(gpu_uuid="GPU-aaaa")
        self.assertEqual(lifecycle.instance(self.root)["gpu_uuid"], "GPU-aaaa")
        code, _, errors = self.cli("--gpus", "GPU-aaaa,GPU-bbbb")
        self.assertEqual(code, 2)
        self.assertIn("conflicts with --gpus", errors)

    def test_a_trtllm_engine_on_its_own_gpu_keeps_the_bound_to_its_share(self):
        from narwhal.backends.trtllm import dev as trtllm_dev
        from narwhal.backends.trtllm.plan import serve_args
        from narwhal.backends.trtllm.runtime import SHARED_GPU_FLAG

        spec = template._read_template("small-cuda-trtllm-v1.json")
        runtime = trtllm_dev.runtime(spec["runtime"], self.model, 0.075)
        self.assertIs(runtime["bound_kv_cache"], True)
        args = serve_args(
            {"runtime": runtime, "tensor_parallel_size": 1},
            model=str(self.model),
            served_name="m",
            host="127.0.0.1",
            port=1,
        )
        self.assertEqual(args[args.index(SHARED_GPU_FLAG) + 1], "3072")

    def test_a_template_that_starts_its_engines_directly_takes_no_gpu_list(self):
        spec = template._read_template("direct-qwen35-08b-v1.json")
        with self.assertRaisesRegex(ValueError, "this template takes no --gpus"):
            template.materialize(self.root, template=spec, gpu_uuids=GPUS[:2])


class MultiGpuLifecycleTests(Instance):
    def test_up_starts_each_engine_on_its_own_gpu(self):
        self.initialize(gpu_uuids=GPUS[:3], engine_count=3)
        started = {}

        class Started(Exception):
            pass

        def command(root, module, args, log, default_timeout=300):
            if log.startswith("native-start"):
                started.update(log=log, args=args, timeout=default_timeout)
                raise Started()

        with (
            patch.object(lifecycle, "_run", side_effect=command),
            patch.object(lifecycle, "_check_free_ports"),
            patch.object(lifecycle, "memory_samples", return_value=contextlib.nullcontext()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(Started),
        ):
            lifecycle.up(self.root)
        self.assertEqual(started["log"], "native-start")
        self.assertEqual(started["args"][0], "start-native")
        self.assertNotIn("--backend", started["args"])
        self.assertEqual(started["timeout"], 3 * 180)
        runs = [Path(started["args"][i + 1]) for i, a in enumerate(started["args"]) if a == "--run"]
        visible = []
        for engine_run in runs:
            env = dict(
                line.split("=", 1) for line in (engine_run / "engine.env").read_text().splitlines()
            )
            visible.append(env["CUDA_VISIBLE_DEVICES"])
            self.assertNotIn("shared_device", lifecycle.read(engine_run / "launch.json"))
        self.assertEqual(visible, GPUS[:3])

    def test_engines_on_their_own_gpus_profile_one_split_without_neighbours(self):
        self.initialize(gpu_uuids=GPUS[:3], engine_count=3)
        self.assertIn("neighbour_prefill_rps", self.spec["profile"])
        run = self.root / "run-profiles"
        run.mkdir()
        (run / "profiles-1p2d.json").write_text('{"profiles": []}')
        with patch.object(lifecycle, "_run") as command, contextlib.redirect_stderr(io.StringIO()):
            launch._profiles(run, lifecycle.read(self.root / "fleet.json"), self.spec)
        self.assertEqual([call.args[3] for call in command.call_args_list], ["profile-1p2d"])
        args = command.call_args.args[2]
        self.assertNotIn("--colocated", args)
        self.assertFalse(any(arg.startswith("--neighbour-") for arg in args))
        # Each engine serves discarded requests before its sweep.
        self.assertEqual(args[args.index("--warmup-requests") + 1], str(launch.WARMUP_REQUESTS))

    def test_engines_on_one_gpu_profile_without_the_warm_up(self):
        sglang = template._read_template("small-cuda-sglang-v1.json")
        for spec in (self.spec, sglang):
            with self.subTest(template=spec["name"]):
                shutil.rmtree(self.root, ignore_errors=True)
                self.initialize(gpu_uuid="GPU-aaaa")
                run = self.root / "run-profiles"
                run.mkdir()
                (run / "profiles-1p1d.json").write_text('{"profiles": []}')
                with (
                    patch.object(lifecycle, "_run") as command,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    launch._profiles(run, lifecycle.read(self.root / "fleet.json"), spec)
                for call in command.call_args_list:
                    self.assertNotIn("--warmup-requests", call.args[2])

    def test_memory_samples_name_each_gpu(self):
        run = self.parent / "samples"
        run.mkdir()
        readings = {"GPU-aaaa": 100, "GPU-bbbb": 200}
        with patch.object(
            lifecycle,
            "gpu_memory",
            side_effect=lambda uuid: {"used_mib": readings[uuid], "total_mib": 8192},
        ):
            with lifecycle.memory_samples(run, ["GPU-aaaa", "GPU-bbbb"], "up"):
                time.sleep(0.1)
            with lifecycle.memory_samples(run, ["GPU-aaaa"], "verify"):
                time.sleep(0.1)
        several = [json.loads(line) for line in (run / "up-memory.jsonl").read_text().splitlines()]
        self.assertEqual(
            [(row["gpu_uuid"], row["used_mib"]) for row in several[:2]],
            [("GPU-aaaa", 100), ("GPU-bbbb", 200)],
        )
        one = json.loads((run / "verify-memory.jsonl").read_text().splitlines()[0])
        self.assertEqual(set(one), {"time", "used_mib", "total_mib"})


def attested(cfg: FleetConfig, root: Path) -> FleetConfig:
    """The fleet as `up` serves it, with the engine contract that attestation adds."""
    cfg.engine_contract = FleetConfig.load(ROOT / "tests/data/fleet.json").engine_contract
    cfg.profiles_path = root / "profiles.json"
    cfg.state_path = root / "handoff.json"
    return cfg


class MembershipPreconditionTests(Instance, unittest.IsolatedAsyncioTestCase):
    async def router(self):
        cfg = attested(FleetConfig.load(self.root / "fleet.json"), self.parent)
        router = create_app(cfg).state.router
        self.addAsyncCleanup(router.engines.aclose)
        return cfg, router

    async def test_engines_on_their_own_gpus_may_leave_and_rejoin(self):
        self.initialize(gpu_uuids=GPUS[:3], engine_count=3)
        cfg, router = await self.router()
        self.assertFalse(cfg.contract_missing())
        self.assertEqual(cfg.engine_restart_policy, "individual")
        self.assertEqual(leave_refusal(router, "n3"), "")
        capture = AsyncMock(return_value=({"n3": 100.0}, {}))
        with patch.object(leave, "capture_process_identities", capture):
            async with router.lifecycle.lock:
                await begin_leave(router, "n3", deadline_s=60.0)
        self.assertNotIn("n3", router.membership)
        departed = next(spec for spec in cfg.engines if spec.iid == "n3")
        self.assertEqual(join_refusal(router, departed), "")

    async def test_engines_on_one_gpu_may_neither_leave_nor_join(self):
        self.initialize(gpu_uuid="GPU-aaaa", engine_count=2)
        cfg, router = await self.router()
        self.assertIn("shared-device group cannot leave", leave_refusal(router, "n2"))
        candidate = replace(cfg.engines[1], iid="n9", url="http://127.0.0.1:1")
        self.assertIn("shared-device group cannot join", join_refusal(router, candidate))


class DevFleetRejoinTests(test_join.JoinHarness):
    """A member of a dev fleet on its own GPU leaves and rejoins through the join checks."""

    def setUp(self):
        instance = Instance()
        instance.setUp()
        self.addCleanup(instance.doCleanups)
        instance.initialize(gpu_uuids=GPUS[:3], engine_count=3)
        self.fleet_path = instance.root / "fleet.json"
        cfg = attested(FleetConfig.load(self.fleet_path), instance.parent)
        with (
            patch.object(test_join, "fleet", return_value=cfg),
            patch.object(test_join.JoinHarness, "bind_profile"),
        ):
            super().setUp()
        self.starts = {"n1": 100.0, "n2": 100.0, "n3": 100.0}
        self.manager.process_starts = dict(self.starts)
        for iid in self.starts:
            self.bind_profile(iid)

    def http(self, request):
        # The harness's candidate is a member of this fleet, so its own routes answer.
        with patch.object(test_join, "NEW", self.cfg.engines[0]):
            return super().http(request)

    async def test_the_documented_requests_remove_and_rejoin_an_engine(self):
        # Dev-Runtime saves the engine's entry from the fleet file and posts it back.
        fleet = json.loads(self.fleet_path.read_text())
        saved = json.dumps(next(e for e in fleet["engines"] if e["iid"] == "n3"))
        departed = next(spec for spec in self.cfg.engines if spec.iid == "n3")
        self.assertEqual(read_engine_spec(json.loads(saved)), departed)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://router",
            headers=CONTROL_HEADERS,
        )
        self.addAsyncCleanup(client.aclose)
        capture = AsyncMock(return_value=({"n3": 100.0}, {}))
        with patch.object(leave, "capture_process_identities", capture):
            left = await client.delete("/narwhal/engines/n3", params={"deadline_s": 300})
        self.assertEqual(left.status_code, 200, left.text)
        listed = (await client.get("/narwhal/engines")).json()
        self.assertEqual(
            (listed["generation"], [e["iid"] for e in listed["engines"]]), (1, ["n1", "n2"])
        )
        joined = await client.post(
            "/narwhal/engines", content=saved, headers={"content-type": "application/json"}
        )
        self.assertEqual(joined.status_code, 200, joined.text)
        self.assertTrue(joined.json()["join"]["joined"])
        listed = (await client.get("/narwhal/engines")).json()
        self.assertEqual(listed["generation"], 2)
        self.assertEqual([e["iid"] for e in listed["engines"]], ["n1", "n2", "n3"])

    async def test_a_departed_engine_rejoins_with_its_profile(self):
        capture = AsyncMock(return_value=({"n3": 100.0}, {}))
        with patch.object(leave, "capture_process_identities", capture):
            async with self.manager.lock:
                await begin_leave(self.router, "n3", deadline_s=60.0)
        self.assertNotIn("n3", self.router.membership)
        self.assertEqual(self.router.membership.generation, 1)
        departed = next(spec for spec in self.cfg.engines if spec.iid == "n3")
        outcome = await self.join(departed)
        self.assertTrue(outcome.joined, outcome.error)
        self.assertEqual(outcome.generation, 2)
        self.assertIn("n3", self.router.membership)
        self.assertIn("n3", {inst.iid for inst in self.router.scheduler.live_instances()})


if __name__ == "__main__":
    unittest.main()


class RouterTokenTests(unittest.TestCase):
    """The instance's router takes the exported control token, or else one of its own."""

    def test_an_instance_draws_its_token_once_and_keeps_it_private(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {ROUTER_TOKEN_ENV: ""}):
                token = launch.router_token(root)
                self.assertIsNone(token_problem(token))
                path = root / launch.ROUTER_TOKEN_FILE
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(launch.router_token(root), token)
            self.assertEqual(launch.router_token(root), CONTROL_TOKEN)
            path.write_text("short\n")
            with (
                patch.dict(os.environ, {ROUTER_TOKEN_ENV: ""}),
                self.assertRaisesRegex(ValueError, "32 random bytes"),
            ):
                launch.router_token(root)
