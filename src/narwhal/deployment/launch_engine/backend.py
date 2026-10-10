from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ...backends import EngineBackend


class EngineLauncher(ABC):
    # Standalone module copied into each launch's hook as launch_engine.py.
    runtime_script: ClassVar[Path]
    # Delivered as the hook's sitecustomize.py.
    cache_hook: ClassVar[Path]
    side_channel: ClassVar[str]
    side_channel_port_env: ClassVar[str]
    api_key_env: ClassVar[str]
    args_field: ClassVar[str]
    version_field: ClassVar[str]
    checked_version_field: ClassVar[str]

    @abstractmethod
    def validate_runtime(self, runtime: dict) -> None: ...

    @abstractmethod
    def engine_env(self, host: str, side_channel_port: int, api_key: str) -> dict[str, str]: ...

    @abstractmethod
    def side_channel_address(self, env: Mapping[str, str]) -> tuple[str, int]: ...

    @abstractmethod
    def check_model(self, runtime: dict, model: str) -> None: ...

    @abstractmethod
    def publishes_kv_events(self, extra_args: list[str]) -> bool: ...

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
    def tensor_parallel_args(self, size: int) -> list[str]: ...

    @abstractmethod
    def memory_fraction(self, args: list[str]) -> str | None: ...

    @abstractmethod
    def check(self, run: Path, plan: dict) -> None: ...

    @abstractmethod
    def measure_cache(self, run: Path, plan: dict) -> None: ...

    @abstractmethod
    def capture_cache(self, run: Path, plan: dict) -> None: ...

    @abstractmethod
    def model_dimensions(self, run: Path, plan: dict) -> None: ...

    @abstractmethod
    def handshake_policy(self, run: Path, plan: dict) -> None: ...

    @abstractmethod
    def registration_layout(
        self, run: Path, plan: dict, source: Path, from_runtime: bool
    ) -> None: ...

    @abstractmethod
    def capture_connector(self, run: Path) -> Path: ...

    @abstractmethod
    def capture_model_dimensions(self, run: Path) -> Path: ...

    @abstractmethod
    def capture_native(self, run: Path) -> Path: ...

    @abstractmethod
    def engine_document(self, run: Path, startup_log: Path) -> dict: ...

    @abstractmethod
    def dev_runtime(self, template: dict, model_dir: Path, memory_fraction: float) -> dict: ...

    @abstractmethod
    def check_dev_imports(self, runtime: dict) -> None: ...

    @abstractmethod
    def check_dev_files(self, runtime: dict) -> None: ...


def engine_backend(name: str | None = None) -> EngineBackend:
    from ...backends import DEFAULT_BACKEND, load

    return load(name or DEFAULT_BACKEND)


def launcher(name: str | None = None) -> EngineLauncher:
    return engine_backend(name).launcher()


def plan_launcher(plan: Mapping) -> EngineLauncher:
    return launcher(plan.get("engine"))
