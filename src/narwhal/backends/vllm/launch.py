from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ...deployment.launch_engine.backend import EngineLauncher
from . import attestation, captures, dev
from . import check as runtime_check
from . import plan as policy
from .runtime import LAUNCHER


class VllmLauncher(EngineLauncher):
    runtime_script = LAUNCHER
    cache_hook = Path(__file__).with_name("cache_capture_hook.py")
    side_channel = "NIXL"
    side_channel_port_env = "NARWHAL_NIXL_SIDE_CHANNEL_PORT"
    api_key_env = "VLLM_API_KEY"
    args_field = "vllm_args"
    version_field = "vllm_version"
    checked_version_field = "vllm_api_version"

    def validate_runtime(self, runtime: dict) -> None:
        policy.validate_runtime(runtime)

    def engine_env(self, host: str, side_channel_port: int, api_key: str) -> dict[str, str]:
        return {
            "NIXL_HOST_IP": host,
            "VLLM_NIXL_SIDE_CHANNEL_HOST": host,
            "VLLM_NIXL_SIDE_CHANNEL_PORT": str(side_channel_port),
            **({self.api_key_env: api_key} if api_key else {}),
        }

    def side_channel_address(self, env: Mapping[str, str]) -> tuple[str, int]:
        return env["VLLM_NIXL_SIDE_CHANNEL_HOST"], int(env["VLLM_NIXL_SIDE_CHANNEL_PORT"])

    def check_model(self, runtime: dict, model: str) -> None:
        if model.endswith(".gguf") and not runtime["expected_packages"].get("vllm-gguf-plugin"):
            raise ValueError("GGUF launch requires a pinned vllm-gguf-plugin package")

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
            record,
            model=model,
            served_name=served_name,
            host=host,
            port=port,
            kv_events=kv_events,
            evict_peers=evict_peers,
        )

    def tensor_parallel_args(self, size: int) -> list[str]:
        return ["--tensor-parallel-size", str(size)]

    def memory_fraction(self, args: list[str]) -> str | None:
        return policy.memory_fraction(args)

    def check(self, run: Path, plan: dict) -> None:
        runtime_check.check(run, plan)

    def measure_cache(self, run: Path, plan: dict) -> None:
        captures.measure_cache(run, plan)

    def capture_cache(self, run: Path, plan: dict) -> None:
        captures.capture_cache(run, plan)

    def model_dimensions(self, run: Path, plan: dict) -> None:
        captures.model_dimensions(run, plan)

    def handshake_policy(self, run: Path, plan: dict) -> None:
        captures.handshake_policy(run, plan)

    def registration_layout(self, run: Path, plan: dict, source: Path, from_runtime: bool) -> None:
        captures.registration_layout(run, plan, source, from_runtime)

    def capture_connector(self, run: Path) -> Path:
        return attestation.capture_nixl(run)

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
        dev.check_plugin(runtime)
