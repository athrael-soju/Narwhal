import hashlib
import importlib.util
import json
import os
import sys
from importlib.abc import MetaPathFinder
from pathlib import Path
from typing import Any

# The module whose KV cache creator builds each rank's cache managers.
CREATOR_MODULE = "tensorrt_llm._torch.pyexecutor._util"
# The cache pages a handoff sends, as narwhal-engine capture-cache records them.
SIZING = "runtime_padded_page_upper_bound"


class Unsupported(Exception):
    """A cache the hook cannot size. Serving continues, and capture-cache reports why."""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_bytes(manager: Any) -> int:
    """Bytes of recurrent state one sequence holds on this rank, sent with its pages."""
    try:
        from tensorrt_llm._torch.pyexecutor.resource_manager import (  # type: ignore[import-not-found]
            LinearCacheType,
        )

        marker = LinearCacheType.RECURRENT_STATES.value
    except (ImportError, AttributeError):
        marker = None
    windows = list(getattr(manager, "max_attention_window_vec", None) or [])
    layers = windows.count(marker) if marker is not None else 0
    metadata = getattr(manager, "linear_attention_metadata", None)
    if metadata is None:
        if layers or "mamba" in type(manager).__name__.lower():
            raise Unsupported(f"{type(manager).__name__} exposes no recurrent state size")
        return 0
    if marker is None:
        raise Unsupported("the engine names no recurrent state layers")
    return layers * int(metadata.all_recurrent_states_bytes)


def rank_layout(creator: Any, manager: Any) -> dict:
    """The pages one sequence of a complete prompt occupies on this rank."""
    tokens = int(manager.tokens_per_block)
    layers = [
        {
            "layer": "attention",
            "page_bytes": int(manager.get_cache_bytes_per_token()) * tokens,
            "block_tokens": tokens,
            "extra_blocks": 0,
        }
    ]
    if state := state_bytes(manager):
        # One state per sequence: a block as long as the longest sequence.
        layers.append(
            {
                "layer": "recurrent_state",
                "page_bytes": state,
                "block_tokens": int(creator._max_seq_len),
                "extra_blocks": 0,
            }
        )
    return {"rank": int(creator._mapping.tp_rank), "layers": layers}


def record(creator: Any, resources: dict) -> None:
    from tensorrt_llm._torch.pyexecutor.resource_manager import ResourceManagerType

    plan_path = Path(os.environ["NARWHAL_CACHE_PLAN"])
    plan_data = plan_path.read_bytes()
    plan = json.loads(plan_data)
    if (
        digest(Path(plan.get("model_config_path", "/model/config.json")))
        != plan["model_config_sha256"]
    ):
        raise ValueError("model config differs from the checked serving plan")
    if digest(Path(__file__).with_name("launch_engine.py")) != plan["launcher_sha256"]:
        raise ValueError("cache hook launcher differs from the checked serving plan")
    rank = int(creator._mapping.tp_rank)
    try:
        if int(creator._mapping.pp_size) != 1:
            raise Unsupported("cache capture supports pipeline parallel size 1")
        if resources.get(ResourceManagerType.DRAFT_KV_CACHE_MANAGER) is not None:
            raise Unsupported("cache capture does not size a separate draft cache")
        layout = rank_layout(creator, resources[ResourceManagerType.KV_CACHE_MANAGER])
        unsupported = {}
    except Unsupported as error:
        layout = {"rank": rank, "layers": []}
        unsupported = {"unsupported": str(error)}
    value = {
        "schema_version": 1,
        "sizing": SIZING,
        "image": plan["image"],
        "expected_packages": plan["expected_packages"],
        "revision": plan["revision"],
        "model_config_sha256": plan["model_config_sha256"],
        "launch_config_sha256": plan["launch_sha256"],
        "plan_sha256": hashlib.sha256(plan_data).hexdigest(),
        "launcher_sha256": plan["launcher_sha256"],
        "cache_capture_sha256": plan["cache_capture_sha256"],
        **unsupported,
        "ranks": [layout],
    }
    # Each rank runs in its own process, so each writes its own record.
    output = Path(os.environ["NARWHAL_CACHE_OUTPUT"])
    path = output.with_name(f"{output.stem}.rank{rank}{output.suffix}")
    text = json.dumps(value, indent=2) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_text() != text:
            raise ValueError("cache layout differs from this launch's earlier start") from None
        return
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


def patch(module: Any) -> None:
    original = module.KvCacheCreator.build_managers

    def build_managers(self: Any, resources: dict, estimating_kv_cache: bool = False) -> Any:
        result = original(self, resources, estimating_kv_cache)
        # The estimation pass builds throwaway managers; the serving pass follows it.
        if not estimating_kv_cache:
            record(self, resources)
        return result

    module.KvCacheCreator.build_managers = build_managers


class CreatorFinder(MetaPathFinder):
    """Patch the KV cache creator when a worker imports it, and only then."""

    def __init__(self) -> None:
        self.searching = False

    def find_spec(self, name: str, path: Any, target: Any = None) -> Any:
        if name != CREATOR_MODULE or self.searching:
            return None
        # The other finders locate the module; this one only wraps its loader.
        self.searching = True
        try:
            spec = importlib.util.find_spec(name)
        finally:
            self.searching = False
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        execute = loader.exec_module

        def exec_module(module: Any) -> None:
            execute(module)
            patch(module)
            if self in sys.meta_path:
                sys.meta_path.remove(self)

        loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec


if os.environ.get("NARWHAL_CAPTURE_CACHE") == "1":
    sys.meta_path.insert(0, CreatorFinder())
