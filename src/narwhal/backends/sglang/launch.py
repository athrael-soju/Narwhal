from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from ...deployment.attestation_contract.document import AttestationContext, AttestationEvidence
from ...deployment.launch_engine.backend import EngineLauncher
from ...deployment.launch_engine.check import CheckContext, ProbeResult
from . import attestation, dev, discovery
from . import check as runtime_check
from . import plan as policy
from .runtime import API_KEY_ENV, BOOTSTRAP_PORT_ENV, LAUNCHER

BOOTSTRAP_HOST_ENV = "NARWHAL_SGLANG_BOOTSTRAP_HOST"


class SglangLauncher(EngineLauncher):
    runtime_script = LAUNCHER
    side_channel = "bootstrap"
    api_key_env = API_KEY_ENV
    image_packages = discovery.IMAGE_PACKAGES
    environment_prefixes = policy.ENV_PREFIXES
    managed_environment = frozenset(policy.MANAGED_ENV)
    launch_settings = policy.SETTINGS
    check_summary = "Runtime identity, package pins and server arguments passed."
    prefix_cache_name = "The radix cache"
    launch_varying_flags = attestation.LAUNCH_VARYING_FLAGS
    version_label = "SGLang"

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
        # The bootstrap server listens on the HTTP bind address.
        bootstrap = host if bind_host in (None, "0.0.0.0", "::") else bind_host
        return {
            BOOTSTRAP_HOST_ENV: bootstrap,
            BOOTSTRAP_PORT_ENV: str(side_channel_port),
            # SGLang advertises this address to decode peers.
            "SGLANG_HOST_IP": host,
            "SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT": str(policy.BOOTSTRAP_TIMEOUT_S),
            **({self.api_key_env: api_key} if api_key else {}),
        }

    def side_channel_address(self, env: Mapping[str, str]) -> tuple[str, int]:
        return env[BOOTSTRAP_HOST_ENV], int(env[BOOTSTRAP_PORT_ENV])

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

    def memory_fraction(self, args: list[str]) -> str | None:
        return policy.memory_fraction(args)

    def runtime_probe(self, context: CheckContext) -> list[str]:
        return runtime_check.probe(context)

    def probe_result(self, context: CheckContext, output: str) -> ProbeResult:
        return runtime_check.result(context, output)

    def capture_connector(self, run: Path) -> Path:
        return attestation.capture_server(run)

    def capture_native(self, run: Path) -> Path:
        return attestation.capture_native(run)

    def transfer_package(self, plan: dict) -> str | None:
        return policy.transfer_package(
            plan["connector"]["transfer_backend"], plan["expected_packages"]
        )

    def attestation_evidence(self, context: AttestationContext) -> AttestationEvidence:
        return attestation.attestation_evidence(context)

    def dev_runtime(self, template: dict, model_dir: Path, memory_fraction: float) -> dict:
        return dev.runtime(template, model_dir, memory_fraction)

    def check_dev_imports(self, runtime: dict) -> None:
        dev.check_imports()

    def check_dev_files(self, runtime: dict) -> None:
        return None
