"""Packaging binds the wheel and role files to the inventory; install checks them on each host."""

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment.cli import forward_ports
from narwhal.deployment.hosts import Host, RemoteError
from narwhal.deployment.install import install, load_run, package, profiling_limits
from narwhal.deployment.launch_engine.backend import launcher
from narwhal.deployment.role_env import ENGINE_FIELDS, helpers
from narwhal.dev.cli import main
from tests.deployment.fixtures import cuda_engine, launch_document, runtime, synthetic_wheel
from tools.maintenance.check_publication import private_path

ROOT = Path(__file__).resolve().parents[2]


class LocalSSH:
    """Execute receiver scripts in temporary host directories with a synthetic installer."""

    def __init__(self, root, installed_version="0.13.0"):
        self.root = root
        self.uploads = []
        self.calls = []
        self.bin = root / "bin"
        self.bin.mkdir(exist_ok=True)
        launcher = self.bin / "python3"
        launcher.write_text(f"""#!/bin/sh
set -eu
test "$1 $2" = '-m venv'
mkdir -p "$3/bin"
cat > "$3/bin/python" <<'SCRIPT'
#!/bin/sh
if test "$1" = -c; then
  printf '%s\\n' {installed_version}
  exit 0
fi
test "$1 $2 $3" = '-m pip install'
test -f "$4"
printf 'install %s\\n' "$4" >> "$TEST_COUNTER"
SCRIPT
cat > "$3/bin/narwhal-check" <<'SCRIPT'
#!/bin/sh
exit 0
SCRIPT
chmod +x "$3/bin/python" "$3/bin/narwhal-check"
""")
        launcher.chmod(0o700)

    def run(self, host, stage, operation, script, payload=None, reason=None):
        home = self.root / host.id
        home.mkdir(exist_ok=True)
        self.calls.append((host.id, stage, operation))
        if payload:
            self.uploads.append((host.id, payload.name))
        env = dict(os.environ)
        env.update(PATH=f"{self.bin}:{env['PATH']}", TEST_COUNTER=str(home / "installs"))
        result = subprocess.run(
            ["sh", "-c", script],
            cwd=home,
            env=env,
            input=payload.read_bytes() if payload else b"",
            capture_output=True,
        )
        if result.returncode:
            raise RemoteError(host.id, stage, operation, home / "log", f": {result.stderr}")
        return result.stdout.decode().strip()


class PackageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.hosts = [
            Host("node-1", "NODE_1_SSH", "NODE_1_PASSWORD", ("router", "engine-1")),
            Host("node-2", "NODE_2_SSH", None, ("engine-2",)),
        ]
        self.env = {
            "NODE_1_SSH": "operator@one.example.invalid",
            "NODE_1_PASSWORD": "synthetic-management-secret",
            "NODE_2_SSH": "operator@two.example.invalid",
            **synthetic_wheel(self.root),
            **{f"NARWHAL_{field}": "synthetic-value" for field in ENGINE_FIELDS},
        }

    def inventory(self):
        path = self.root / "hosts.json"
        entries = [
            {"id": h.id, "ssh_env": h.ssh_env, "password_env": h.password_env, "roles": h.roles}
            for h in self.hosts
        ]
        path.write_text(json.dumps({"hosts": entries}))
        return path

    def package_run(self, backend=None):
        fleet = self.root / "fleet.json"
        document = {
            "engines": [
                {"iid": "n1", "url": "http://one.invalid"},
                {"iid": "n2", "url": "http://two.invalid"},
            ]
        }
        if backend:
            document["engine"] = {"backend": backend}
        fleet.write_text(json.dumps(document))
        self.env["NARWHAL_FLEET"] = str(fleet)
        launch = self.root / "launch.json"
        records = launch_document()
        serving = runtime()
        serving["max_sequences"] = 8
        records["engines"]["engine-1"]["runtime"] = serving
        records["engines"]["engine-2"] = records["engines"]["engine-1"]
        launch.write_text(json.dumps(records))
        self.env["NARWHAL_LAUNCH_CONFIG"] = str(launch)
        run = self.root / "packaged"
        package(self.hosts, self.env, run)
        return run, load_run(run, self.hosts, self.env)

    def test_colocated_cuda_engines_see_peer_gpus_after_their_own(self):
        self.hosts = [
            Host("node-1", "NODE_1_SSH", "NODE_1_PASSWORD", ("router", "engine-1", "engine-2"))
        ]
        engine = cuda_engine(launch_document()["engines"]["engine-1"])
        engine["runtime"] = runtime()
        engine["runtime"]["max_sequences"] = 8
        second = cuda_engine(json.loads(json.dumps(engine)), gpu="1")
        fleet = self.root / "fleet.json"
        fleet.write_text(
            json.dumps(
                {
                    "engines": [
                        {"iid": "n1", "url": "http://one.invalid"},
                        {"iid": "n2", "url": "http://one.invalid"},
                    ]
                }
            )
        )
        launch = self.root / "launch.json"
        launch.write_text(
            json.dumps(
                {
                    "schema": "narwhal.engine-launch",
                    "schema_version": 1,
                    "engines": {"engine-1": engine, "engine-2": second},
                }
            )
        )
        self.env.update(NARWHAL_FLEET=str(fleet), NARWHAL_LAUNCH_CONFIG=str(launch))
        package(self.hosts, self.env, self.root / "packaged")
        visible = {
            role: json.loads(
                (self.root / "packaged/node-1" / f"engine-launch.{role}.json").read_text()
            )["environment"]["CUDA_VISIBLE_DEVICES"]
            for role in ("engine-1", "engine-2")
        }
        self.assertEqual(visible, {"engine-1": "0,1", "engine-2": "1,0"})

    def test_profiling_limits_read_each_backend_sequence_limit(self):
        fleet = {"engines": [{"iid": "n1"}, {"iid": "n2"}]}
        vllm = {"runtime": {**runtime(), "max_sequences": 12}}
        sglang = {"runtime": {"backend": "sglang", "extra_args": []}}
        limits = profiling_limits({"engine-1": vllm, "engine-2": sglang}, fleet)
        self.assertEqual(limits["engines"], {"n1": 12, "n2": 256})
        sglang["runtime"] = {"backend": "sglang", "max_sequences": 24, "extra_args": []}
        limits = profiling_limits({"engine-1": vllm, "engine-2": sglang}, fleet)
        self.assertEqual(limits["engines"], {"n1": 12, "n2": 24})
        del vllm["runtime"]["max_sequences"]
        with self.assertRaisesRegex(ValueError, "^engine-1: runtime.max_sequences is required$"):
            profiling_limits({"engine-1": vllm, "engine-2": sglang}, fleet)

    def test_package_requires_the_named_wheel_and_its_sha256(self):
        wheel = Path(self.env["NARWHAL_WHEEL"])
        for change, message in (
            ({"NARWHAL_WHEEL": ""}, "Set NARWHAL_WHEEL to a narwhal_inference"),
            (
                {"NARWHAL_WHEEL": str(self.root / "other-0.1-py3-none-any.whl")},
                "a narwhal_inference",
            ),
            ({"NARWHAL_WHEEL_SHA256": "A" * 64}, "Set NARWHAL_WHEEL_SHA256"),
            ({"NARWHAL_WHEEL_SHA256": "0" * 64}, "NARWHAL_WHEEL differs from"),
            (
                {"NARWHAL_WHEEL": str(wheel.with_name(wheel.name.replace("13", "14")))},
                "regular file",
            ),
        ):
            with self.subTest(change=change):
                self.env.update(synthetic_wheel(self.root))
                self.env.update(change)
                with self.assertRaisesRegex(ValueError, message):
                    self.package_run()
                self.assertFalse((self.root / "packaged").exists())

    def test_engine_hosts_receive_the_helpers_of_the_packaged_wheel(self):
        wheel = Path(self.env["NARWHAL_WHEEL"])
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("narwhal/deployment/fabric_budget.py", "released helper\n")
        self.env["NARWHAL_WHEEL_SHA256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        with self.assertRaisesRegex(
            ValueError, "^NARWHAL_WHEEL lacks narwhal/backends/vllm/cache_capture_hook.py; "
        ):
            self.package_run()
        self.assertFalse((self.root / "packaged").exists())
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("narwhal/backends/vllm/cache_capture_hook.py", "released hook\n")
        self.env["NARWHAL_WHEEL_SHA256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
        run, _ = self.package_run()
        self.assertEqual((run / "node-2/fabric_budget.py").read_text(), "released helper\n")
        self.assertEqual((run / "node-2/cache_capture_hook.py").read_text(), "released hook\n")

    def test_the_package_records_the_wheel_as_every_role_revision(self):
        run, manifest = self.package_run()
        wheel = Path(self.env["NARWHAL_WHEEL"])
        self.assertEqual(manifest["wheel"], wheel.name)
        self.assertEqual(manifest["wheel_sha256"], self.env["NARWHAL_WHEEL_SHA256"])
        self.assertEqual((run / wheel.name).read_bytes(), wheel.read_bytes())
        self.assertEqual((run / wheel.name).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("source.bundle", manifest["hashes"])
        self.assertNotIn("revision", manifest)
        for path in run.glob("node-*/.env.*"):
            self.assertIn(
                f"export NARWHAL_DEPLOYMENT_REVISION={self.env['NARWHAL_WHEEL_SHA256']}\n",
                path.read_text(),
            )

    def test_each_backend_delivers_its_helper_snapshots_with_their_sha256(self):
        self.assertEqual(
            [helper.name for helper in helpers(launcher("sglang"))],
            ["fabric_budget.py", "transfer_measure.py"],
        )
        run, manifest = self.package_run()
        names = ["fabric_budget.py", "cache_capture_hook.py"]
        self.assertEqual(manifest["helpers"], names)
        self.assertEqual([helper.name for helper in helpers(launcher(None))], names)
        environment = (run / "node-2/.env.engine-2").read_text()
        for name in names:
            sha256 = hashlib.sha256((run / "node-2" / name).read_bytes()).hexdigest()
            self.assertIn(sha256, environment)
            self.assertEqual(manifest["hashes"][f"node-2/{name}"], sha256)
        self.assertFalse((run / "node-2/fleet.local.json").exists())

    def test_install_and_retry_transfer_the_wheel_and_install_once_per_host(self):
        run, manifest = self.package_run()
        transport = LocalSSH(self.root)
        install(self.hosts, manifest, run, transport)
        first = len(transport.calls)
        install(self.hosts, manifest, run, transport)
        wheel = Path(self.env["NARWHAL_WHEEL"]).name
        for host in self.hosts:
            self.assertEqual(transport.uploads.count((host.id, wheel)), 1)
            self.assertEqual(
                [op for owner, _, op in transport.calls[first:] if owner == host.id],
                ["verify files", "wheel installation"],
            )
            self.assertEqual(
                {stage for owner, stage, _ in transport.calls if owner == host.id}, {"install"}
            )
            self.assertEqual(
                (self.root / host.id / "installs").read_text().splitlines(),
                [f"install package/{wheel}"],
            )
        deployment = self.root / "node-1" / manifest["remote_dir"]
        self.assertEqual(
            (deployment / "installed").read_text(), self.env["NARWHAL_WHEEL_SHA256"] + "\n"
        )
        self.assertFalse((deployment / "checkout").exists())
        self.assertTrue((deployment / "runs/deployment/.env.router").is_file())
        self.assertTrue((deployment / "runs/deployment/.env.engine-1").is_file())
        effective_fleet = deployment / "runs/deployment/fleet.json"
        self.assertTrue(effective_fleet.is_file())
        limits = json.loads((deployment / "runs/deployment/profiling-limits.json").read_text())
        self.assertEqual(limits["engines"], {"n1": 8, "n2": 8})
        record = json.loads((deployment / "config/engine-launch.engine-1.json").read_text())
        self.assertEqual(record["tensor_parallel_size"], 2)
        engine_env = deployment / "runs/deployment/.env.engine-1"
        self.assertIn("NARWHAL_ENGINE_LAUNCH_CONFIG", engine_env.read_text())
        for path in (deployment / "runs/deployment").glob(".env.*"):
            self.assertNotIn("synthetic-management-secret", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((deployment / ".env.router").exists())
        edited = json.loads(effective_fleet.read_text())
        edited["engine_contract"] = {"example": "live-run-value"}
        effective_fleet.write_text(json.dumps(edited))
        install(self.hosts, manifest, run, transport)
        self.assertEqual(json.loads(effective_fleet.read_text()), edited)
        existing = deployment / "package/.env.router"
        existing.write_text("existing operator configuration")
        calls = len(transport.calls)
        with self.assertRaisesRegex(RemoteError, "node-1: blocked at verify files"):
            install(self.hosts, manifest, run, transport)
        self.assertTrue(all(host == "node-1" for host, _, _ in transport.calls[calls:]))
        self.assertEqual(existing.read_text(), "existing operator configuration")

    def test_install_refuses_a_wheel_that_differs_on_the_host(self):
        run, manifest = self.package_run()
        with self.assertRaisesRegex(RemoteError, "node-1: blocked at wheel installation"):
            install(self.hosts[:1], manifest, run, LocalSSH(self.root, installed_version="0.12.0"))
        deployment = self.root / "node-1" / manifest["remote_dir"]
        self.assertFalse((deployment / "installed").exists())
        (deployment / "package" / manifest["wheel"]).write_bytes(b"replaced on the host")
        with self.assertRaisesRegex(RemoteError, "node-1: blocked at verify files"):
            install(self.hosts[:1], manifest, run, LocalSSH(self.root))
        self.assertFalse((deployment / "installed").exists())

    def test_engine_shell_runs_the_packaged_helper_from_the_deployment_directory(self):
        run, manifest = self.package_run()
        transport = LocalSSH(self.root)
        install(self.hosts[:1], manifest, run, transport)
        deployment = self.root / "node-1" / manifest["remote_dir"]
        tool = deployment / "runs/deployment-tools/fabric_budget.py"
        self.assertEqual(tool.read_bytes(), (run / "node-1/fabric_budget.py").read_bytes())
        self.assertEqual(tool.stat().st_mode & 0o777, 0o600)
        result = subprocess.run(
            [
                "bash",
                "-c",
                """set -eu
. ./runs/deployment/.env.engine-1
check() { test "$(sha256sum "$1" | cut -d' ' -f1)" = "$2"; }
check "$NARWHAL_FABRIC_BUDGET_TOOL" "$NARWHAL_FABRIC_BUDGET_SHA256"
check "$NARWHAL_CACHE_CAPTURE_HOOK" "$NARWHAL_CACHE_CAPTURE_HOOK_SHA256"
python3 -I "$NARWHAL_FABRIC_BUDGET_TOOL" --help
""",
            ],
            cwd=deployment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("calculate", result.stdout)
        self.assertEqual(transport.uploads.count(("node-1", "fabric_budget.py")), 1)
        (run / "node-1/fabric_budget.py").write_text("changed after packaging")
        with self.assertRaisesRegex(ValueError, "Packaged input changed"):
            load_run(run, self.hosts, self.env)

    def test_packaged_inputs_and_host_assignments_are_bound_to_the_run(self):
        run, _ = self.package_run()
        self.env["NODE_2_SSH"] = "operator@replacement.example.invalid"
        with self.assertRaisesRegex(ValueError, "Host assignments changed"):
            load_run(run, self.hosts, self.env)
        self.env["NODE_2_SSH"] = "operator@two.example.invalid"
        wheel = run / Path(self.env["NARWHAL_WHEEL"]).name
        original = wheel.read_bytes()
        wheel.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Packaged input changed"):
            load_run(run, self.hosts, self.env)
        wheel.write_bytes(original)
        (run / "node-1/.env.router").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Packaged input changed"):
            load_run(run, self.hosts, self.env)

    def test_role_selection_deduplicates_router_and_engine_installation(self):
        path = self.inventory()
        run, _ = self.package_run()
        transport = LocalSSH(self.root)
        with (
            patch.dict(os.environ, self.env),
            patch("narwhal.deployment.cli.SSH", return_value=transport),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            arguments = ["deploy", "install", "--hosts", str(path), "--run", str(run)]
            self.assertEqual(main([*arguments, "--role", "router", "--role", "engine-1"]), 0)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main([*arguments, "--role", "engine-9"]), 2)
            self.assertIn("narwhal: deploy install: Select --role values", stderr.getvalue())
        self.assertEqual({host for host, _, _ in transport.calls}, {"node-1"})
        wheel = Path(self.env["NARWHAL_WHEEL"]).name
        self.assertEqual(transport.uploads.count(("node-1", wheel)), 1)

    def test_private_host_inventory_is_excluded_from_publication(self):
        self.assertTrue(private_path("config/ssh.known_hosts"))
        self.assertTrue(private_path("config/deployment.env"))
        self.assertTrue(private_path("config/hosts.local.json"))
        self.assertFalse(private_path("config/hosts.example.json"))
        self.assertTrue(private_path("config/engine-launch.local.json"))
        self.assertFalse(private_path("config/engine-launch.example.json"))

    def test_documented_gpu_discovery_uses_pci_vendor_on_the_engine_host(self):
        blocks = re.findall(
            r"```bash\n(.*?)\n```", (ROOT / "docs/deploy/03-Validate-Engines.md").read_text(), re.S
        )
        block = next(b for b in blocks if "/sys/bus/pci/devices" in b)
        for vendor, command in (("0x10de", "nvidia-smi"), ("0x1002", "rocminfo"), ("0x1a03", None)):
            with self.subTest(vendor=vendor), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                pci = root / "pci"
                device = pci / "synthetic-device"
                device.mkdir(parents=True)
                (device / "class").write_text("0x030200")
                (device / "vendor").write_text(vendor)
                tool = root / "nvidia-smi"
                tool.write_text('#!/bin/sh\nprintf nvidia-smi > "$TEST_OBSERVED"\n')
                tool.chmod(0o700)
                tool = root / "rocminfo"
                tool.write_text('#!/bin/sh\nprintf rocminfo > "$TEST_OBSERVED"\n')
                tool.chmod(0o700)
                observed = root / "observed"
                result = subprocess.run(
                    ["bash", "--noprofile", "--norc"],
                    input=block.replace("/sys/bus/pci/devices", str(pci)),
                    env={"PATH": f"{root}:{os.environ['PATH']}", "TEST_OBSERVED": str(observed)},
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode == 0, command is not None)
                if command:
                    self.assertEqual(observed.read_text(), command)
                else:
                    self.assertFalse(observed.exists())


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.hosts = [
            Host("node-1", "NODE_1_SSH", None, ("router", "engine-1")),
            Host("node-2", "NODE_2_SSH", None, ("engine-2",)),
        ]

    def test_tunnel_selects_shared_router_host_and_checks_local_ports(self):
        with (
            patch("narwhal.deployment.cli.load_hosts", return_value=self.hosts),
            patch("narwhal.deployment.cli.SSH") as ssh,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(["deploy", "tunnel", "--forward", "18000:8000", "--forward", "19090:9090"]),
                0,
            )
            ssh.return_value.tunnel.assert_called_once_with(
                self.hosts[0],
                ["127.0.0.1:18000:127.0.0.1:8000", "127.0.0.1:19090:127.0.0.1:9090"],
            )
            ssh.return_value.tunnel.reset_mock()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(
                        ["deploy", "tunnel", "--forward", "18000:8000", "--forward", "18000:9090"]
                    ),
                    2,
                )
            ssh.return_value.tunnel.assert_not_called()
            self.assertEqual(
                main(["deploy", "tunnel", "--remote-address", "::1", "--forward", "18000:8000"]),
                0,
            )
            self.assertEqual(
                ssh.return_value.tunnel.call_args.args[1], ["127.0.0.1:18000:[::1]:8000"]
            )
            ssh.return_value.tunnel.side_effect = KeyboardInterrupt
            self.assertEqual(main(["deploy", "tunnel", "--forward", "18000:8000"]), 130)

    def test_interactive_actions_refuse_a_command_result(self):
        with (
            patch("narwhal.deployment.cli.load_hosts", return_value=self.hosts),
            patch("narwhal.deployment.cli.SSH") as ssh,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = main(["deploy", "shell", "--role", "engine-2", "--format", "json"])
        self.assertEqual(code, 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["operation"], "deploy shell")
        self.assertEqual(result["errors"][0]["code"], "invalid_input")
        ssh.return_value.shell.assert_not_called()

    def test_access_reports_each_host_and_a_remote_failure_names_it(self):
        with (
            patch("narwhal.deployment.cli.load_hosts", return_value=self.hosts),
            patch("narwhal.deployment.cli.SSH") as ssh,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(main(["deploy", "access"]), 0)
            self.assertEqual(
                [call.args[:3] for call in ssh.return_value.run.call_args_list],
                [(host, "access", "management access") for host in self.hosts],
            )
            self.assertIn("node-1: router, engine-1; host key matched", stdout.getvalue())
            ssh.return_value.run.side_effect = RemoteError(
                "node-2", "access", "management access", Path("runs/access-x/node-2.log")
            )
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["deploy", "access"]), 1)
            self.assertIn(
                "narwhal: deploy access: node-2: blocked at management access", stderr.getvalue()
            )
        with (
            patch("narwhal.deployment.cli.load_hosts", return_value=self.hosts),
            patch("narwhal.deployment.cli.SSH") as ssh,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            ssh.return_value.run.side_effect = RemoteError(
                "node-2", "access", "management access", Path("runs/access-x/node-2.log")
            )
            self.assertEqual(main(["deploy", "access", "--format", "json"]), 4)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["errors"][0]["stage"], "access")
        self.assertEqual(result["errors"][0]["context"]["host"], "node-2")

    def test_tunnel_ports_reject_ambiguous_or_invalid_forward_specs(self):
        self.assertEqual(forward_ports("18000:8000"), (18000, 8000))
        for value in (
            "0:8000",
            "18000:65536",
            "*:8000",
            "18000:host:8000",
            "-L:8000",
            "18000:8000\n",
        ):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                forward_ports(value)


if __name__ == "__main__":
    unittest.main()
