"""Validate operator-owned local dev settings and fixed initialization recipes."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from narwhal import contracts
from narwhal.deployment.management_access import AccessError, read_input
from narwhal.deployment.management_records import OperationError
from narwhal.deployment.management_registry import ManagementTarget

Milliseconds = Annotated[int, Field(ge=1, le=86_400_000)]
Fraction = Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]


class Exact(BaseModel):
    """Reject coercion and unknown registered options."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class InitSettings(Exact):
    """Expose the existing initialization overrides without a command escape."""

    model_path: str | None = None
    model_dir: str | None = None
    gpu_uuid: Annotated[str, Field(pattern=r"^GPU-[A-Za-z0-9-]+$")] | None = None
    fabric_interface: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")] | None = None
    engine_count: Annotated[int, Field(ge=2, le=8)] | None = None
    port_base: Annotated[int, Field(ge=1, le=65535)] | None = None
    gpu_memory_utilization: Fraction | None = None
    device_allowance: Fraction | None = None

    @field_validator("model_path", "model_dir")
    @classmethod
    def absolute_path(cls, value: str | None) -> str | None:
        """Require operator paths to be literal absolute filesystem names."""
        if value is not None and (
            not Path(value).is_absolute()
            or ".." in Path(value).parts
            or any(char in value for char in "\0\r\n")
        ):
            raise ValueError("Model paths must be literal absolute paths")
        return value


class ActionBudget(Exact):
    """Bound action execution and all three cleanup phases."""

    timeout_ms: Milliseconds = 300_000
    term_grace_ms: Milliseconds = 10_000
    kill_grace_ms: Milliseconds = 5000
    reconcile_ms: Milliseconds = 30_000


class UpBudget(ActionBudget):
    """Keep the launch timeout when operators override only cleanup settings."""

    timeout_ms: Milliseconds = 3_600_000


class DownBudget(ActionBudget):
    """Keep the teardown timeout when operators override only cleanup settings."""

    timeout_ms: Milliseconds = 60_000


class Budgets(Exact):
    """Assign a finite deadline to each installed dev action."""

    dev_init: ActionBudget = Field(default_factory=ActionBudget)
    dev_up: UpBudget = Field(default_factory=UpBudget)
    dev_verify: ActionBudget = Field(default_factory=ActionBudget)
    dev_down: DownBudget = Field(default_factory=DownBudget)


class DevSettings(Exact):
    """Select registered initialization inputs and finite action budgets."""

    schema_name: Literal["narwhal.local-dev-settings"] = Field(
        default="narwhal.local-dev-settings", alias="schema"
    )
    schema_version: Literal[1] = 1
    init: InitSettings = Field(default_factory=InitSettings)
    budgets: Budgets = Field(default_factory=Budgets)

    def init_arguments(self) -> list[str]:
        """Render only explicitly registered overrides as fixed CLI flags."""
        flags = {
            "model_path": "--model",
            "model_dir": "--model-dir",
            "gpu_uuid": "--gpu",
            "fabric_interface": "--interface",
            "engine_count": "--engine-count",
            "port_base": "--port-base",
            "gpu_memory_utilization": "--gpu-memory-utilization",
            "device_allowance": "--device-allowance",
        }
        return [
            argument
            for name, value in self.init.model_dump(exclude_none=True).items()
            for argument in (flags[name], str(value))
        ]

    def budget(self, action: str) -> ActionBudget:
        """Select a supported action's fixed budget."""
        if action not in Budgets.model_fields:
            raise OperationError("invalid_input", "Unsupported local dev action")
        result: ActionBudget = getattr(self.budgets, action)
        return result


def load(target: ManagementTarget) -> DevSettings:
    """Read the selected settings or use the documented initialization defaults."""
    if target.adapter.settings_path is None:
        return DevSettings()
    try:
        raw = read_input(target.adapter.settings_path)
        value = json.loads(raw)
        contracts.validate_document(value, contracts.LOCAL_DEV_SETTINGS)
        return DevSettings.model_validate_json(raw)
    except OperationError:
        raise
    except AccessError as error:
        raise OperationError(error.code, error.message) from None
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise OperationError("invalid_input", "Local dev settings are invalid") from None


def validate_recipe(document: dict[str, Any]) -> dict[str, Any]:
    """Validate a native template before using it as managed executable input."""
    from narwhal.deployment.launch_engine import validate_runtime

    from .template import _port_layout

    try:
        required = {
            "schema",
            "schema_version",
            "name",
            "gpu",
            "model",
            "runtime",
            "allocation",
            "ports",
            "slo",
            "controller",
            "profile",
        }
        if (
            not required <= document.keys()
            or set(document) - required - {"role_cycle"}
            or document["schema"] != "narwhal.dev-template"
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
        ):
            raise ValueError("Template schema is invalid")
        for name in required - {"schema", "schema_version", "name"}:
            if not isinstance(document[name], dict):
                raise ValueError("Template section is invalid")
        runtime = document["runtime"]
        model = document["model"]
        model_required = {
            "repository",
            "revision",
            "filename",
            "sha256",
            "served_name",
            "tokenizer_repository",
            "tokenizer_revision",
            "tokenizer_sha256",
        }
        if not model_required <= model.keys() or set(model) - model_required - {"quantization"}:
            raise ValueError("Template model fields are invalid")
        for key in ("repository", "tokenizer_repository"):
            if not isinstance(model[key], str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model[key]
            ):
                raise ValueError("Model repository must be a fixed repository name")
        if not isinstance(model["served_name"], str) or not model["served_name"]:
            raise ValueError("Served model name must be nonempty")
        if "quantization" in model and not isinstance(model["quantization"], str):
            raise ValueError("Model quantization must be a string")
        for key in ("revision", "tokenizer_revision"):
            if not isinstance(model[key], str) or not re.fullmatch(
                r"[0-9a-f]{40}|sha256:[0-9a-f]{64}", model[key]
            ):
                raise ValueError("Model revisions must be immutable")
        if (
            not isinstance(model["filename"], str)
            or Path(model["filename"]).name != model["filename"]
            or not model["filename"].endswith(".gguf")
            or not isinstance(model["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", model["sha256"])
        ):
            raise ValueError("Model file or digest is invalid")
        if not isinstance(model["tokenizer_sha256"], dict):
            raise ValueError("Tokenizer hashes must be an object")
        for name, value in model["tokenizer_sha256"].items():
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or name in {".", ".."}
                or not isinstance(value, str)
                or not re.fullmatch("[0-9a-f]{64}", value)
            ):
                raise ValueError("Tokenizer file or digest is invalid")
        gpu = document["gpu"]
        if set(gpu) - {"product", "reserve_mib", "minimum_total_mib"}:
            raise ValueError("GPU fields are invalid")
        for name in ("reserve_mib", "minimum_total_mib"):
            value = gpu.get(name, 0)
            if type(value) is not int or not 0 <= value <= 1_048_576:
                raise ValueError("GPU memory limits are invalid")
        if "reserve_mib" not in gpu:
            raise ValueError("GPU reserve is required")
        if "product" in gpu and (not isinstance(gpu["product"], str) or not gpu["product"]):
            raise ValueError("GPU product must be nonempty")
        if not isinstance(runtime.get("expected_packages"), dict) or not isinstance(
            runtime.get("environment", {}), dict
        ):
            raise ValueError("Runtime packages and environment must be objects")
        if set(runtime) - {
            "expected_packages",
            "model_dtype",
            "kv_cache_dtype",
            "block_size",
            "max_model_len",
            "max_num_seqs",
            "environment",
            "gguf_plugin_source_revision",
            "gguf_plugin_python_sha256",
            "gguf_plugin_extension_sha256",
        }:
            raise ValueError("Template runtime contains unsupported fields")
        if {"PYTHONPATH", "LD_LIBRARY_PATH"} & set(runtime.get("environment", {})):
            raise ValueError("Managed templates cannot change code-loading paths")
        allowed_environment = {
            "VLLM_SSM_CONV_STATE_LAYOUT": {"DS"},
            "VLLM_USE_FLASHINFER_SAMPLER": {"0", "1"},
            "VLLM_USE_V2_MODEL_RUNNER": {"0", "1"},
        }
        if any(
            name not in allowed_environment or value not in allowed_environment[name]
            for name, value in runtime.get("environment", {}).items()
        ):
            raise ValueError("Managed runtime environment option is unsupported")
        validate_runtime(runtime)
        for key in ("max_model_len", "max_num_seqs"):
            if type(runtime.get(key)) is not int or not 1 <= runtime[key] <= 1_048_576:
                raise ValueError("Template serving limit is invalid")
        allocation = document["allocation"]
        if set(allocation) != {"engine_count", "device_allowance", "gpu_memory_utilization"}:
            raise ValueError("Template allocation fields are invalid")
        InitSettings.model_validate(allocation)
        count = allocation["engine_count"]
        ports = document["ports"]
        if set(ports) != {"router", "engine_first", "attestation_first", "nixl_first", "ucx_range"}:
            raise ValueError("Template port fields are invalid")
        if type(ports["router"]) is not int:
            raise ValueError("Router port must be an integer")
        if not isinstance(ports["ucx_range"], str) or not re.fullmatch(
            r"[0-9]+-[0-9]+", ports["ucx_range"]
        ):
            raise ValueError("UCX TCP range is invalid")
        _port_layout(document, count)
        allowed_profile = {
            "prefill_lens",
            "decode_input_lens",
            "decode_concurrency",
            "decode_tokens",
            "decode_repeats",
            "prefill_repeats",
            "neighbour_prefill_rps",
            "neighbour_decode_rps",
            "neighbour_prefill_tokens",
            "neighbour_decode_input_tokens",
            "neighbour_decode_output_tokens",
        }
        profile = document["profile"]
        if not profile or set(profile) - allowed_profile:
            raise ValueError("Template profile options are invalid")
        for key, value in profile.items():
            if (key in {"prefill_lens", "decode_input_lens", "decode_concurrency"}) != isinstance(
                value, list
            ):
                raise ValueError("Template profile scalar or sequence type is invalid")
            values = value if isinstance(value, list) else [value]
            if not 1 <= len(values) <= 128 or any(
                type(item) not in {int, float} or not math.isfinite(item) or item <= 0
                for item in values
            ):
                raise ValueError("Template profile values must be finite and positive")
            if key not in {"neighbour_prefill_rps", "neighbour_decode_rps"} and any(
                type(item) is not int for item in values
            ):
                raise ValueError("Template profile counts must be integers")
        # Reject JSON NaN/infinity even in fields consumed by a later fixed module.
        json.dumps(document, allow_nan=False)
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise OperationError("invalid_input", "Registered dev template is invalid") from None
    return document


def resolve_init(target: ManagementTarget, recipe_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a registered recipe and its initialization overrides."""
    recipe = next((row for row in target.recipes if row.id == recipe_id), None)
    if recipe is None or recipe.kind != "dev":
        raise OperationError("invalid_input", "Dev recipe is not registered")
    try:
        document = json.loads(read_input(recipe.path))
        if not isinstance(document, dict):
            raise ValueError("Recipe must be an object")
        validate_recipe(document)
    except OperationError:
        raise
    except AccessError as error:
        raise OperationError(error.code, error.message) from None
    except (ValueError, TypeError, RecursionError):
        raise OperationError("invalid_input", "Registered dev recipe is invalid") from None
    overrides = load(target).init.model_dump(exclude_none=True)
    for name in ("model_path", "model_dir"):
        if name in overrides:
            overrides[name] = Path(overrides[name])
    return document, overrides
