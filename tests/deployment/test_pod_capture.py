"""A Kubernetes engine pod's attestation, captured and served by its sidecar from a checked pod."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narwhal.backends import load as load_backend
from narwhal.deployment.attestation_contract import capture as capture_module
from narwhal.deployment.attestation_contract import pod_engine, sidecar
from narwhal.deployment.attestation_contract.capture import CaptureFailed, published_attempt
from narwhal.deployment.attestation_contract.document import attested
from narwhal.deployment.launch_engine import kubernetes, pod
from narwhal.deployment.launch_engine.check import read_checked
from narwhal.deployment.launch_engine.plan import API_KEY_ENV
from narwhal.deployment.launch_engine.runtime import digest
from narwhal.engines.pod_identity import PodIdentity
from tests.backends.contract import CONTAINER_STARTED_S, LiveContainer, fixtures, prepared
from tests.deployment.pod_fixtures import (
    ENGINE_PID,
    ENGINE_PROCESS,
    ENGINE_TICKS,
    IMAGE,
    NODE,
    POD_IP,
    POD_UID,
    SECRET_VALUE,
    EnginePod,
)

ENGINE_ID = "a" * 64
RESTARTED_ID = "d" * 64
IMAGE_ID = IMAGE.rpartition("@")[2]


def engine_identity(container_id: str = ENGINE_ID) -> PodIdentity:
    return PodIdentity(
        namespace="engines",
        pod="engine-1",
        pod_uid=POD_UID,
        node=NODE,
        pod_ip=POD_IP,
        container="engine",
        runtime="containerd",
        container_id=container_id,
        image=IMAGE,
        image_id=IMAGE_ID,
        started_at="2026-01-01T00:00:00Z",
        restart_count=0,
        pid=ENGINE_PID,
        start_ticks=ENGINE_TICKS,
        process_start_time_seconds=CONTAINER_STARTED_S,
    )


class PodCaptureTests(unittest.TestCase):
    """The sidecar of a vLLM engine pod whose init containers and engine step passed, and whose
    records `narwhal-engine check` passed."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.fixtures = fixtures("vllm")
        launcher = load_backend("vllm").launcher()
        (root / "plan").mkdir()
        self.run_dir, plan, _ = prepared(
            root / "plan", launcher, self.fixtures, backend="kubernetes"
        )
        # What the engine image answers: the runtime probe's output, the inspections' output,
        # the cache capture hook's layout and the engine's startup log.
        reference = root / "reference"
        reference.mkdir()
        self.fixtures.attestation_evidence(self.run_dir, reference, plan, ENGINE_ID)
        assert self.fixtures.engine_answers is not None
        answers = self.fixtures.engine_answers(reference)
        self.pod = EnginePod(root / "pod", self.run_dir)
        self.pod.start(self.fixtures.check_output(plan) + "\n" + answers.check_log)
        with contextlib.redirect_stdout(io.StringIO()):
            kubernetes.check(self.run_dir, plan, self.pod.state)
        self.checked = read_checked(self.run_dir)
        directory = self.pod.path(pod.ENGINE) / ENGINE_PROCESS
        records = {}
        inspections = launcher.pod_inspections.inspections(
            plan, digest(self.run_dir / "launch.json")
        )
        for name, argv in inspections.items():
            [tag] = [tag for tag in answers.scripts if tag in argv[1]]
            records[name] = {
                "argv": argv,
                "returncode": 0,
                "stdout": answers.scripts[tag],
                "stderr": "",
            }
        (directory / pod.INSPECTIONS_RECORD).write_text(json.dumps(records))
        # The hook also records the plan and runtime it ran under.
        layout = json.loads((self.run_dir / "cache-layout.json").read_text())
        (self.run_dir / "cache-layout.json").unlink()
        layout.update(
            expected_packages=plan["expected_packages"],
            revision=plan["revision"],
            launcher_sha256=plan["launcher_sha256"],
            cache_capture_sha256=plan["cache_capture_sha256"],
        )
        (directory / pod.CACHE_LAYOUT).write_text(json.dumps(layout))
        (directory / pod.STARTUP_LOG).write_text(self.fixtures.startup_log)
        self.identities = [engine_identity()]
        self.live = LiveContainer(self.run_dir, IMAGE_ID, answers, self.fixtures.startup_log)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for target, value in (
            ("POD_ROOT", self.pod.root),
            ("service_account_namespace", lambda: "engines"),
            ("read_identity", self.read_identity),
        ):
            stack.enter_context(patch.object(pod_engine, target, value))
        stack.enter_context(
            patch.dict(
                os.environ,
                # The sidecar holds the engine API key from its Secret, as its pod spec gives it.
                {
                    "NARWHAL_POD_NAME": "engine-1",
                    pod.POD_UID_ENV: POD_UID,
                    API_KEY_ENV: SECRET_VALUE,
                },
            )
        )
        stack.enter_context(self.live.serving())
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def read_identity(self, namespace, name, uid, image) -> PodIdentity:
        """The pod as the API and the shared process namespace report it: each read takes the
        next identity, and the last one stays."""
        if (namespace, name, uid, image) != ("engines", "engine-1", POD_UID, IMAGE):
            raise AssertionError(f"read another pod: {namespace}/{name} {uid} {image}")
        return self.identities.pop(0) if len(self.identities) > 1 else self.identities[0]

    def capture(self) -> Path:
        return capture_module.capture(self.run_dir, pod_evidence=self.pod.state)

    def test_the_capture_attests_the_engine_container_in_its_directory(self):
        document = self.capture()
        home = pod_engine.engine_home(self.pod.state, ENGINE_ID)
        self.assertEqual(published_attempt(home), document.parent)
        record = json.loads(document.read_text())
        self.assertEqual(record["contract"]["image_digest"], IMAGE_ID)
        self.assertEqual(record["launch"]["image_id"], IMAGE_ID)
        self.assertIn(str(document.parent / "pod-identity.json"), record["sources"]["image_digest"])
        identity = json.loads((document.parent / "pod-identity.json").read_text())
        self.assertEqual(identity["container_id"], ENGINE_ID)
        self.assertEqual(identity["transfer_device"], self.checked["transfer_device"])
        # The document attests the evidence the published attempt holds.
        live = attested(self.run_dir, document.parent)[0]
        self.assertEqual(live, record)

    def test_the_sidecar_serves_the_engine_containers_attestation_through_its_hca(self):
        with (
            patch.object(sidecar, "engine_ready", return_value=True),
            patch.object(sidecar, "attest_main", return_value=0) as served,
        ):
            self.assertEqual(sidecar.serve(self.run_dir, self.pod.state), 0)
        [arguments] = served.call_args.args
        home = pod_engine.engine_home(self.pod.state, ENGINE_ID)
        document = published_attempt(home) / "engine-attestation.json"

        def option(name: str) -> str:
            return arguments[arguments.index(name) + 1]

        self.assertEqual(option("--document"), str(document))
        self.assertEqual(option("--host"), POD_IP)
        self.assertEqual(option("--engine-pod-container"), ENGINE_ID)
        # The HCA the device plugin allocated to the pod, which the check of the pod recorded.
        self.assertEqual(self.checked["transfer_device"]["ucx_net_devices"], "hca0:1")
        self.assertEqual(option("--hca"), "hca0:1")
        # Nodes can share a kernel and its boot ID, so the reading names the pod's node.
        self.assertEqual(option("--hca-host"), NODE)
        # A second start of the sidecar serves the attempt the container published.
        with (
            patch.object(sidecar, "engine_ready", return_value=True),
            patch.object(sidecar, "attest_main", return_value=0) as again,
            patch.object(sidecar, "capture") as captured,
        ):
            self.assertEqual(sidecar.serve(self.run_dir, self.pod.state), 0)
        captured.assert_not_called()
        self.assertEqual(again.call_args.args, served.call_args.args)

    def test_the_published_attempt_is_readable_by_the_sidecars_group_alone(self):
        # The gate Jobs that copy and verify it run as another user, with this group.
        document = self.capture()
        home = pod_engine.engine_home(self.pod.state, ENGINE_ID)
        attempt = document.parent
        shared = [home.parent, home, home / capture_module.ATTEMPTS, attempt]
        shared += [home / capture_module.PUBLISHED, *attempt.rglob("*")]
        for path in shared:
            info = path.stat()
            with self.subTest(path=path.name):
                self.assertEqual(info.st_gid, os.getegid())
                self.assertEqual(info.st_mode & 0o777, 0o750 if path.is_dir() else 0o640)

    def test_the_startup_log_keeps_no_engine_key(self):
        # An engine may print its own arguments as it starts, its key among them.
        directory = self.pod.path(pod.ENGINE) / ENGINE_PROCESS
        printed = f"ServerArgs(model='m', api_key='{SECRET_VALUE}')\n"
        (directory / pod.STARTUP_LOG).write_text(self.fixtures.startup_log + printed)
        document = self.capture()
        log = (document.parent / "startup.log").read_text()
        self.assertNotIn(SECRET_VALUE, log)
        self.assertIn("api_key='[REDACTED]'", log)
        self.assertTrue(log.startswith(self.fixtures.startup_log))

    def test_an_attempt_that_still_holds_the_key_is_not_shared(self):
        home = pod_engine.engine_home(self.pod.state, ENGINE_ID)
        with (
            patch.object(
                capture_module, "redact_secrets", lambda text, extra=(): text, create=True
            ),
            self.assertRaisesRegex(ValueError, "holds the engine's credential in startup.log"),
        ):
            # The engine's startup log names its key, and the redaction misses it.
            directory = self.pod.path(pod.ENGINE) / ENGINE_PROCESS
            log = self.fixtures.startup_log + f"api_key={SECRET_VALUE}\n"
            (directory / pod.STARTUP_LOG).write_text(log)
            self.capture()
        self.assertFalse((home / capture_module.PUBLISHED).exists())
        [attempt] = (home / capture_module.ATTEMPTS).iterdir()
        for path in [attempt, *attempt.rglob("*")]:
            with self.subTest(path=path.name):
                self.assertEqual(path.stat().st_mode & 0o077, 0)
        # The gates read why, which names the file and not the key.
        refusal = home / capture_module.REFUSED
        self.assertEqual(refusal.stat().st_mode & 0o777, 0o640)
        self.assertNotIn(SECRET_VALUE, refusal.read_text())
        self.assertIn("startup.log", json.loads(refusal.read_text())["files"])

    def test_a_link_planted_in_the_pods_directory_is_refused(self):
        # The engine container can write the pod's directory.
        elsewhere = self.pod.state.parent / "elsewhere"
        elsewhere.mkdir()
        (self.pod.state / pod_engine.CONTAINERS).symlink_to(elsewhere)
        with self.assertRaisesRegex(ValueError, "is a symbolic link"):
            self.capture()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_sharing_refuses_a_link_in_the_attempt(self):
        document = self.capture()
        home = pod_engine.engine_home(self.pod.state, ENGINE_ID)
        target = self.pod.state.parent / "target"
        target.write_text("x")
        (document.parent / "planted").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "planted is a symbolic link"):
            capture_module.share(home, document.parent)
        home.parent.chmod(0o755)
        (home.parent / "link").symlink_to(home)
        with self.assertRaisesRegex(ValueError, "or a link"):
            capture_module.share(home.parent / "link", document.parent)

    def test_an_attempt_that_fails_stays_private(self):
        config = self.pod.path(pod.MODEL) / "config.json"
        status = config.stat()
        os.utime(config, ns=(status.st_atime_ns, status.st_mtime_ns + 10**9))
        with self.assertRaises(CaptureFailed):
            self.capture()
        home = pod_engine.engine_home(self.pod.state, ENGINE_ID)
        [attempt] = (home / capture_module.ATTEMPTS).iterdir()
        for path in [attempt, *attempt.rglob("*")]:
            with self.subTest(path=path.name):
                self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_model_files_changed_after_the_checkpoint_are_not_attested(self):
        # A model file rewritten in place, here with its own content.
        config = self.pod.path(pod.MODEL) / "config.json"
        config.write_bytes(config.read_bytes())
        status = config.stat()
        os.utime(config, ns=(status.st_atime_ns, status.st_mtime_ns + 10**9))
        with self.assertRaisesRegex(CaptureFailed, "changed after the pod's checkpoint init"):
            self.capture()
        home = pod_engine.engine_home(self.pod.state, ENGINE_ID)
        self.assertFalse((home / capture_module.PUBLISHED).exists())

    def test_a_container_that_starts_again_during_the_capture_publishes_nothing(self):
        # The capture finds container A, and the next reads find its restart B.
        self.identities = [engine_identity(), engine_identity(RESTARTED_ID)]
        with self.assertRaisesRegex(CaptureFailed, "started again during the capture"):
            self.capture()
        for container in (ENGINE_ID, RESTARTED_ID):
            home = pod_engine.engine_home(self.pod.state, container)
            self.assertFalse((home / capture_module.PUBLISHED).exists())


if __name__ == "__main__":
    unittest.main()
