"""Launch a fleet in a stubbed Slurm allocation with stub engine, profiler and router processes."""

import argparse
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from narwhal.backends.vllm.allocation import VllmAllocation
from narwhal.config.loading import load as load_fleet
from narwhal.deployment.allocation_start import AllocationEngine, AllocationStart
from narwhal.deployment.slurm import cli
from narwhal.deployment.slurm.allocation import (
    Allocation,
    expand_hostlist,
    from_environment,
    resolve,
)
from narwhal.deployment.slurm.launch import (
    Launch,
    Settings,
    StageFailed,
    Stopped,
    check_fleet_setting,
    engine_environment,
    fleet_document,
    limits_document,
    place,
)

# A stand-in engine or router: answers 200 to every GET on the port in argv[1].
SERVER = (
    "import http.server, sys\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        self.send_response(200); self.end_headers(); self.wfile.write(b'{}')\n"
    "    def log_message(self, *a): pass\n"
    "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n"
)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubStart(AllocationStart):
    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def command(self, python, engine):
        if self.exit_code is not None:
            return [sys.executable, "-c", f"raise SystemExit({self.exit_code})"]
        return [sys.executable, "-c", SERVER, str(engine.port)]

    def environment(self, engine):
        return {"STUB_TRANSFER_PORT": str(engine.transfer_port)}


ENV = {
    "SLURM_JOB_ID": "4242",
    "SLURM_JOB_NODELIST": "gpu[01-02]",
    "SLURM_JOB_NUM_NODES": "2",
    "SLURM_NTASKS": "2",
    "SLURM_NODEID": "1",
    "CUDA_VISIBLE_DEVICES": "0,1,2,3",
}


class AllocationTests(unittest.TestCase):
    def test_host_lists_expand_in_order_with_padding(self):
        cases = {
            "gpu[01-03,07],login1": ["gpu01", "gpu02", "gpu03", "gpu07", "login1"],
            "node[8-10]": ["node8", "node9", "node10"],
            "r[1-2]-n[01-02]": ["r1-n01", "r1-n02", "r2-n01", "r2-n02"],
            "single": ["single"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expand_hostlist(text), expected)
        for bad in ("gpu[01-03", "gpu]01", "gpu[3-1]", "gpu[a]", "a,,b", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                expand_hostlist(bad)

    def test_rank_nodes_and_devices_come_from_the_task_environment(self):
        allocation = from_environment(ENV, "CUDA_VISIBLE_DEVICES")
        self.assertEqual(
            allocation, Allocation("4242", 1, ("gpu01", "gpu02"), ("0", "1", "2", "3"))
        )
        self.assertFalse(allocation.head)

    def test_a_step_on_part_of_the_job_uses_the_step_nodes(self):
        env = {
            **ENV,
            "SLURM_JOB_NODELIST": "gpu[01-04]",
            "SLURM_JOB_NUM_NODES": "4",
            "SLURM_STEP_NODELIST": "gpu[03-04]",
            "SLURM_STEP_NUM_NODES": "2",
        }
        allocation = from_environment(env, "CUDA_VISIBLE_DEVICES")
        self.assertEqual((allocation.rank, allocation.nodes), (1, ("gpu03", "gpu04")))

    def test_an_environment_outside_one_task_per_node_is_refused(self):
        cases = (
            ({"SLURM_JOB_ID": None}, "SLURM_JOB_ID"),
            ({"SLURM_NTASKS": "8"}, "one task per node"),
            ({"SLURM_NODEID": "2"}, "outside"),
            ({"SLURM_NODEID": None}, "SLURM_NODEID"),
            ({"SLURM_JOB_NUM_NODES": "3"}, "3"),
            ({"SLURM_JOB_NODELIST": "gpu01,gpu01"}, "twice"),
        )
        for change, expected in cases:
            env = {**ENV, **change}
            env = {key: value for key, value in env.items() if value is not None}
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, expected):
                from_environment(env, "CUDA_VISIBLE_DEVICES")

    def test_resolve_returns_the_first_ipv4_address(self):
        lookup = lambda *args: [(2, 1, 6, "", ("10.0.0.7", 0))]
        self.assertEqual(resolve("gpu01", lookup), "10.0.0.7")

        def missing(*args):
            raise OSError("not known")

        with self.assertRaisesRegex(ValueError, "gpu01"):
            resolve("gpu01", missing)


def settings(root, **changes):
    values = {
        "model": "/models/m",
        "served_name": "m",
        "run_root": Path(root),
        "slo_ttft_s": 2.0,
        "slo_tpot_s": 0.05,
        "engines_per_node": 2,
        "tensor_parallel": 2,
        "stop_grace_s": 5,
    }
    return Settings(**{**values, **changes})


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.allocation = from_environment(ENV, "CUDA_VISIBLE_DEVICES")

    def test_engines_are_numbered_in_node_order_with_their_ports_and_devices(self):
        placements = place(settings(self.temp.name), self.allocation, ["10.0.0.1", "10.0.0.2"])
        self.assertEqual([p.engine.iid for p in placements], ["n1", "n2", "n3", "n4"])
        self.assertEqual([p.engine.host for p in placements], ["10.0.0.1"] * 2 + ["10.0.0.2"] * 2)
        self.assertEqual([p.engine.port for p in placements], [8100, 8101, 8100, 8101])
        self.assertEqual([p.engine.transfer_port for p in placements], [5600, 5601] * 2)
        self.assertEqual([p.devices for p in placements if p.rank == 1], [("0", "1"), ("2", "3")])

    def test_the_fleet_file_lists_literal_urls_and_opens_half_as_prefill(self):
        s = settings(self.temp.name, max_sequences=48)
        placements = place(s, self.allocation, ["10.0.0.1", "10.0.0.2"])
        directory = Path(self.temp.name, "slurm-4242")
        document = fleet_document(s, placements, directory)
        self.assertEqual(
            [(e["iid"], e["url"], e["role"]) for e in document["engines"]],
            [
                ("n1", "http://10.0.0.1:8100", "prefill"),
                ("n2", "http://10.0.0.1:8101", "prefill"),
                ("n3", "http://10.0.0.2:8100", "decode"),
                ("n4", "http://10.0.0.2:8101", "decode"),
            ],
        )
        self.assertNotIn("engine_contract", document)
        path = Path(self.temp.name, "fleet.json")
        path.write_text(json.dumps(document))
        fleet = load_fleet(path)
        self.assertEqual(len(fleet.engines), 4)
        self.assertEqual(
            limits_document(s, placements)["engines"], dict.fromkeys(("n1", "n2", "n3", "n4"), 48)
        )
        self.assertIsNone(limits_document(settings(self.temp.name), placements))

    def test_fleet_settings_set_fields_by_dotted_path(self):
        s = settings(
            self.temp.name,
            fleet_settings={
                "serving.admission": "open",
                "serving.max_connections": 1024,
                "engine.first_token_timeout_s": 5.0,
            },
        )
        placements = place(s, self.allocation, ["10.0.0.1", "10.0.0.2"])
        document = fleet_document(s, placements, Path(self.temp.name, "slurm-4242"))
        self.assertEqual(document["serving"], {"admission": "open", "max_connections": 1024})
        self.assertEqual(document["engine"], {"backend": "vllm", "first_token_timeout_s": 5.0})
        path = Path(self.temp.name, "fleet.json")
        path.write_text(json.dumps(document))
        fleet = load_fleet(path)
        self.assertEqual((fleet.admission, fleet.max_connections), ("open", 1024))

    def test_fleet_settings_cannot_replace_the_fields_the_launch_writes(self):
        refused = (
            "engines",
            "slo",
            "slo.ttft_s",
            "engine.backend",
            "profiles",
            "recovery.state_path",
        )
        for path in (*refused, "a..b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                check_fleet_setting(path)
        check_fleet_setting("engine.tokenize_timeout_s")

    def test_a_split_without_both_roles_or_without_devices_is_refused(self):
        for changes, expected in (
            ({"prefill": 4}, "prefill"),
            ({"engines_per_node": 1, "tensor_parallel": 1, "prefill": 1}, None),
            ({"engines_per_node": 3}, "devices"),
        ):
            with self.subTest(changes=changes):
                s = settings(self.temp.name, **changes)
                if expected is None:
                    self.assertEqual(len(place(s, self.allocation, ["a", "b"])), 2)
                else:
                    with self.assertRaisesRegex(ValueError, expected):
                        place(s, self.allocation, ["a", "b"])

    def test_the_engine_environment_leaves_narwhals_python_out(self):
        s = settings(self.temp.name, engine_env={"UCX_TLS": "rc,sm,self"})
        placement = place(s, self.allocation, ["10.0.0.1", "10.0.0.2"])[2]
        own_bin = str(Path(sys.prefix) / "bin")
        base = {
            "PATH": os.pathsep.join([own_bin, "/usr/bin"]),
            "VIRTUAL_ENV": sys.prefix,
            "PYTHONPATH": "/narwhal/src",
            "HOME": "/home/op",
        }
        env = engine_environment(s, placement, VllmAllocation(), base)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0,1")
        self.assertEqual(env["UCX_TLS"], "rc,sm,self")
        self.assertEqual(env["VLLM_NIXL_SIDE_CHANNEL_HOST"], "10.0.0.2")
        self.assertEqual(env["VLLM_NIXL_SIDE_CHANNEL_PORT"], "5600")
        self.assertEqual(env["NIXL_HOST_IP"], "10.0.0.2")

    def test_the_vllm_command_runs_the_image_entrypoint_with_both_kv_roles(self):
        engine = AllocationEngine(
            "n1", "/models/m", "m", "10.0.0.1", 8100, 5600, 2, 48, ("--max-model-len=32768",)
        )
        command = VllmAllocation().command("python3", engine)
        self.assertEqual(command[:3], ["python3", "-m", "vllm.entrypoints.openai.api_server"])
        config = json.loads(command[command.index("--kv-transfer-config") + 1])
        self.assertEqual((config["kv_connector"], config["kv_role"]), ("NixlConnector", "kv_both"))
        self.assertEqual(command[command.index("--host") + 1], "10.0.0.1")
        self.assertEqual(command[command.index("--tensor-parallel-size") + 1], "2")
        self.assertEqual(command[command.index("--max-num-seqs") + 1], "48")
        self.assertEqual(command[-1], "--max-model-len=32768")


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lines = []
        self.stop = threading.Event()

    def launch(self, *, rank=0, start=None, profile_exit=0, router=True, **changes):
        nodes = ("gpu01", "gpu02") if rank else ("gpu01",)
        allocation = Allocation("7", rank, nodes, ("0", "1"))
        values = {
            "tensor_parallel": 1,
            "engine_port": free_port(),
            "router_port": free_port(),
            "health_timeout_s": 20,
            "ready_timeout_s": 20,
        }
        s = settings(self.temp.name, **{**values, **changes})
        launch = Launch(
            s,
            allocation,
            start or StubStart(),
            ["127.0.0.1"] * len(nodes),
            stop=self.stop,
            env={"PATH": os.environ.get("PATH", "")},
            say=self.record,
        )
        port = s.router_port

        def child(name, module, args):
            self.lines.append(f"child {name} {module}")
            if name == "profile":
                code = [sys.executable, "-c", f"raise SystemExit({profile_exit})"]
            elif router:
                code = [sys.executable, "-c", SERVER, str(port)]
            else:
                code = [sys.executable, "-c", "raise SystemExit(5)"]
            return subprocess.Popen(code, start_new_session=True)

        launch._child = child
        return launch

    def record(self, line):
        self.lines.append(line)
        if line.startswith("ready:"):
            self.stop.set()

    def stages(self):
        return [line.split(":")[0] for line in self.lines if not line.startswith("child")]

    def test_the_head_runs_the_stages_in_order_and_stops_the_router_before_the_engines(self):
        launch = self.launch(max_sequences=32)
        with self.assertRaises(Stopped):
            launch.run()
        self.assertEqual(
            self.stages(),
            [
                "engines",
                "engines",
                "fleet",
                "health",
                "profile",
                "profile",
                "router",
                "ready",
                "stop",
                "stop",
                "stop",
            ],
        )
        stops = [line for line in self.lines if line.startswith("stop:")]
        self.assertEqual(
            stops, ["stop: stopped router", "stop: stopped engine n1", "stop: stopped engine n2"]
        )
        self.assertIn("child profile narwhal.profiling.probe.cli", self.lines)
        self.assertIn("child router narwhal.cli", self.lines)
        directory = Path(self.temp.name, "slurm-7")
        self.assertTrue((directory / "fleet.json").exists())
        self.assertTrue((directory / "profiling-limits.json").exists())
        self.assertEqual(launch.engines, [])

    def test_a_worker_starts_only_its_engines_and_waits(self):
        launch = self.launch(rank=1)
        timer = threading.Timer(1.5, self.stop.set)
        timer.start()
        with self.assertRaises(Stopped):
            launch.run()
        self.assertEqual(self.stages(), ["engines", "engines", "stop", "stop"])
        self.assertIn("engine n3", " ".join(self.lines))
        self.assertFalse(Path(self.temp.name, "slurm-7", "fleet.json").exists())

    def test_each_failed_stage_is_named(self):
        cases = (
            ({"profile_exit": 3}, "profile", "narwhal-profile exited with status 3"),
            ({"start": StubStart(exit_code=9)}, "health", "engine n1 exited with status 9"),
            ({"router": False}, "ready", "router exited with status 5"),
        )
        for options, stage, detail in cases:
            with self.subTest(stage=stage):
                self.lines.clear()
                self.temp.cleanup()
                self.temp = tempfile.TemporaryDirectory()
                self.addCleanup(self.temp.cleanup)
                launch = self.launch(**options)
                with self.assertRaises(StageFailed) as caught:
                    launch.run()
                self.assertEqual(caught.exception.stage, stage)
                self.assertIn(detail, caught.exception.detail)
                self.assertEqual(launch.engines, [])

    def test_health_names_the_engines_that_never_answer(self):
        class Silent(StubStart):
            def command(self, python, engine):
                return [sys.executable, "-c", "import time; time.sleep(60)"]

        launch = self.launch(start=Silent(), health_timeout_s=1.5)
        with self.assertRaises(StageFailed) as caught:
            launch.run()
        self.assertEqual(caught.exception.stage, "health")
        self.assertIn("n1, n2", caught.exception.detail)

    def test_ready_names_the_router_when_it_never_answers(self):
        launch = self.launch(ready_timeout_s=1.5)
        stub = launch._child

        def child(name, module, args):
            if name == "router":
                return subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
                )
            return stub(name, module, args)

        launch._child = child
        with self.assertRaises(StageFailed) as caught:
            launch.run()
        self.assertEqual(caught.exception.stage, "ready")
        self.assertEqual(caught.exception.detail, "no /ready 200 from the router after 1.5 seconds")

    def test_a_second_launch_into_the_same_run_directory_is_refused(self):
        Path(self.temp.name, "slurm-7").mkdir()
        Path(self.temp.name, "slurm-7", "fleet.json").write_text("{}")
        with self.assertRaises(StageFailed) as caught:
            self.launch().run()
        self.assertEqual(caught.exception.stage, "prepare")
        self.assertIn("fleet.json already exists", caught.exception.detail)


class CliTests(unittest.TestCase):
    def run_cli(self, env, argv=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = [
            "--model",
            "/models/m",
            "--served-model-name",
            "m",
            "--run-dir",
            tempfile.gettempdir(),
            "--slo-ttft-s",
            "2",
            "--slo-tpot-s",
            "0.05",
            *argv,
        ]
        with (
            patch.dict(os.environ, env, clear=True),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                code = cli.main(args, stop=threading.Event())
            except SystemExit as exit:
                code = exit.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_outside_an_allocation_the_launch_exits_2(self):
        code, _, stderr = self.run_cli({})
        self.assertEqual(code, 2)
        self.assertIn("narwhal-slurm: allocation: SLURM_JOB_ID is not set", stderr)

    def test_a_failed_stage_exits_1_and_names_the_stage(self):
        env = {**ENV, "SLURM_NODEID": "0"}
        failure = StageFailed("profile", "narwhal-profile exited with status 1")
        with (
            patch.object(cli, "resolve", return_value="10.0.0.1"),
            patch.object(Launch, "run", side_effect=failure),
        ):
            code, stdout, stderr = self.run_cli(env)
        self.assertEqual(code, 1)
        self.assertIn("node 0 of 2 (head)", stdout)
        self.assertIn("narwhal-slurm: profile: narwhal-profile exited with status 1", stderr)

    def test_fleet_setting_values_are_read_as_json_or_else_as_strings(self):
        required = ["--model", "/m", "--served-model-name", "m", "--run-dir", "/tmp"]
        required += ["--slo-ttft-s", "2", "--slo-tpot-s", "0.05"]
        values = ["serving.max_connections=1024", "serving.admission=open", "engine.tokenize=false"]
        args = cli.parser().parse_args(
            required + [arg for value in values for arg in ("--fleet-setting", value)]
        )
        self.assertEqual(
            cli.settings_from(args).fleet_settings,
            {
                "serving.max_connections": 1024,
                "serving.admission": "open",
                "engine.tokenize": False,
            },
        )

    def test_a_fleet_setting_on_a_launch_field_exits_2(self):
        code, _, stderr = self.run_cli(ENV, ["--fleet-setting", "slo.ttft_s=3"])
        self.assertEqual(code, 2)
        self.assertIn("narwhal-slurm: error: fleet setting 'slo.ttft_s'", stderr)
        self.assertIn("slo.ttft_s, which the launch writes", stderr)

    def test_a_fleet_setting_that_does_not_load_exits_2_before_any_engine_starts(self):
        for value, expected in (
            ("serving.admision=open", "admision"),
            ("serving.max_connections=many", "max_connections"),
        ):
            with (
                self.subTest(value=value),
                patch.object(cli, "resolve", return_value="10.0.0.1"),
                patch.object(Launch, "run") as run,
            ):
                code, _, stderr = self.run_cli(ENV, ["--fleet-setting", value])
                self.assertEqual(code, 2)
                self.assertIn("narwhal-slurm: fleet: ", stderr)
                self.assertIn(expected, stderr)
                run.assert_not_called()

    def test_a_non_finite_fleet_setting_exits_2_naming_the_path(self):
        for value, expected in (
            ("engine.first_token_timeout_s=NaN", "engine.first_token_timeout_s: NaN is not a"),
            ('serving={"max_connections":-Infinity}', "serving: -Infinity is not a finite"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(argparse.ArgumentTypeError, expected):
                    cli._setting(value)
                code, _, _ = self.run_cli(ENV, ["--fleet-setting", value])
                self.assertEqual(code, 2)

    def test_a_fleet_setting_path_may_be_given_once_and_not_inside_another(self):
        for values, expected in (
            (
                ["serving.admission=open", "serving.admission=predictive"],
                "'serving.admission' is also set by 'serving.admission'",
            ),
            (
                ["serving.admission=open", 'serving={"max_connections":128}'],
                "'serving.admission' is also set by 'serving'",
            ),
            (
                ["serving.admission=open", "serving.admission.mode=open"],
                "'serving.admission.mode' is also set by 'serving.admission'",
            ),
            (["x=1", "x=2"], "'x' is also set by 'x'"),
        ):
            with self.subTest(values=values):
                argv = [arg for value in values for arg in ("--fleet-setting", value)]
                code, _, stderr = self.run_cli(ENV, argv)
                self.assertEqual(code, 2)
                self.assertIn(f"narwhal-slurm: error: fleet setting {expected}", stderr)

    def test_a_fleet_setting_value_may_hold_an_equals_sign(self):
        args = cli.parser().parse_args(
            [
                *("--model", "/m", "--served-model-name", "m", "--run-dir", "/tmp"),
                *("--slo-ttft-s", "2", "--slo-tpot-s", "0.05"),
                *("--fleet-setting", "engine.headers=a=b"),
            ]
        )
        self.assertEqual(cli.settings_from(args).fleet_settings, {"engine.headers": "a=b"})

    def test_the_device_variable_cannot_be_set_through_engine_env(self):
        code, _, stderr = self.run_cli(ENV, ["--engine-env", "CUDA_VISIBLE_DEVICES=0"])
        self.assertEqual(code, 2)
        self.assertIn(
            "narwhal-slurm: error: --engine-env must not set CUDA_VISIBLE_DEVICES", stderr
        )

    def test_an_unknown_backend_is_an_argument_error(self):
        code, _, stderr = self.run_cli(ENV, ["--backend", "missing"])
        self.assertEqual(code, 2)
        self.assertIn("narwhal-slurm: error: unknown engine backend 'missing'", stderr)


if __name__ == "__main__":
    unittest.main()
