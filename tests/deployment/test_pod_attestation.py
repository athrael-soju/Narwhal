"""The live identity of a Kubernetes engine container, and its attestation sidecar's checks."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment.attestation_contract import capture as capture_module
from narwhal.deployment.attestation_contract import pod_engine, sidecar
from narwhal.engines import attestation as attest
from narwhal.engines import pod_identity
from narwhal.engines.pod_identity import EngineNotRunning, PodIdentity, PodIdentityError

POD_UID = "6f1c2a3b-4d5e-4f60-8a9b-0c1d2e3f4a5b"
ENGINE_ID = "a" * 64
SIDECAR_ID = "b" * 64
IMAGE_ID = "sha256:" + "c" * 64
IMAGE = "registry.example/engine@" + IMAGE_ID
TRANSFER_DEVICE = {"verbs_device": "uverbs0", "hca": "hca0", "port": 1, "ucx_net_devices": "hca0:1"}
BOOT = 1_700_000_000.0
TICKS_PER_S = 100
STARTED_AT = "2023-11-14T22:13:30Z"  # BOOT + 10 seconds
ENGINE_TICKS = 10 * TICKS_PER_S


def pod_object(**changes) -> dict:
    """The pod object the Kubernetes API returns for a running engine pod."""
    engine = {
        "name": "engine",
        "containerID": f"containerd://{ENGINE_ID}",
        "image": IMAGE,
        "imageID": IMAGE,
        "restartCount": 0,
        "state": {"running": {"startedAt": STARTED_AT}},
    }
    pod = {
        "kind": "Pod",
        "metadata": {"name": "engine-1", "uid": POD_UID},
        "spec": {
            "shareProcessNamespace": True,
            "nodeName": "gpu-node-a",
            "containers": [{"name": "engine", "image": IMAGE}, {"name": "attestation"}],
        },
        "status": {"podIP": "198.51.100.7", "containerStatuses": [engine]},
    }
    for path, value in changes.items():
        target = pod
        *parents, name = path.split(".")
        for parent in parents:
            target = target[parent] if not parent.isdigit() else target[int(parent)]
        target[name] = value
    return pod


class ProcFixture:
    """A /proc of a pod that shares its process namespace."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def process(self, pid: int, cgroup: str, *, parent: int = 0, ticks: int, state="S") -> None:
        """Add a process; `cgroup` is a cgroup v2 path, or a whole cgroup file when it holds a
        colon."""
        directory = self.root / str(pid)
        directory.mkdir()
        fields = [state, str(parent), *["0"] * 17, str(ticks), "0"]
        (directory / "stat").write_text(f"{pid} (python3) {' '.join(fields)}\n")
        (directory / "cgroup").write_text(cgroup if ":" in cgroup else f"0::{cgroup}\n")


def scope(container: str) -> str:
    return f"/kubepods.slice/kubepods-pod.slice/cri-containerd-{container}.scope"


class PodIdentityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.proc = ProcFixture(Path(folder.name))
        for target, value in (
            ("PROC", self.proc.root),
            ("boot_time", lambda: BOOT),
        ):
            patcher = patch.object(pod_identity, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sysconf = patch("os.sysconf", return_value=TICKS_PER_S)
        sysconf.start()
        self.addCleanup(sysconf.stop)
        # The engine's first process, a worker it started, and the sidecar's own process.
        self.proc.process(7, scope(ENGINE_ID), ticks=ENGINE_TICKS)
        self.proc.process(9, scope(ENGINE_ID), parent=7, ticks=ENGINE_TICKS + 5)
        self.proc.process(12, scope(SIDECAR_ID), ticks=ENGINE_TICKS + 50)

    def identity(self, pod: dict | None = None) -> PodIdentity:
        with patch.object(pod_identity, "read_pod", return_value=pod or pod_object()):
            return pod_identity.read_identity("engines", "engine-1", POD_UID, IMAGE)

    def test_the_engine_process_binds_to_the_container_the_api_reports(self):
        identity = self.identity()
        self.assertEqual((identity.pid, identity.start_ticks), (7, ENGINE_TICKS))
        self.assertEqual(identity.container_id, ENGINE_ID)
        self.assertEqual((identity.runtime, identity.image_id), ("containerd", IMAGE_ID))
        self.assertEqual(identity.process_start_time_seconds, BOOT + 10)
        self.assertEqual((identity.node, identity.pod_ip), ("gpu-node-a", "198.51.100.7"))

    def test_another_pod_or_an_unshared_process_namespace_is_refused(self):
        with self.assertRaisesRegex(PodIdentityError, "has UID other, not the checked pod's"):
            self.identity(pod_object(**{"metadata.uid": "other"}))
        with self.assertRaisesRegex(PodIdentityError, "does not set shareProcessNamespace"):
            self.identity(pod_object(**{"spec.shareProcessNamespace": False}))

    def test_an_engine_container_that_is_not_running_or_unidentified_is_refused(self):
        status = pod_object()["status"]["containerStatuses"][0]
        for changes, error, message in (
            ({"state": {"waiting": {"reason": "CrashLoopBackOff"}}}, EngineNotRunning, "waiting"),
            ({"containerID": ENGINE_ID}, PodIdentityError, "no <runtime>://<ID> container ID"),
            ({"imageID": "registry.example/engine:latest"}, PodIdentityError, "no sha256 image"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(error, message):
                pod = pod_object()
                pod["status"]["containerStatuses"] = [{**status, **changes}]
                self.identity(pod)
        pod = pod_object()
        pod["status"]["containerStatuses"] = []
        with self.assertRaisesRegex(EngineNotRunning, "no status for its engine container"):
            self.identity(pod)

    def test_the_container_must_run_the_checked_image(self):
        patched = pod_object()
        patched["spec"]["containers"][0]["image"] = "registry.example/engine@sha256:" + "e" * 64
        with self.assertRaisesRegex(PodIdentityError, "specifies image .*, not the checked"):
            self.identity(patched)
        pod = pod_object()
        pod["status"]["containerStatuses"][0]["imageID"] = "sha256:" + "e" * 64
        with self.assertRaisesRegex(PodIdentityError, "runs image sha256:e+, not the checked"):
            self.identity(pod)

    def test_the_process_binds_however_long_the_runtime_took_to_start_its_container(self):
        # The runtime forks the first process before its hooks run, and reports the start
        # after them; a clock step moves the boot time the start ticks count from.
        for started_at in ("2023-11-14T22:13:33Z", "2023-11-14T23:13:30Z"):
            with self.subTest(started_at=started_at):
                pod = pod_object()
                pod["status"]["containerStatuses"][0]["state"]["running"]["startedAt"] = started_at
                self.assertEqual(self.identity(pod).pid, 7)

    def test_the_first_process_must_be_visible_and_alone(self):
        # The API can report a container running for a moment after its processes end.
        with self.assertRaisesRegex(EngineNotRunning, "runs in this pod's process namespace"):
            pod_identity.container_process("d" * 64)
        self.proc.process(15, scope(ENGINE_ID), ticks=ENGINE_TICKS)
        with self.assertRaisesRegex(PodIdentityError, "shows 2 first processes"):
            pod_identity.container_process(ENGINE_ID)

    def test_a_process_left_by_kubectl_exec_does_not_stand_in_for_an_ended_engine(self):
        # The engine's first process has ended; a process `kubectl exec` started 3 seconds after
        # the container remains in its cgroup.
        for path in (self.proc.root / "7").iterdir():
            path.unlink()
        (self.proc.root / "7").rmdir()
        self.proc.process(15, scope(ENGINE_ID), ticks=ENGINE_TICKS + 3 * TICKS_PER_S)
        with self.assertRaisesRegex(EngineNotRunning, "first process has ended"):
            self.identity()

    def test_a_process_kubectl_exec_started_is_not_the_first(self):
        # `kubectl exec` starts its process from outside the pod's process namespace too.
        self.proc.process(15, scope(ENGINE_ID), ticks=ENGINE_TICKS + 900)
        self.assertEqual(pod_identity.container_process(ENGINE_ID), (7, ENGINE_TICKS))
        self.assertEqual(self.identity().pid, 7)

    def test_an_ended_process_does_not_count_as_the_first(self):
        self.proc.process(15, scope(ENGINE_ID), ticks=ENGINE_TICKS + 9, state="Z")
        self.assertEqual(pod_identity.container_process(ENGINE_ID), (7, ENGINE_TICKS))

    def test_each_runtime_cgroup_layout_names_its_container(self):
        for number, path in enumerate(
            (
                scope(ENGINE_ID),
                f"/kubepods/burstable/pod{POD_UID}/crio-{ENGINE_ID}.scope",
                f"/kubepods/besteffort/pod{POD_UID}/{ENGINE_ID}",
                # crun under the systemd driver runs the processes below the scope.
                f"/kubepods.slice/kubepods-pod.slice/crio-{ENGINE_ID}.scope/container",
                # A private cgroup namespace shows paths from the sidecar's own cgroup.
                f"/../cri-containerd-{ENGINE_ID}.scope",
                f"/../../crio-{ENGINE_ID}.scope/container",
                f"/../{ENGINE_ID}",
                # cgroup v1 names the container in each hierarchy, beside a hybrid v2 line.
                f"12:memory:/kubepods/burstable/pod{POD_UID}/{ENGINE_ID}\n"
                f"11:cpu,cpuacct:/kubepods/burstable/pod{POD_UID}/{ENGINE_ID}\n"
                "0::/system.slice/containerd.service\n",
            ),
            start=20,
        ):
            with self.subTest(path=path):
                self.proc.process(number, path, parent=7, ticks=1)
                self.assertEqual(pod_identity.cgroup_container(number), ENGINE_ID)
        self.proc.process(40, "/system.slice/kubelet.service", ticks=1)
        self.assertIsNone(pod_identity.cgroup_container(40))
        # A process in a cgroup it created below the container names no container.
        self.proc.process(41, f"/../cri-containerd-{ENGINE_ID}.scope/worker", ticks=1)
        self.assertIsNone(pod_identity.cgroup_container(41))

    def test_the_clock_raises_once_the_engine_process_ends_or_is_replaced(self):
        clock = pod_identity.pod_clock(ENGINE_ID)
        self.assertEqual(clock(), BOOT + 10)
        (self.proc.root / "7" / "stat").write_text(
            "7 (python3) S 0 " + " ".join(["0"] * 17) + f" {ENGINE_TICKS + 900} 0\n"
        )
        with self.assertRaisesRegex(PodIdentityError, "has ended"):
            clock()
        for path in (self.proc.root / "7").iterdir():
            path.unlink()
        (self.proc.root / "7").rmdir()
        with self.assertRaisesRegex(PodIdentityError, "engine process 7 has ended"):
            clock()


class PodApiTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.account = Path(folder.name)
        for name, text in (("token", "token\n"), ("ca.crt", ""), ("namespace", "engines\n")):
            (self.account / name).write_text(text)
        patcher = patch.object(pod_identity, "SERVICE_ACCOUNT", self.account)
        patcher.start()
        self.addCleanup(patcher.stop)
        context = patch("ssl.create_default_context")
        context.start()
        self.addCleanup(context.stop)

    def refused(self, code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://kubernetes.default.svc", code, "", {}, None)

    def test_the_pod_is_read_with_the_service_account_token(self):
        response = io.BytesIO(json.dumps(pod_object()).encode())
        with patch("urllib.request.urlopen", return_value=response) as opened:
            pod = pod_identity.read_pod("engines", "engine-1")
        self.assertEqual(pod["metadata"]["uid"], POD_UID)
        [request] = opened.call_args.args
        self.assertEqual(
            request.full_url,
            "https://kubernetes.default.svc/api/v1/namespaces/engines/pods/engine-1",
        )
        self.assertEqual(request.headers["Authorization"], "Bearer token")
        self.assertEqual(pod_identity.service_account_namespace(), "engines")

    def test_api_refusals_name_the_missing_permission_or_pod(self):
        for code, message in (
            (403, "bind it to a Role with get on pods in its namespace"),
            (404, "pod engines/engine-1 does not exist"),
            (500, "returned HTTP 500"),
        ):
            with (
                self.subTest(code=code),
                patch("urllib.request.urlopen", side_effect=self.refused(code)),
                self.assertRaisesRegex(PodIdentityError, message),
            ):
                pod_identity.read_pod("engines", "engine-1")
        (self.account / "token").unlink()
        with self.assertRaisesRegex(PodIdentityError, "mounts its service account token"):
            pod_identity.read_pod("engines", "engine-1")


def identity(**changes) -> PodIdentity:
    values = {
        "namespace": "engines",
        "pod": "engine-1",
        "pod_uid": POD_UID,
        "node": "gpu-node-a",
        "pod_ip": "198.51.100.7",
        "container": "engine",
        "runtime": "containerd",
        "container_id": ENGINE_ID,
        "image": IMAGE,
        "image_id": IMAGE_ID,
        "started_at": STARTED_AT,
        "restart_count": 0,
        "pid": 7,
        "start_ticks": ENGINE_TICKS,
        "process_start_time_seconds": BOOT + 10,
    }
    return PodIdentity(**{**values, **changes})


class PodEngineTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patcher = patch.object(pod_engine, "POD_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = pod_engine.PodEngine(identity(), TRANSFER_DEVICE)
        self.engine.directory.mkdir(parents=True)

    def record(self, **records) -> None:
        (self.engine.directory / "inspections.json").write_text(json.dumps(records))

    def test_the_engine_directory_is_named_by_its_process(self):
        self.assertEqual(self.engine.directory, self.root / "narwhal-engine" / f"7-{ENGINE_TICKS}")
        self.assertEqual(self.engine.binding(), {"image_id": IMAGE_ID, "container_id": ENGINE_ID})
        values = {"NIXL_HOST_IP": "$(NARWHAL_POD_IP)", "OTHER": "kept"}
        # The engine container's serve step sets UCX_NET_DEVICES to the allocated HCA.
        self.assertEqual(
            self.engine.environment(values),
            {"NIXL_HOST_IP": "198.51.100.7", "OTHER": "kept", "UCX_NET_DEVICES": "hca0:1"},
        )

    def test_a_recorded_inspection_must_have_run_these_arguments_and_passed(self):
        log = self.root / "inspection.log"
        argv = ["-c", "print('inspected')"]
        self.record(passed={"argv": argv, "returncode": 0, "stdout": "inspected\n", "stderr": ""})
        self.assertEqual(
            pod_engine.recorded_inspection(self.engine, "passed", argv, log), "inspected\n"
        )
        self.assertIn("inspected", log.read_text())
        self.record(
            other={"argv": ["-c", "other"], "returncode": 0, "stdout": "", "stderr": ""},
            failed={"argv": argv, "returncode": 3, "stdout": "", "stderr": "broken"},
            stalled={"argv": argv, "error": "the inspection exceeded 300 seconds"},
        )
        for name, message in (
            ("missing", "recorded no missing inspection"),
            ("other", "ran other arguments"),
            ("failed", "exited 3"),
            ("stalled", "exceeded 300 seconds"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                pod_engine.recorded_inspection(self.engine, name, argv, self.root / f"{name}.log")
        (self.engine.directory / "inspections.json").unlink()
        with self.assertRaisesRegex(ValueError, "serve step writes it for process 7"):
            pod_engine.recorded_inspection(self.engine, "passed", argv, log)

    def test_an_attempt_binds_one_engine_process(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        pod_engine.write_identity(self.engine, attempt)
        self.assertEqual(
            pod_engine.require_identity(self.engine, attempt).name, "pod-identity.json"
        )
        recorded = json.loads((attempt / "pod-identity.json").read_text())
        self.assertEqual(recorded["transfer_device"], TRANSFER_DEVICE)
        restarted = pod_engine.PodEngine(identity(container_id="d" * 64, pid=8), TRANSFER_DEVICE)
        with self.assertRaisesRegex(ValueError, "the engine container started again"):
            pod_engine.require_identity(restarted, attempt)

    def test_the_engine_binds_only_in_its_pods_attestation_container(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(ValueError, "NARWHAL_POD_NAME is unset"),
        ):
            pod_engine.live_pod({"pod_uid": POD_UID})

    def test_the_engine_binds_to_the_checked_image_and_hca(self):
        checked = {"pod_uid": POD_UID, "planned_image": IMAGE, "transfer_device": TRANSFER_DEVICE}
        with (
            patch.dict("os.environ", {"NARWHAL_POD_NAME": "engine-1"}),
            patch.object(pod_engine, "service_account_namespace", return_value="engines"),
            patch.object(pod_engine, "read_identity", return_value=identity()) as read,
        ):
            engine = pod_engine.live_pod(checked)
            read.assert_called_once_with("engines", "engine-1", POD_UID, IMAGE)
            self.assertEqual(engine.transfer_device, TRANSFER_DEVICE)
            with self.assertRaisesRegex(ValueError, "records no allocated HCA"):
                pod_engine.live_pod({**checked, "transfer_device": None})


class SidecarPodTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.evidence = self.root / "pod"
        sleep = patch.object(sidecar.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        # A sidecar that keeps waiting fails the test instead of hanging it.
        self.sleep.side_effect = self.waited

    def waited(self, _: float) -> None:
        if self.sleep.call_count > 5:
            raise AssertionError("the sidecar kept waiting")

    def publish(self, container: str, **changes) -> Path:
        """Publish an attestation of `container` with its identity record, as capture does."""
        home = pod_engine.engine_home(self.evidence, container)
        attempt = capture_module.new_attempt(home)
        record = identity(container_id=container, **changes).record()
        (attempt / pod_engine.IDENTITY_RECORD).write_text(json.dumps(record))
        (attempt / "engine-attestation.json").write_text("{}")
        capture_module.publish(home, attempt)
        return home

    def verification(self, pod: dict) -> tuple[Path, dict]:
        with patch.object(sidecar, "read_pod", return_value=pod):
            return sidecar.pod_verification(self.evidence, {"pod_uid": POD_UID})

    def test_the_sidecar_waits_for_the_launch_directory_that_checks_its_pod(self):
        run = self.root / "launch"
        run.mkdir()

        def placed(_: float) -> None:
            (run / "launch.json").write_text('{"backend": "kubernetes"}')
            (run / "checked.json").write_text("{}")

        self.sleep.side_effect = placed
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            sidecar.await_launch(run, self.evidence)
        self.assertEqual(self.sleep.call_count, 1)
        self.assertIn(f"waiting for {run}, the launch directory", printed.getvalue())

    def test_the_sidecar_refuses_a_host_plan_without_waiting_for_its_check(self):
        run = self.root / "launch"
        run.mkdir()
        (run / "launch.json").write_text('{"backend": "container"}')
        with self.assertRaisesRegex(ValueError, "this plan's engine runs on a host"):
            sidecar.serve(run, self.evidence)
        self.sleep.assert_not_called()

    def test_verification_takes_the_attestation_of_the_live_engine_container(self):
        self.publish("d" * 64, started_at="2023-11-14T21:00:00Z")
        home = self.publish(ENGINE_ID)
        found, record = self.verification(pod_object())
        self.assertEqual(found, home)
        self.assertEqual(record["container_id"], ENGINE_ID)

    def test_verification_refuses_an_engine_container_its_attestation_does_not_bind(self):
        with self.assertRaisesRegex(ValueError, "has published an attestation"):
            self.verification(pod_object())
        self.publish("d" * 64)
        with self.assertRaisesRegex(ValueError, f"engine container {ENGINE_ID} .* no attestation"):
            self.verification(pod_object())
        self.publish(ENGINE_ID, image_id="sha256:" + "e" * 64)
        with self.assertRaisesRegex(ValueError, "image_id is sha256:c.*, not the sha256:e"):
            self.verification(pod_object())
        with self.assertRaisesRegex(ValueError, "is no longer the checked pod"):
            self.verification(pod_object(**{"metadata.uid": "replaced"}))

    def test_verification_refuses_attestations_of_another_pod(self):
        self.publish(ENGINE_ID, pod_uid="other")
        with self.assertRaisesRegex(ValueError, "attestations of pods other than the checked one"):
            self.verification(pod_object())

    def test_the_served_attestation_must_name_the_bound_image_and_process(self):
        recorded = identity().record()
        payload = {
            "launch": {"image_id": IMAGE_ID},
            "engine": {"process_start_time_seconds": BOOT + 10.5},
        }
        with patch.object(sidecar, "attested_launch", side_effect=lambda served: served["launch"]):
            self.assertEqual(sidecar.pod_failures(payload, recorded), [])
            wrong = {
                "launch": {"image_id": "sha256:" + "e" * 64},
                "engine": {"process_start_time_seconds": BOOT + 30},
            }
            failures = sidecar.pod_failures(wrong, recorded)
        self.assertEqual(len(failures), 2)
        self.assertIn("the engine container runs", failures[0])
        self.assertIn(f"not with engine container {ENGINE_ID}", failures[1])


class PodEvidenceTests(unittest.TestCase):
    def test_capture_names_pod_evidence_before_it_reads_the_check(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            (run / "launch.json").write_text('{"backend": "kubernetes"}')
            with self.assertRaisesRegex(ValueError, "pass --pod-evidence naming that directory"):
                capture_module.capture(run)
            (run / "launch.json").write_text('{"backend": "native"}')
            with self.assertRaisesRegex(ValueError, "this plan's engine runs on a host"):
                capture_module.capture(run, pod_evidence=run / "pod")

    def test_only_a_kubernetes_plan_keeps_its_attestation_in_its_pods_directory(self):
        pod = {"backend": "kubernetes"}
        self.assertTrue(capture_module.require_pod_evidence(pod, Path("/narwhal-state")))
        self.assertFalse(capture_module.require_pod_evidence({"backend": "native"}, None))
        with self.assertRaisesRegex(ValueError, "pass --pod-evidence naming that directory"):
            capture_module.require_pod_evidence(pod, None)
        with self.assertRaisesRegex(ValueError, "this plan's engine runs on a host"):
            capture_module.require_pod_evidence({}, Path("/narwhal-state"))


class PodHomeTests(unittest.TestCase):
    def test_an_attempt_publishes_only_under_the_container_it_attests(self):
        with tempfile.TemporaryDirectory() as folder:
            home = pod_engine.engine_home(Path(folder), ENGINE_ID)
            attempt = capture_module.new_attempt(home)
            (attempt / pod_engine.IDENTITY_RECORD).write_text(json.dumps(identity().record()))
            capture_module.require_home(home, attempt)
            restarted = identity(container_id="d" * 64).record()
            (attempt / pod_engine.IDENTITY_RECORD).write_text(json.dumps(restarted))
            with self.assertRaisesRegex(ValueError, "started again during the capture"):
                capture_module.require_home(home, attempt)


class ProcessWatchTests(unittest.TestCase):
    def test_the_sidecar_stops_once_its_engine_process_ends(self):
        stopped = threading.Event()

        def ended() -> float:
            raise PodIdentityError("engine process 7 has ended")

        with (
            patch.object(attest, "POD_WATCH_S", 0),
            patch("os.kill", side_effect=lambda *_: stopped.set()) as kill,
            contextlib.redirect_stderr(io.StringIO()) as printed,
        ):
            attest.watch_process(ended)
            self.assertTrue(stopped.wait(5))
        self.assertEqual(kill.call_args.args[1], attest.signal.SIGTERM)
        self.assertIn("engine process 7 has ended; stopping", printed.getvalue())

    def test_a_pod_engine_needs_no_other_process_owner(self):
        with patch.object(attest, "pod_clock") as clock, patch.object(attest, "uvicorn"):
            clock.side_effect = PodIdentityError("no process of container")
            with contextlib.redirect_stderr(io.StringIO()):
                code = attest.main(
                    [
                        "--document",
                        "/missing.json",
                        "--engine-base",
                        "http://127.0.0.1:1",
                        "--backend",
                        "sglang",
                        "--engine-pod-container",
                        ENGINE_ID,
                    ]
                )
        # The document fails to load before the sidecar reads the pod.
        self.assertEqual(code, 2)
        clock.assert_not_called()

    def test_a_pod_engine_is_followed_through_its_containers_first_process(self):
        document = unittest.mock.Mock()
        document.contract.engine_version = "1.0"
        live = unittest.mock.Mock(version="1.0")
        with (
            patch.object(attest.AttestationDocument, "load", return_value=document),
            patch.object(
                attest, "fetch_engine_identity", new=unittest.mock.AsyncMock(return_value=live)
            ) as fetched,
            patch.object(attest, "pod_clock") as clock,
            patch.object(attest, "watch_process") as watched,
            patch.object(attest, "build_app") as built,
            patch.object(attest, "uvicorn"),
        ):
            code = attest.main(
                [
                    "--document",
                    "/document.json",
                    "--engine-base",
                    "http://127.0.0.1:1",
                    "--backend",
                    "sglang",
                    "--engine-pod-container",
                    ENGINE_ID,
                    "--hca",
                    "hca0:1",
                ]
            )
        self.assertEqual(code, 0)
        clock.assert_called_once_with(ENGINE_ID)
        self.assertIs(fetched.call_args.kwargs["process"], clock.return_value)
        watched.assert_called_once_with(clock.return_value)
        self.assertIs(built.call_args.kwargs["process"], clock.return_value)
        self.assertEqual(str(built.call_args.kwargs["hca"].device), "hca0")


if __name__ == "__main__":
    unittest.main()
