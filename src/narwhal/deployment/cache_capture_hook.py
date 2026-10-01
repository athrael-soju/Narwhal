"""Capture vLLM's resolved KV pages during a normal serving startup.

The launcher installs this file as sitecustomize.py in a private, read-only
container mount. Only serving containers set NARWHAL_CAPTURE_CACHE. A restart of
the same launch keeps the first capture and stops if the pages differ. Before
vLLM measures free GPU memory, a serving worker waits up to PEER_RELEASE_WAIT_S
for memory that KV peers still map from a stopped engine.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Covers the router's first two peer release rounds after an ejection.
PEER_RELEASE_WAIT_S = 180.0
PEER_RELEASE_POLL_S = 5.0

if os.environ.get("NARWHAL_CAPTURE_CACHE") == "1":
    from launch_engine import cache_groups, digest  # type: ignore[import-not-found]
    from vllm.v1.engine.core import EngineCore  # type: ignore[import-not-found]
    from vllm.v1.worker.gpu_worker import Worker  # type: ignore[import-not-found]

    original_init_device = Worker.init_device

    def wait_for_device_memory(self: Any) -> Any:
        import torch  # type: ignore[import-not-found]

        budget = self.cache_config.gpu_memory_utilization
        deadline = time.monotonic() + PEER_RELEASE_WAIT_S
        while True:
            free, total = torch.cuda.mem_get_info(self.local_rank)
            if free >= budget * total or time.monotonic() >= deadline:
                break
            print(
                f"narwhal: GPU {self.local_rank} has {free / 2**30:.1f} GiB free of "
                f"{budget * total / 2**30:.1f} GiB requested; waiting for KV peers to release it",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(PEER_RELEASE_POLL_S)
        return original_init_device(self)

    Worker.init_device = wait_for_device_memory

    original_initialize_caches = EngineCore._initialize_kv_caches

    def capture_caches(self: Any, vllm_config: Any) -> Any:
        executor = self.model_executor
        original_initialize_from_config = executor.initialize_from_config

        def capture_allocation(configs: Any) -> Any:
            plan_path = Path(os.environ["NARWHAL_CACHE_PLAN"])
            plan_data = plan_path.read_bytes()
            plan = json.loads(plan_data)
            model_config = Path(plan.get("model_config_path", "/model/config.json"))
            if digest(model_config) != plan["model_config_sha256"]:
                raise ValueError("model config differs from the checked serving plan")
            if digest(Path(__file__).with_name("launch_engine.py")) != plan["launcher_sha256"]:
                raise ValueError("cache hook launcher differs from the checked serving plan")
            ranks = [
                {
                    "rank": rank,
                    "layers": cache_groups(allocation.kv_cache_groups),
                    "kv_cache_layout": allocation.kv_cache_layout,
                }
                for rank, allocation in enumerate(configs)
            ]
            if len(ranks) != vllm_config.parallel_config.tensor_parallel_size:
                raise ValueError("serving cache capture requires every TP rank")
            result = original_initialize_from_config(configs)
            value = {
                "schema_version": 1,
                "sizing": "runtime_padded_page_upper_bound",
                "image": plan["image"],
                "expected_packages": plan["expected_packages"],
                "revision": plan["revision"],
                "model_config_sha256": plan["model_config_sha256"],
                "launch_config_sha256": plan["launch_sha256"],
                "plan_sha256": hashlib.sha256(plan_data).hexdigest(),
                "launcher_sha256": plan["launcher_sha256"],
                "cache_capture_sha256": plan["cache_capture_sha256"],
                "ranks": ranks,
            }
            output = Path(os.environ["NARWHAL_CACHE_OUTPUT"])
            text = json.dumps(value, indent=2) + "\n"
            try:
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # A restarted container or native run keeps its first capture.
                if output.read_text() != text:
                    message = "cache layout differs from this launch's earlier start"
                    raise ValueError(message) from None
                return result
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            return result

        executor.initialize_from_config = capture_allocation
        try:
            return original_initialize_caches(self, vllm_config)
        finally:
            executor.initialize_from_config = original_initialize_from_config

    EngineCore._initialize_kv_caches = capture_caches
