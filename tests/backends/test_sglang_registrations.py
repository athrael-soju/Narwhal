import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from narwhal.backends.sglang import runtime

CONN = """
import threading


class Engine:
    def __init__(self):
        self.calls = []

    def batch_register(self, pointers, lengths):
        self.calls.append(["register", pointers, lengths])

    def batch_deregister(self, pointers):
        self.calls.append(["deregister", pointers])


ENGINE = Engine()


class MooncakeKVManager:
    def __init__(self, regions):
        self.engine = ENGINE
        self.regions = regions
        self.connection_pool = {"peer": "endpoint"}
        self.connection_lock = threading.Lock()
        self.register_buffer_to_engine()

    def _registerable_regions(self):
        return self.regions

    def register_buffer_to_engine(self):
        pointers, lengths = zip(*self.regions)
        self.engine.batch_register(list(pointers), list(lengths))

    def deregister_buffer_to_engine(self):
        self.engine.batch_deregister([pointer for pointer, _ in self.regions])
        with self.connection_lock:
            self.connection_pool.clear()

    def teardown(self):
        self.deregister_buffer_to_engine()
"""

UTILS = """
class MetadataBuffers:
    def __init__(self, size, hidden_size=16):
        self.size = size
        self.hidden_size = hidden_size
"""

ROLE_SWITCH = """
def _reject_reason(scheduler, new_role):
    return scheduler.rejection
"""

DECODE = """
class DecodeTransferQueue:
    def __init__(self, pending):
        self.pending = pending

    def has_pending_deferred_releases(self):
        return bool(self.pending)
"""

# Runs in a process that multiprocessing spawns, as SGLang starts its scheduler.
PROBE = """
def run(queue):
    from sglang.srt.disaggregation.mooncake import conn

    first = conn.MooncakeKVManager([(4096, 64), (8192, 64)])
    first.teardown()
    second = conn.MooncakeKVManager([(4096, 64), (8192, 64), (16384, 32)])
    queue.put({"calls": conn.ENGINE.calls, "pool": second.connection_pool})
"""

SPAWN = """
import json
import multiprocessing

from sglang.srt.disaggregation import probe

if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    queue = multiprocessing.Queue()
    child = multiprocessing.Process(target=probe.run, args=(queue,))
    child.start()
    print(json.dumps(queue.get(timeout=60)))
    child.join()
"""


def fake_sglang(root: Path) -> None:
    package = root / "sglang/srt/disaggregation"
    (package / "mooncake").mkdir(parents=True)
    for directory in (root / "sglang", root / "sglang/srt", package, package / "mooncake"):
        (directory / "__init__.py").write_text("")
    (package / "mooncake/conn.py").write_text(CONN)
    (package / "utils.py").write_text(UTILS)
    (package / "probe.py").write_text(PROBE)
    (package / "role_switch.py").write_text(ROLE_SWITCH)
    (package / "decode.py").write_text(DECODE)


class MooncakeRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        fake_sglang(self.root)
        meta_path, path = list(sys.meta_path), list(sys.path)
        sys.path.insert(0, str(self.root))

        def restore():
            sys.meta_path[:] = meta_path
            sys.path[:] = path
            for name in [n for n in sys.modules if n == "sglang" or n.startswith("sglang.")]:
                del sys.modules[name]

        self.addCleanup(restore)

    def test_a_role_switch_keeps_the_registered_regions(self):
        runtime.keep_mooncake_registrations()
        from sglang.srt.disaggregation.mooncake import conn  # type: ignore[import-not-found]

        first = conn.MooncakeKVManager([(4096, 64), (8192, 64)])
        first.teardown()
        second = conn.MooncakeKVManager([(4096, 64), (8192, 64), (16384, 32)])
        self.assertEqual(
            conn.ENGINE.calls,
            [["register", [4096, 8192], [64, 64]], ["register", [16384], [32]]],
        )
        self.assertEqual(first.connection_pool, {})
        self.assertEqual(second.connection_pool, {"peer": "endpoint"})

    def test_each_metadata_buffer_shape_is_allocated_once(self):
        runtime.keep_mooncake_registrations()
        from sglang.srt.disaggregation import utils  # type: ignore[import-not-found]

        first = utils.MetadataBuffers(128, hidden_size=16)
        self.assertIs(utils.MetadataBuffers(128, hidden_size=16), first)
        self.assertIsNot(utils.MetadataBuffers(256, hidden_size=16), first)

    def test_a_switch_waits_for_deferred_kv_releases(self):
        runtime.keep_mooncake_registrations()
        from sglang.srt.disaggregation import decode, role_switch  # type: ignore[import-not-found]

        def scheduler(pending, rejection=None):
            queue = decode.DecodeTransferQueue(pending)
            return SimpleNamespace(disagg_decode_transfer_queue=queue, rejection=rejection)

        self.assertEqual(
            role_switch._reject_reason(scheduler(["held"]), "prefill"),
            (runtime.PENDING_RELEASES, True),
        )
        self.assertIn("not idle", runtime.PENDING_RELEASES)
        self.assertIsNone(role_switch._reject_reason(scheduler([]), "prefill"))
        refused = ("another role switch is already in progress", False)
        self.assertEqual(
            role_switch._reject_reason(scheduler(["held"], refused), "decode"), refused
        )
        prefill = SimpleNamespace(disagg_decode_transfer_queue=None, rejection=None)
        self.assertIsNone(role_switch._reject_reason(prefill, "decode"))

    def test_the_image_check_names_each_missing_target(self):
        sglang = self.root / "sglang"
        self.assertEqual(runtime.missing_registration_targets(sglang), [])
        (sglang / "srt/disaggregation/decode.py").write_text(
            "class DecodeTransferQueue:\n    pass\n"
        )
        (sglang / "srt/disaggregation/role_switch.py").unlink()
        self.assertEqual(
            runtime.missing_registration_targets(sglang),
            [
                runtime.ROLE_SWITCH_MODULE,
                f"{runtime.DECODE_MODULE}.DecodeTransferQueue.has_pending_deferred_releases",
            ],
        )

    def test_missing_metadata_buffers_are_reported(self):
        (self.root / "sglang/srt/disaggregation/utils.py").write_text("")
        runtime.keep_mooncake_registrations()
        with patch("sys.stderr") as stderr:
            from sglang.srt.disaggregation import utils  # noqa: F401

        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("metadata buffers are reallocated", written)

    def test_an_unknown_manager_is_left_unchanged(self):
        conn = self.root / "sglang/srt/disaggregation/mooncake/conn.py"
        conn.write_text("class MooncakeKVManager:\n    pass\n")
        runtime.keep_mooncake_registrations()
        with patch("sys.stderr") as stderr:
            from sglang.srt.disaggregation.mooncake import conn as module

        self.assertFalse(hasattr(module.MooncakeKVManager, "register_buffer_to_engine"))
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("Mooncake registrations are not kept", written)

    def test_the_launcher_installs_the_fix_in_spawned_engine_processes(self):
        hook = self.root / "hook"
        hook.mkdir()
        shutil.copy(runtime.LAUNCHER, hook / "launch_engine.py")
        script = self.root / "spawn.py"
        script.write_text(SPAWN)
        image = self.root / "image"
        image.mkdir()
        marker = self.root / "image-sitecustomize-ran"
        (image / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'a').write('ran\\n')\n")
        environ = dict(os.environ, PYTHONPATH=f"{hook}:{self.root}:{image}")
        environ = runtime.registration_environment(environ)
        site, rest = environ["PYTHONPATH"].split(":", 1)
        self.addCleanup(shutil.rmtree, site)
        self.assertEqual(rest, f"{hook}:{self.root}:{image}")
        result = subprocess.run(
            [sys.executable, str(script)],
            env=environ,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        observed = json.loads(result.stdout)
        self.assertEqual(
            observed["calls"],
            [["register", [4096, 8192], [64, 64]], ["register", [16384], [32]]],
        )
        self.assertEqual(observed["pool"], {"peer": "endpoint"})
        # The image's own sitecustomize still runs in every interpreter, the spawned one included.
        runs = marker.read_text().splitlines()
        self.assertGreaterEqual(len(runs), 2)
        self.assertEqual(set(runs), {"ran"})

    def serve(self, environ):
        """`serve` with `environ`; return SGLang's argv, the environment it ran with, and the
        argument lists of any process it started."""
        seen = {}

        def run_module(name, run_name, alter_sys):
            self.assertEqual(
                (name, run_name, alter_sys), ("sglang.launch_server", "__main__", True)
            )
            seen.update(argv=list(sys.argv), environ=dict(os.environ))

        started = []
        with (
            patch.dict(os.environ, environ, clear=True),
            patch.object(sys, "argv", list(sys.argv)),
            patch.object(runtime.runpy, "run_module", run_module),
            patch.object(runtime, "keep_mooncake_registrations") as kept,
            patch.object(runtime, "redact_output") as redacted,
            patch.object(runtime.os, "execve", lambda *args: started.append(args[1])),
            patch.object(runtime.os, "execv", lambda *args: started.append(args[1])),
            patch.object(subprocess, "Popen", lambda args, **_: started.append(args)),
        ):
            runtime.serve(["--port", "8000"])
        kept.assert_called_once_with()
        key = environ.get(runtime.API_KEY_ENV)
        self.assertEqual(redacted.call_args_list, [mock.call(key)] if key else [])
        site = seen["environ"]["PYTHONPATH"].split(":", 1)[0]
        self.addCleanup(shutil.rmtree, site)
        return seen["argv"], seen["environ"], started

    def test_serve_runs_sglang_with_the_site_hook_first_on_the_path(self):
        environ = {runtime.BOOTSTRAP_PORT_ENV: "5600", "PYTHONPATH": "/narwhal-hooks"}
        argv, started, _ = self.serve(environ)
        self.assertEqual(argv[1:], ["--port", "8000", "--disaggregation-bootstrap-port", "5600"])
        site, rest = started["PYTHONPATH"].split(":", 1)
        self.assertEqual(rest, "/narwhal-hooks")
        self.assertEqual((Path(site) / "sitecustomize.py").read_text(), runtime.SITE_HOOK)

    def test_the_engine_key_reaches_no_process_argument_list(self):
        # Any local user reads a process's arguments in ps or /proc/<pid>/cmdline, so the key
        # goes only into SGLang's argv in this process, and leaves the inherited environment.
        key = "engine-key-" + "9" * 32
        environ = {runtime.BOOTSTRAP_PORT_ENV: "5600", runtime.API_KEY_ENV: key}
        before = list(sys.orig_argv)
        argv, started, processes = self.serve(environ)
        self.assertEqual(processes, [])
        self.assertEqual(sys.orig_argv, before)
        self.assertNotIn(key, " ".join(sys.orig_argv))
        self.assertEqual(argv[-2:], ["--api-key", key])
        self.assertNotIn(runtime.API_KEY_ENV, started)
        self.assertNotIn(key, json.dumps(started))


# Logs its server arguments the way SGLang does: through logging after basicConfig(force=True),
# with print, through uvicorn's dictConfig handlers, to the unwrapped stream, and as a key split
# across two writes.
LAUNCH_SERVER = """
import logging
import logging.config
import os
import shutil
import sys

# Only spawned interpreters read the wrapper's site hook directory.
shutil.rmtree(os.environ["PYTHONPATH"].split(":", 1)[0])
key = sys.argv[sys.argv.index("--api-key") + 1]
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", force=True)
engine = logging.getLogger("sglang.srt.entrypoints.engine")
engine.info(f"server_args={ {'api_key': key, 'port': 8000} }")
engine.warning("server_args=%s", {"api_key": key})
print(f"server_args=ServerArgs(model_path='/model', api_key={key!r}, port=8000)")
# A handler on the unwrapped stream relies on the redacted record alone.
direct = logging.getLogger("sglang.direct")
direct.addHandler(logging.StreamHandler(sys.__stderr__))
direct.propagate = False
direct.error("direct %s", key)
sys.stderr.write("split " + key[:5])
sys.stderr.flush()
sys.stderr.write(key[5:] + " joined\\n")
sys.stdout.write(key[:4])
sys.stdout.write("-is-no-key\\n")
logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": {"format": "%(levelname)s %(message)s"}},
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }
)
logging.getLogger("uvicorn.error").info("rejected Bearer %s", key)
logging.getLogger("uvicorn.access").info('%s - "GET /?key=%s" 401', "127.0.0.1", key)
sys.stdout.write("served")
"""


class OutputRedactionTests(unittest.TestCase):
    def test_the_engine_key_reaches_no_log_line(self):
        # The container's stdout and stderr reach `docker logs` and `kubectl logs`.
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        hook, sglang = root / "hook", root / "sglang"
        hook.mkdir()
        sglang.mkdir()
        shutil.copy(runtime.LAUNCHER, hook / "launch_engine.py")
        (sglang / "__init__.py").write_text("")
        (sglang / "launch_server.py").write_text(LAUNCH_SERVER)
        key = "engine-key-" + "7" * 32
        result = subprocess.run(
            [sys.executable, "-m", "launch_engine", "serve", "--port", "8000"],
            env={
                **os.environ,
                "PYTHONPATH": f"{hook}:{root}",
                runtime.BOOTSTRAP_PORT_ENV: "5600",
                runtime.API_KEY_ENV: key,
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        self.assertNotIn(key, result.stdout + result.stderr)
        self.assertIn("server_args={'api_key': '[REDACTED]', 'port': 8000}", result.stderr)
        self.assertIn("server_args={'api_key': '[REDACTED]'}", result.stderr)
        self.assertIn("api_key='[REDACTED]', port=8000)", result.stdout)
        self.assertIn("direct [REDACTED]\n", result.stderr)
        self.assertIn("split [REDACTED] joined\n", result.stderr)
        self.assertIn("INFO rejected Bearer [REDACTED]", result.stderr)
        self.assertIn('"GET /?key=[REDACTED]" 401', result.stdout)
        # A held tail that turns out not to be the key is written, and so is the last one.
        self.assertIn("engi-is-no-key\n", result.stdout)
        self.assertTrue(result.stdout.endswith("served"))

    def test_a_key_split_at_every_point_is_replaced(self):
        key = "k3y-" + "x" * 12
        for split in range(1, len(key)):
            written = []
            stream = runtime.RedactingStream(
                SimpleNamespace(write=written.append, flush=lambda: None), [key]
            )
            stream.write(f"a {key[:split]}")
            stream.write(f"{key[split:]} b\n")
            stream.finish()
            self.assertEqual("".join(written), f"a {runtime.REDACTED} b\n")


if __name__ == "__main__":
    unittest.main()
