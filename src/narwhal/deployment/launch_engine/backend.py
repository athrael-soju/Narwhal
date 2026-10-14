from __future__ import annotations

import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ...backends import EngineBackend
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

    @abstractmethod
    def memory_fraction(self, args: list[str]) -> str | None: ...

    @abstractmethod
    def runtime_probe(self, context: CheckContext) -> list[str]:
        """Return the interpreter arguments of the in-image probe; raise for a failed input."""

    @abstractmethod
    def probe_result(self, context: CheckContext, output: str) -> ProbeResult: ...

    @abstractmethod
    def capture_connector(self, run: Path) -> Path: ...

    @abstractmethod
    def capture_native(self, run: Path) -> Path: ...

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

    @abstractmethod
    def measure_cache(self, run: Path, plan: dict) -> None: ...

    @abstractmethod
    def capture_cache(self, run: Path, plan: dict) -> None: ...

    @abstractmethod
    def registration_layout(
        self, run: Path, plan: dict, source: Path, from_runtime: bool
    ) -> None: ...

    @abstractmethod
    def handshake_policy(self, run: Path, plan: dict) -> None: ...


class LiveModelDimensions(ABC):
    """Optional capability; without it, dimensions come from the model configuration."""

    @abstractmethod
    def model_dimensions(self, run: Path, plan: dict) -> None:
        """Write model-dimensions.json from the engine image."""

    @abstractmethod
    def capture_model_dimensions(self, run: Path) -> Path: ...


class ModelFormats(ABC):
    """Optional capability: a native launch may name a model file, not only a directory."""

    @abstractmethod
    def check_model(self, runtime: dict, model: str) -> None: ...


def cache_capture(engine: EngineLauncher) -> CacheCapture | None:
    return engine if isinstance(engine, CacheCapture) else None


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
    return engine_backend(name).launcher()


def plan_launcher(plan: Mapping) -> EngineLauncher:
    return launcher(plan.get("engine"))
