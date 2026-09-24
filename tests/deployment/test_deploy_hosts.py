"""Host grouping, private authentication and retries for deployment preparation."""

import argparse
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.deployment.fixtures import launch_document, runtime
from tools.deployment.deploy_hosts import (
    forward_ports,
    install,
    load_run,
    main,
    prepare,
)
from tools.deployment.host_access import SSH, Host, load_hosts
from tools.deployment.prepare_host_env import ENGINE_FIELDS
from tools.maintenance.check_publication import private_path

ROOT = Path(__file__).resolve().parents[2]


class LocalSSH:
    """Execute receiver scripts in temporary host directories with a synthetic installer."""

    def __init__(self, root):
        self.root = root
        self.uploads = []
        self.calls = []
        self.bin = root / "bin"
        self.bin.mkdir()
        launcher = self.bin / "python3"
        launcher.write_text("""#!/bin/sh
set -eu
test "$1 $2" = '-m venv'
mkdir -p "$3/bin"
cat > "$3/bin/python" <<'SCRIPT'
#!/bin/sh
printf 'install\n' >> "$TEST_COUNTER"
SCRIPT
cat > "$3/bin/narwhal-check" <<'SCRIPT'
#!/bin/sh
exit 0
SCRIPT
chmod +x "$3/bin/python" "$3/bin/narwhal-check"
""")
        launcher.chmod(0o700)

    def run(self, host, gate, script, payload=None, interactive=False):
        home = self.root / host.id
        home.mkdir(exist_ok=True)
        self.calls.append((host.id, gate))
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
            raise ValueError(f"{host.id}: {gate}: {result.stderr.decode()}")
        return result.stdout.decode().strip()


class HostDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.hosts = [
            Host("node-1", "NODE_1_SSH", "NODE_1_PASSWORD", ("router", "engine-1")),
            Host("node-2", "NODE_2_SSH", None, ("engine-2",)),
        ]
        self.env = {
            "NODE_1_SSH": "operator@one.example.invalid",
            "NODE_1_PASSWORD": "synthetic-management-secret",
            "NODE_2_SSH": "operator@two.example.invalid",
            "NARWHAL_DEPLOYMENT_REVISION": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            **{f"NARWHAL_{field}": "synthetic-value" for field in ENGINE_FIELDS},
        }

    def inventory(self, path, entries=None):
        entries = entries or [
            {"id": h.id, "ssh_env": h.ssh_env, "password_env": h.password_env, "roles": h.roles}
            for h in self.hosts
        ]
        path.write_text(json.dumps({"hosts": entries}))
        return path

    def prepare_run(self, root):
        fleet = root / "fleet.json"
        fleet.write_text(
            json.dumps(
                {
                    "engines": [
                        {"iid": "n1", "url": "http://one.invalid"},
                        {"iid": "n2", "url": "http://two.invalid"},
                    ]
                }
            )
        )
        self.env["NARWHAL_FLEET"] = str(fleet)
        launch = root / "launch.json"
        document = launch_document()
        serving = runtime()
        serving["extra_args"].extend(("--max-num-seqs", "8"))
        document["engines"]["engine-1"]["runtime"] = serving
        document["engines"]["engine-2"] = document["engines"]["engine-1"]
        launch.write_text(json.dumps(document))
        self.env["NARWHAL_LAUNCH_CONFIG"] = str(launch)
        run = root / "prepared"
        prepare(self.hosts, self.env, run, ROOT)
        return run, load_run(run, self.hosts, self.env)

    def test_roles_resolve_to_one_authentication_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = self.inventory(Path(folder) / "hosts.json")
            contents = inventory.read_text()
            self.assertIn('"password_env": "NODE_1_PASSWORD"', contents)
            self.assertNotIn(self.env["NODE_1_PASSWORD"], contents)
            hosts = load_hosts(inventory, self.env)
            router = next(h for h in hosts if "router" in h.roles)
            engine = next(h for h in hosts if "engine-1" in h.roles)
            self.assertIs(router, engine)
            self.assertEqual(len(hosts), 2)
            self.env["NODE_2_SSH"] = self.env["NODE_1_SSH"]
            with self.assertRaisesRegex(ValueError, "combine roles"):
                load_hosts(Path(folder) / "hosts.json", self.env)

    def test_duplicate_roles_and_missing_credentials_fail_locally(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.inventory(Path(folder) / "hosts.json")
            del self.env["NODE_1_PASSWORD"]
            with self.assertRaisesRegex(ValueError, "NODE_1_PASSWORD is unset"):
                load_hosts(path, self.env)
            self.env["NODE_1_PASSWORD"] = "synthetic"
            entries = json.loads(path.read_text())["hosts"]
            entries[1]["roles"].append("router")
            self.inventory(path, entries)
            with self.assertRaisesRegex(ValueError, "one owner"):
                load_hosts(path, self.env)

    def test_install_and_retry_transfer_source_and_install_once_per_host(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, manifest = self.prepare_run(root)
            transport = LocalSSH(root)
            install(self.hosts, manifest, run, transport)
            install(self.hosts, manifest, run, transport)
            for host in self.hosts:
                self.assertEqual(transport.uploads.count((host.id, "source.bundle")), 1)
                self.assertEqual(
                    (root / host.id / "installs").read_text().splitlines(), ["install"]
                )
            checkout = root / "node-1" / manifest["remote_dir"] / "checkout"
            self.assertTrue((checkout / "runs/deployment/.env.router").is_file())
            self.assertTrue((checkout / "runs/deployment/.env.engine-1").is_file())
            effective_fleet = checkout / "runs/deployment/fleet.json"
            self.assertTrue(effective_fleet.is_file())
            limits = json.loads((checkout / "runs/deployment/profiling-limits.json").read_text())
            self.assertEqual(limits["engines"], {"n1": 8, "n2": 8})
            record = json.loads((checkout / "config/engine-launch.engine-1.json").read_text())
            self.assertEqual(record["tensor_parallel_size"], 2)
            engine_env = checkout / "runs/deployment/.env.engine-1"
            self.assertIn("NARWHAL_ENGINE_LAUNCH_CONFIG", engine_env.read_text())
            for path in (checkout / "runs/deployment").glob(".env.*"):
                self.assertNotIn("synthetic-management-secret", path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse((checkout / ".env.router").exists())
            self.assertFalse((checkout / "config/fleet.local.json").exists())
            edited = json.loads(effective_fleet.read_text())
            edited["engine_contract"] = {"example": "live-run-value"}
            effective_fleet.write_text(json.dumps(edited))
            install(self.hosts, manifest, run, transport)
            self.assertEqual(json.loads(effective_fleet.read_text()), edited)
            existing = root / "node-1" / manifest["remote_dir"] / ".env.router"
            existing.write_text("existing operator configuration")
            calls = len(transport.calls)
            with self.assertRaisesRegex(ValueError, "node-1: verify file"):
                install(self.hosts, manifest, run, transport)
            self.assertTrue(all(host == "node-1" for host, _ in transport.calls[calls:]))
            self.assertEqual(existing.read_text(), "existing operator configuration")

    def test_engine_shell_runs_the_prepared_tool_outside_the_application_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, manifest = self.prepare_run(root)
            transport = LocalSSH(root)
            install(self.hosts[:1], manifest, run, transport)
            checkout = root / "node-1" / manifest["remote_dir"] / "checkout"
            tool = checkout / "runs/deployment-tools/fabric_budget.py"
            self.assertEqual(tool.read_bytes(), (run / "node-1/fabric_budget.py").read_bytes())
            self.assertEqual(tool.stat().st_mode & 0o777, 0o600)
            bundled_tool = subprocess.run(
                ["git", "cat-file", "-e", "HEAD:runs/deployment-tools/fabric_budget.py"],
                cwd=checkout,
                capture_output=True,
            )
            self.assertNotEqual(bundled_tool.returncode, 0)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    """set -eu
. ./runs/deployment/.env.engine-1
test "$(sha256sum "$NARWHAL_FABRIC_BUDGET_TOOL" | cut -d' ' -f1)" = "$NARWHAL_FABRIC_BUDGET_SHA256"
python3 "$NARWHAL_FABRIC_BUDGET_TOOL" --help
test "$(sha256sum "$NARWHAL_ENGINE_LAUNCHER" | cut -d' ' -f1)" = "$NARWHAL_ENGINE_LAUNCHER_SHA256"
python3 "$NARWHAL_ENGINE_LAUNCHER" --help
""",
                ],
                cwd=checkout,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("calculate", result.stdout)
            self.assertEqual(transport.uploads.count(("node-1", "fabric_budget.py")), 1)
            (run / "node-1/fabric_budget.py").write_text("changed after preparation")
            with self.assertRaisesRegex(ValueError, "Prepared input changed"):
                load_run(run, self.hosts, self.env)

    def test_prepared_inputs_and_host_assignments_are_bound_to_the_run(self):
        with tempfile.TemporaryDirectory() as folder:
            run, _ = self.prepare_run(Path(folder))
            self.env["NODE_2_SSH"] = "operator@replacement.example.invalid"
            with self.assertRaisesRegex(ValueError, "Host assignments changed"):
                load_run(run, self.hosts, self.env)
            self.env["NODE_2_SSH"] = "operator@two.example.invalid"
            (run / "node-1/.env.router").write_text("changed")
            with self.assertRaisesRegex(ValueError, "Prepared input changed"):
                load_run(run, self.hosts, self.env)

    def test_role_selection_deduplicates_router_and_engine_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = self.inventory(root / "hosts.json")
            run, _ = self.prepare_run(root)
            transport = LocalSSH(root)
            with (
                patch.dict(os.environ, self.env),
                patch("tools.deployment.deploy_hosts.SSH", return_value=transport),
            ):
                self.assertEqual(
                    main(
                        [
                            "--hosts",
                            str(path),
                            "install",
                            "--run",
                            str(run),
                            "--role",
                            "router",
                            "--role",
                            "engine-1",
                        ]
                    ),
                    0,
                )
            self.assertEqual({host for host, _ in transport.calls}, {"node-1"})
            self.assertEqual(transport.uploads.count(("node-1", "source.bundle")), 1)

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

    def test_ssh_password_uses_descriptor_and_strict_keys_with_payload_intact(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            known_hosts = root / "known-hosts"
            known_hosts.touch()
            self.env["NARWHAL_SSH_KNOWN_HOSTS"] = str(known_hosts)
            payload = root / "payload"
            payload.write_bytes(b"synthetic-role-file")
            seen = []

            def execute(args, **kwargs):
                self.assertIn("StrictHostKeyChecking=yes", args)
                self.assertIn("GlobalKnownHostsFile=/dev/null", args)
                self.assertNotIn(self.env["NODE_1_PASSWORD"], args)
                self.assertNotIn("NODE_1_PASSWORD", kwargs["env"])
                self.assertFalse(any(key.startswith("NARWHAL_") for key in kwargs["env"]))
                self.assertEqual(kwargs["stdin"].read(), payload.read_bytes())
                if args[0] == "sshpass":
                    fd = kwargs["pass_fds"][0]
                    self.assertEqual(os.read(fd, 1024), b"synthetic-management-secret\n")
                else:
                    self.assertEqual(kwargs["pass_fds"], ())
                seen.append(args[0])
                return subprocess.CompletedProcess(args, 0, b"", b"")

            ssh = SSH(self.env, root / "logs")
            with patch("tools.deployment.deploy_hosts.subprocess.run", side_effect=execute):
                for host in self.hosts:
                    ssh.run(host, "test", "cat", payload=payload)
            self.assertEqual(seen, ["sshpass", "ssh"])
            for path in (root / "logs").iterdir():
                self.assertNotIn(self.env["NODE_1_PASSWORD"], path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_tunnel_reuses_inventory_authentication_and_opens_no_remote_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            known_hosts = root / "known-hosts"
            known_hosts.touch()
            self.env["NARWHAL_SSH_KNOWN_HOSTS"] = str(known_hosts)
            ssh = SSH(self.env, root / "logs")

            def execute(args, **kwargs):
                self.assertIn("StrictHostKeyChecking=yes", args)
                self.assertIn(f"UserKnownHostsFile={known_hosts}", args)
                self.assertIn("ExitOnForwardFailure=yes", args)
                self.assertIn("ServerAliveInterval=30", args)
                self.assertIn("-N", args)
                self.assertNotIn("-t", args)
                self.assertEqual(args[args.index("-L") + 1], "127.0.0.1:18000:127.0.0.1:8000")
                self.assertIn(args[-1], [self.env[h.ssh_env] for h in self.hosts])
                self.assertIsNone(kwargs["stderr"])
                self.assertNotIn(self.env["NODE_1_PASSWORD"], args)
                self.assertNotIn("NODE_1_PASSWORD", kwargs["env"])
                if args[0] == "sshpass":
                    self.assertEqual(
                        os.read(kwargs["pass_fds"][0], 1024), b"synthetic-management-secret\n"
                    )
                else:
                    self.assertEqual(kwargs["pass_fds"], ())
                return subprocess.CompletedProcess(args, 0, b"", None)

            with patch("tools.deployment.deploy_hosts.subprocess.run", side_effect=execute):
                for host in self.hosts:
                    ssh.run(host, "service tunnel", "", forwards=["127.0.0.1:18000:127.0.0.1:8000"])
            for path in (root / "logs").iterdir():
                self.assertNotIn(self.env["NODE_1_PASSWORD"], path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with (
                patch(
                    "tools.deployment.deploy_hosts.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 255, b"", None),
                ),
                self.assertRaisesRegex(ValueError, "blocked at service tunnel"),
            ):
                ssh.run(
                    self.hosts[0],
                    "service tunnel",
                    "",
                    forwards=["127.0.0.1:18000:127.0.0.1:8000"],
                )

    def test_tunnel_selects_shared_router_host_and_checks_local_ports(self):
        with (
            patch("tools.deployment.deploy_hosts.load_hosts", return_value=self.hosts),
            patch("tools.deployment.deploy_hosts.SSH") as ssh,
            patch.dict(os.environ, self.env),
        ):
            self.assertEqual(
                main(["tunnel", "--forward", "18000:8000", "--forward", "19090:9090"]), 0
            )
            ssh.return_value.run.assert_called_once_with(
                self.hosts[0],
                "service tunnel",
                "",
                forwards=[
                    "127.0.0.1:18000:127.0.0.1:8000",
                    "127.0.0.1:19090:127.0.0.1:9090",
                ],
            )
            ssh.return_value.run.reset_mock()
            with self.assertRaises(SystemExit):
                main(["tunnel", "--forward", "18000:8000", "--forward", "18000:9090"])
            ssh.return_value.run.assert_not_called()
            self.assertEqual(
                main(["tunnel", "--remote-address", "::1", "--forward", "18000:8000"]), 0
            )
            self.assertEqual(
                ssh.return_value.run.call_args.kwargs["forwards"],
                ["127.0.0.1:18000:[::1]:8000"],
            )

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
