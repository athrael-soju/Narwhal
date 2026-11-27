from __future__ import annotations

import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ...backends import EngineBackend
    from ..attestation_contract.capture import Capture, CaptureContext
    from ..attestation_contract.document import AttestationContext, AttestationEvidence
    from .check import CheckContext, ProbeResult
    from .settings import LaunchSettings


class EngineLauncher(ABC):
    # Standalone module copied into each launch's hook as launch_engine.py.
    runtime_script: ClassVar[Path]
    side_channel: ClassVar[str]
    api_key_env: ClassVar[str]
    # Discovery pins the image distributions whose names fully match this pattern, and copies
    # image variables with these prefixes except the launcher-managed ones.
    image_packages: ClassVar[str]
    environment_prefixes: ClassVar[tuple[str, ...]]
    managed_environment: ClassVar[frozenset[str]]
    launch_settings: ClassVar[LaunchSettings]
    # Runtime check output: the pass summary, the cache line's name and the version label.
    check_summary: ClassVar[str]
    prefix_cache_name: ClassVar[str]
    version_label: ClassVar[str]
    # Flags whose values change per launch; the attested launch arguments omit their values.
    launch_varying_flags: ClassVar[frozenset[str]]
    # Discovery settings that take one value for the fleet; a NARWHAL_NODE_<n>_ override is
    # refused.
    fleet_settings: ClassVar[frozenset[str]] = frozenset()

    @abstractmethod
    def validate_runtime(self, runtime: dict) -> None: ...

    @abstractmethod
    def discovered_runtime(
        self, observed: dict, setting: Callable[[str, str], str], connector: str
    ) -> dict: ...

    @abstractmethod
    def sequence_limit(self, runtime: dict) -> int: ...

    @abstractmethod
    def engine_env(
        self, host: str, side_channel_port: int, api_key: str, bind_host: str | None = None
    ) -> dict[str, str]: ...

    @abstractmethod
    def side_channel_address(self, env: Mapping[str, str]) -> tuple[str, int]: ...

    @abstractmethod
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
    ) -> tuple[list[str], dict]: ...

    def config_files(self, record: dict, directory: str) -> tuple[dict[str, str], list[str]]:
        """Engine configuration files for this launch, by file name, and the arguments that
        pass them to the engine from `directory`.
        """
        return {}, []

    def runtime_pins(self, runtime: dict) -> dict:
        """Runtime fields the plan records for the runtime check to verify; none by default."""
        return {}

    @abstractmethod
    def memory_fraction(self, args: list[str]) -> str | None: ...

    @abstractmethod
    def runtime_probe(self, context: CheckContext) -> list[str]:
        """Return the interpreter arguments of the in-image probe; raise for a failed input."""

    @abstractmethod
    def probe_result(self, context: CheckContext, output: str) -> ProbeResult: ...

    @abstractmethod
    def attestation_captures(self, context: CaptureContext) -> list[Capture]:
        """The backend's attestation captures, in order, for a container or native engine."""

    @abstractmethod
    def transfer_package(self, plan: dict) -> str | None:
        """Name the pinned package whose version the contract records as transfer_version."""

    @abstractmethod
    def attestation_evidence(self, context: AttestationContext) -> AttestationEvidence: ...

    @abstractmethod
    def dev_runtime(self, template: dict, model_dir: Path, memory_fraction: float) -> dict: ...

    @abstractmethod
    def check_dev_imports(self, runtime: dict) -> None: ...

    @abstractmethod
    def check_dev_files(self, runtime: dict) -> None: ...


class CacheCapture(ABC):
    """Optional launcher capability: the engine's KV cache layout, read through a hook."""

    # Delivered as the launch hook's sitecustomize.py, pinned in the plan and enabled at start.
    cache_hook: ClassVar[Path]
    # Whether capture-cache also reads a native launch's records from its launch directory.
    native_capture: ClassVar[bool] = False

    @abstractmethod
    def capture_cache(self, run: Path, plan: dict) -> None: ...


class TransferMeasurement(ABC):
    """Optional launcher capability: KV handoff payloads measured between two live engines.

    Engine hosts run `transfer_tool` as a copy checked by its SHA-256, with the host's own
    Python and only the standard library. It writes the transfer record that the fabric
    budget's `calculate --transfer-record` sizes a budget from.
    """

    transfer_tool: ClassVar[Path]


class CacheMeasurement(ABC):
    """Optional capability: the KV cache layout from a temporary sizing container."""

    @abstractmethod
    def measure_cache(self, run: Path, plan: dict) -> None: ...


class CacheRegistration(ABC):
    """Optional capability: the connector registers the KV cache in one resolved layout."""

    @abstractmethod
    def cache_layout(self, run: Path) -> str:
        """The KV cache layout that the live engine of `run` resolved across its ranks."""


class LiveModelDimensions(ABC):
    """Optional capability; without it, dimensions come from the model configuration."""

    @abstractmethod
    def model_dimensions(self, run: Path, plan: dict) -> None:
        """Write model-dimensions.json from the engine image."""

    @abstractmethod
    def capture_model_dimensions(self, run: Path, out: Path) -> Path:
        """Write model-dimensions.live.json into `out` from the live engine of `run`."""


class ModelFormats(ABC):
    """Optional capability: a launch may name a model file, not only a directory."""

    # Discovery also pins the image distributions whose names fully match this pattern when
    # the deployment names a model file; None pins no further distribution.
    model_file_packages: ClassVar[str | None] = None

    @abstractmethod
    def check_model(self, runtime: dict, model: str) -> None: ...


def cache_capture(engine: EngineLauncher) -> CacheCapture | None:
    return engine if isinstance(engine, CacheCapture) else None


def transfer_measurement(engine: EngineLauncher) -> TransferMeasurement | None:
    return engine if isinstance(engine, TransferMeasurement) else None


def run_import_check(script: str, label: str, *, traceback_fails: bool = False) -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = exc.stderr[-800:] if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise ValueError(f"native {label} import failed: {detail}") from exc
    if traceback_fails and result.stderr and "Traceback" in result.stderr:
        raise ValueError(f"native {label} import reported: {result.stderr[-800:]}")


def engine_backend(name: str | None = None) -> EngineBackend:
    from ...backends import DEFAULT_BACKEND, load

    return load(name or DEFAULT_BACKEND)


def launcher(name: str | None = None) -> EngineLauncher:
    return engine_backend(name).engine_launcher()


def plan_launcher(plan: Mapping) -> EngineLauncher:
    return launcher(plan.get("engine"))
