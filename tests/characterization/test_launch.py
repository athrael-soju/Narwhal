import contextlib
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from narwhal.deployment.attestation_contract import document as attestation_document
from narwhal.deployment.attestation_contract.document import engine_document
from narwhal.deployment.launch_engine import plan as launch_plan
from narwhal.deployment.launch_engine.check import check
from narwhal.deployment.launch_engine.plan import build, load, prepare
from narwhal.deployment.launch_engine.runtime import digest
from narwhal.deployment.launch_engine.start import start
from narwhal.deployment.native_engine import start_shared
from narwhal.dev import template
from tests.deployment.fixtures import (
    cache_settings_line,
    cuda_engine,
    launcher_inputs,
    patched_docker,
)

from .golden import assert_golden

CONTAINER_ID = "c" * 64
# Variables the native start sets beside the prepared engine environment.
LAUNCH_ENV = {"VLLM_API_KEY", "NARWHAL_CAPTURE_CACHE", "NARWHAL_CACHE_PLAN", "NARWHAL_CACHE_OUTPUT"}
SHARED_DEVICE = {
    "group": "example-host:GPU-fixture",
    "gpu_uuid": "GPU-fixture",
    "device_allowance": 0.9,
    "gpu_memory_utilization": 0.2,
}
# File digests change with the source tree, not with launch behaviour.
SOURCE_DIGESTS = {
    "env_sha256",
    "launch_sha256",
    "launcher_sha256",
    "cache_capture_sha256",
    "plan_sha256",
    "model_sha256",
}


def placeholders(root: Path, plan: dict | None = None) -> dict[str, str]:
    values = {
        str(launch_plan.KV_EVENTS_ROOT / f"narwhal-{os.geteuid()}"): "<kv-events-root>",
        str(root.resolve()): "<root>",
        str(root): "<root>",
        sys.executable: "<python>",
    }
    if plan is not None:
        values = {plan["name"]: "<name>", **values}
    return values


def redacted(value):
    if isinstance(value, dict):
        return {
            key: "<sha256>" if key in SOURCE_DIGESTS and value[key] else redacted(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def env_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def runtime_output(plan: dict, ucx: str | None = "1.22.0") -> str:
    return (
        json.dumps(plan["expected_packages"])
        + "\nNARWHAL_TOKENIZER_READY=1\n"
        + cache_settings_line(plan)
        + "\nNARWHAL_IMAGE_RUNTIME="
        + json.dumps({"vllm_api_version": "0.29.0", "ucx_version": ucx})
        + "\n"
    )


def docker_calls(mock: Mock) -> list[dict]:
    calls = []
    for call in mock.call_args_list:
        command = list(call.args[0])
        if "-c" in command:
            index = command.index("-c") + 1
            command[index] = command[index].splitlines()
        calls.append({"command": command, "log": call.args[2]})
    return calls


def prepared(root: Path, *, backend: str = "container", record_changes=None) -> tuple[Path, dict]:
    record, env = launcher_inputs(root)
    if record_changes is not None:
        record_changes(record)
    Path(env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).write_text(json.dumps(record))
    if backend == "native":
        env.pop("NARWHAL_ENGINE_IMAGE")
        env["NARWHAL_MODEL_REVISION"] = "a" * 40
        env["NARWHAL_NODE_1_ATTESTATION_URL"] = "http://192.0.2.11:8010/v1/attestation"
    run = root / "launch"
    with contextlib.redirect_stdout(io.StringIO()):
        prepare(run, env, backend=backend)
    return run, load(run)


def checked_container(root: Path) -> tuple[Path, dict, dict]:
    run, plan = prepared(root)
    image = json.dumps([{"Id": plan["image"]}])
    with (
        patched_docker(side_effect=[image, runtime_output(plan)]) as docker,
        contextlib.redirect_stdout(io.StringIO()) as printed,
    ):
        check(run, plan)
    return run, plan, {"docker": docker_calls(docker), "printed": printed.getvalue()}


def shared_native(record: dict) -> None:
    cuda_engine(record, visible="0")
    record["shared_device"] = dict(SHARED_DEVICE)
    record["runtime"]["extra_args"] += ["--gpu-memory-utilization", "0.2"]


class LaunchPlanTests(unittest.TestCase):
    def test_container_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, plan = prepared(root)
            record = {
                "plan": redacted(plan),
                "container_env": env_lines(run / "container.env"),
                "hook_files": sorted(path.name for path in (run / "hook").iterdir()),
            }
            assert_golden(self, "launch_container_plan", record, placeholders(root, plan))

    def test_native_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, plan = prepared(root, backend="native")
            record = {"plan": redacted(plan), "engine_env": env_lines(run / "engine.env")}
            assert_golden(self, "launch_native_plan", record, placeholders(root, plan))

    def test_cuda_ipc_peer_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def colocated(record):
                cuda_engine(record, visible="0,1")
                record["runtime"]["environment"]["UCX_CUDA_IPC_CACHE"] = "n"

            run, plan = prepared(root, record_changes=colocated)
            record = {
                "common": plan["common"],
                "connector": plan["connector"],
                "cuda_ipc_peers": plan["cuda_ipc_peers"],
                "ucx_tls": plan["ucx_tls"],
                "container_env": env_lines(run / "container.env"),
            }
            assert_golden(self, "launch_cuda_ipc_plan", record, placeholders(root, plan))

    def test_runtime_rules(self):
        cases = {
            "missing_vllm_pin": lambda r, e: r["runtime"]["expected_packages"].pop("vllm"),
            "missing_nixl_pin": lambda r, e: r["runtime"]["expected_packages"].pop("nixl"),
            "nixl_rocm_pin": lambda r, e: r["runtime"]["expected_packages"].update(
                {"nixl-rocm": r["runtime"]["expected_packages"].pop("nixl")}
            ),
            "unpinned_version": lambda r, e: r["runtime"]["expected_packages"].update(
                vllm=">=0.29"
            ),
            "float32_model": lambda r, e: r["runtime"].update(model_dtype="float32"),
            "short_kv_lease": lambda r, e: r["runtime"].update(kv_lease_s=5),
            "custom_kv_lease": lambda r, e: r["runtime"].update(kv_lease_s=12),
            "unknown_option": lambda r, e: r["runtime"]["extra_args"].append("--port"),
            "kv_transfer_override": lambda r, e: r["runtime"]["extra_args"].extend(
                ["--kv-transfer-config", "{}"]
            ),
            "kv_events_endpoint": lambda r, e: r["runtime"]["extra_args"].extend(
                ["--kv-events-config", '{"endpoint": "tcp://*:5557"}']
            ),
            "kv_events_disabled": lambda r, e: r["runtime"]["extra_args"].extend(
                ["--kv-events-config", '{"enable_kv_cache_events": false}']
            ),
            "prefix_caching_off": lambda r, e: r["runtime"]["extra_args"].append(
                "--no-enable-prefix-caching"
            ),
            "managed_env": lambda r, e: r["runtime"]["environment"].update(VLLM_API_KEY="x"),
            "foreign_env": lambda r, e: r["runtime"]["environment"].update(OTHER_FLAG="1"),
            "secret_env": lambda r, e: r["runtime"]["environment"].update(VLLM_SECRET="x"),
            "mutable_image": lambda r, e: e.update(NARWHAL_ENGINE_IMAGE="vllm/vllm:latest"),
            "registry_digest": lambda r, e: e.update(
                NARWHAL_ENGINE_IMAGE="registry.example/engine@sha256:" + "a" * 64
            ),
            "shared_ports": lambda r, e: e.update(NARWHAL_NIXL_SIDE_CHANNEL_PORT="8000"),
        }
        prefixes = {
            f"{prefix}FIXTURE": None for prefix in (*launch_plan.ENV_PREFIXES, "LD_", "OTHER_")
        }
        results = {}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, change in [
                *cases.items(),
                *(
                    (
                        f"env_{variable}",
                        lambda r, e, v=variable: r["runtime"]["environment"].update({v: "1"}),
                    )
                    for variable in prefixes
                ),
            ]:
                (root / name).mkdir()
                record, env = launcher_inputs(root / name)
                change(record, env)
                try:
                    plan, _ = build(record, env, root / name / "launch")
                except ValueError as error:
                    results[name] = {"error": str(error)}
                else:
                    results[name] = {
                        "connector": plan["connector"],
                        "kv_events": plan["kv_events"] is not None,
                        "image": plan["image"],
                    }
            (root / "gguf").mkdir()
            gguf, env = launcher_inputs(root / "gguf")
            model = root / "gguf" / "model" / "weights.gguf"
            model.write_bytes(b"gguf")
            env.update(NARWHAL_MODEL_PATH=str(model), NARWHAL_MODEL_REVISION="a" * 40)
            for pinned in (False, True):
                if pinned:
                    gguf["runtime"]["expected_packages"]["vllm-gguf-plugin"] = "0.0.5"
                try:
                    plan, _ = build(gguf, env, root / "gguf" / "launch", backend="native")
                except ValueError as error:
                    results[f"gguf_plugin_{pinned}"] = {"error": str(error)}
                else:
                    results[f"gguf_plugin_{pinned}"] = {"model": plan["model_path"]}
        constants = {
            "value_options": sorted(launch_plan.VALUE_OPTIONS),
            "flag_options": sorted(launch_plan.FLAG_OPTIONS),
            "managed_env": sorted(launch_plan.MANAGED_ENV),
            "env_prefixes": list(launch_plan.ENV_PREFIXES),
            "engine_ttl_s": launch_plan.ENGINE_TTL_S,
            "kv_lease_s": launch_plan.KV_LEASE_S,
            "min_kv_lease_s": launch_plan.MIN_KV_LEASE_S,
            "kv_events_mount": launch_plan.KV_EVENTS_MOUNT,
            "kv_events_sockets": launch_plan.KV_EVENTS_SOCKETS,
        }
        assert_golden(
            self,
            "launch_runtime_rules",
            {"cases": results, "constants": constants},
            placeholders(root),
        )


class RuntimeCheckAndStartTests(unittest.TestCase):
    def test_container_check_and_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, plan, checked = checked_container(root)
            with (
                patched_docker(side_effect=[CONTAINER_ID, "started"]) as docker,
                contextlib.redirect_stdout(io.StringIO()) as printed,
            ):
                start(run, load(run))
            record = {
                "check": checked,
                "checked": redacted(json.loads((run / "checked.json").read_text())),
                "start": {"docker": docker_calls(docker), "printed": printed.getvalue()},
            }
            assert_golden(self, "launch_container_check_start", record, placeholders(root, plan))

    def test_native_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, plan = prepared(root, backend="native")
            with (
                patch("narwhal.deployment.stages.run") as invoke,
                contextlib.redirect_stdout(io.StringIO()) as printed,
            ):
                invoke.return_value = subprocess.CompletedProcess([], 0, runtime_output(plan), "")
                check(run, plan)
            command = list(invoke.call_args.args[0])
            command[2] = command[2].splitlines()
            engine_env = dict(line.split("=", 1) for line in env_lines(run / "engine.env"))
            environment = invoke.call_args.kwargs["env"]
            record = {
                "command": command,
                "engine_env": {name: environment[name] for name in sorted(engine_env)},
                "stage": invoke.call_args.kwargs["stage"],
                "printed": printed.getvalue(),
                "checked": redacted(json.loads((run / "checked.json").read_text())),
            }
            assert_golden(self, "launch_native_check", record, placeholders(root, plan))

    def test_native_shared_start(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"VLLM_API_KEY": "inherited-key"}),
        ):
            root = Path(folder)
            run, plan = prepared(root, backend="native", record_changes=shared_native)
            with (
                patch("narwhal.deployment.stages.run") as invoke,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                invoke.return_value = subprocess.CompletedProcess([], 0, runtime_output(plan), "")
                check(run, plan)
            readings = iter((1000, 6100))
            live = SimpleNamespace(version="0.29.0", process_start_time_seconds=1234.5)
            prefix = "narwhal.deployment.native_engine."
            with (
                patch(prefix + "validate_shared_runs", return_value=[(run, load(run))]),
                patch(prefix + "_ports_free"),
                patch(prefix + "gpu_memory") as memory,
                patch(prefix + "subprocess.Popen", return_value=Mock(pid=101)) as popen,
                patch(
                    prefix + "process_identity",
                    return_value={"pid": 101, "boot_id": "boot", "start_ticks": 10},
                ),
                patch(prefix + "_wait_ready"),
                patch(
                    prefix + "fetch_engine_identity", new_callable=AsyncMock, return_value=live
                ) as fetch,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                memory.side_effect = lambda _: {"used_mib": next(readings), "total_mib": 30000}
                start_shared([run], ready_seconds=30)
            environment = popen.call_args.kwargs["env"]
            engine_env = dict(line.split("=", 1) for line in env_lines(run / "engine.env"))
            record = {
                "argv": popen.call_args.args[0],
                "env": {
                    name: environment[name]
                    for name in sorted(environment)
                    if name in engine_env or name in LAUNCH_ENV
                },
                "identity_request": {
                    "endpoint": fetch.await_args.args[0],
                    "headers": fetch.await_args.kwargs["headers"],
                },
                "record": redacted(json.loads((run / "shared-start.json").read_text())),
            }
            assert_golden(self, "launch_native_start", record, placeholders(root, plan))


class AttestationContractTests(unittest.TestCase):
    def test_container_engine_document(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run, plan, _ = checked_container(root)
            with (
                patched_docker(side_effect=[CONTAINER_ID, "started"]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                start(run, load(run))
            plan = load(run)
            plan_hash = digest(run / "launch.json")
            image = plan["image"]
            tp = int(plan["args"][plan["args"].index("--tensor-parallel-size") + 1])
            captures = {
                "model-dimensions.json": {
                    "plan_sha256": plan_hash,
                    "image": image,
                    "model_config_sha256": plan["model_config_sha256"],
                    "revision": plan["revision"],
                    "contract": {"kv_heads": 8, "head_size": 64, "hidden_layers": 4},
                    "model_architecture": "FixtureForCausalLM",
                },
                "cache-registration.json": {
                    "plan_sha256": plan_hash,
                    "image": image,
                    "kv_cache_layout": "NHD",
                    "cross_layers_blocks": False,
                },
                "cache-layout.json": {
                    "plan_sha256": plan_hash,
                    "image": image,
                    "model_config_sha256": plan["model_config_sha256"],
                    "launch_config_sha256": plan["launch_sha256"],
                    "ranks": [
                        {
                            "rank": rank,
                            "kv_cache_layout": "NHD",
                            "layers": [{"kind": "FullAttentionSpec"}],
                        }
                        for rank in range(tp)
                    ],
                },
                "nixl-connector-version.json": {
                    "plan_sha256": plan_hash,
                    "image_id": image,
                    "container_id": CONTAINER_ID,
                    "nixl_connector_version": 9,
                },
                "transfer-mode.json": {
                    "plan_sha256": plan_hash,
                    "image_id": image,
                    "transfer_mode": "pull",
                },
                "handshake-policy.json": {
                    "plan_sha256": plan_hash,
                    "image": image,
                    "connector_config": plan["connector"],
                    "enforce_handshake_compat": True,
                },
                "version.json": {"version": "0.29.0"},
            }
            for name, value in captures.items():
                (run / name).write_text(json.dumps(value) + "\n")
            (run / "metrics.txt").write_text("process_start_time_seconds 100\n")
            log = run / "startup.log"
            log.write_text("Using FLASH_ATTN attention backend.\n")
            with patch.object(attestation_document, "live_container", return_value=CONTAINER_ID):
                document = engine_document(run, log)
            document["sources"] = {
                field: re.sub(r"sha256:[0-9a-f]{64}$", "sha256:<digest>", source)
                for field, source in document["sources"].items()
            }
            assert_golden(
                self,
                "launch_engine_document",
                redacted(document),
                placeholders(root, plan),
            )


class DevTemplateTests(unittest.TestCase):
    def materialized(self, spec: dict, root: Path) -> tuple[Path, list]:
        model = root / "model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        weights = model / "fixture.gguf"
        weights.write_bytes(b"fixture")
        spec["model"].update(
            repository="example/fixture-gguf",
            revision="0" * 40,
            filename=weights.name,
            sha256=hashlib.sha256(b"fixture").hexdigest(),
            served_name="fixture-model",
            tokenizer_repository="example/fixture",
            tokenizer_revision="1" * 40,
            tokenizer_sha256={},
        )
        for field in ("gguf_plugin_python_sha256", "gguf_plugin_extension_sha256"):
            spec["runtime"].pop(field, None)
        gpu = spec["gpu"]
        if "product" in gpu:
            gpu["product"] = "fixture-gpu"
        allowance = spec["allocation"]["device_allowance"]
        total = max(
            gpu.get("minimum_total_mib", 0), math.ceil(gpu["reserve_mib"] / (1 - allowance))
        )
        packages = {"narwhal-inference": "0.0.0", **spec["runtime"]["expected_packages"]}
        environment = {k: v for k, v in os.environ.items() if k != "NARWHAL_ENGINE_API_KEY"}
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                template,
                "_gpu_rows",
                return_value=[{"name": "fixture-gpu", "uuid": "GPU-fixture"}],
            ),
            patch.object(
                template, "gpu_memory", return_value={"total_mib": total + 1, "used_mib": 0}
            ),
            patch.object(template.metadata, "version", side_effect=packages.__getitem__),
            patch.object(
                template.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as run,
            patch.object(template, "_address", return_value="127.0.0.1"),
            patch.object(template, "_check_free_ports"),
            patch.object(template.socket, "gethostname", return_value="example-host"),
        ):
            output = template.materialize(
                root / "instance",
                model_dir=model,
                model_path=weights,
                fabric_interface="lo",
                template=spec,
            )
        checks = []
        for call in run.call_args_list:
            command = list(call.args[0])
            command[0] = "<python>" if command[0] == sys.executable else command[0]
            command[-1] = command[-1].splitlines()
            checks.append(command)
        return output, checks

    def test_dev_templates(self):
        for name, spec in (
            ("reference", template.reference()),
            ("default", template.default_template()),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                output, checks = self.materialized(spec, root)
                record = {
                    "engine_launch": json.loads((output / "engine-launch.json").read_text()),
                    "fleet": json.loads((output / "fleet.json").read_text()),
                    "runtime_checks": checks,
                }
                assert_golden(self, f"launch_dev_{name}", record, placeholders(root))


if __name__ == "__main__":
    unittest.main()
