from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import ClassVar

from ...deployment.attestation_contract.document import AttestationContext, AttestationEvidence
from ...deployment.launch_engine.backend import CacheCapture, EngineLauncher
from ...deployment.launch_engine.check import CheckContext
from . import attestation, captures, dev, discovery, probe_reader
from . import check as runtime_check
from . import plan as policy
from .runtime import API_KEY_ENV, LAUNCHER

SIDE_CHANNEL_HOST_ENV = "NARWHAL_TRTLLM_TRANSFER_HOST"
SIDE_CHANNEL_PORT_ENV = "NARWHAL_TRTLLM_TRANSFER_PORT"


class TrtllmLauncher(EngineLauncher, CacheCapture):
    runtime_script = LAUNCHER
    cache_hook = Path(__file__).with_name("cache_capture_hook.py")
    native_capture = True
    side_channel = "transfer"
    api_key_env = API_KEY_ENV
    image_packages = discovery.IMAGE_PACKAGES
    environment_prefixes = policy.ENV_PREFIXES
    managed_environment = frozenset(policy.MANAGED_ENV)
    launch_settings = policy.SETTINGS
    check_summary = "Runtime identity, package pins and server configuration passed."
    prefix_cache_name = "KV block reuse"
    launch_varying_flags = attestation.LAUNCH_VARYING_FLAGS
    probe_reader: ClassVar[ModuleType] = probe_reader

    def validate_runtime(self, runtime: dict) -> None:
        policy.validate_runtime(runtime)

    def discovered_runtime(
        self, observed: dict, setting: Callable[[str, str], str], connector: str
    ) -> dict:
        return discovery.runtime(observed, setting, connector)

    def sequence_limit(self, runtime: dict) -> int:
        return discovery.sequence_limit(runtime)

    def engine_env(
        self, host: str, side_channel_port: int, api_key: str, bind_host: str | None = None
    ) -> dict[str, str]:
        # trtllm-serve takes no API key and serves every caller: keep its port on a private
        # network. The transceiver opens its own listener ports; the launch reserves this one.
        return {
            SIDE_CHANNEL_HOST_ENV: host,
            SIDE_CHANNEL_PORT_ENV: str(side_channel_port),
            **({self.api_key_env: api_key} if api_key else {}),
        }

    def side_channel_address(self, env: Mapping[str, str]) -> tuple[str, int]:
        return env[SIDE_CHANNEL_HOST_ENV], int(env[SIDE_CHANNEL_PORT_ENV])

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
        args = policy.serve_args(record, model=model, served_name=served_name, host=host, port=port)
        return args, policy.transfer(record["runtime"])

    def config_files(self, record: dict, directory: str) -> tuple[dict[str, str], list[str]]:
        return policy.config_files(record, directory)

    def memory_fraction(self, args: list[str]) -> str | None:
        return policy.memory_fraction(args)

    def runtime_probe(self, context: CheckContext) -> list[str]:
        return runtime_check.probe(context)

    def capture_cache(self, run: Path, plan: dict) -> None:
        captures.capture_cache(run, plan)

    def capture_connector(self, run: Path) -> Path:
        return attestation.capture_server(run)

    def capture_native(self, run: Path) -> Path:
        return attestation.capture_native(run)

    def transfer_package(self, plan: dict) -> str | None:
        # The cache transceiver ships inside the TensorRT-LLM wheel.
        return "tensorrt_llm" if plan["expected_packages"].get("tensorrt_llm") else None

    def attestation_evidence(self, context: AttestationContext) -> AttestationEvidence:
        return attestation.attestation_evidence(context)

    def dev_runtime(self, template: dict, model_dir: Path, memory_fraction: float) -> dict:
        return dev.runtime(template, model_dir, memory_fraction)

    def check_dev_imports(self, runtime: dict) -> None:
        dev.check_imports()

    def check_dev_files(self, runtime: dict) -> None:
        return None
