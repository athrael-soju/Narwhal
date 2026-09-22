"""Capture vLLM's resolved KV pages during a normal serving startup.

The launcher installs this file as sitecustomize.py in a private, read-only
container mount. Only serving containers set NARWHAL_CAPTURE_CACHE.
"""

import hashlib
import json
import os
from pathlib import Path

if os.environ.get("NARWHAL_CAPTURE_CACHE") == "1":
    from launch_engine import cache_groups, digest
    from vllm.v1.engine.core import EngineCore

    original_initialize_caches = EngineCore._initialize_kv_caches

    def capture_caches(self, vllm_config):
        executor = self.model_executor
        original_initialize_from_config = executor.initialize_from_config

        def capture_allocation(configs):
            plan_path = Path(os.environ["NARWHAL_CACHE_PLAN"])
            plan_data = plan_path.read_bytes()
            plan = json.loads(plan_data)
            if digest(Path("/model/config.json")) != plan["model_config_sha256"]:
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
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, indent=2)
                stream.write("\n")
            return result

        executor.initialize_from_config = capture_allocation
        try:
            return original_initialize_caches(self, vllm_config)
        finally:
            executor.initialize_from_config = original_initialize_from_config

    EngineCore._initialize_kv_caches = capture_caches
