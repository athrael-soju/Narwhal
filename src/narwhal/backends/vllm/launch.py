from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import ClassVar

from ...deployment.attestation_contract.document import AttestationContext, AttestationEvidence
from ...deployment.launch_engine.backend import (
    CacheCapture,
    CacheMeasurement,
    CacheRegistration,
    EngineLauncher,
    LiveModelDimensions,
    ModelFormats,
)
from ...deployment.launch_engine.check import CheckContext
from . import attestation, captures, dev, discovery, probe_reader
from . import check as runtime_check
from . import plan as policy
from .runtime import LAUNCHER


class VllmLauncher(
    EngineLauncher,
    CacheCapture,
    CacheMeasurement,
    CacheRegistration,
    LiveModelDimensions,
    ModelFormats,
):
    runtime_script = LAUNCHER
    cache_hook = Path(__file__).with_name("cache_capture_hook.py")
    side_channel = "NIXL"
    api_key_env = "VLLM_API_KEY"
    image_packages = discovery.IMAGE_PACKAGES
    model_file_packages = discovery.GGUF_PLUGIN
    environment_prefixes = policy.ENV_PREFIXES
    managed_environment = frozenset(policy.MANAGED_ENV)
    launch_settings = policy.SETTINGS
    check_summary = "Runtime identity, package pins, connector import and tokenizer passed."
    prefix_cache_name = "Prefix caching"
    launch_varying_flags = attestation.LAUNCH_VARYING_FLAGS
    fleet_settings = discovery.FLEET_SETTINGS
    probe_reader: ClassVar[ModuleType] = probe_reader

    def validate_runtime(self, runtime: dict) -> None:
        policy.validate_runtime(runtime)

    def discovered_runtime(
        self, observed: dict, setting: Callable[[str, str], str], connector: str
    ) -> dict:
        return discovery.runtime(observed, setting)

    def sequence_limit(self, runtime: dict) -> int:
        return discovery.sequence_limit(runtime)

    def engine_env(
        self, host: str, side_channel_port: int, api_key: str, bind_host: str | None = None
    ) -> dict[str, str]:
        return {
            "NIXL_HOST_IP": host,
            "VLLM_NIXL_SIDE_CHANNEL_HOST": host,
            "VLLM_NIXL_SIDE_CHANNEL_PORT": str(side_channel_port),
            **({self.api_key_env: api_key} if api_key else {}),
        }

    def side_channel_address(self, env: Mapping[str, str]) -> tuple[str, int]:
        return env["VLLM_NIXL_SIDE_CHANNEL_HOST"], int(env["VLLM_NIXL_SIDE_CHANNEL_PORT"])

    def check_model(self, runtime: dict, model: str) -> None:
        if not model.endswith(".gguf"):
            if not Path(model).is_dir():
                raise ValueError("vLLM launches take a model directory or a .gguf model file")
            return
        plugin = discovery.GGUF_PLUGIN
        if not runtime["expected_packages"].get(plugin):
            raise ValueError(f"GGUF launch requires a pinned {plugin} package")

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

    def runtime_pins(self, runtime: dict) -> dict:
        return policy.runtime_pins(runtime)

    def memory_fraction(self, args: list[str]) -> str | None:
        return policy.memory_fraction(args)

    def runtime_probe(self, context: CheckContext) -> list[str]:
        return runtime_check.probe(context)

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

    def transfer_package(self, plan: dict) -> str | None:
        return attestation.nixl_package(plan)

    def attestation_evidence(self, context: AttestationContext) -> AttestationEvidence:
        return attestation.attestation_evidence(context)

    def dev_runtime(self, template: dict, model_dir: Path, memory_fraction: float) -> dict:
        return dev.runtime(template, model_dir, memory_fraction)

    def check_dev_imports(self, runtime: dict) -> None:
        dev.check_imports()

    def check_dev_files(self, runtime: dict) -> None:
        dev.check_plugin(runtime)
