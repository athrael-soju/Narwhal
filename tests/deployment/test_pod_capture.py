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
                {"NARWHAL_POD_NAME": "engine-1", pod.POD_UID_ENV: POD_UID},
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
        # A second start of the sidecar serves the attempt the container published.
        with (
            patch.object(sidecar, "engine_ready", return_value=True),
            patch.object(sidecar, "attest_main", return_value=0) as again,
            patch.object(sidecar, "capture") as captured,
        ):
            self.assertEqual(sidecar.serve(self.run_dir, self.pod.state), 0)
        captured.assert_not_called()
        self.assertEqual(again.call_args.args, served.call_args.args)

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
