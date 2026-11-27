import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from narwhal.backends.vllm import attestation as attestation_capture
from narwhal.backends.vllm.attestation import (
    MODEL_DIMENSIONS_CAPTURE,
    MODEL_DIMENSIONS_CAPTURE_TAG,
    attention_backends,
    capture_model_dimensions,
    capture_nixl,
)
from narwhal.backends.vllm.launch import VllmLauncher
from narwhal.config import EngineContract
from narwhal.deployment.attestation_contract import evidence as attestation_evidence
from narwhal.deployment.attestation_contract import sidecar as attestation_sidecar
from narwhal.deployment.attestation_contract.capture import publish
from narwhal.deployment.attestation_contract.document import engine_document, generate, launch_args
from narwhal.deployment.attestation_contract.evidence import live_native, read_json
from narwhal.deployment.attestation_contract.fleet import finalize_fleet
from narwhal.deployment.attestation_contract.sidecar import residency_arguments, serve
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from tests.backends.contract import ATTEMPT
from tests.deployment.fixtures import model_file_fields

ROOT = Path(__file__).resolve().parents[2]
# The container start that engine_evidence records; its metrics report the same start.
STARTED_AT = "1970-01-01T00:01:40Z"
# Files engine_evidence writes into the launch directory rather than the attempt.
INPUTS = ("model-dimensions.json", "cache-layout.json")


# Attestation modules that look up `live_container`.
LIVE_CONTAINER_CALLERS = (attestation_evidence, attestation_sidecar)


@contextlib.contextmanager
def patched_live_container(**kwargs):
    mock = MagicMock(**kwargs)
    with contextlib.ExitStack() as stack:
        for module in LIVE_CONTAINER_CALLERS:
            stack.enter_context(patch.object(module, "live_container", mock))
        yield mock


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value) + "\n")


class LaunchArgsTests(unittest.TestCase):
    def test_launch_args_drop_only_per_launch_values(self):
        args = [
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--host",
            "0.0.0.0",
            "--port",
            "8201",
            "--served-model-name",
            "model",
            "--kv-events-config",
            '{"endpoint": "ipc:///run/one"}',
            "--max-num-seqs",
            "64",
        ]
        self.assertEqual(
            launch_args(args, VllmLauncher.launch_varying_flags),
            [
                "-m",
                "vllm.entrypoints.openai.api_server",
                "--host",
                "--port",
                "--served-model-name",
                "--kv-events-config",
                "--max-num-seqs",
                "64",
            ],
        )


class AttestationContractTests(unittest.TestCase):
    def native_evidence(
        self, root: Path, plan_fields: dict | None = None
    ) -> tuple[Path, Path, dict]:
        run, attempt = self.engine_evidence(
            root, plan_fields={"backend": "native", **(plan_fields or {})}
        )
        plan = read_json(run / "launch.json")
        plan.update(
            backend="native",
            image="",
            python_executable=sys.executable,
            model_revision="a" * 40,
            model_config_path=str(root / "model/config.json"),
        )
        plan.update(model_file_fields(plan))
        save(run / "launch.json", plan)
        plan_hash = hashlib.sha256((run / "launch.json").read_bytes()).hexdigest()
        checked = {
            "backend": "native",
            "plan_sha256": plan_hash,
            "python_executable": sys.executable,
            "expected_packages": plan["expected_packages"],
            "engine_version": "0.29.0",
        }
        save(run / "checked.json", checked)
        process = {"pid": 1234, "boot_id": "boot", "start_ticks": 5678}
        save(run / "native-process.json", process)
        save(
            run / "shared-start.json",
            {
                "status": "running",
                "plan_sha256": plan_hash,
                "process": process,
                "engine_version": "0.29.0",
                "process_start_time_seconds": 100.0,
            },
        )
        (run / "engine.env").write_text("VLLM_API_KEY=test\n")
        for name in (
            "model-dimensions.json",
            "cache-layout.json",
            "model-dimensions.live.json",
            "cache-registration.json",
            "nixl-connector-version.json",
            "transfer-mode.json",
            "handshake-policy.json",
        ):
            path = (run if name in INPUTS else attempt) / name
            record = read_json(path)
            record["plan_sha256"] = plan_hash
            if "image" in record:
                record["image"] = ""
            if name in {
                "model-dimensions.live.json",
                "nixl-connector-version.json",
                "transfer-mode.json",
            }:
                record.pop("image_id", None)
                record.pop("container_id", None)
                record["process"] = process
            if name == "model-dimensions.live.json":
                record["model_revision"] = plan["model_revision"]
            save(path, record)
        (run / "container-start.json").unlink()
        return run, attempt, process

    def test_native_attestation_uses_existing_contract_and_rejects_restarted_process(self):
        with tempfile.TemporaryDirectory() as folder:
            run, attempt, process = self.native_evidence(Path(folder))
            identity = EngineIdentity("0.29.0", 100.0)
            with (
                patch(
                    "narwhal.deployment.attestation_contract.evidence.process_identity",
                    return_value=process,
                ) as observed,
                patch(
                    "narwhal.deployment.native_engine.fetch_engine_identity",
                    new_callable=AsyncMock,
                    return_value=identity,
                ) as fetch,
            ):
                document = engine_document(run, attempt)
                self.assertEqual(fetch.await_args.args, ("http://192.0.2.11:8000",))
                self.assertEqual(
                    fetch.await_args.kwargs["headers"], {"Authorization": "Bearer test"}
                )
                self.assertEqual(document["contract"]["image_digest"], "")
                self.assertFalse(EngineContract(**document["contract"]).missing())
                self.assertNotIn("image_digest", document["sources"])
                self.assertEqual(observed.call_args.args, (1234,))
                changed = {**process, "start_ticks": 5679}
                observed.return_value = changed
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    live_native(
                        run, read_json(run / "launch.json"), read_json(run / "checked.json")
                    )

    def test_native_sidecar_uses_frozen_startup_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            run, attempt, process = self.native_evidence(Path(folder))
            # The engine keeps writing its own log; the attempt holds the copy it attests.
            log = run / "startup.log"
            log.write_text((attempt / "startup.log").read_text())
            with (
                patch(
                    "narwhal.deployment.attestation_contract.evidence.process_identity",
                    return_value=process,
                ),
                patch(
                    "narwhal.deployment.native_engine.fetch_engine_identity",
                    new_callable=AsyncMock,
                    return_value=EngineIdentity("0.29.0", 100.0),
                ),
                patch(
                    "narwhal.deployment.attestation_contract.sidecar.fetch_engine_identity",
                    new_callable=AsyncMock,
                    return_value=EngineIdentity("0.29.0", 100.0),
                ),
                patch(
                    "narwhal.deployment.attestation_contract.sidecar.attest_main", return_value=0
                ) as sidecar,
                patch.dict(
                    os.environ,
                    {"NARWHAL_NODE_1_ATTESTATION_URL": "http://192.0.2.11:8010/v1/attestation"},
                ),
            ):
                generate(run, attempt)
                publish(run, attempt)
                with log.open("a") as output:
                    output.write("GET /metrics HTTP/1.1 200 OK\n")
                self.assertEqual(serve(run), 0)
                sidecar.assert_called_once()

    def test_sidecar_serves_residency_only_for_checked_event_publication(self):
        plan = {
            "args": ["-m", "vllm", "--served-model-name", "served"],
            "kv_events": {"socket_dir": "/tmp/narwhal-0/plan"},
        }
        events = {"endpoint": "ipc:///narwhal-kv-events/events.sock"}
        self.assertEqual(
            residency_arguments(plan, {"prefix_caching": True, "kv_events": events}),
            ["--kv-events", "/tmp/narwhal-0/plan", "--model", "served"],
        )
        for checked in (
            {"prefix_caching": False, "kv_events": events},
            {"prefix_caching": True, "kv_events": None},
            {},
        ):
            with self.subTest(checked=checked):
                self.assertEqual(residency_arguments(plan, checked), [])
        self.assertEqual(
            residency_arguments(
                {**plan, "kv_events": None}, {"prefix_caching": True, "kv_events": events}
            ),
            [],
        )

    def engine_evidence(
        self, root: Path, extra_args: tuple[str, ...] = (), plan_fields: dict | None = None
    ) -> tuple[Path, Path]:
        """A started container's launch directory and a capture attempt holding its captures."""
        run = root / "run"
        run.mkdir()
        model = root / "model"
        model.mkdir(exist_ok=True)
        config = model / "config.json"
        save(config, {"architectures": ["TestForCausalLM"]})
        image = "sha256:" + "a" * 64
        cid = "b" * 64
        plan = {
            "role": "engine-1",
            "endpoint": "http://192.0.2.11:8000",
            "image": image,
            "model_dir": str(model),
            "model_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "launch_sha256": "c" * 64,
            "revision": "d" * 40,
            "args": [
                "--tensor-parallel-size",
                "2",
                "--dtype",
                "bfloat16",
                "--kv-cache-dtype",
                "auto",
                *extra_args,
            ],
            "env_sha256": "e" * 64,
            "launcher_sha256": "f" * 64,
            "launcher_code_sha256": "f" * 64,
            "cache_capture_code_sha256": "f" * 64,
            "connector": {"kv_connector": "NixlConnector", "kv_role": "kv_both"},
            "expected_packages": {"vllm": "0.29.0+test", "nixl-rocm": "1.0.0"},
            **(plan_fields or {}),
        }
        plan.update(model_file_fields(plan))
        save(run / "launch.json", plan)
        plan_hash = hashlib.sha256((run / "launch.json").read_bytes()).hexdigest()
        save(
            run / "checked.json",
            {
                "plan_sha256": plan_hash,
                "image_id": image,
                "engine_version": "0.29.0",
                "ucx_version": "1.22.0",
                "peer_release": True,
            },
        )
        (run / "container.id").write_text(cid + "\n")
        (run / "container.env").write_text("VLLM_API_KEY=test\n")
        # The process start the fixture's metrics report: 100 s after the epoch.
        save(run / "container-start.json", {"container_id": cid, "started_at": STARTED_AT})
        attempt = run / "attestation" / ATTEMPT
        attempt.mkdir(parents=True)
        save(
            run / "model-dimensions.json",
            {
                "plan_sha256": plan_hash,
                "image": image,
                "model_config_sha256": plan["model_config_sha256"],
                "revision": plan["revision"],
                "contract": {"kv_heads": 8, "head_size": 64, "hidden_layers": 4},
                "model_architecture": "TestForCausalLM",
            },
        )
        save(
            attempt / "cache-registration.json",
            {
                "plan_sha256": plan_hash,
                "image": image,
                "kv_cache_layout": "LBNHC",
                "cross_layers_blocks": False,
            },
        )
        save(
            run / "cache-layout.json",
            {
                "plan_sha256": plan_hash,
                "image": image,
                "model_config_sha256": plan["model_config_sha256"],
                "launch_config_sha256": plan["launch_sha256"],
                "ranks": [
                    {
                        "rank": rank,
                        "kv_cache_layout": "LBNHC",
                        "layers": [
                            {"kind": "MLAAttentionSpec"},
                            {"kind": "MambaSpec"},
                        ],
                    }
                    for rank in range(2)
                ],
            },
        )
        save(
            attempt / "nixl-connector-version.json",
            {
                "plan_sha256": plan_hash,
                "image_id": image,
                "container_id": cid,
                "nixl_connector_version": 9,
            },
        )
        save(
            attempt / "transfer-mode.json",
            {
                "plan_sha256": plan_hash,
                "image_id": image,
                "transfer_mode": "pull",
            },
        )
        save(
            attempt / "handshake-policy.json",
            {
                "plan_sha256": plan_hash,
                "image": image,
                "connector_config": plan["connector"],
                "enforce_handshake_compat": True,
            },
        )
        save(
            attempt / "model-dimensions.live.json",
            {
                **read_json(run / "model-dimensions.json"),
                "launcher_sha256": plan["launcher_sha256"],
                "image_id": image,
                "container_id": cid,
            },
        )
        save(attempt / "version.json", {"version": "0.29.0"})
        (attempt / "metrics.txt").write_text("process_start_time_seconds 100\n")
        (attempt / "startup.log").write_text("Using ROCM_AITER_MLA backend.\n")
        return run, attempt

    def test_backend_capture_accepts_pinned_rocm_and_cuda_log_formats(self):
        self.assertEqual(
            attention_backends("Using ROCM_AITER_MLA backend out of potential backends: []"),
            "ROCM_AITER_MLA",
        )
        self.assertEqual(
            attention_backends("Using FLASH_ATTN attention backend out of potential backends: []"),
            "FLASH_ATTN",
        )
        self.assertEqual(
            attention_backends("Overriding with TRITON_MLA out of potential backends: []"),
            "TRITON_MLA",
        )

    def test_nixl_capture_ignores_vllm_stdout_logs_and_rejects_ambiguous_output(self):
        from narwhal.backends.vllm.attestation import NIXL_CAPTURE_TAG

        with tempfile.TemporaryDirectory() as folder:
            run, attempt = self.engine_evidence(Path(folder))
            (attempt / "nixl-connector-version.json").unlink()
            capture = {
                "nixl_connector_version": 9,
                "module": "vllm.distributed.kv_transfer.kv_connector.v1.nixl.metadata",
                "constant": "NIXL_CONNECTOR_VERSION",
                "module_file": "/vllm/nixl/metadata.py",
                "module_sha256": "f" * 64,
            }
            tagged = NIXL_CAPTURE_TAG + json.dumps(capture)
            result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"INFO: vLLM startup\n{tagged}\nINFO: native shutdown\n",
                stderr="",
            )
            with (
                patched_live_container(return_value="b" * 64),
                patch(
                    "narwhal.backends.vllm.attestation.subprocess.run",
                    return_value=result,
                ),
            ):
                output = capture_nixl(run, attempt)
                record = json.loads(output.read_text())
                self.assertEqual(record["nixl_connector_version"], 9)
                self.assertEqual(record["module_sha256"], "f" * 64)
                self.assertEqual(record["container_id"], "b" * 64)
                self.assertEqual(list(record)[-3:], ["plan_sha256", "image_id", "container_id"])
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                self.assertIn(tagged, (attempt / "nixl-connector-version.log").read_text())
                for index, stdout in enumerate(
                    ("INFO: vLLM startup\n", f"{tagged}\n{tagged}\n", NIXL_CAPTURE_TAG + "{bad}")
                ):
                    retry = attempt / f"retry-{index}"
                    retry.mkdir()
                    result.stdout = stdout
                    with self.assertRaisesRegex(ValueError, "NIXL capture"):
                        capture_nixl(run, retry)
                    self.assertFalse((retry / "nixl-connector-version.json").exists())
                result.returncode, result.stderr = 1, "ImportError: no module\n"
                failed = attempt / "failed"
                failed.mkdir()
                with self.assertRaisesRegex(
                    ValueError, r"^NIXL connector inspection exited 1; inspect .*\.log$"
                ):
                    capture_nixl(run, failed)

    def test_native_nixl_capture_uses_recorded_environment_for_connector_import(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, attempt, process = self.native_evidence(root)
            (attempt / "nixl-connector-version.json").unlink()
            paths = {}
            for name, version in (("parent", 11), ("engine", 22)):
                base = root / name
                package = base
                for component in [
                    "vllm",
                    "distributed",
                    "kv_transfer",
                    "kv_connector",
                    "v1",
                    "nixl",
                ]:
                    package /= component
                    package.mkdir(parents=True)
                    (package / "__init__.py").write_text("")
                (package / "metadata.py").write_text(
                    f"NIXL_CONNECTOR_VERSION = {version}\n"
                    "import os\n"
                    "assert os.environ['NARWHAL_CAPTURE_CACHE'] == '0'\n"
                )
                paths[name] = base
            (run / "engine.env").write_text(f"PYTHONPATH={paths['engine']}\n")
            with (
                patch(
                    "narwhal.deployment.attestation_contract.evidence.live_native",
                    return_value=process,
                ),
                patch.dict(
                    os.environ,
                    {"PYTHONPATH": str(paths["parent"]), "NARWHAL_CAPTURE_CACHE": "1"},
                ),
            ):
                record = read_json(capture_nixl(run, attempt))
            self.assertEqual(record["nixl_connector_version"], 22)
            self.assertTrue(Path(record["module_file"]).is_relative_to(paths["engine"]))
            self.assertEqual(record["process"], process)
            self.assertEqual(list(record)[-2:], ["plan_sha256", "process"])

    def test_native_live_dimensions_bind_the_process_and_model_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            run, attempt, process = self.native_evidence(Path(folder))
            (attempt / "model-dimensions.live.json").unlink()
            plan = read_json(run / "launch.json")
            record = {
                **read_json(run / "model-dimensions.json"),
                "launcher_sha256": plan["launcher_sha256"],
            }
            result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=MODEL_DIMENSIONS_CAPTURE_TAG + json.dumps(record) + "\n",
                stderr="",
            )
            with (
                patch(
                    "narwhal.deployment.attestation_contract.evidence.live_native",
                    return_value=process,
                ),
                patch(
                    "narwhal.backends.vllm.attestation.subprocess.run", return_value=result
                ) as probe,
            ):
                live = read_json(capture_model_dimensions(run, attempt))
            self.assertEqual(probe.call_args.args[0][0], plan["python_executable"])
            self.assertEqual(probe.call_args.kwargs["env"]["NARWHAL_CAPTURE_CACHE"], "0")
            self.assertEqual(live["process"], process)
            self.assertEqual(live["model_revision"], plan["model_revision"])
            self.assertEqual(list(live)[-3:], ["process", "model_revision", "capture_log_sha256"])

    def http_identity(self, run: Path, attempt: Path, start: str, live: dict):
        """Capture /version and /metrics, with /metrics reporting the process start `start`."""
        requests: list[httpx.Request] = []
        client = httpx.Client

        def answer(request):
            requests.append(request)
            if request.url.path == "/version":
                return httpx.Response(200, json={"version": "0.29.0"})
            return httpx.Response(200, text=f"process_start_time_seconds {start}\n")

        def connect(**kwargs):
            return client(transport=httpx.MockTransport(answer), **kwargs)

        with (
            patch.object(attestation_capture, "live_identity", **live) as identity,
            patch("narwhal.backends.vllm.attestation.httpx.Client", connect),
        ):
            attestation_capture.capture_http_identity(run, attempt)
        identity.assert_called_once()
        return requests

    def test_http_identity_sends_the_recorded_engine_key(self):
        for env, authorization in (("VLLM_API_KEY=test\n", "Bearer test"), ("", None)):
            with self.subTest(authorization=authorization), tempfile.TemporaryDirectory() as folder:
                run, attempt, process = self.native_evidence(Path(folder))
                (run / "engine.env").write_text(env)
                (attempt / "version.json").unlink()
                (attempt / "metrics.txt").unlink()
                requests = self.http_identity(run, attempt, "100", {"return_value": process})
                self.assertEqual(
                    [str(request.url) for request in requests],
                    ["http://192.0.2.11:8000/version", "http://192.0.2.11:8000/metrics"],
                )
                for request in requests:
                    self.assertEqual(request.headers.get("Authorization"), authorization)
                self.assertEqual(read_json(attempt / "version.json"), {"version": "0.29.0"})
                self.assertEqual(
                    (attempt / "metrics.txt").read_text(), "process_start_time_seconds 100\n"
                )

    def test_http_identity_requires_the_recorded_process_start(self):
        # The container started 100 s after the epoch; /metrics names its first process.
        for backend, start, passes in (
            ("container", "100.6", True),
            ("container", "98.5", True),
            ("container", "40", False),
            ("native", "100", True),
            ("native", "100.5", False),
        ):
            with (
                self.subTest(backend=backend, start=start),
                tempfile.TemporaryDirectory() as folder,
            ):
                if backend == "native":
                    run, attempt, process = self.native_evidence(Path(folder))
                    identity: dict | str = process
                else:
                    run, attempt = self.engine_evidence(Path(folder))
                    identity = "b" * 64
                (attempt / "version.json").unlink()
                (attempt / "metrics.txt").unlink()
                if passes:
                    self.http_identity(run, attempt, start, {"return_value": identity})
                    self.assertTrue((attempt / "metrics.txt").is_file())
                    continue
                with self.assertRaisesRegex(ValueError, "process start"):
                    self.http_identity(run, attempt, start, {"return_value": identity})
                self.assertFalse((attempt / "metrics.txt").exists())

    def test_live_dimensions_complete_an_old_plan_without_restarting_its_container(self):
        with tempfile.TemporaryDirectory() as folder:
            run, attempt = self.engine_evidence(Path(folder))
            previous = read_json(run / "model-dimensions.json")
            previous.pop("model_architecture")
            save(run / "model-dimensions.json", previous)
            plan = read_json(run / "launch.json")
            plan_hash = hashlib.sha256((run / "launch.json").read_bytes()).hexdigest()
            cid = "b" * 64
            (attempt / "model-dimensions.live.json").unlink()
            with patched_live_container(return_value=cid):
                # A backend that reads live dimensions attests no configuration dimensions.
                with self.assertRaisesRegex(FileNotFoundError, "model-dimensions.live.json"):
                    engine_document(run, attempt)
                record = {
                    **previous,
                    "model_architecture": "TestForCausalLM",
                    "launcher_sha256": plan["launcher_sha256"],
                }
                output = (
                    "INFO: vLLM initialization\n"
                    + MODEL_DIMENSIONS_CAPTURE_TAG
                    + json.dumps(record)
                    + "\n"
                )
                result = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=output, stderr=""
                )
                with patch(
                    "narwhal.backends.vllm.attestation.subprocess.run",
                    return_value=result,
                ) as run_docker:
                    capture = capture_model_dimensions(run, attempt)
                command = run_docker.call_args.args[0]
                self.assertEqual(
                    command[:4], ["docker", "exec", "--env", "NARWHAL_CAPTURE_CACHE=0"]
                )
                self.assertEqual(command[4], cid)
                self.assertEqual(command[command.index("-c") + 2], plan_hash)
                live = read_json(capture)
                self.assertEqual(live["model_architecture"], "TestForCausalLM")
                self.assertEqual(live["container_id"], cid)
                self.assertEqual(
                    list(live)[-3:], ["image_id", "container_id", "capture_log_sha256"]
                )
                self.assertEqual(capture.stat().st_mode & 0o777, 0o600)
                log = attempt / "model-dimensions.live.log"
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    live["capture_log_sha256"], hashlib.sha256(log.read_bytes()).hexdigest()
                )
                self.assertEqual(
                    engine_document(run, attempt)["contract"]["model_architecture"],
                    "TestForCausalLM",
                )
                self.assertNotIn("model_architecture", read_json(run / "model-dimensions.json"))
                with self.assertRaisesRegex(ValueError, "already exist"):
                    capture_model_dimensions(run, attempt)
                live["container_id"] = "c" * 64
                save(capture, live)
                with self.assertRaisesRegex(ValueError, "container_id differs"):
                    engine_document(run, attempt)

    def test_live_dimensions_reject_mismatch_before_writing_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            run, attempt = self.engine_evidence(Path(folder))
            (attempt / "model-dimensions.live.json").unlink()
            previous = read_json(run / "model-dimensions.json")
            previous.pop("model_architecture")
            save(run / "model-dimensions.json", previous)
            plan = read_json(run / "launch.json")
            plan_hash = hashlib.sha256((run / "launch.json").read_bytes()).hexdigest()
            record = {
                **previous,
                "model_architecture": "TestForCausalLM",
                "launcher_sha256": plan["launcher_sha256"],
                "contract": {**previous["contract"], "head_size": 576},
            }
            result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=MODEL_DIMENSIONS_CAPTURE_TAG + json.dumps(record) + "\n",
                stderr="",
            )
            with (
                patched_live_container(return_value="b" * 64),
                patch(
                    "narwhal.backends.vllm.attestation.subprocess.run",
                    return_value=result,
                ),
                self.assertRaisesRegex(ValueError, "differ from the retained plan capture"),
            ):
                capture_model_dimensions(run, attempt)
            self.assertFalse((attempt / "model-dimensions.live.json").exists())
            self.assertEqual(plan_hash, record["plan_sha256"])

    def test_live_dimension_probe_uses_the_plan_mounted_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / "launch_engine.py"
            launcher.write_text("# pinned launcher\n")
            config = root / "config.json"
            config.write_text("{}")
            plan = {
                "launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
                "model_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
                "image": "sha256:" + "a" * 64,
                "revision": "d" * 40,
            }
            plan_path = root / "launch.json"
            save(plan_path, plan)
            plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
            model = SimpleNamespace(
                get_head_size=lambda: 576,
                get_total_num_kv_heads=lambda: 96,
                get_total_num_hidden_layers=lambda: 93,
                architecture="TestForCausalLM",
                use_mla=True,
            )
            pinned = ModuleType("launch_engine")
            pinned.__file__ = str(launcher)
            pinned.runtime_config = lambda _: SimpleNamespace(model_config=model)
            with (
                patch.dict(sys.modules, {"launch_engine": pinned}),
                patch.object(sys, "argv", ["-c", plan_hash, str(plan_path), str(config)]),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                exec(compile(MODEL_DIMENSIONS_CAPTURE, "live-model-dimensions", "exec"), {})
            record = json.loads(output.getvalue().split(MODEL_DIMENSIONS_CAPTURE_TAG, 1)[1])
            self.assertEqual(
                record["contract"], {"head_size": 576, "kv_heads": 96, "hidden_layers": 93}
            )
            self.assertEqual(record["launcher_sha256"], plan["launcher_sha256"])
            launcher.write_text("# changed launcher\n")
            with (
                patch.dict(sys.modules, {"launch_engine": pinned}),
                patch.object(sys, "argv", ["-c", plan_hash, str(plan_path), str(config)]),
                self.assertRaisesRegex(ValueError, "mounted launcher differs"),
            ):
                exec(compile(MODEL_DIMENSIONS_CAPTURE, "live-model-dimensions", "exec"), {})

    def test_engine_document_attests_the_model_tree_and_refuses_changed_model_files(self):
        with tempfile.TemporaryDirectory() as folder:
            run, attempt = self.engine_evidence(Path(folder))
            plan = read_json(run / "launch.json")
            with patched_live_container(return_value="b" * 64):
                document = engine_document(run, attempt)
                self.assertEqual(document["launch"]["model_tree_sha256"], plan["model_tree_sha256"])
                (Path(plan["model_dir"]) / "model.safetensors").write_bytes(b"other weights")
                with self.assertRaisesRegex(ValueError, "^model files changed"):
                    engine_document(run, attempt)

    def test_engine_document_uses_checked_captures_and_rejects_stale_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            run, attempt = self.engine_evidence(Path(folder))
            with patched_live_container(return_value="b" * 64):
                document = engine_document(run, attempt)
                # The goldens sort keys, so this pins the written order.
                self.assertEqual(
                    list(document), ["schema", "schema_version", "contract", "sources", "launch"]
                )
                self.assertEqual(document["contract"]["connector_version"], 9)
                self.assertEqual(document["contract"]["attention_backend"], "ROCM_AITER_MLA")
                self.assertIs(document["contract"]["hybrid_kv_cache_manager"], True)
                self.assertFalse(EngineContract(**document["contract"]).missing())
                self.assertEqual(document["launch"]["ucx_version"], "1.22.0")
                self.assertIs(document["launch"]["peer_release"], True)
                (Path(folder) / "runs").mkdir()
                previous = Path.cwd()
                os.chdir(folder)
                try:
                    output = generate(run, attempt)
                    self.assertEqual(output, attempt / "engine-attestation.json")
                    self.assertFalse((Path("runs") / "engine-attestation.engine-1.json").exists())
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(
                        AttestationDocument.load(output).contract.fields(), document["contract"]
                    )
                    with self.assertRaises(FileExistsError):
                        generate(run, attempt)
                finally:
                    os.chdir(previous)
                stale = json.loads((attempt / "transfer-mode.json").read_text())
                stale["plan_sha256"] = "f" * 64
                save(attempt / "transfer-mode.json", stale)
                with self.assertRaisesRegex(ValueError, "Transfer mode plan_sha256"):
                    engine_document(run, attempt)

    def test_gguf_launch_attests_the_model_file_and_rejects_a_changed_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            weights = root / "model/weights.gguf"
            weights.parent.mkdir()
            weights.write_bytes(b"gguf")
            fields = {
                "model_path": str(weights),
                "model_sha256": hashlib.sha256(b"gguf").hexdigest(),
            }
            run, attempt = self.engine_evidence(
                root, ("--model", "/model/weights.gguf"), plan_fields=fields
            )
            with patched_live_container(return_value="b" * 64):
                document = engine_document(run, attempt)
                self.assertEqual(document["launch"]["model_sha256"], fields["model_sha256"])
                # An identical file outside the mounted directory is another file to the engine.
                outside = root / "outside.gguf"
                weights.rename(outside)
                weights.symlink_to(outside)
                with self.assertRaisesRegex(ValueError, "Model file changed"):
                    engine_document(run, attempt)
                weights.unlink()
                weights.write_bytes(b"other")
                with self.assertRaisesRegex(ValueError, "^model files changed after launch"):
                    engine_document(run, attempt)

    def test_native_gguf_launch_attests_the_model_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            weights = root / "weights.gguf"
            weights.write_bytes(b"gguf")
            fields = {
                "model_path": str(weights),
                "model_sha256": hashlib.sha256(b"gguf").hexdigest(),
            }
            run, attempt, process = self.native_evidence(root, fields)
            with (
                patch(
                    "narwhal.deployment.attestation_contract.evidence.process_identity",
                    return_value=process,
                ),
                patch(
                    "narwhal.deployment.native_engine.fetch_engine_identity",
                    new_callable=AsyncMock,
                    return_value=EngineIdentity("0.29.0", 100.0),
                ),
            ):
                document = engine_document(run, attempt)
                self.assertEqual(document["launch"]["model_sha256"], fields["model_sha256"])
                weights.write_bytes(b"other")
                with self.assertRaisesRegex(ValueError, "^model files changed after launch"):
                    engine_document(run, attempt)

    def test_checkpoint_launch_attests_no_model_file(self):
        with tempfile.TemporaryDirectory() as folder:
            run, attempt = self.engine_evidence(Path(folder))
            with patched_live_container(return_value="b" * 64):
                self.assertNotIn("model_sha256", engine_document(run, attempt)["launch"])

    def test_contract_records_the_launched_speculative_decoding(self):
        for extra, expected in (
            ((), "disabled"),
            (("--speculative-config", '{"method": "eagle"}'), '{"method": "eagle"}'),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                run, attempt = self.engine_evidence(Path(folder), extra)
                with patched_live_container(return_value="b" * 64):
                    document = engine_document(run, attempt)
                self.assertEqual(document["contract"]["speculative_config"], expected)

    def test_sidecar_uses_derived_role_url_and_current_document(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, attempt = self.engine_evidence(root)
            (root / "runs").mkdir()
            previous = Path.cwd()
            os.chdir(root)
            try:
                with (
                    patched_live_container(
                        return_value="b" * 64,
                    ),
                    patch(
                        "narwhal.deployment.attestation_contract.sidecar.fetch_engine_identity",
                        new_callable=AsyncMock,
                        return_value=EngineIdentity("0.29.0", 100.0),
                    ) as live,
                    patch(
                        "narwhal.deployment.attestation_contract.sidecar.attest_main",
                        return_value=0,
                    ) as start,
                    patch.dict(
                        os.environ,
                        {"NARWHAL_NODE_1_ATTESTATION_URL": "http://192.0.2.11:8010/v1/attestation"},
                    ),
                ):
                    generate(run, attempt)
                    publish(run, attempt)
                    self.assertEqual(serve(run), 0)
                    self.assertEqual(
                        live.await_args.args, (read_json(run / "launch.json")["endpoint"],)
                    )
                    args = start.call_args.args[0]
                    self.assertEqual(args[args.index("--host") + 1], "192.0.2.11")
                    self.assertEqual(args[args.index("--port") + 1], "8010")
                    document = attempt / "engine-attestation.json"
                    published = document.read_bytes()
                    record = json.loads(published)
                    record["contract"]["head_size"] = 32
                    save(document, record)
                    with self.assertRaisesRegex(ValueError, "differs from the document published"):
                        serve(run)
                    document.write_bytes(published)
                    with (attempt / "startup.log").open("a") as log:
                        log.write("Using TRITON_MLA backend.\n")
                    with self.assertRaisesRegex(
                        ValueError, "differs from current serving evidence"
                    ):
                        serve(run)
                    model = Path(read_json(run / "launch.json")["model_dir"])
                    (model / "model.safetensors").write_bytes(b"other weights")
                    with self.assertRaisesRegex(ValueError, "^model files changed"):
                        serve(run)
                    self.assertEqual(start.call_count, 1)
            finally:
                os.chdir(previous)

    def test_router_finalization_rejects_mixed_live_contracts(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet.local.json"
            raw = json.loads((ROOT / "tests/data/fleet.json").read_text())
            contract = EngineContract(**raw.pop("engine_contract"))
            save(path, raw)
            identity = EngineIdentity(contract.engine_version, 100.0)
            sources = dict.fromkeys(contract.fields(), "test evidence")
            first = make_attestation(AttestationDocument(contract, sources), identity)
            second = make_attestation(
                AttestationDocument(replace(contract, head_size=2), sources), identity
            )
            responses = [
                httpx.Response(
                    200, json=payload, request=httpx.Request("GET", "http://sidecar.invalid")
                )
                for payload in (first, second, second, second, second, second)
            ]
            with (
                patch(
                    "narwhal.deployment.attestation_contract.fleet.fetch_spec_identity",
                    new_callable=AsyncMock,
                    return_value=identity,
                ),
                patch("narwhal.deployment.attestation_contract.fleet.httpx.Client") as client,
            ):
                client.return_value.__enter__.return_value.get.side_effect = responses
                with self.assertRaisesRegex(ValueError, "different contracts"):
                    finalize_fleet(path)
            self.assertNotIn("engine_contract", json.loads(path.read_text()))

    def test_router_finalization_requires_one_attested_model_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet.local.json"
            raw = json.loads((ROOT / "tests/data/fleet.json").read_text())
            contract = EngineContract(**raw.pop("engine_contract"))
            save(path, raw)
            identity = EngineIdentity(contract.engine_version, 100.0)
            sources = dict.fromkeys(contract.fields(), "test evidence")
            gguf, other, checkpoint = (
                make_attestation(AttestationDocument(contract, sources, launch=launch), identity)
                for launch in ({"model_sha256": "a" * 64}, {"model_sha256": "b" * 64}, {})
            )
            rest = len(raw["engines"]) - 1
            for case, payloads in (
                ("two GGUF files", [gguf] + [other] * rest),
                ("GGUF beside a checkpoint", [gguf] + [checkpoint] * rest),
            ):
                with self.subTest(case):
                    self.assert_mixed_model_files(path, identity, payloads)
            self.assertNotIn("engine_contract", json.loads(path.read_text()))

    def assert_mixed_model_files(
        self, path: Path, identity: EngineIdentity, payloads: list[dict]
    ) -> None:
        with (
            patch(
                "narwhal.deployment.attestation_contract.fleet.fetch_spec_identity",
                new_callable=AsyncMock,
                return_value=identity,
            ),
            patch("narwhal.deployment.attestation_contract.fleet.httpx.Client") as client,
        ):
            client.return_value.__enter__.return_value.get.side_effect = [
                httpx.Response(
                    200, json=payload, request=httpx.Request("GET", "http://sidecar.invalid")
                )
                for payload in payloads
            ]
            with self.assertRaisesRegex(ValueError, "different model files"):
                finalize_fleet(path)

    def test_router_finalization_rejects_engines_with_different_model_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet.local.json"
            raw = json.loads((ROOT / "tests/data/fleet.json").read_text())
            contract = EngineContract(**raw.pop("engine_contract"))
            save(path, raw)
            identity = EngineIdentity(contract.engine_version, 100.0)
            sources = dict.fromkeys(contract.fields(), "test evidence")
            payloads = [
                make_attestation(
                    AttestationDocument(contract, sources, launch={"model_tree_sha256": tree}),
                    identity,
                )
                for tree in ("a" * 64, "b" * 64)
            ]
            iids = [engine["iid"] for engine in raw["engines"]]
            responses = [
                httpx.Response(
                    200,
                    json=payloads[index == len(iids) - 1],
                    request=httpx.Request("GET", "http://sidecar.invalid"),
                )
                for index in range(len(iids))
            ]
            with (
                patch(
                    "narwhal.deployment.attestation_contract.fleet.fetch_spec_identity",
                    new_callable=AsyncMock,
                    return_value=identity,
                ),
                patch("narwhal.deployment.attestation_contract.fleet.httpx.Client") as client,
            ):
                client.return_value.__enter__.return_value.get.side_effect = responses
                with self.assertRaisesRegex(
                    ValueError,
                    "^Engine sidecars attest different model files: launch.model_tree_sha256 "
                    f"differs: '{'a' * 64}' from {', '.join(sorted(iids[:-1]))}; "
                    f"'{'b' * 64}' from {iids[-1]}$",
                ):
                    finalize_fleet(path)
            self.assertNotIn("engine_contract", json.loads(path.read_text()))

    def test_router_finalization_requires_all_live_sidecars_to_agree(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet.local.json"
            raw = json.loads((ROOT / "tests/data/fleet.json").read_text())
            contract = EngineContract(**raw.pop("engine_contract"))
            save(path, raw)
            identity = EngineIdentity(contract.engine_version, 100.0)
            document = AttestationDocument(
                contract, dict.fromkeys(contract.fields(), "test evidence")
            )
            payload = make_attestation(document, identity)
            with (
                patch(
                    "narwhal.deployment.attestation_contract.fleet.fetch_spec_identity",
                    new_callable=AsyncMock,
                    return_value=identity,
                ),
                patch("narwhal.deployment.attestation_contract.fleet.httpx.Client") as client,
            ):
                client.return_value.__enter__.return_value.get.return_value = httpx.Response(
                    200, json=payload, request=httpx.Request("GET", "http://sidecar.invalid")
                )
                old = Path.cwd()
                os.chdir(folder)
                try:
                    self.assertEqual(finalize_fleet(path), contract)
                finally:
                    os.chdir(old)
            self.assertEqual(json.loads(path.read_text())["engine_contract"], contract.fields())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
