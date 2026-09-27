"""Exercise the installed command boundary with real bounded CPU processes."""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from narwhal import command_results, contracts
from narwhal.deployment import management_commands as commands
from tests.fixtures import ROOT


class ManagementCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.arguments = ["config", "inspect", "--fleet", "fleet.json", "--format", "json"]

    def document(self, status="success"):
        return contracts.versioned(
            contracts.COMMAND_RESULT,
            {
                "command": "narwhal",
                "operation": "config inspect",
                "status": status,
                "exit_code": command_results.EXIT_CODES[status],
                "data": {"answer": 42},
                "errors": [{"code": "future_code", "message": "reported", "command": "narwhal"}],
                "artifacts": [
                    {"kind": "evidence", "path": str(self.root / "evidence"), "state": "missing"}
                ],
                "future_field": {"preserve": True},
            },
        )

    def executable(self, body):
        script = self.root / "fake-narwhal"
        script.write_text(f"#!{sys.executable}\n" + body)
        script.chmod(0o700)
        selected = patch.object(commands, "_executable", return_value=script)
        selected.start()
        self.addCleanup(selected.stop)
        return script

    async def run_command(self, *, arguments=None, env=None, timeout_s=2, pass_fds=()):
        return await commands.run_command(
            self.arguments if arguments is None else arguments,
            cwd=self.root,
            env={} if env is None else env,
            timeout_s=timeout_s,
            pass_fds=pass_fds,
        )

    async def test_fixed_executable_literal_arguments_explicit_directory_and_environment(self):
        document = self.document()
        script = self.executable(
            "import json,os,sys\n"
            f"result={document!r}\n"
            "result['data']={'argv':sys.argv[1:], 'cwd':os.getcwd(), "
            "'provided':os.environ.get('ALLOW'), "
            "'ambient':os.environ.get('AMBIENT_SECRET')}\n"
            "print(json.dumps(result))\n"
        )
        literal = "$(touch escaped); touch escaped"
        arguments = ["config", "inspect", "--fleet", literal, "--format", "json"]
        spawn = asyncio.create_subprocess_exec
        with (
            patch.dict(os.environ, {"AMBIENT_SECRET": "not-in-command-environment"}),
            patch.object(commands.asyncio, "create_subprocess_exec", wraps=spawn) as invoked,
        ):
            result = await self.run_command(arguments=arguments, env={"ALLOW": "registered"})
        self.assertEqual(result["data"]["argv"], arguments)
        self.assertEqual(result["data"]["cwd"], str(self.root))
        self.assertEqual(result["data"]["provided"], "registered")
        self.assertIsNone(result["data"]["ambient"])
        self.assertEqual(invoked.call_args.args, (str(script), *arguments))
        self.assertTrue(invoked.call_args.kwargs["start_new_session"])
        self.assertNotIn("shell", invoked.call_args.kwargs)
        self.assertFalse((self.root / "escaped").exists())

    async def test_rejects_unapproved_actions_and_text_output_before_spawn(self):
        for arguments in (
            ["dev", "up", "--format", "json"],
            ["config", "inspect-more", "--format", "json"],
            ["diagnostics", "collect", "--format", "text"],
            ["--help"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(
                    commands.asyncio, "create_subprocess_exec", new_callable=AsyncMock
                ) as spawn,
            ):
                with self.assertRaises(commands.CommandError) as caught:
                    await self.run_command(arguments=arguments)
                self.assertEqual(caught.exception.code, "invalid_input")
                spawn.assert_not_called()

    async def test_preserves_success_degraded_and_error_documents(self):
        for status in (
            "success",
            "degraded",
            "error",
            "failed_gate",
            "invalid_input",
            "interrupted",
        ):
            with self.subTest(status=status):
                document = self.document(status)
                self.executable(
                    f"import json,sys\nprint(json.dumps({document!r}))\n"
                    f"sys.exit({document['exit_code']})\n"
                )
                self.assertEqual(await self.run_command(), document)

    async def test_unsupported_result_version_is_reported_before_field_use(self):
        document = {"schema": "narwhal.command-result", "schema_version": 2}
        self.executable(f"import json\nprint(json.dumps({document!r}))\n")
        with self.assertRaises(contracts.ContractVersionError):
            await self.run_command()

    async def test_invalid_output_has_fixed_error_without_child_diagnostics(self):
        valid = json.dumps(self.document())
        for payload in (
            "secret-value",
            valid + valid,
            valid.replace('"answer": 42', '"answer": NaN'),
            '{"schema":1,"schema":2}',
        ):
            with self.subTest(payload=payload[:30]):
                self.executable(
                    "import sys\nsys.stderr.write('private-child-diagnostic')\n"
                    f"print({payload!r})\n"
                )
                with self.assertRaises(commands.CommandError) as caught:
                    await self.run_command()
                self.assertEqual(caught.exception.code, "invalid_input")
                self.assertNotIn("private-child-diagnostic", str(caught.exception))
                self.assertNotIn("secret-value", str(caught.exception))

    async def test_inconsistent_status_or_process_exit_code_is_rejected(self):
        for document in (self.document("error"), {**self.document(), "exit_code": False}):
            with self.subTest(document=document):
                self.executable(f"import json\nprint(json.dumps({document!r}))\n")
                with self.assertRaises(commands.CommandError) as caught:
                    await self.run_command()
                self.assertEqual(caught.exception.code, "invalid_input")

    async def test_stdout_and_stderr_limits_stop_the_writer(self):
        for descriptor, limit in ((1, commands.MAX_STDOUT_BYTES), (2, commands.MAX_STDERR_BYTES)):
            with self.subTest(descriptor=descriptor):
                self.executable(
                    "import os,time\n"
                    "from pathlib import Path\n"
                    f"Path({str(self.root / 'pid')!r}).write_text(str(os.getpid()))\n"
                    f"os.write({descriptor}, b'x' * {limit + 1})\n"
                    "time.sleep(30)\n"
                )
                with self.assertRaises(commands.CommandError) as caught:
                    await self.run_command()
                self.assertEqual(caught.exception.code, "source_truncated")
                self.assertFalse(Path("/proc/" + (self.root / "pid").read_text()).exists())

    def process_tree(self, *, exit_leader=False):
        worker_ready = self.root / "worker"
        leader_ready = self.root / "leader"
        child = (
            "import os,signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"Path({str(worker_ready)!r}).write_text(str(os.getpid())); time.sleep(30)"
        )
        self.executable(
            "import os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            f"Path({str(leader_ready)!r}).write_text(str(os.getpid()))\n"
            f"subprocess.Popen([sys.executable,'-c',{child!r}], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"while not Path({str(worker_ready)!r}).exists(): time.sleep(.001)\n"
            + (
                f"import json\nprint(json.dumps({self.document()!r}))\n"
                if exit_leader
                else "time.sleep(30)\n"
            )
        )
        return leader_ready, worker_ready

    async def assert_tree_reaped(self, files):
        for path in files:
            self.assertTrue(path.exists(), path.name)
            self.assertFalse(Path("/proc/" + path.read_text()).exists(), path.name)

    async def test_timeout_stops_and_reaps_the_leader_and_term_ignoring_worker(self):
        files = self.process_tree()
        started = time.monotonic()
        with self.assertRaises(commands.CommandError) as caught:
            await self.run_command(timeout_s=0.6)
        self.assertEqual(caught.exception.code, "stage_timeout")
        self.assertLess(time.monotonic() - started, 1.2)
        await self.assert_tree_reaped(files)

    async def test_repeated_cancellation_waits_for_owned_group_cleanup(self):
        files = self.process_tree()
        task = asyncio.create_task(self.run_command(timeout_s=5))
        deadline = time.monotonic() + 2
        while not files[1].exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        self.assertTrue(files[1].exists())
        task.cancel()
        asyncio.get_running_loop().call_later(0.05, task.cancel)
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_tree_reaped(files)

    async def test_cancellation_during_spawn_reaps_the_created_process_group(self):
        files = self.process_tree()
        original_spawn = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        release = asyncio.Event()

        async def delayed_spawn(*arguments, **options):
            process = await original_spawn(*arguments, **options)
            spawned.set()
            await release.wait()
            return process

        with patch.object(commands.asyncio, "create_subprocess_exec", delayed_spawn):
            task = asyncio.create_task(self.run_command(timeout_s=5))
            await asyncio.wait_for(spawned.wait(), 2)
            deadline = time.monotonic() + 2
            while not files[1].exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(files[1].exists())
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.assert_tree_reaped(files)

    async def test_overlapping_commands_reap_only_their_own_groups(self):
        worker_code = (
            "import os,signal,sys,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
        )
        self.executable(
            "import os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            "name=os.environ['CASE']\n"
            "Path(name+'-leader').write_text(str(os.getpid()))\n"
            f"subprocess.Popen([sys.executable,'-c',{worker_code!r},name+'-worker'])\n"
            "time.sleep(30)\n"
        )
        short = asyncio.create_task(self.run_command(env={"CASE": "short"}, timeout_s=0.5))
        long = asyncio.create_task(self.run_command(env={"CASE": "long"}, timeout_s=1))
        with self.assertRaises(commands.CommandError) as caught:
            await short
        self.assertEqual(caught.exception.code, "stage_timeout")
        long_pid = (self.root / "long-leader").read_text()
        self.assertTrue(Path("/proc/" + long_pid).exists())
        self.assertFalse(long.done())
        with self.assertRaises(commands.CommandError) as caught:
            await long
        self.assertEqual(caught.exception.code, "stage_timeout")
        await self.assert_tree_reaped(
            [
                self.root / f"{case}-{role}"
                for case in ("short", "long")
                for role in ("leader", "worker")
            ]
        )

    async def test_success_also_reaps_workers_left_by_an_exited_leader(self):
        files = self.process_tree(exit_leader=True)
        self.assertEqual(await self.run_command(), self.document())
        await self.assert_tree_reaped(files)

    async def test_installed_config_command_runs_outside_the_checkout(self):
        with (ROOT / "tests/data/fleet.json").open() as source:
            descriptor = source.fileno()
            result = await self.run_command(
                arguments=[
                    "config",
                    "inspect",
                    "--fleet",
                    f"/proc/self/fd/{descriptor}",
                    "--format",
                    "json",
                ],
                timeout_s=5,
                pass_fds=(descriptor,),
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["operation"], "config inspect")
        self.assertEqual(result["data"]["schema"], "narwhal.effective-config")
