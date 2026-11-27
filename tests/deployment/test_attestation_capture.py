"""The attestation capture command: its order per backend, its failures, and its checks."""

import contextlib
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
from unittest.mock import AsyncMock, Mock, patch

import httpx

from narwhal.backends.vllm import attestation as vllm_attestation
from narwhal.backends.vllm.launch import VllmLauncher
from narwhal.deployment.attestation_contract import capture as capture_module
from narwhal.deployment.attestation_contract import cli
from narwhal.deployment.attestation_contract import sidecar as sidecar_module
from narwhal.deployment.attestation_contract.capture import (
    CaptureFailed,
    capture,
    match_representative,
    save_startup_log,
)
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation

ROOT = Path(__file__).resolve().parents[2]
# A vLLM 0.30.0 engine's startup log with NixlConnector, trimmed to its connector lines.
STARTUP_LOG = ROOT / "tests/data/vllm-0.30.0-nixl-startup.log"
CONNECTOR = "vllm.distributed.kv_transfer.kv_connector.v1.nixl.connector."
CONTAINER = "b" * 64
PROCESS = {"pid": 1234, "boot_id": "boot", "start_ticks": 5678}

VLLM = "narwhal.backends.vllm.attestation"
CAPTURE = "narwhal.deployment.attestation_contract.capture"
# The function each capture runs, by capture name.
LEAVES = {
    "startup-log": (CAPTURE, "save_startup_log"),
    "model-dimensions": (CAPTURE, "capture_model_dimensions"),
    "connector-version": (VLLM, "capture_nixl"),
    "cache-registration": (VLLM, "registration_layout"),
    "transfer-mode": (VLLM, "capture_transfer_mode"),
    "startup-connector": (VLLM, "check_startup_connector"),
    "handshake-policy": (VLLM, "handshake_policy"),
    "http-identity": (VLLM, "capture_native_http"),
    "representative-layout": (CAPTURE, "match_representative"),
    "document": (CAPTURE, "generate"),
}
VLLM_CAPTURES = [
    "startup-log",
    "model-dimensions",
    "connector-version",
    "cache-registration",
    "transfer-mode",
    "startup-connector",
    "handshake-policy",
]
SERVER_CAPTURES = ["startup-log", "model-dimensions", "server-info"]


def checked_run(root: Path, engine: str, backend: str, **fields: object) -> Path:
    """A launch directory whose plan and runtime check agree."""
    run = root / f"{engine}-{backend}"
    run.mkdir()
    plan = {
        "engine": engine,
        "backend": backend,
        "role": "engine-1",
        "endpoint": "http://engine.invalid:8000",
        "image": "" if backend == "native" else "sha256:" + "a" * 64,
        "python_executable": sys.executable,
        "expected_packages": {"vllm": "0.30.0"},
        "connector": {"kv_connector": "NixlConnector", "kv_role": "kv_both"},
        **fields,
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
    return run


class CaptureOrderTests(unittest.TestCase):
    def cases(self):
        for backend in ("container", "native"):
            vllm = [*VLLM_CAPTURES, *(["http-identity"] if backend == "native" else [])]
            yield "vllm", backend, None, [*vllm, "document"]
            yield "vllm", backend, "LBNHC", [*vllm, "representative-layout", "document"]
            for engine in ("sglang", "trtllm"):
                yield engine, backend, None, [*SERVER_CAPTURES, "document"]

    @contextlib.contextmanager
    def leaves(self, engine: str):
        """Replace every capture with a mock that records its name."""
        ran: list[str] = []
        mocks = {}
        targets = dict(LEAVES)
        if engine != "vllm":
            targets["server-info"] = (f"narwhal.backends.{engine}.attestation", "capture_server")
        with contextlib.ExitStack() as stack:
            for name, (module, function) in targets.items():
                mock = Mock(side_effect=lambda *_, name=name: ran.append(name))
                mocks[name] = mock
                stack.enter_context(patch(f"{module}.{function}", mock))
            yield ran, mocks

    def test_each_backend_runs_every_capture_in_order(self):
        for engine, backend, layout, expected in self.cases():
            with (
                self.subTest(engine=engine, backend=backend, layout=layout),
                tempfile.TemporaryDirectory() as folder,
                self.leaves(engine) as (ran, mocks),
            ):
                run = checked_run(Path(folder), engine, backend)
                self.assertEqual(capture(run, layout), run / "engine-attestation.json")
                self.assertEqual(ran, expected)
                log = "startup-attestation.log" if backend == "native" else "startup.log"
                mocks["document"].assert_called_once_with(run, run / log)
                if layout is not None:
                    registration = mocks["representative-layout"].call_args.args
                    self.assertIsInstance(registration[0], VllmLauncher)
                    self.assertEqual(registration[1:], (run, layout))

    def test_a_failing_capture_is_named_and_stops_the_rest(self):
        for engine, backend, layout, expected in self.cases():
            for index, failing in enumerate(expected):
                with (
                    self.subTest(engine=engine, backend=backend, layout=layout, capture=failing),
                    tempfile.TemporaryDirectory() as folder,
                    self.leaves(engine) as (ran, mocks),
                ):
                    run = checked_run(Path(folder), engine, backend)

                    def fail(*_, name=failing):
                        ran.append(name)
                        raise ValueError("synthetic cause")

                    mocks[failing].side_effect = fail
                    with self.assertRaisesRegex(
                        CaptureFailed, rf"^Capture {failing} \(.+\) failed: synthetic cause$"
                    ) as failed:
                        capture(run, layout)
                    self.assertEqual(failed.exception.capture, failing)
                    self.assertEqual(ran, expected[: index + 1])

    def test_failures_name_the_output_and_a_missing_field(self):
        with tempfile.TemporaryDirectory() as folder, self.leaves("vllm") as (_, mocks):
            run = checked_run(Path(folder), "vllm", "container")
            mocks["transfer-mode"].side_effect = KeyError("resolved_connector")
            with self.assertRaisesRegex(
                CaptureFailed,
                r"^Capture transfer-mode \(transfer-mode\.json\) failed: "
                r"missing field 'resolved_connector'$",
            ):
                capture(run)

    def test_only_vllm_takes_a_representative_layout(self):
        for engine, label in (("sglang", "SGLang"), ("trtllm", "TensorRT-LLM")):
            with (
                self.subTest(engine=engine),
                tempfile.TemporaryDirectory() as folder,
                self.leaves(engine) as (ran, _),
            ):
                run = checked_run(Path(folder), engine, "container")
                with self.assertRaisesRegex(ValueError, f"^{label} engines register no cache"):
                    capture(run, "LBNHC")
                self.assertEqual(ran, [])

    def test_the_command_reports_the_failed_capture(self):
        with tempfile.TemporaryDirectory() as folder, self.leaves("vllm") as (_, mocks):
            run = checked_run(Path(folder), "vllm", "container")
            mocks["startup-log"].side_effect = ValueError("docker logs exited 1: no container")
            with (
                contextlib.redirect_stderr(io.StringIO()) as error,
                self.assertRaises(SystemExit) as exited,
            ):
                cli.main(["capture", "--run", str(run)])
            self.assertEqual(exited.exception.code, 1)
            self.assertEqual(
                error.getvalue(),
                "Capture startup-log (startup.log) failed: docker logs exited 1: no container\n",
            )
            mocks["model-dimensions"].assert_not_called()

    def test_the_command_prints_the_document(self):
        with tempfile.TemporaryDirectory() as folder, self.leaves("sglang"):
            run = checked_run(Path(folder), "sglang", "native")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["capture", "--run", str(run)]), 0)
            self.assertEqual(
                output.getvalue(),
                f"Generated private engine attestation in {run / 'engine-attestation.json'}\n",
            )


class StartupLogTests(unittest.TestCase):
    def test_a_container_log_is_saved_once_with_both_streams(self):
        with tempfile.TemporaryDirectory() as folder:
            run = checked_run(Path(folder), "vllm", "container")
            logs = subprocess.CompletedProcess([], 0, stdout=b"stdout line\nstderr line\n")
            with (
                patch.object(capture_module, "live_identity", return_value=CONTAINER),
                patch.object(capture_module.subprocess, "run", return_value=logs) as docker,
            ):
                self.assertEqual(save_startup_log(run), run / "startup.log")
                with self.assertRaises(FileExistsError):
                    save_startup_log(run)
            self.assertEqual(docker.call_args.args, (["docker", "logs", CONTAINER],))
            self.assertEqual(docker.call_args.kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual((run / "startup.log").read_bytes(), logs.stdout)
            self.assertEqual((run / "startup.log").stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                sorted(path.name for path in run.iterdir() if ".tmp-" in path.name), []
            )

    def test_a_failed_docker_logs_writes_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            run = checked_run(Path(folder), "vllm", "container")
            failed = subprocess.CompletedProcess([], 1, stdout=b"Error: No such container\n")
            with (
                patch.object(capture_module, "live_identity", return_value=CONTAINER),
                patch.object(capture_module.subprocess, "run", return_value=failed),
                self.assertRaisesRegex(
                    ValueError, "^docker logs exited 1: Error: No such container$"
                ),
            ):
                save_startup_log(run)
            self.assertEqual(
                sorted(path.name for path in run.iterdir()), ["checked.json", "launch.json"]
            )

    def test_a_native_engine_attests_a_copy_of_its_growing_log(self):
        with tempfile.TemporaryDirectory() as folder:
            run = checked_run(Path(folder), "vllm", "native")
            (run / "startup.log").write_bytes(STARTUP_LOG.read_bytes())
            with (
                patch.object(capture_module, "live_identity", return_value=PROCESS),
                patch.object(capture_module.subprocess, "run") as docker,
            ):
                snapshot = save_startup_log(run)
            docker.assert_not_called()
            self.assertEqual(snapshot, run / "startup-attestation.log")
            self.assertEqual(snapshot.read_bytes(), STARTUP_LOG.read_bytes())
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)


class TransferModeTests(unittest.TestCase):
    def check_log(self, run: Path, *attempts: tuple[str, list[str]]) -> Path:
        name = "runtime-check.log" if "native" in run.name else "image-check.log"
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
                    run = checked_run(Path(folder), "vllm", backend)
                    log = self.check_log(run, (self.plan_hash(run), [name, name]))
                    original = log.read_bytes()
                    with patch.object(vllm_attestation, "live_identity", return_value=identity):
                        output = vllm_attestation.capture_transfer_mode(run)
                        retained = output.read_bytes()
                        with self.assertRaisesRegex(ValueError, "already exists"):
                            vllm_attestation.capture_transfer_mode(run)
                    record = json.loads(retained)
                    binding = (
                        {"process": PROCESS}
                        if backend == "native"
                        else {"image_id": "sha256:" + "a" * 64}
                    )
                    self.assertEqual(
                        record,
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
                    self.assertEqual(output.read_bytes(), retained)
                    self.assertEqual(log.read_bytes(), original)

    def test_an_unknown_ambiguous_or_missing_class_is_refused(self):
        for classes, reason in (
            (["NixlConnector"], "Inspect the pinned connector implementation"),
            (["NixlPullConnector", "NixlPushConnector"], "holds 2 factory-resolved"),
            ([], "holds 0 factory-resolved"),
        ):
            with self.subTest(classes=classes), tempfile.TemporaryDirectory() as folder:
                run = checked_run(Path(folder), "vllm", "container")
                self.check_log(run, (self.plan_hash(run), classes))
                with (
                    patch.object(vllm_attestation, "live_identity", return_value=CONTAINER),
                    self.assertRaisesRegex(ValueError, reason),
                ):
                    vllm_attestation.capture_transfer_mode(run)
                self.assertFalse((run / "transfer-mode.json").exists())

    def test_only_check_attempts_for_this_plan_count(self):
        with tempfile.TemporaryDirectory() as folder:
            run = checked_run(Path(folder), "vllm", "container")
            self.check_log(
                run, ("0" * 64, ["NixlPushConnector"]), (self.plan_hash(run), ["NixlPullConnector"])
            )
            with patch.object(vllm_attestation, "live_identity", return_value=CONTAINER):
                output = vllm_attestation.capture_transfer_mode(run)
            self.assertEqual(json.loads(output.read_text())["transfer_mode"], "pull")
        with tempfile.TemporaryDirectory() as folder:
            run = checked_run(Path(folder), "vllm", "container")
            self.check_log(run, ("0" * 64, ["NixlPullConnector"]))
            with (
                patch.object(vllm_attestation, "live_identity", return_value=CONTAINER),
                self.assertRaisesRegex(ValueError, "rerun the runtime check"),
            ):
                vllm_attestation.capture_transfer_mode(run)


class StartupConnectorTests(unittest.TestCase):
    def run_with(self, root: Path, resolved: str, log: str) -> tuple[Path, Path]:
        (root / "transfer-mode.json").write_text(
            json.dumps({"resolved_connector": CONNECTOR + resolved})
        )
        startup = root / "startup.log"
        startup.write_text(log)
        return root, startup

    def test_a_live_vllm_log_confirms_the_resolved_class(self):
        with tempfile.TemporaryDirectory() as folder:
            run, startup = self.run_with(Path(folder), "NixlPullConnector", STARTUP_LOG.read_text())
            vllm_attestation.check_startup_connector(run, startup)

    def test_another_class_or_the_configured_alias_is_refused(self):
        # The fixture names NixlConnector in its layout and deprecation lines; only the
        # factory line names the class the engine created.
        for resolved in ("NixlPushConnector", "NixlConnector"):
            with self.subTest(resolved=resolved), tempfile.TemporaryDirectory() as folder:
                run, startup = self.run_with(Path(folder), resolved, STARTUP_LOG.read_text())
                with self.assertRaisesRegex(
                    ValueError,
                    f"^startup.log records the engine creating NixlPullConnector, "
                    f"not the resolved {resolved}$",
                ):
                    vllm_attestation.check_startup_connector(run, startup)

    def test_a_log_without_a_created_connector_is_refused(self):
        lines = [
            line
            for line in STARTUP_LOG.read_text().splitlines(keepends=True)
            if "Creating v1 connector" not in line
        ]
        self.assertIn("NixlConnector setting KV cache layout", "".join(lines))
        with tempfile.TemporaryDirectory() as folder:
            run, startup = self.run_with(Path(folder), "NixlPullConnector", "".join(lines))
            with self.assertRaisesRegex(ValueError, "records no connector the engine created"):
                vllm_attestation.check_startup_connector(run, startup)

    def test_a_mixed_log_is_refused(self):
        push = STARTUP_LOG.read_text().replace(
            "name: NixlPullConnector and", "name: NixlPushConnector and", 1
        )
        with tempfile.TemporaryDirectory() as folder:
            run, startup = self.run_with(Path(folder), "NixlPullConnector", push)
            with self.assertRaisesRegex(
                ValueError, "creating NixlPullConnector, NixlPushConnector, not the resolved"
            ):
                vllm_attestation.check_startup_connector(run, startup)


class RepresentativeLayoutTests(unittest.TestCase):
    def test_the_registered_layout_must_match_the_representative(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            (run / "cache-registration.json").write_text(json.dumps({"kv_cache_layout": "LBNHC"}))
            match_representative(VllmLauncher(), run, "LBNHC")
            with self.assertRaisesRegex(
                ValueError, "^Resolved layout LBNHC differs from the representative's BLHNC$"
            ):
                match_representative(VllmLauncher(), run, "BLHNC")


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
        shutil.copy(self.DOCUMENT, run / "engine-attestation.json")
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


if __name__ == "__main__":
    unittest.main()
