"""Reject stale engine evidence and derive the router contract from live sidecars."""

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
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.config import EngineContract
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from tools.deployment.attestation_contract import (
    MODEL_DIMENSIONS_CAPTURE,
    MODEL_DIMENSIONS_CAPTURE_TAG,
    attention_backends,
    capture_model_dimensions,
    capture_nixl,
    engine_document,
    finalize_fleet,
    generate,
    read_json,
    serve,
)

ROOT = Path(__file__).resolve().parents[2]


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value) + "\n")


class AttestationContractTests(unittest.TestCase):
    def engine_evidence(self, root: Path) -> tuple[Path, Path]:
        run = root / "run"
        run.mkdir()
        model = root / "model"
        model.mkdir()
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
            ],
            "env_sha256": "e" * 64,
            "launcher_sha256": "f" * 64,
            "connector": {"kv_connector": "NixlConnector", "kv_role": "kv_both"},
            "expected_packages": {"vllm": "0.29.0+test", "nixl-rocm": "1.0.0"},
        }
        save(run / "launch.json", plan)
        plan_hash = hashlib.sha256((run / "launch.json").read_bytes()).hexdigest()
        save(
            run / "checked.json",
            {"plan_sha256": plan_hash, "image_id": image, "vllm_api_version": "0.29.0"},
        )
        (run / "container.id").write_text(cid + "\n")
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
            run / "cache-registration.json",
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
            run / "nixl-connector-version.json",
            {
                "plan_sha256": plan_hash,
                "image_id": image,
                "container_id": cid,
                "nixl_connector_version": 9,
            },
        )
        save(
            run / "transfer-mode.json",
            {
                "plan_sha256": plan_hash,
                "image_id": image,
                "transfer_mode": "pull",
            },
        )
        save(
            run / "handshake-policy.json",
            {
                "plan_sha256": plan_hash,
                "image": image,
                "connector_config": plan["connector"],
                "enforce_handshake_compat": True,
            },
        )
        save(run / "version.json", {"version": "0.29.0"})
        (run / "metrics.txt").write_text("process_start_time_seconds 100\n")
        log = run / "startup.log"
        log.write_text("Using ROCM_AITER_MLA backend.\n")
        return run, log

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
        from tools.deployment.attestation_contract import NIXL_CAPTURE_TAG

        with tempfile.TemporaryDirectory() as folder:
            run, _ = self.engine_evidence(Path(folder))
            (run / "nixl-connector-version.json").unlink()
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
            )
            with (
                patch(
                    "tools.deployment.attestation_contract.live_container", return_value="b" * 64
                ),
                patch("tools.deployment.attestation_contract.subprocess.run", return_value=result),
            ):
                output = capture_nixl(run)
                record = json.loads(output.read_text())
                self.assertEqual(record["nixl_connector_version"], 9)
                self.assertEqual(record["module_sha256"], "f" * 64)
                self.assertEqual(record["container_id"], "b" * 64)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                output.unlink()
                for stdout in (
                    "INFO: vLLM startup\n",
                    f"{tagged}\n{tagged}\n",
                    NIXL_CAPTURE_TAG + "{bad}",
                ):
                    result.stdout = stdout
                    with self.assertRaisesRegex(ValueError, "NIXL capture"):
                        capture_nixl(run)
                    self.assertFalse(output.exists())

    def test_live_dimensions_complete_an_old_plan_without_restarting_its_container(self):
        with tempfile.TemporaryDirectory() as folder:
            run, log = self.engine_evidence(Path(folder))
            previous = read_json(run / "model-dimensions.json")
            previous.pop("model_architecture")
            save(run / "model-dimensions.json", previous)
            plan = read_json(run / "launch.json")
            plan_hash = hashlib.sha256((run / "launch.json").read_bytes()).hexdigest()
            cid = "b" * 64
            with patch("tools.deployment.attestation_contract.live_container", return_value=cid):
                with self.assertRaisesRegex(ValueError, "Capture live model dimensions"):
                    engine_document(run, log)
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
                    "tools.deployment.attestation_contract.subprocess.run", return_value=result
                ) as run_docker:
                    capture = capture_model_dimensions(run)
                command = run_docker.call_args.args[0]
                self.assertEqual(
                    command[:4], ["docker", "exec", "--env", "NARWHAL_CAPTURE_CACHE=0"]
                )
                self.assertEqual(command[4], cid)
                self.assertEqual(command[command.index("-c") + 2], plan_hash)
                live = read_json(capture)
                self.assertEqual(live["model_architecture"], "TestForCausalLM")
                self.assertEqual(live["container_id"], cid)
                self.assertEqual(capture.stat().st_mode & 0o777, 0o600)
                logs = list(run.glob("model-dimensions.live-*.log"))
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    live["capture_log_sha256"], hashlib.sha256(logs[0].read_bytes()).hexdigest()
                )
                self.assertEqual(
                    engine_document(run, log)["contract"]["model_architecture"], "TestForCausalLM"
                )
                self.assertNotIn("model_architecture", read_json(run / "model-dimensions.json"))
                with self.assertRaisesRegex(ValueError, "already exist"):
                    capture_model_dimensions(run)
                live["container_id"] = "c" * 64
                save(capture, live)
                with self.assertRaisesRegex(ValueError, "container_id differs"):
                    engine_document(run, log)

    def test_live_dimensions_reject_mismatch_before_writing_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            run, _ = self.engine_evidence(Path(folder))
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
                patch(
                    "tools.deployment.attestation_contract.live_container", return_value="b" * 64
                ),
                patch("tools.deployment.attestation_contract.subprocess.run", return_value=result),
                self.assertRaisesRegex(ValueError, "differ from the retained plan capture"),
            ):
                capture_model_dimensions(run)
            self.assertFalse((run / "model-dimensions.live.json").exists())
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

    def test_engine_document_uses_checked_captures_and_rejects_stale_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            run, log = self.engine_evidence(Path(folder))
            with patch(
                "tools.deployment.attestation_contract.live_container", return_value="b" * 64
            ):
                document = engine_document(run, log)
                self.assertEqual(document["contract"]["nixl_connector_version"], 9)
                self.assertEqual(document["contract"]["attention_backend"], "ROCM_AITER_MLA")
                self.assertIs(document["contract"]["hybrid_kv_cache_manager"], True)
                self.assertFalse(EngineContract(**document["contract"]).missing())
                (Path(folder) / "runs").mkdir()
                previous = Path.cwd()
                os.chdir(folder)
                try:
                    output = generate(run, log)
                    self.assertEqual(output, run / "engine-attestation.json")
                    self.assertFalse((Path("runs") / "engine-attestation.engine-1.json").exists())
                    self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(
                        AttestationDocument.load(output).contract.fields(), document["contract"]
                    )
                    with self.assertRaises(FileExistsError):
                        generate(run, log)
                finally:
                    os.chdir(previous)
                stale = json.loads((run / "transfer-mode.json").read_text())
                stale["plan_sha256"] = "f" * 64
                save(run / "transfer-mode.json", stale)
                with self.assertRaisesRegex(ValueError, "Transfer mode plan_sha256"):
                    engine_document(run, log)

    def test_sidecar_uses_derived_role_url_and_current_document(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, log = self.engine_evidence(root)
            (root / "runs").mkdir()
            previous = Path.cwd()
            os.chdir(root)
            try:
                with (
                    patch(
                        "tools.deployment.attestation_contract.live_container",
                        return_value="b" * 64,
                    ),
                    patch(
                        "tools.deployment.attestation_contract.attest_main", return_value=0
                    ) as start,
                    patch.dict(
                        os.environ,
                        {"NARWHAL_NODE_1_ATTESTATION_URL": "http://192.0.2.11:8010/v1/attestation"},
                    ),
                ):
                    generate(run, log)
                    self.assertEqual(serve(run), 0)
                    args = start.call_args.args[0]
                    self.assertEqual(args[args.index("--host") + 1], "192.0.2.11")
                    self.assertEqual(args[args.index("--port") + 1], "8010")
                    record = json.loads((run / "engine-attestation.json").read_text())
                    record["contract"]["head_size"] = 32
                    save(run / "engine-attestation.json", record)
                    with self.assertRaisesRegex(
                        ValueError, "differs from current serving evidence"
                    ):
                        serve(run)
            finally:
                os.chdir(previous)

    def test_router_finalization_rejects_mixed_live_contracts(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet.local.json"
            raw = json.loads((ROOT / "tests/data/fleet.json").read_text())
            contract = EngineContract(**raw.pop("engine_contract"))
            save(path, raw)
            identity = EngineIdentity(contract.vllm_version, 100.0)
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
                    "tools.deployment.attestation_contract.fetch_engine_identity",
                    new_callable=AsyncMock,
                    return_value=identity,
                ),
                patch("tools.deployment.attestation_contract.httpx.Client") as client,
            ):
                client.return_value.__enter__.return_value.get.side_effect = responses
                with self.assertRaisesRegex(ValueError, "different contracts"):
                    finalize_fleet(path)
            self.assertNotIn("engine_contract", json.loads(path.read_text()))

    def test_router_finalization_requires_all_live_sidecars_to_agree(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet.local.json"
            raw = json.loads((ROOT / "tests/data/fleet.json").read_text())
            contract = EngineContract(**raw.pop("engine_contract"))
            save(path, raw)
            identity = EngineIdentity(contract.vllm_version, 100.0)
            document = AttestationDocument(
                contract, dict.fromkeys(contract.fields(), "test evidence")
            )
            payload = make_attestation(document, identity)
            with (
                patch(
                    "tools.deployment.attestation_contract.fetch_engine_identity",
                    new_callable=AsyncMock,
                    return_value=identity,
                ),
                patch("tools.deployment.attestation_contract.httpx.Client") as client,
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
