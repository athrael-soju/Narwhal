"""Host grouping, private authentication and retries for deployment preparation."""

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.check_publication import private_path
from tools.deploy_hosts import SSH, Host, install, load_hosts, load_run, main, prepare
from tools.prepare_host_env import ENGINE_FIELDS
from tools.tests.test_engine_launch import launch_document

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
            json.dumps({"engines": [{"url": "http://one.invalid"}, {"url": "http://two.invalid"}]})
        )
        self.env["NARWHAL_FLEET"] = str(fleet)
        launch = root / "launch.json"
        document = launch_document()
        document["engines"]["engine-2"] = document["engines"]["engine-1"]
        launch.write_text(json.dumps(document))
        self.env["NARWHAL_LAUNCH_CONFIG"] = str(launch)
        run = root / "prepared"
        prepare(self.hosts, self.env, run, ROOT)
        return run, load_run(run, self.hosts, self.env)

    def test_roles_resolve_to_one_authentication_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            hosts = load_hosts(self.inventory(Path(folder) / "hosts.json"), self.env)
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
            self.assertTrue((checkout / ".env.router").is_file())
            self.assertTrue((checkout / ".env.engine-1").is_file())
            record = json.loads((checkout / "config/engine-launch.engine-1.json").read_text())
            self.assertEqual(record["tensor_parallel_size"], 2)
            self.assertIn("NARWHAL_ENGINE_LAUNCH_CONFIG", (checkout / ".env.engine-1").read_text())
            for path in checkout.glob(".env.*"):
                if path.name != ".env.example":
                    self.assertNotIn("synthetic-management-secret", path.read_text())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
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
. ./.env.engine-1
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
                patch("tools.deploy_hosts.SSH", return_value=transport),
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
        self.assertTrue(private_path("config/hosts.local.json"))
        self.assertFalse(private_path("config/hosts.example.json"))
        self.assertTrue(private_path("config/engine-launch.local.json"))
        self.assertFalse(private_path("config/engine-launch.example.json"))

    def test_documented_gpu_discovery_uses_pci_vendor_on_the_engine_host(self):
        blocks = re.findall(r"```bash\n(.*?)\n```", (ROOT / "docs/Deploy.md").read_text(), re.S)
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
            with patch("tools.deploy_hosts.subprocess.run", side_effect=execute):
                for host in self.hosts:
                    ssh.run(host, "test", "cat", payload=payload)
            self.assertEqual(seen, ["sshpass", "ssh"])
            for path in (root / "logs").iterdir():
                self.assertNotIn(self.env["NODE_1_PASSWORD"], path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
