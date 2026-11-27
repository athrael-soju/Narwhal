"""The attestation capture command: its attempts, its failures and recovery, and its checks."""

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.backends.vllm import attestation as vllm_attestation
from narwhal.deployment.attestation_contract import capture as capture_module
from narwhal.deployment.attestation_contract import cli
from narwhal.deployment.attestation_contract import sidecar as sidecar_module
from narwhal.deployment.attestation_contract.capture import (
    ATTEMPTS,
    FAILURE,
    PUBLISHED,
    AlreadyPublished,
    Capture,
    CaptureFailed,
    capture,
    publish,
    published_attempt,
    save_startup_log,
)
from narwhal.deployment.attestation_contract.document import engine_document
from narwhal.deployment.launch_engine.check import read_checked
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from tests.backends.contract import (
    ATTEMPT,
    LiveContainer,
    fixtures,
    started_container,
)

ROOT = Path(__file__).resolve().parents[2]
# A vLLM 0.30.0 engine's startup log with NixlConnector, trimmed to its connector lines.
STARTUP_LOG = ROOT / "tests/data/vllm-0.30.0-nixl-startup.log"
CONNECTOR = "vllm.distributed.kv_transfer.kv_connector.v1.nixl.connector."
CONTAINER = "b" * 64
PROCESS = {"pid": 1234, "boot_id": "boot", "start_ticks": 5678}
# The first HTTP route each backend's captures read.
FIRST_ROUTE = {"vllm": "/version", "sglang": "/server_info", "trtllm": "/version"}
FIRST_HTTP_CAPTURE = {"vllm": "http-identity", "sglang": "server-info", "trtllm": "server-info"}
BACKENDS = tuple(FIRST_ROUTE)


class StartedEngine(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.index = 0

    def started(self, name: str, **answers: object) -> tuple[Path, LiveContainer]:
        """A started container engine that answers its captures as the backend's fixtures hold,
        with the answer fields in `answers` replaced."""
        self.index += 1
        root = self.root / f"{name}-{self.index}"
        root.mkdir()
        run, reference = started_container(root, name)
        fixture = fixtures(name)
        assert fixture.engine_answers is not None
        replies = dataclasses.replace(fixture.engine_answers(reference), **answers)
        live = LiveContainer(run, read_checked(run)["image_id"], replies, fixture.startup_log)
        return run, live


class CaptureRecoveryTests(StartedEngine):
    def test_a_failed_capture_stays_as_evidence_and_capturing_again_publishes(self):
        for name in BACKENDS:
            with self.subTest(backend=name):
                run, live = self.started(name)
                live.unavailable.add(FIRST_ROUTE[name])
                with live.serving():
                    with self.assertRaises(CaptureFailed) as failed:
                        capture(run)
                    self.assertEqual(failed.exception.capture, FIRST_HTTP_CAPTURE[name])
                    self.assertRegex(
                        str(failed.exception),
                        rf"^Capture {FIRST_HTTP_CAPTURE[name]} \(.+\) failed: Server error "
                        rf"'503 Service Unavailable' for url '.+{FIRST_ROUTE[name]}'",
                    )
                    self.assertFalse((run / PUBLISHED).exists())
                    with self.assertRaisesRegex(ValueError, f"^{PUBLISHED} is missing"):
                        published_attempt(run)
                    (first,) = (run / ATTEMPTS).iterdir()
                    record = json.loads((first / FAILURE).read_text())
                    self.assertEqual(record["capture"], FIRST_HTTP_CAPTURE[name])
                    self.assertEqual(record["error"], failed.exception.reason)
                    self.assertEqual((first / FAILURE).stat().st_mode & 0o777, 0o600)
                    self.assertFalse((first / "engine-attestation.json").exists())

                    document = capture(run)
                    second = document.parent
                    self.assertNotEqual(second, first)
                    self.assertEqual(published_attempt(run), second)
                    self.assertEqual(
                        json.loads((run / PUBLISHED).read_text()),
                        {
                            "attempt": second.name,
                            "document_sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
                        },
                    )
                    self.assertEqual(json.loads(document.read_text()), engine_document(run, second))
                    # The failed attempt keeps what it captured.
                    self.assertTrue((first / "startup.log").is_file())
                    self.assertFalse((second / FAILURE).exists())
                    self.assertEqual(second.stat().st_mode & 0o777, 0o700)

                    with self.assertRaisesRegex(
                        ValueError, f"^Attempt {second.name} already published"
                    ):
                        capture(run)
                self.assertEqual(sorted((run / ATTEMPTS).iterdir()), sorted([first, second]))

    def test_the_first_published_attempt_wins(self):
        run, live = self.started("sglang")
        first: list[Path] = []

        def race(run: Path, attempt: Path) -> Path:
            # Another capture of this launch directory passes and publishes first.
            if not first:
                first.append(Path(shutil.copytree(attempt, run / ATTEMPTS / ATTEMPT)))
                publish(run, first[0])
            return publish(run, attempt)

        with (
            live.serving(),
            patch.object(capture_module, "publish", side_effect=race),
            self.assertRaisesRegex(AlreadyPublished, f"^Attempt {ATTEMPT} already published"),
        ):
            capture(run)
        self.assertEqual(published_attempt(run), first[0])
        (lost,) = [path for path in (run / ATTEMPTS).iterdir() if path != first[0]]
        self.assertTrue((lost / "engine-attestation.json").is_file())
        self.assertEqual(
            json.loads((lost / FAILURE).read_text()),
            {
                "capture": "publish",
                "output": PUBLISHED,
                "error": "another attempt published first",
                "published": ATTEMPT,
            },
        )
        self.assertFalse((first[0] / FAILURE).exists())
        self.assertEqual(sorted(path.name for path in run.glob(PUBLISHED + "*")), [PUBLISHED])

    def test_only_a_capture_attempt_is_published_or_read(self):
        run, live = self.started("trtllm")
        with live.serving():
            document = capture(run)
        for outside in (run / "elsewhere", run / ATTEMPTS / "not-an-attempt"):
            with (
                self.subTest(attempt=outside),
                self.assertRaisesRegex(ValueError, "is not a capture attempt of"),
            ):
                publish(run, outside)
        record = json.loads((run / PUBLISHED).read_text())
        for name in ("../../elsewhere", "/tmp", f"{document.parent.name}/..", "", 7):
            with self.subTest(name=name):
                (run / PUBLISHED).write_text(json.dumps({**record, "attempt": name}))
                with self.assertRaisesRegex(ValueError, f"^{PUBLISHED} names no capture attempt$"):
                    published_attempt(run)

    def test_a_published_document_must_match_its_record(self):
        run, live = self.started("trtllm")
        with live.serving():
            document = capture(run)
        record = json.loads(document.read_text())
        record["contract"]["engine_version"] = "0.0.0"
        document.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, f"differs from the document {PUBLISHED} names"):
            published_attempt(run)

    def test_a_restarted_container_is_refused_at_capture_and_after_publishing(self):
        for name in BACKENDS:
            with self.subTest(backend=name):
                run, live = self.started(name)
                with live.serving():
                    document = capture(run)
                    live.restart("2026-01-01T01:00:00Z", "the engine started again\n")
                    for refused, arguments in (
                        (engine_document, (run, document.parent)),
                        (sidecar_module.serve, (run,)),
                        (save_startup_log, (run, self.root)),
                    ):
                        with self.assertRaisesRegex(ValueError, "^Serving container started again"):
                            refused(*arguments)

                run, live = self.started(name)
                live.restart("2026-01-01T01:00:00Z", "the engine started again\n")
                with live.serving(), self.assertRaises(CaptureFailed) as failed:
                    capture(run)
                self.assertEqual(failed.exception.capture, "startup-log")
                self.assertIn("Serving container started again", failed.exception.reason)
                (attempt,) = (run / ATTEMPTS).iterdir()
                self.assertEqual(sorted(path.name for path in attempt.iterdir()), [FAILURE])

    def test_serve_requires_the_live_engine_process_its_captures_attest(self):
        run, live = self.started("vllm")
        route = {"NARWHAL_NODE_1_ATTESTATION_URL": "http://192.0.2.11:8010/v1/attestation"}
        with (
            live.serving(),
            patch.dict(os.environ, route),
            patch.object(sidecar_module, "attest_main", return_value=0) as sidecar,
        ):
            capture(run)
            self.assertEqual(sidecar_module.serve(run), 0)
            # The engine process starts again; its container keeps running.
            live.answers.routes["/metrics"] = "process_start_time_seconds 1767229200.0\n"
            with self.assertRaisesRegex(
                ValueError,
                "^The engine reports process start 1767229200.0, not the 1767225600.25 its "
                "attestation captured",
            ):
                sidecar_module.serve(run)
        sidecar.assert_called_once()

    def test_the_startup_log_holds_only_the_recorded_container_start(self):
        run, live = self.started("vllm")
        # Lines an earlier start of the container wrote, which `docker logs` also returns.
        live.logs.insert(0, ("2025-12-31T23:00:00Z", "Creating v1 connector with name: X\n"))
        with live.serving():
            document = capture(run)
        self.assertEqual(
            (document.parent / "startup.log").read_text(), fixtures("vllm").startup_log
        )

    def test_a_defect_is_not_reported_as_a_capture_failure(self):
        run, live = self.started("vllm")
        with (
            live.serving(),
            patch.object(capture_module, "generate", side_effect=RuntimeError("defect")),
            self.assertRaisesRegex(RuntimeError, "^defect$"),
        ):
            capture(run)
        (attempt,) = (run / ATTEMPTS).iterdir()
        self.assertFalse((attempt / FAILURE).exists())
        self.assertTrue((attempt / "handshake-policy.json").is_file())
        self.assertFalse((run / PUBLISHED).exists())

    def test_a_missing_field_names_the_field(self):
        step = Capture("transfer-mode", "transfer-mode.json", lambda: None)
        failed = CaptureFailed(step, KeyError("resolved_connector"))
        self.assertEqual(
            str(failed),
            "Capture transfer-mode (transfer-mode.json) failed: missing field 'resolved_connector'",
        )


class VllmCaptureTests(StartedEngine):
    def test_the_startup_log_must_create_the_connector_the_check_resolved(self):
        push = json.dumps({"connector": CONNECTOR + "NixlPushConnector"}) + "\n"
        run, live = self.started("vllm", check_log=push)
        with live.serving(), self.assertRaises(CaptureFailed) as failed:
            capture(run)
        self.assertEqual(failed.exception.capture, "startup-connector")
        self.assertEqual(
            failed.exception.reason,
            "startup.log records the engine creating NixlPullConnector, "
            "not the resolved NixlPushConnector",
        )
        (attempt,) = (run / ATTEMPTS).iterdir()
        transfer = json.loads((attempt / "transfer-mode.json").read_text())
        self.assertEqual(transfer["transfer_mode"], "push")
        self.assertFalse((attempt / "handshake-policy.json").exists())

    def test_a_representative_layout_must_match_before_any_capture(self):
        run, live = self.started("vllm")
        with (
            live.serving(),
            self.assertRaisesRegex(
                ValueError, "^Resolved layout NHD differs from the representative's LBNHC$"
            ),
        ):
            capture(run, "LBNHC")
        self.assertEqual(live.calls, [])
        self.assertEqual(list((run / ATTEMPTS).iterdir()), [])
        with live.serving():
            document = capture(run, "NHD")
        registration = json.loads((document.parent / "cache-registration.json").read_text())
        self.assertEqual(registration["kv_cache_layout"], "NHD")

    def test_only_vllm_takes_a_representative_layout(self):
        for name, label in (("sglang", "SGLang"), ("trtllm", "TensorRT-LLM")):
            with self.subTest(backend=name):
                run, live = self.started(name)
                with (
                    live.serving(),
                    self.assertRaisesRegex(ValueError, f"^{label} engines register no cache"),
                ):
                    capture(run, "LBNHC")
                self.assertEqual(list((run / ATTEMPTS).iterdir()), [])
                self.assertEqual(live.calls, [])


class CommandTests(StartedEngine):
    def test_the_command_reports_the_failed_capture(self):
        run, live = self.started("sglang")
        live.restart("2026-01-01T01:00:00Z", "")
        with (
            live.serving(),
            contextlib.redirect_stderr(io.StringIO()) as error,
            self.assertRaises(SystemExit) as exited,
        ):
            cli.main(["capture", "--run", str(run)])
        self.assertEqual(exited.exception.code, 1)
        self.assertRegex(
            error.getvalue(),
            r"^Capture startup-log \(startup\.log\) failed: Serving container started again "
            r"[^\n]+\n$",
        )

    def test_the_command_prints_the_published_document(self):
        run, live = self.started("vllm")
        with live.serving(), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["capture", "--run", str(run)]), 0)
        document = published_attempt(run) / "engine-attestation.json"
        self.assertEqual(output.getvalue(), f"Generated private engine attestation in {document}\n")


def checked_run(root: Path, backend: str) -> Path:
    """A vLLM launch directory whose plan and runtime check agree."""
    run = root / backend
    run.mkdir()
    plan = {
        "engine": "vllm",
        "backend": backend,
        "role": "engine-1",
        "endpoint": "http://engine.invalid:8000",
        "image": "" if backend == "native" else "sha256:" + "a" * 64,
        "python_executable": sys.executable,
        "expected_packages": {"vllm": "0.30.0"},
        "connector": {"kv_connector": "NixlConnector", "kv_role": "kv_both"},
    }
    (run / "launch.json").write_text(json.dumps(plan))
    checked = {"plan_sha256": hashlib.sha256((run / "launch.json").read_bytes()).hexdigest()}
    if backend == "native":
        checked.update(
            backend="native",
            python_executable=plan["python_executable"],
            expected_packages=plan["expected_packages"],
        )
    else:
        checked["image_id"] = plan["image"]
    (run / "checked.json").write_text(json.dumps(checked))
    (run / "container-start.json").write_text(
        json.dumps({"container_id": CONTAINER, "started_at": "2026-01-01T00:00:00Z"})
    )
    attempt = run / ATTEMPTS / ATTEMPT
    attempt.mkdir(parents=True)
    return run


class StartupLogTests(unittest.TestCase):
    def test_a_failed_docker_logs_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            run = checked_run(Path(folder), "container")
            attempt = run / ATTEMPTS / ATTEMPT
            failed = subprocess.CompletedProcess([], 1, stdout=b"Error: No such container\n")
            with (
                patch.object(capture_module, "live_identity", return_value=CONTAINER),
                patch.object(capture_module.subprocess, "run", return_value=failed) as docker,
                self.assertRaisesRegex(
                    ValueError, "^docker logs exited 1: Error: No such container$"
                ),
            ):
                save_startup_log(run, attempt)
            self.assertEqual(
                docker.call_args.args,
                (["docker", "logs", "--since", "2026-01-01T00:00:00Z", CONTAINER],),
            )
            self.assertEqual(docker.call_args.kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(docker.call_args.kwargs["timeout"], capture_module.DOCKER_LOGS_SECONDS)
            self.assertEqual(list(attempt.iterdir()), [])

    def test_a_native_engine_attests_a_copy_of_its_growing_log(self):
        with tempfile.TemporaryDirectory() as folder:
            run = checked_run(Path(folder), "native")
            attempt = run / ATTEMPTS / ATTEMPT
            (run / "startup.log").write_bytes(STARTUP_LOG.read_bytes())
            with (
                patch.object(capture_module, "live_identity", return_value=PROCESS),
                patch.object(capture_module.subprocess, "run") as docker,
            ):
                snapshot = save_startup_log(run, attempt)
            docker.assert_not_called()
            self.assertEqual(snapshot, attempt / "startup.log")
            self.assertEqual(snapshot.read_bytes(), STARTUP_LOG.read_bytes())
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)


class TransferModeTests(unittest.TestCase):
    def check_log(self, run: Path, *attempts: tuple[str, list[str]]) -> Path:
        name = "runtime-check.log" if run.name == "native" else "image-check.log"
        lines = ["startup diagnostic"]
        for plan_hash, classes in attempts:
            lines.append(json.dumps({"check_attempt": "1" * 32, "plan_sha256": plan_hash}))
            lines += [json.dumps({"connector": CONNECTOR + item}) for item in classes]
        log = run / name
        log.write_text("\n".join(lines) + "\n")
        return log

    def plan_hash(self, run: Path) -> str:
        return hashlib.sha256((run / "launch.json").read_bytes()).hexdigest()

    def test_the_resolved_class_gives_the_mode_and_keeps_its_source(self):
        for backend, identity in (("container", CONTAINER), ("native", PROCESS)):
            for name, mode in (("NixlPullConnector", "pull"), ("NixlPushConnector", "push")):
                with (
                    self.subTest(backend=backend, connector=name),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    run = checked_run(Path(folder), backend)
                    attempt = run / ATTEMPTS / ATTEMPT
                    log = self.check_log(run, (self.plan_hash(run), [name, name]))
                    original = log.read_bytes()
                    with patch.object(vllm_attestation, "live_identity", return_value=identity):
                        output = vllm_attestation.capture_transfer_mode(run, attempt)
                    binding = (
                        {"process": PROCESS}
                        if backend == "native"
                        else {"image_id": "sha256:" + "a" * 64}
                    )
                    self.assertEqual(output, attempt / "transfer-mode.json")
                    self.assertEqual(
                        json.loads(output.read_text()),
                        {
                            "transfer_mode": mode,
                            "configured_connector": "NixlConnector",
                            "kv_role": "kv_both",
                            "resolved_connector": CONNECTOR + name,
                            "plan_sha256": self.plan_hash(run),
                            "source": str(log.resolve()),
                            "source_sha256": hashlib.sha256(original).hexdigest(),
                            **binding,
                        },
                    )
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(log.read_bytes(), original)

    def test_an_unknown_ambiguous_or_missing_class_is_refused(self):
        for classes, reason in (
            (["NixlConnector"], "Inspect the pinned connector implementation"),
            (["NixlPullConnector", "NixlPushConnector"], "holds 2 factory-resolved"),
            ([], "holds 0 factory-resolved"),
        ):
            with self.subTest(classes=classes), tempfile.TemporaryDirectory() as folder:
                run = checked_run(Path(folder), "container")
                attempt = run / ATTEMPTS / ATTEMPT
                self.check_log(run, (self.plan_hash(run), classes))
                with (
                    patch.object(vllm_attestation, "live_identity", return_value=CONTAINER),
                    self.assertRaisesRegex(ValueError, reason),
                ):
                    vllm_attestation.capture_transfer_mode(run, attempt)
                self.assertFalse((attempt / "transfer-mode.json").exists())

    def test_only_check_attempts_for_this_plan_count(self):
        with tempfile.TemporaryDirectory() as folder:
            run = checked_run(Path(folder), "container")
            attempt = run / ATTEMPTS / ATTEMPT
            self.check_log(
                run, ("0" * 64, ["NixlPushConnector"]), (self.plan_hash(run), ["NixlPullConnector"])
            )
            with patch.object(vllm_attestation, "live_identity", return_value=CONTAINER):
                output = vllm_attestation.capture_transfer_mode(run, attempt)
            self.assertEqual(json.loads(output.read_text())["transfer_mode"], "pull")
        with tempfile.TemporaryDirectory() as folder:
            run = checked_run(Path(folder), "container")
            self.check_log(run, ("0" * 64, ["NixlPullConnector"]))
            with (
                patch.object(vllm_attestation, "live_identity", return_value=CONTAINER),
                self.assertRaisesRegex(ValueError, "rerun the runtime check"),
            ):
                vllm_attestation.capture_transfer_mode(run, run / ATTEMPTS / ATTEMPT)


class StartupConnectorTests(unittest.TestCase):
    def attempt(self, root: Path, resolved: str, log: str) -> Path:
        (root / "transfer-mode.json").write_text(
            json.dumps({"resolved_connector": CONNECTOR + resolved})
        )
        (root / "startup.log").write_text(log)
        return root

    def test_a_live_vllm_log_confirms_the_resolved_class(self):
        with tempfile.TemporaryDirectory() as folder:
            attempt = self.attempt(Path(folder), "NixlPullConnector", STARTUP_LOG.read_text())
            vllm_attestation.check_startup_connector(attempt)

    def test_another_class_is_refused(self):
        # The fixture names NixlConnector in its layout and deprecation lines; only the
        # factory line names the class the engine created.
        self.assertIn("NixlConnector setting KV cache layout", STARTUP_LOG.read_text())
        with tempfile.TemporaryDirectory() as folder:
            attempt = self.attempt(Path(folder), "NixlPushConnector", STARTUP_LOG.read_text())
            with self.assertRaisesRegex(
                ValueError,
                "^startup.log records the engine creating NixlPullConnector, "
                "not the resolved NixlPushConnector$",
            ):
                vllm_attestation.check_startup_connector(attempt)

    def test_a_log_without_a_created_connector_is_refused(self):
        lines = [
            line
            for line in STARTUP_LOG.read_text().splitlines(keepends=True)
            if "Creating v1 connector" not in line
        ]
        with tempfile.TemporaryDirectory() as folder:
            attempt = self.attempt(Path(folder), "NixlPullConnector", "".join(lines))
            with self.assertRaisesRegex(ValueError, "records no connector the engine created"):
                vllm_attestation.check_startup_connector(attempt)

    def test_a_mixed_log_is_refused(self):
        push = STARTUP_LOG.read_text().replace(
            "name: NixlPullConnector and", "name: NixlPushConnector and", 1
        )
        with tempfile.TemporaryDirectory() as folder:
            attempt = self.attempt(Path(folder), "NixlPullConnector", push)
            with self.assertRaisesRegex(
                ValueError, "creating NixlPullConnector, NixlPushConnector, not the resolved"
            ):
                vllm_attestation.check_startup_connector(attempt)


class VerifyTests(unittest.TestCase):
    DOCUMENT = ROOT / "config/engine-attestation.example.json"
    ROUTE = "http://sidecar.invalid:8010/v1/attestation"

    def launch(self, root: Path, engine: str = "vllm") -> Path:
        run = root / "run"
        run.mkdir()
        plan = {"engine": engine, "role": "engine-1", "endpoint": "http://engine.invalid:8000"}
        (run / "launch.json").write_text(json.dumps(plan))
        key = {"vllm": "VLLM_API_KEY", "sglang": "NARWHAL_SGLANG_API_KEY"}[engine]
        (run / "container.env").write_text(f"{key}=synthetic-key\n")
        attempt = run / ATTEMPTS / ATTEMPT
        attempt.mkdir(parents=True)
        shutil.copy(self.DOCUMENT, attempt / "engine-attestation.json")
        publish(run, attempt)
        return run

    def verify(self, run: Path, status: int, attested: EngineIdentity, live: EngineIdentity):
        document = AttestationDocument.load(self.DOCUMENT)
        responses = [
            httpx.Response(status, json=body)
            for body in ({"status": "ok"}, make_attestation(document, attested))
        ]
        with (
            patch.dict(os.environ, {"NARWHAL_NODE_1_ATTESTATION_URL": self.ROUTE}),
            patch.object(sidecar_module.httpx, "Client") as client,
            patch.object(
                sidecar_module, "fetch_engine_identity", new_callable=AsyncMock, return_value=live
            ) as fetch,
        ):
            client.return_value.__enter__.return_value.get.side_effect = responses
            try:
                return sidecar_module.verify(run), fetch, client
            except ValueError as error:
                return error, fetch, client

    def test_the_sidecar_must_answer_and_match_the_live_engine(self):
        version = AttestationDocument.load(self.DOCUMENT).contract.engine_version
        live = EngineIdentity(version, 200.0)
        stale = EngineIdentity(version, 100.0)
        for status, attested, failure in (
            (200, live, None),
            (200, stale, "^Sidecar attestation fails against the live engine: "),
            (503, live, "^Sidecar health returned HTTP 503; inspect "),
        ):
            with (
                self.subTest(status=status, attested=attested),
                tempfile.TemporaryDirectory() as folder,
            ):
                run = self.launch(Path(folder))
                result, fetch, client = self.verify(run, status, attested, live)
                (out,) = run.glob("attestation-check-*")
                if failure is None:
                    self.assertEqual(result, out)
                else:
                    self.assertRegex(str(result), failure)
                get = client.return_value.__enter__.return_value.get
                self.assertEqual(
                    [call.args[0] for call in get.call_args_list],
                    ["http://sidecar.invalid:8010/health", self.ROUTE],
                )
                if status == 503:
                    fetch.assert_not_called()
                    self.assertFalse((out / "identity.json").exists())
                else:
                    fetch.assert_awaited_once_with(
                        "http://engine.invalid:8000",
                        headers={"Authorization": "Bearer synthetic-key"},
                    )
                    self.assertEqual(
                        json.loads((out / "identity.json").read_text()),
                        {"version": version, "process_start_time_seconds": 200.0},
                    )
                self.assertEqual((out / "health.status").read_text(), f"{status}\n")
                self.assertEqual((out / "attestation.status").read_text(), f"{status}\n")
                self.assertTrue((out / "attestation.json").is_file())
                self.assertEqual(out.stat().st_mode & 0o777, 0o700)
                for path in out.iterdir():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertNotIn("synthetic-key", path.read_text())

    def test_an_engine_without_a_process_start_reads_it_through_the_sidecar(self):
        with tempfile.TemporaryDirectory() as folder:
            run = self.launch(Path(folder), "sglang")
            version = AttestationDocument.load(self.DOCUMENT).contract.engine_version
            live = EngineIdentity(version, 200.0)
            _, fetch, _ = self.verify(run, 200, live, live)
            options = fetch.await_args.kwargs
            self.assertEqual(options["attestation_url"], self.ROUTE)
            self.assertFalse(options["reader"].reports_process_start)

    def test_the_sidecar_route_must_be_configured(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"NARWHAL_NODE_1_ATTESTATION_URL": "http://sidecar.invalid"}),
            self.assertRaisesRegex(ValueError, "NARWHAL_NODE_1_ATTESTATION_URL must name"),
        ):
            sidecar_module.verify(self.launch(Path(folder)))

    def test_verify_needs_a_published_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            run = self.launch(Path(folder))
            (run / PUBLISHED).unlink()
            with (
                patch.dict(os.environ, {"NARWHAL_NODE_1_ATTESTATION_URL": self.ROUTE}),
                self.assertRaisesRegex(ValueError, f"^{PUBLISHED} is missing"),
            ):
                sidecar_module.verify(run)


if __name__ == "__main__":
    unittest.main()
