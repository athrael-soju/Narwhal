from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ...deployment.launch_engine.backend import EngineLauncher
from . import attestation, dev
from . import check as runtime_check
from . import plan as policy
from .runtime import API_KEY_ENV, BOOTSTRAP_PORT_ENV, LAUNCHER

BOOTSTRAP_HOST_ENV = "NARWHAL_SGLANG_BOOTSTRAP_HOST"


def _no_step(step: str) -> ValueError:
    return ValueError(f"SGLang engines report their KV cache over HTTP and need no {step} step")


class SglangLauncher(EngineLauncher):
    runtime_script = LAUNCHER
    cache_hook = Path(__file__).with_name("cache_capture_hook.py")
    side_channel = "bootstrap"
    side_channel_port_env = "NARWHAL_SGLANG_BOOTSTRAP_PORT"
    api_key_env = API_KEY_ENV
    args_field = "sglang_args"
    version_field = "sglang_version"
    checked_version_field = "sglang_version"

    def validate_runtime(self, runtime: dict) -> None:
        policy.validate_runtime(runtime)

    def engine_env(self, host: str, side_channel_port: int, api_key: str) -> dict[str, str]:
        return {
            BOOTSTRAP_HOST_ENV: host,
            BOOTSTRAP_PORT_ENV: str(side_channel_port),
            # SGLang advertises this address to decode peers.
            "SGLANG_HOST_IP": host,
            "SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT": str(policy.BOOTSTRAP_TIMEOUT_S),
            **({self.api_key_env: api_key} if api_key else {}),
        }

    def side_channel_address(self, env: Mapping[str, str]) -> tuple[str, int]:
        return env[BOOTSTRAP_HOST_ENV], int(env[BOOTSTRAP_PORT_ENV])

    def check_model(self, runtime: dict, model: str) -> None:
        if model.endswith(".gguf"):
            raise ValueError("SGLang launches take a model directory, not a GGUF file")

    def publishes_kv_events(self, extra_args: list[str]) -> bool:
        return policy.publishes_kv_events(extra_args)

    def serve(
        self,
        record: dict,
        *,
        model: str,
        served_name: str,
        host: str,
        port: int,
        kv_events: dict | None,
        evict_peers: bool,
    ) -> tuple[list[str], dict]:
        return policy.serve_args(
            record, model=model, served_name=served_name, host=host, port=port, kv_events=kv_events
        )

    def tensor_parallel_args(self, size: int) -> list[str]:
        return ["--tp-size", str(size)]

    def memory_fraction(self, args: list[str]) -> str | None:
        return policy.memory_fraction(args)

    def check(self, run: Path, plan: dict) -> None:
        runtime_check.check(run, plan)

    def measure_cache(self, run: Path, plan: dict) -> None:
        raise _no_step("cache sizing")

    def capture_cache(self, run: Path, plan: dict) -> None:
        raise _no_step("cache capture")

    def model_dimensions(self, run: Path, plan: dict) -> None:
        attestation.model_dimensions(run, plan)

    def handshake_policy(self, run: Path, plan: dict) -> None:
        raise ValueError("SGLang has no handshake compatibility policy to inspect")

    def registration_layout(self, run: Path, plan: dict, source: Path, from_runtime: bool) -> None:
        raise _no_step("cache registration")

    def capture_connector(self, run: Path) -> Path:
        return attestation.capture_server(run)

    def capture_model_dimensions(self, run: Path) -> Path:
        return attestation.capture_model_dimensions(run)

    def capture_native(self, run: Path) -> Path:
        return attestation.capture_native(run)

    def engine_document(self, run: Path, startup_log: Path) -> dict:
        return attestation.engine_document(run, startup_log)

    def dev_runtime(self, template: dict, model_dir: Path, memory_fraction: float) -> dict:
        return dev.runtime(template, model_dir, memory_fraction)

    def check_dev_imports(self, runtime: dict) -> None:
        dev.check_imports()

    def check_dev_files(self, runtime: dict) -> None:
        return None
