from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

# Mounted into engine images and copied into each launch's serving hook.
LAUNCHER = Path(__file__)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)


def write_once(path: Path, text: str, mismatch: str) -> None:
    try:
        write_private(path, text)
    except FileExistsError:
        if path.read_text() != text:
            raise ValueError(mismatch) from None


def cache_groups(groups: list) -> list[dict]:
    result = []
    for group in groups:
        spec = group.kv_cache_spec
        layers = getattr(spec, "kv_cache_specs", None)
        layers = layers if layers is not None else dict.fromkeys(group.layer_names, spec)
        for name, layer in layers.items():
            kind = type(layer).__name__
            extra = 0
            if kind == "MambaSpec":
                # Bound an aligned state's previous/current pages and checkpoint slots.
                extra = (
                    1
                    + layer.num_speculative_blocks
                    + getattr(layer, "num_prefill_checkpoint_blocks", 0)
                )
            elif kind in ("SlidingWindowSpec", "ChunkedLocalAttentionSpec"):
                extra = 1
            elif kind not in ("FullAttentionSpec", "MLAAttentionSpec", "AttentionSpec"):
                raise ValueError(f"Cache sizing requires a page bound for {kind}")
            result.append(
                {
                    "layer": name,
                    "kind": kind,
                    "block_tokens": layer.block_size,
                    "page_bytes": layer.page_size_bytes,
                    "extra_blocks": extra,
                }
            )
    return result


def engine_arguments(plan: dict) -> list[str]:
    # HTTP listener options belong to the API server.
    arguments = list(plan["args"][2:])
    for option in ("--host", "--port"):
        index = arguments.index(option)
        del arguments[index : index + 2]
    return arguments


def runtime_config(plan: dict) -> Any:
    from vllm.engine.arg_utils import EngineArgs  # type: ignore[import-not-found]
    from vllm.utils.argparse_utils import FlexibleArgumentParser  # type: ignore[import-not-found]

    model_config = Path(plan.get("model_config_path", "/model/config.json"))
    if digest(model_config) != plan["model_config_sha256"]:
        raise ValueError("model config changed since launch preparation")
    parser = EngineArgs.add_cli_args(FlexibleArgumentParser())
    return EngineArgs.from_cli_args(
        parser.parse_args(engine_arguments(plan))
    ).create_engine_config()


def runtime_model_dimensions(plan_path: Path) -> dict:
    plan = json.loads(plan_path.read_text())
    model = runtime_config(plan).model_config
    methods = {
        "head_size": "get_head_size",
        "kv_heads": "get_total_num_kv_heads",
        "hidden_layers": "get_total_num_hidden_layers",
    }
    values = {field: getattr(model, method)() for field, method in methods.items()}
    if any(type(value) is not int or value < 1 for value in values.values()):
        raise ValueError("model contract getters must return positive integers")
    return {
        "contract": values,
        "sources": {field: f"ModelConfig.{method}()" for field, method in methods.items()},
        "model_architecture": model.architecture,
        "use_mla": model.use_mla,
        "model_config_sha256": plan["model_config_sha256"],
        "plan_sha256": digest(plan_path),
        "image": plan["image"],
        "revision": plan["revision"],
    }


def runtime_cache_probe(plan_path: Path) -> None:
    from vllm.v1.engine.core import EngineCore  # type: ignore[import-not-found]
    from vllm.v1.executor.abstract import Executor  # type: ignore[import-not-found]

    plan = json.loads(plan_path.read_text())
    config = runtime_config(plan)
    workers = []
    captured = []

    class SizingComplete(Exception):
        pass

    class ProbeExecutor(Executor.get_class(config)):  # type: ignore[misc]
        def __init__(self, vllm_config: Any) -> None:
            workers.append(self)
            super().__init__(vllm_config)

        def initialize_from_config(self, configs: Any) -> None:
            for rank, allocation in enumerate(configs):
                captured.append(
                    {
                        "rank": rank,
                        "layers": cache_groups(allocation.kv_cache_groups),
                        "kv_cache_layout": allocation.kv_cache_layout,
                    }
                )
            # EngineCore has loaded/profiled the model and resolved padding at this point.
            raise SizingComplete

    try:
        EngineCore(config, executor_class=ProbeExecutor, log_stats=False)
        raise ValueError("runtime cache allocation hook was skipped")
    except SizingComplete:
        pass
    finally:
        for worker in workers:
            worker.shutdown()
    if len(captured) != config.parallel_config.tensor_parallel_size:
        raise ValueError("cache sizing requires one allocation record per TP rank")
    value = {
        "schema_version": 1,
        "sizing": "runtime_padded_page_upper_bound",
        "image": plan["image"],
        "expected_packages": plan["expected_packages"],
        "revision": plan["revision"],
        "model_config_sha256": plan["model_config_sha256"],
        "launch_config_sha256": plan["launch_sha256"],
        "plan_sha256": digest(plan_path),
        "launcher_sha256": plan["launcher_sha256"],
        "ranks": captured,
    }
    write_private(
        plan_path.parent / "cache-layout.pending.json", json.dumps(value, indent=2) + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("_cache-probe", "_model-dimensions"):
        sub.add_parser(name).add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    context = f"narwhal-engine: {args.command}"
    try:
        if args.command == "_cache-probe":
            runtime_cache_probe(args.plan)
        else:
            print("NARWHAL_MODEL_DIMENSIONS=" + json.dumps(runtime_model_dimensions(args.plan)))
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        if isinstance(error, FileExistsError):
            parser.exit(1, f"{context}: artifact already exists: {error.filename or error}\n")
        parser.exit(1, f"{context}: {args.plan}: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
