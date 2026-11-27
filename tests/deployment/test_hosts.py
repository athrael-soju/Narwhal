"""SSH access pins host keys, keeps passwords off argument lists and bounds every command."""

import base64
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import remote
from narwhal.deployment.hosts import (
    HOST_GRACE_S,
    SSH,
    Host,
    RemoteError,
    RemoteTimeout,
    load_hosts,
    stage_limit,
)
from narwhal.deployment.launch_engine import checkpoint

PASSWORD = "synthetic-management-secret"


def askpass_answer(helper):
    """Return what the askpass helper answers, read without starting a process.

    The tests that patch `subprocess` cannot run the helper; the tests with a fake `ssh` on
    PATH run it. The helper reads the password from a FIFO, never from a file.
    """
    channel = Path(shlex.split(Path(helper).read_text().splitlines()[1])[-1])
    assert stat.S_ISFIFO(channel.stat().st_mode), f"{channel} is not a FIFO"
    pipe = os.open(channel, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return os.read(pipe, 1024)
    finally:
        os.close(pipe)


# Stands in for `ssh` and the host's sshd: it runs the command string in a session of its own
# with bash as the login shell, which runs a lone command in place of itself as sshd's login
# shells do, and exits with 255 when a signal ended that shell, as OpenSSH does.
SSHD_LIKE = """#!/usr/bin/env python3
import os, subprocess, sys
helper = os.environ.get("SSH_ASKPASS")
if helper and subprocess.run([helper, "password:"], capture_output=True).stdout != %r:
    sys.exit(255)
shell = subprocess.Popen(["bash", "-c", sys.argv[-1]], start_new_session=True)
status = shell.wait()
sys.exit(255 if status < 0 else status)
"""


class FakeProcess:
    """Stands in for one OpenSSH process, recording how the client started it."""

    def __init__(self, test, result, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.pid = 0
        self.returncode, self.stdout, self.stderr = result
        test.started.append(self)
        helper = kwargs["env"].get("SSH_ASKPASS")
        self.password = helper and askpass_answer(helper)
        self.stdin = kwargs["stdin"].read()

    def communicate(self, timeout=None):
        self.timeout = timeout
        return self.stdout, self.stderr


class SshTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.known_hosts = self.root / "known-hosts"
        self.known_hosts.write_text("synthetic.invalid ssh-ed25519 synthetic-public-key\n")
        self.hosts = [
            Host("node-1", "NODE_1_SSH", "NODE_1_PASSWORD", ("router", "engine-1")),
            Host("node-2", "NODE_2_SSH", None, ("engine-2",)),
        ]
        self.env = {
            "NODE_1_SSH": "operator@one.example.invalid",
            "NODE_1_PASSWORD": PASSWORD,
            "NODE_2_SSH": "operator@two.example.invalid",
            "NARWHAL_SSH_KNOWN_HOSTS": str(self.known_hosts),
        }
        self.started = []

    def popen(self, result=(0, b"", b"")):
        return patch(
            "narwhal.deployment.hosts.subprocess.Popen",
            side_effect=lambda args, **kwargs: FakeProcess(self, result, args, **kwargs),
        )

    def assert_private_logs(self):
        for path in (self.root / "logs").iterdir():
            self.assertTrue(path.is_file(), f"{path} remains after the command")
            self.assertNotIn(PASSWORD, path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def fake_ssh(self, body):
        """Put an `ssh` on PATH that checks the askpass password, then runs `body`."""
        tools = self.root / "bin"
        tools.mkdir(exist_ok=True)
        fake = tools / "ssh"
        fake.write_text(
            "#!/bin/sh\n"
            'if test -n "${SSH_ASKPASS:-}"; then\n'
            f'  test "$("$SSH_ASKPASS" password:)" = {shlex.quote(PASSWORD)} || exit 255\n'
            "fi\n"
            "for last; do :; done\n" + body + "\n"
        )
        fake.chmod(0o700)
        return patch.dict(os.environ, {"PATH": f"{tools}:{os.environ['PATH']}"})

    def assert_stopped(self, pid_file):
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail("a process of the remote command survived the time limit")

    def test_every_command_pins_host_keys_and_passes_the_password_through_askpass(self):
        payload = self.root / "payload"
        payload.write_bytes(b"synthetic-role-file")
        ssh = SSH(self.env, self.root / "logs")
        with self.popen():
            for host in self.hosts:
                ssh.run(host, "install", "test", "cat", payload=payload)
        password, key = self.started
        for process in self.started:
            args = process.args
            self.assertEqual(args[0], "ssh")
            for option in (
                "StrictHostKeyChecking=yes",
                "UpdateHostKeys=no",
                "GlobalKnownHostsFile=/dev/null",
                f"UserKnownHostsFile={self.known_hosts.resolve()}",
                "KnownHostsCommand=none",
                "VerifyHostKeyDNS=no",
                "ControlMaster=no",
                "ControlPath=none",
                "ControlPersist=no",
                "ProxyJump=none",
                "ProxyCommand=none",
                "PermitLocalCommand=no",
                "RemoteCommand=none",
                "ForwardAgent=no",
                "ClearAllForwardings=yes",
            ):
                self.assertEqual(args[args.index(option) - 1], "-o", option)
            self.assertIn("-T", args)
            self.assertNotIn("StrictHostKeyChecking=accept-new", args)
            self.assertNotIn(PASSWORD, " ".join(args))
            self.assertNotIn(PASSWORD, "".join(process.kwargs["env"].values()))
            self.assertLessEqual(
                set(process.kwargs["env"]),
                {
                    "PATH",
                    "HOME",
                    "TERM",
                    "SSH_AUTH_SOCK",
                    "XDG_RUNTIME_DIR",
                    "SSH_ASKPASS",
                    "SSH_ASKPASS_REQUIRE",
                },
            )
            self.assertEqual(args[args.index("SetEnv=NARWHAL_DEPLOY=1") - 1], "-o")
            self.assertFalse(any(name.startswith("NARWHAL_") for name in process.kwargs["env"]))
            self.assertTrue(process.kwargs["start_new_session"])
            self.assertEqual(process.stdin, payload.read_bytes())
            self.assertEqual(process.timeout, 1800 + HOST_GRACE_S)
            self.assertEqual(shlex.split(args[-1])[:2], ["sh", "-c"])
            self.assertTrue(
                shlex.split(args[-1])[2].endswith("\ntimeout -k 5 1800.000s sh -c cat; exit $?")
            )
        self.assertEqual(password.password, (PASSWORD + "\n").encode())
        self.assertEqual(password.kwargs["env"]["SSH_ASKPASS_REQUIRE"], "force")
        self.assertIn("NumberOfPasswordPrompts=1", password.args)
        self.assertNotIn("BatchMode=yes", password.args)
        self.assertFalse(Path(password.kwargs["env"]["SSH_ASKPASS"]).exists())
        self.assertIsNone(key.password)
        self.assertIn("BatchMode=yes", key.args)
        self.assert_private_logs()

    @unittest.skipUnless(shutil.which("ssh"), "needs the OpenSSH client")
    def test_ssh_config_cannot_weaken_the_host_key_check(self):
        config = self.root / "ssh_config"
        config.write_text(
            "Host *\n"
            "  KnownHostsCommand /bin/echo synthetic-key\n"
            "  VerifyHostKeyDNS yes\n"
            "  ControlMaster auto\n"
            "  ControlPath /tmp/synthetic-%r@%h:%p\n"
            "  ControlPersist yes\n"
            "  ProxyJump unpinned.example.invalid\n"
            "  StrictHostKeyChecking no\n"
            "  UserKnownHostsFile /tmp/synthetic-known-hosts\n"
            "  GlobalKnownHostsFile /tmp/synthetic-global-known-hosts\n"
            "  UpdateHostKeys yes\n"
            "  PermitLocalCommand yes\n"
            "  LocalCommand touch /tmp/synthetic-local-command\n"
            "  RemoteCommand id\n"
            "  ForwardAgent yes\n"
            "  BatchMode no\n"
            "  SetEnv SYNTHETIC_SECRET=1\n"
        )

        def effective(ssh, host):
            # `ssh -G` prints the configuration a connection would use, without connecting.
            args = ssh._arguments(host, ["-T"])
            output = subprocess.run(
                ["ssh", "-F", str(config), "-G", *args[1:]],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            return dict(line.split(" ", 1) for line in output.splitlines())

        values = effective(SSH(self.env, self.root / "logs"), self.hosts[1])
        for name, value in (
            ("stricthostkeychecking", "true"),
            ("userknownhostsfile", str(self.known_hosts.resolve())),
            ("globalknownhostsfile", "/dev/null"),
            ("verifyhostkeydns", "false"),
            ("updatehostkeys", "false"),
            ("controlmaster", "false"),
            ("controlpersist", "no"),
            ("permitlocalcommand", "no"),
            ("forwardagent", "no"),
            ("batchmode", "yes"),
            ("setenv", "NARWHAL_DEPLOY=1"),
        ):
            self.assertEqual(values[name], value, name)
        for name in (
            "knownhostscommand",
            "controlpath",
            "proxyjump",
            "proxycommand",
            "remotecommand",
        ):
            self.assertNotIn(name, values)
        self.env["NARWHAL_SSH_JUMP"] = "operator@jump.example.invalid"
        values = effective(SSH(self.env, self.root / "logs"), self.hosts[1])
        self.assertNotIn("proxyjump", values)
        hop = shlex.split(values["proxycommand"])
        self.assertEqual(hop[0], "ssh")
        self.assertEqual(hop[-3:], ["-W", "[%h]:%p", "operator@jump.example.invalid"])
        for option in (
            "StrictHostKeyChecking=yes",
            f"UserKnownHostsFile={self.known_hosts.resolve()}",
            "KnownHostsCommand=none",
            "ControlPath=none",
            "ProxyJump=none",
            "ProxyCommand=none",
            "BatchMode=yes",
        ):
            self.assertEqual(hop[hop.index(option) - 1], "-o", option)

    def test_jump_and_known_hosts_inputs_that_openssh_would_misread_are_refused(self):
        for value in ("-oProxyCommand=x", "jump host", ""):
            with (
                self.subTest(value=value),
                patch.dict(self.env, {"NARWHAL_SSH_JUMP": value}),
            ):
                if value:
                    with self.assertRaisesRegex(ValueError, "NARWHAL_SSH_JUMP must be an SSH"):
                        SSH(self.env, self.root / "logs")
                else:
                    self.assertIn("ProxyCommand=none", SSH(self.env, self.root / "logs").options)
        for name in ("known hosts", "known%hosts"):
            store = self.root / name
            store.write_text(self.known_hosts.read_text())
            self.env["NARWHAL_SSH_KNOWN_HOSTS"] = str(store)
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "contains whitespace or %; move the file"),
            ):
                SSH(self.env, self.root / "logs")

    def test_a_missing_known_hosts_file_stops_before_any_login(self):
        for value in ("", str(self.root / "absent")):
            self.env["NARWHAL_SSH_KNOWN_HOSTS"] = value
            with (
                self.subTest(value=value),
                self.popen(),
                self.assertRaisesRegex(ValueError, "NARWHAL_SSH_KNOWN_HOSTS must name"),
            ):
                SSH(self.env, self.root / "logs")
        self.assertEqual(self.started, [])

    def test_an_unknown_or_changed_host_key_names_the_host(self):
        ssh = SSH(self.env, self.root / "logs")
        failure = (255, b"", b"Host key verification failed.\r\n")
        with (
            self.popen(failure),
            self.assertRaisesRegex(
                RemoteError,
                "^node-2: blocked at management access: the host key differs from "
                "NARWHAL_SSH_KNOWN_HOSTS or is missing from it; inspect ",
            ),
        ):
            ssh.run(self.hosts[1], "access", "management access", "hostname")

    def test_each_stage_bounds_its_remote_commands(self):
        self.assertEqual(stage_limit({}, "access"), 60)
        self.assertEqual(stage_limit({}, "discover"), 3600)
        self.assertEqual(stage_limit({"NARWHAL_STAGE_TIMEOUT_SECONDS": "90"}, "install"), 90)
        both = {
            "NARWHAL_STAGE_TIMEOUT_SECONDS": "90",
            "NARWHAL_STAGE_INSTALL_TIMEOUT_SECONDS": "2.5",
        }
        self.assertEqual(stage_limit(both, "install"), 2.5)
        for value in ("0", "-1", "inf", "nan", "soon"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "must be finite and positive"),
            ):
                stage_limit({"NARWHAL_STAGE_ACCESS_TIMEOUT_SECONDS": value}, "access")

    def sshd_like(self):
        """Put an `ssh` on PATH that runs the remote command as OpenSSH and sshd would."""
        tools = self.root / "sshd-bin"
        tools.mkdir(exist_ok=True)
        fake = tools / "ssh"
        fake.write_text(SSHD_LIKE % (PASSWORD + "\n").encode())
        fake.chmod(0o700)
        return patch.dict(os.environ, {"PATH": f"{tools}:{os.environ['PATH']}"})

    def test_a_supervisor_input_reaches_standard_input_and_no_log_or_argument_list(self):
        secret = "engine-key-that-must-stay-private"
        target = self.root / "unit.env"
        started = []
        real = subprocess.Popen

        def recording(args, **kwargs):
            started.append(list(args))
            return real(args, **kwargs)

        with (
            self.sshd_like(),
            patch("narwhal.deployment.hosts.subprocess.Popen", recording),
        ):
            ssh = SSH(self.env, self.root / "logs")
            runner = remote.SSHRunner(ssh, self.hosts[0], "sidecar-start")
            result = runner(
                ["sh", "-c", 'cat > "$1"', "sh", str(target)],
                input=f'NARWHAL_ENGINE_API_KEY="{secret}"\n',
            )
            empty = runner(["sh", "-c", "cat"])
        self.assertEqual((result.returncode, empty.returncode, empty.stdout), (0, 0, ""))
        self.assertEqual(target.read_text(), f'NARWHAL_ENGINE_API_KEY="{secret}"\n')
        encoded = base64.b64encode(secret.encode()).decode()[:16]
        arguments = json.dumps(started)
        log = ssh.log(self.hosts[0]).read_text()
        for text in (arguments, log):
            self.assertNotIn(secret, text)
            self.assertNotIn(encoded, text)

    def test_the_host_stops_a_command_that_exceeds_its_time_limit(self):
        # `timeout` on the "host" must stop the script and its child, whether the script
        # leaves at SIGTERM or ignores it until SIGKILL.
        children = self.root / "children"
        self.env["NARWHAL_STAGE_ACCESS_TIMEOUT_SECONDS"] = "0.5"
        for host, ignore in ((self.hosts[0], ""), (self.hosts[1], ""), (self.hosts[1], "x")):
            script = f"sleep 30 & echo $! > {shlex.quote(str(children))}; wait"
            if ignore:
                script = "trap '' TERM; " + script
            with (
                self.subTest(host=host.id, ignores_term=bool(ignore)),
                self.sshd_like(),
                patch("narwhal.deployment.hosts.HOST_GRACE_S", 60.0),
                patch("narwhal.deployment.hosts.KILL_AFTER_S", 1),
            ):
                ssh = SSH(self.env, self.root / "logs")
                started = time.monotonic()
                with self.assertRaisesRegex(
                    RemoteTimeout,
                    f"^{host.id}: management access exceeded the 0.5s access time limit",
                ) as raised:
                    ssh.run(host, "access", "management access", script)
                self.assertLess(time.monotonic() - started, 10)
                self.assertEqual(raised.exception.stage, "access")
                self.assertEqual(raised.exception.context["host"], host.id)
                self.assert_stopped(children)
                self.assertIn(
                    "timeout=0.5s; the host stopped the command",
                    ssh.log(host).read_text(),
                )
        self.assert_private_logs()

    def test_a_host_that_stops_answering_is_cut_off_with_the_local_client(self):
        # The fake ssh never runs the command, as a host that stopped answering would not.
        children = self.root / "children"
        self.env["NARWHAL_STAGE_ACCESS_TIMEOUT_SECONDS"] = "0.5"
        body = f"sleep 30 & echo $! > {shlex.quote(str(children))}\nwait"
        for host in self.hosts:
            with (
                self.subTest(host=host.id),
                self.fake_ssh(body),
                patch("narwhal.deployment.hosts.HOST_GRACE_S", 0.5),
            ):
                ssh = SSH(self.env, self.root / "logs")
                with self.assertRaisesRegex(RemoteTimeout, f"^{host.id}: management access"):
                    ssh.run(host, "access", "management access", "hostname")
                self.assert_stopped(children)
                self.assertIn("timeout=0.5s; the host did not answer", ssh.log(host).read_text())
        self.assert_private_logs()

    def test_a_host_without_coreutils_timeout_is_named(self):
        tools = self.root / "host-bin"
        tools.mkdir()
        (tools / "sh").symlink_to(shutil.which("sh"))
        shell = shutil.which("sh")
        with self.fake_ssh(f'PATH={shlex.quote(str(tools))} exec {shell} -c "$last"'):
            ssh = SSH(self.env, self.root / "logs")
            with self.assertRaisesRegex(
                RemoteError,
                "^node-2: blocked at management access: the host lacks the coreutils timeout "
                "command; inspect ",
            ):
                ssh.run(self.hosts[1], "access", "management access", "hostname")

    def test_a_failed_checkpoint_hash_names_the_reason_the_command_reports(self):
        ssh = SSH(self.env, self.root / "logs")
        model = self.root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        (self.root / "outside.json").write_text("{}")
        (model / "extra-out.json").symlink_to("../outside.json")
        payload = self.root / "payload"
        payload.write_text(json.dumps({"model_dir": str(model)}))
        script = "python3 -c " + shlex.quote(Path(checkpoint.__file__).read_text())
        remote = subprocess.run(
            ["sh", "-c", script], input=payload.read_bytes(), capture_output=True
        )
        self.assertEqual(remote.returncode, 1)
        host = self.hosts[0]
        for reason, message in (
            (
                checkpoint.FAILURE,
                f"^{host.id}: blocked at hash checkpoint files: checkpoint entry resolves "
                r"outside the model directory: extra-out\.json; inspect ",
            ),
            (None, f"^{host.id}: blocked at hash checkpoint files; inspect "),
        ):
            with (
                self.subTest(reason=reason),
                self.popen((remote.returncode, remote.stdout, b"ssh notice\n" + remote.stderr)),
                self.assertRaisesRegex(RemoteError, message),
            ):
                ssh.run(
                    host,
                    "discover",
                    "hash checkpoint files",
                    script,
                    payload=payload,
                    reason=reason,
                )

    def test_tunnel_reuses_inventory_authentication_and_opens_no_remote_shell(self):
        ssh = SSH(self.env, self.root / "logs")
        calls = []

        def execute(args, **kwargs):
            calls.append(args)
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("ExitOnForwardFailure=yes", args)
            self.assertIn("ServerAliveInterval=30", args)
            self.assertIn("-N", args)
            self.assertNotIn("-t", args)
            self.assertEqual(args[args.index("-L") + 1], "127.0.0.1:18000:127.0.0.1:8000")
            self.assertIn(args[-1], [self.env[h.ssh_env] for h in self.hosts])
            self.assertNotIn(PASSWORD, " ".join(args))
            helper = kwargs["env"].get("SSH_ASKPASS")
            if helper:
                self.assertEqual(askpass_answer(helper), (PASSWORD + "\n").encode())
            return subprocess.CompletedProcess(args, 0)

        with patch("narwhal.deployment.hosts.subprocess.run", side_effect=execute):
            for host in self.hosts:
                ssh.tunnel(host, ["127.0.0.1:18000:127.0.0.1:8000"])
        self.assertEqual([args[0] for args in calls], ["ssh", "ssh"])
        self.assert_private_logs()
        with (
            patch(
                "narwhal.deployment.hosts.subprocess.run",
                return_value=subprocess.CompletedProcess([], 255),
            ),
            self.assertRaisesRegex(RemoteError, "^node-1: blocked at service tunnel"),
        ):
            ssh.tunnel(self.hosts[0], ["127.0.0.1:18000:127.0.0.1:8000"])

    def test_role_shell_allocates_a_terminal(self):
        ssh = SSH(self.env, self.root / "logs")
        with patch(
            "narwhal.deployment.hosts.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            ssh.shell(self.hosts[1], "exec bash -l")
        args = run.call_args.args[0]
        self.assertIn("-t", args)
        self.assertNotIn("BatchMode=yes", args)
        self.assertEqual(args[-1], "sh -c " + shlex.quote("exec bash -l"))


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "NODE_1_SSH": "operator@one.example.invalid",
            "NODE_1_PASSWORD": PASSWORD,
            "NODE_2_SSH": "operator@two.example.invalid",
        }
        self.entries = [
            {
                "id": "node-1",
                "ssh_env": "NODE_1_SSH",
                "password_env": "NODE_1_PASSWORD",
                "roles": ["router", "engine-1"],
            },
            {"id": "node-2", "ssh_env": "NODE_2_SSH", "password_env": None, "roles": ["engine-2"]},
        ]

    def inventory(self, folder):
        path = Path(folder) / "hosts.json"
        path.write_text(json.dumps({"hosts": self.entries}))
        return path

    def test_roles_resolve_to_one_authentication_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = self.inventory(folder)
            self.assertNotIn(PASSWORD, inventory.read_text())
            hosts = load_hosts(inventory, self.env)
            router = next(h for h in hosts if "router" in h.roles)
            engine = next(h for h in hosts if "engine-1" in h.roles)
            self.assertIs(router, engine)
            self.assertEqual(len(hosts), 2)
            self.env["NODE_2_SSH"] = self.env["NODE_1_SSH"]
            with self.assertRaisesRegex(ValueError, "combine roles"):
                load_hosts(inventory, self.env)

    def test_duplicate_roles_and_missing_credentials_fail_locally(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.inventory(folder)
            del self.env["NODE_1_PASSWORD"]
            with self.assertRaisesRegex(ValueError, "NODE_1_PASSWORD is unset"):
                load_hosts(path, self.env)
            self.env["NODE_1_PASSWORD"] = "synthetic"
            self.entries[1]["roles"].append("router")
            path = self.inventory(folder)
            with self.assertRaisesRegex(ValueError, "one owner"):
                load_hosts(path, self.env)


if __name__ == "__main__":
    unittest.main()
