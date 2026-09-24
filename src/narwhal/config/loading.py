"""Strict fleet JSON parsing."""

from __future__ import annotations

import json
import math
from dataclasses import fields
from pathlib import Path
from typing import NoReturn

from ..contracts import FLEET, ContractVersionError, validate_document
from ..scheduling.control import SLO, Thresholds
from ..serving.policy import ServingPolicy
from ..types import Role
from .environment import resolve_endpoint
from .model import (
    DEFAULT_FIRST_TOKEN_TIMEOUT_S,
    EngineContract,
    EngineSpec,
    FleetConfig,
    HardwareSpec,
    ProfileValidationPolicy,
)


def load(path: str | Path) -> FleetConfig:
    """Load a fleet config and report all detectable schema errors."""
    raw = json.loads(Path(path).read_text(), parse_constant=_reject_constant)
    try:
        validate_document(raw, FLEET)
    except ContractVersionError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    problems = _unknown_keys(raw)
    for key in ("model", "engines", "slo"):
        if key not in raw:
            problems.append(f"missing required key {key!r}")
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    # Check exact JSON types before conversion. Invalid values use placeholders
    # while validation collects errors, then the combined errors abort loading.
    engines = []
    if not isinstance(raw["engines"], list):
        problems.append("engines must be a list")
    else:
        for k, e in enumerate(raw["engines"]):
            if not isinstance(e, dict):
                problems.append(f"engines[{k}] must be an object")
                continue
            _check_unknown(problems, f"engines[{k}]", e, _ENGINE_SPEC_KEYS)
            missing = [key for key in ("iid", "url") if key not in e]
            problems.extend(f"engines[{k}] is missing {key!r}" for key in missing)
            if missing:
                continue
            pin = e.get("pin", False)
            if not isinstance(pin, bool):
                problems.append(f"engines[{k}].pin must be a boolean")
                continue
            iid = _read_str(problems, f"engines[{k}].iid", e["iid"])
            url = _read_endpoint(problems, f"engines[{k}].url", e["url"])
            role_name = _read_str(problems, f"engines[{k}].role", e.get("role", "decode"))
            try:
                role = Role(role_name)
            except ValueError:
                problems.append(
                    f"engines[{k}].role must be 'prefill' or 'decode', got {role_name!r}"
                )
                role = Role.DECODE
            engines.append(
                EngineSpec(
                    iid=iid,
                    url=url.rstrip("/"),
                    role=role,
                    pin=pin,
                    attestation_url=_read_endpoint(
                        problems,
                        f"engines[{k}].attestation_url",
                        e.get("attestation_url", ""),
                    ),
                )
            )
    slo_raw = raw["slo"]
    slo: SLO
    if not isinstance(slo_raw, dict):
        problems.append("slo must be an object")
        slo = SLO(ttft_s=0.0, tpot_s=0.0)
    else:
        _check_unknown(problems, "slo", slo_raw, _SLO_KEYS)
        slo = SLO(
            ttft_s=_read_float(problems, "slo.ttft_s", slo_raw.get("ttft_s")),
            tpot_s=_read_float(problems, "slo.tpot_s", slo_raw.get("tpot_s")),
        )
    controller_raw = _read_section(problems, raw, "controller")
    serving_raw = _read_section(problems, raw, "serving")
    engine_raw = _read_section(problems, raw, "engine")
    recovery_raw = _read_section(problems, raw, "recovery")
    profiles_raw = _read_section(problems, raw, "profiles")
    _check_unknown(problems, "controller", controller_raw, _CONTROLLER_KEYS)
    _check_unknown(problems, "serving", serving_raw, _SERVING_KEYS)
    _check_unknown(problems, "engine", engine_raw, _ENGINE_KEYS)
    _check_unknown(problems, "recovery", recovery_raw, _RECOVERY_KEYS)
    _check_unknown(problems, "profiles", profiles_raw, _PROFILE_KEYS)

    thr_raw = _read_nested_section(problems, controller_raw, "thresholds", "controller.thresholds")
    _check_unknown(problems, "controller.thresholds", thr_raw, _THRESHOLD_KEYS)
    thresholds = Thresholds(
        expand=_read_float(problems, "controller.thresholds.expand", thr_raw.get("expand", 1.0)),
        shrink=_read_float(problems, "controller.thresholds.shrink", thr_raw.get("shrink", 0.5)),
        cooldown_s=_read_float(
            problems, "controller.thresholds.cooldown_s", thr_raw.get("cooldown_s", 10.0)
        ),
        sustained_intervals=_read_int(
            problems,
            "controller.thresholds.sustained_intervals",
            thr_raw.get("sustained_intervals", 3),
        ),
        dwell_s=_read_float(problems, "controller.thresholds.dwell_s", thr_raw.get("dwell_s", 0.0)),
        panic_ratio=_read_float(
            problems, "controller.thresholds.panic_ratio", thr_raw.get("panic_ratio", 0.0)
        ),
        flip_resident_guard=_read_int(
            problems,
            "controller.thresholds.flip_resident_guard",
            thr_raw.get("flip_resident_guard", 0),
        ),
    )
    reactive_raw = _read_nested_section(problems, controller_raw, "reactive", "controller.reactive")
    reactive_keys = {
        f.name.removeprefix("reactive_")
        for f in fields(FleetConfig)
        if f.name.startswith("reactive_")
    }
    _check_unknown(problems, "controller.reactive", reactive_raw, reactive_keys)
    health_raw = _read_nested_section(problems, recovery_raw, "health", "recovery.health")
    _check_unknown(problems, "recovery.health", health_raw, _HEALTH_KEYS)
    serving_keys = {f.name for f in fields(ServingPolicy)}
    serving = ServingPolicy(**{k: v for k, v in serving_raw.items() if k in serving_keys})
    try:
        serving.validate()
    except ValueError as exc:
        problems.append(str(exc))
    advisory = _read_bool(problems, "controller.advisory", controller_raw.get("advisory", False))
    min_prefill = _read_int(
        problems, "controller.min_prefill", controller_raw.get("min_prefill", 1)
    )
    min_decode = _read_int(problems, "controller.min_decode", controller_raw.get("min_decode", 1))
    profile_validation = ProfileValidationPolicy(
        max_decode_fit_mape=_read_float(
            problems,
            "profiles.max_decode_fit_mape",
            profiles_raw.get("max_decode_fit_mape", 0.05),
        ),
        max_decode_cv_mape=_read_float(
            problems,
            "profiles.max_decode_cv_mape",
            profiles_raw.get("max_decode_cv_mape", 0.13),
        ),
    )
    hardware_raw = raw.get("hardware")
    if hardware_raw is not None and not isinstance(hardware_raw, dict):
        problems.append("hardware must be an object")
        hardware_raw = None
    hardware: HardwareSpec | None = None
    if hardware_raw is not None:
        _check_unknown(problems, "hardware", hardware_raw, _HARDWARE_KEYS)
        accelerator = hardware_raw.get("accelerator")
        if not isinstance(accelerator, str):
            problems.append("hardware.accelerator must be a string")
            accelerator = ""
        hardware = HardwareSpec(
            accelerator=accelerator,
            accelerators_per_engine=_read_int(
                problems,
                "hardware.accelerators_per_engine",
                hardware_raw.get("accelerators_per_engine"),
            ),
            tensor_parallel=_read_int(
                problems, "hardware.tensor_parallel", hardware_raw.get("tensor_parallel")
            ),
        )
    contract_raw = raw.get("engine_contract")
    if contract_raw is not None and not isinstance(contract_raw, dict):
        problems.append("engine_contract must be an object")
        contract_raw = None
    engine_contract: EngineContract | None = None
    if contract_raw is not None:
        _check_unknown(problems, "engine_contract", contract_raw, _ENGINE_CONTRACT_KEYS)
        handshake = _read_bool(
            problems,
            "engine_contract.enforce_handshake_compat",
            contract_raw.get("enforce_handshake_compat", True),
        )
        for name in ("cross_layers_blocks", "hybrid_kv_cache_manager"):
            value = contract_raw.get(name)
            if value is not None and not isinstance(value, bool):
                problems.append(f"engine_contract.{name} must be a boolean or null")
        engine_contract = EngineContract(
            vllm_version=_read_str(
                problems, "engine_contract.vllm_version", contract_raw.get("vllm_version", "")
            ),
            image_digest=_read_str(
                problems, "engine_contract.image_digest", contract_raw.get("image_digest", "")
            ),
            nixl_version=_read_str(
                problems, "engine_contract.nixl_version", contract_raw.get("nixl_version", "")
            ),
            nixl_connector_version=_read_int(
                problems,
                "engine_contract.nixl_connector_version",
                contract_raw.get("nixl_connector_version", 0),
            ),
            model_architecture=_read_str(
                problems,
                "engine_contract.model_architecture",
                contract_raw.get("model_architecture", ""),
            ),
            model_dtype=_read_str(
                problems, "engine_contract.model_dtype", contract_raw.get("model_dtype", "")
            ),
            kv_heads=_read_int(
                problems, "engine_contract.kv_heads", contract_raw.get("kv_heads", 0)
            ),
            head_size=_read_int(
                problems, "engine_contract.head_size", contract_raw.get("head_size", 0)
            ),
            hidden_layers=_read_int(
                problems,
                "engine_contract.hidden_layers",
                contract_raw.get("hidden_layers", 0),
            ),
            attention_backend=_read_str(
                problems,
                "engine_contract.attention_backend",
                contract_raw.get("attention_backend", ""),
            ),
            kv_cache_dtype=_read_str(
                problems,
                "engine_contract.kv_cache_dtype",
                contract_raw.get("kv_cache_dtype", ""),
            ),
            cross_layers_blocks=contract_raw.get("cross_layers_blocks"),
            hybrid_kv_cache_manager=contract_raw.get("hybrid_kv_cache_manager"),
            connector=_read_str(
                problems,
                "engine_contract.connector",
                contract_raw.get("connector", "NixlConnector"),
            ),
            kv_role=_read_str(problems, "engine_contract.kv_role", contract_raw.get("kv_role", "")),
            transfer_mode=_read_str(
                problems,
                "engine_contract.transfer_mode",
                contract_raw.get("transfer_mode", ""),
            ),
            speculative_config=_read_str(
                problems,
                "engine_contract.speculative_config",
                contract_raw.get("speculative_config", ""),
            ),
            enforce_handshake_compat=handshake,
        )
    cfg = FleetConfig(
        model=_read_str(problems, "model", raw["model"]),
        engines=engines,
        slo=slo,
        thresholds=thresholds,
        monitor_interval_s=_read_float(
            problems,
            "controller.monitor_interval_s",
            controller_raw.get("monitor_interval_s", 1.0),
        ),
        monitor_failure_limit=_read_int(
            problems,
            "controller.monitor_failure_limit",
            controller_raw.get("monitor_failure_limit", 5),
        ),
        advisory=advisory,
        admission=_read_str(
            problems, "serving.admission", serving_raw.get("admission", "predictive")
        ),
        admission_margin=_read_float(
            problems, "serving.admission_margin", serving_raw.get("admission_margin", 0.0)
        ),
        reactive_window_s=_read_float(
            problems, "controller.reactive.window_s", reactive_raw.get("window_s", 120.0)
        ),
        reactive_evidence_span_s=_read_float(
            problems,
            "controller.reactive.evidence_span_s",
            reactive_raw.get("evidence_span_s", 60.0),
        ),
        reactive_evidence_max_span_s=_read_float(
            problems,
            "controller.reactive.evidence_max_span_s",
            reactive_raw.get("evidence_max_span_s", 120.0),
        ),
        reactive_evidence_min_arrivals=_read_int(
            problems,
            "controller.reactive.evidence_min_arrivals",
            reactive_raw.get("evidence_min_arrivals", 10),
        ),
        reactive_demand_rise_tolerance=_read_float(
            problems,
            "controller.reactive.demand_rise_tolerance",
            reactive_raw.get("demand_rise_tolerance", 0.25),
        ),
        reactive_confirmations=_read_int(
            problems,
            "controller.reactive.confirmations",
            reactive_raw.get("confirmations", 2),
        ),
        reactive_utilization=_read_float(
            problems,
            "controller.reactive.utilization",
            reactive_raw.get("utilization", 0.8),
        ),
        reactive_min_arrivals=_read_int(
            problems,
            "controller.reactive.min_arrivals",
            reactive_raw.get("min_arrivals", 10),
        ),
        reactive_demand_floor=_read_float(
            problems,
            "controller.reactive.demand_floor",
            reactive_raw.get("demand_floor", 0.5),
        ),
        reactive_movement_margin=_read_float(
            problems,
            "controller.reactive.movement_margin",
            reactive_raw.get("movement_margin", 0.05),
        ),
        reactive_step_s=_read_float(
            problems, "controller.reactive.step_s", reactive_raw.get("step_s", 5.0)
        ),
        reactive_decode_correction_min=_read_float(
            problems,
            "controller.reactive.decode_correction_min",
            reactive_raw.get("decode_correction_min", 0.5),
        ),
        reactive_decode_correction_max=_read_float(
            problems,
            "controller.reactive.decode_correction_max",
            reactive_raw.get("decode_correction_max", 2.0),
        ),
        reactive_decode_correction_alpha=_read_float(
            problems,
            "controller.reactive.decode_correction_alpha",
            reactive_raw.get("decode_correction_alpha", 0.2),
        ),
        reactive_decode_correction_min_samples=_read_int(
            problems,
            "controller.reactive.decode_correction_min_samples",
            reactive_raw.get("decode_correction_min_samples", 8),
        ),
        health_window_s=_read_float(
            problems, "recovery.health.window_s", health_raw.get("window_s", 30.0)
        ),
        health_drift_band=_read_float(
            problems, "recovery.health.drift_band", health_raw.get("drift_band", 2.0)
        ),
        health_min_samples=_read_int(
            problems, "recovery.health.min_samples", health_raw.get("min_samples", 3)
        ),
        health_probation_windows=_read_int(
            problems,
            "recovery.health.probation_windows",
            health_raw.get("probation_windows", 3),
        ),
        health_evict_windows=_read_int(
            problems, "recovery.health.evict_windows", health_raw.get("evict_windows", 5)
        ),
        health_recovery_windows=_read_int(
            problems,
            "recovery.health.recovery_windows",
            health_raw.get("recovery_windows", 3),
        ),
        health_probation_penalty_s=_read_float(
            problems,
            "recovery.health.probation_penalty_s",
            health_raw.get("probation_penalty_s", 1.5),
        ),
        health_relative_band=_read_float(
            problems,
            "recovery.health.relative_band",
            health_raw.get("relative_band", 1.5),
        ),
        eject_after=_read_int(problems, "recovery.eject_after", recovery_raw.get("eject_after", 3)),
        readmit_every=_read_int(
            problems, "recovery.readmit_every", recovery_raw.get("readmit_every", 10)
        ),
        liveness_every=_read_int(
            problems, "recovery.liveness_every", recovery_raw.get("liveness_every", 10)
        ),
        liveness_misses=_read_int(
            problems, "recovery.liveness_misses", recovery_raw.get("liveness_misses", 2)
        ),
        engine_restart_policy=_read_str(
            problems,
            "recovery.engine_restart_policy",
            recovery_raw.get("engine_restart_policy", "individual"),
        ),
        tokenize_timeout_s=_read_float(
            problems, "engine.tokenize_timeout_s", engine_raw.get("tokenize_timeout_s", 2.0)
        ),
        max_connections=_read_int(
            problems, "serving.max_connections", serving_raw.get("max_connections", 512)
        ),
        serving=serving,
        control_connections=_read_int(
            problems,
            "engine.control_connections",
            engine_raw.get("control_connections", 0),
        ),
        pool_timeout_s=_read_float(
            problems, "engine.pool_timeout_s", engine_raw.get("pool_timeout_s", 5.0)
        ),
        connect_timeout_s=_read_float(
            problems, "engine.connect_timeout_s", engine_raw.get("connect_timeout_s", 10.0)
        ),
        health_timeout_s=_read_float(
            problems, "engine.health_timeout_s", engine_raw.get("health_timeout_s", 5.0)
        ),
        flip_history=_read_int(
            problems, "controller.flip_history", controller_raw.get("flip_history", 1000)
        ),
        graceful_timeout_s=_read_float(
            problems,
            "serving.graceful_timeout_s",
            serving_raw.get("graceful_timeout_s", 30.0),
        ),
        profiles_path=Path(
            _read_str(problems, "profiles.path", profiles_raw.get("path", "runs/profiles.json"))
        ),
        request_timeout_s=_read_float(
            problems,
            "serving.request_timeout_s",
            serving_raw.get("request_timeout_s", 600.0),
        ),
        prefill_timeout_s=_read_float(
            problems,
            "serving.prefill_timeout_s",
            serving_raw.get("prefill_timeout_s", 120.0),
        ),
        chars_per_token=_read_float(
            problems, "engine.chars_per_token", engine_raw.get("chars_per_token", 3.8)
        ),
        tokenize=_read_bool(problems, "engine.tokenize", engine_raw.get("tokenize", True)),
        engine_api_key_env=_read_str(
            problems,
            "engine.engine_api_key_env",
            engine_raw.get("engine_api_key_env", ""),
        ),
        failure_quarantine_s=_read_float(
            problems,
            "recovery.failure_quarantine_s",
            recovery_raw.get("failure_quarantine_s", 0.0),
        ),
        decode_read_timeout_s=_read_float(
            problems,
            "engine.decode_read_timeout_s",
            engine_raw.get("decode_read_timeout_s", 60.0),
        ),
        first_token_timeout_s=_read_float(
            problems,
            "engine.first_token_timeout_s",
            engine_raw.get("first_token_timeout_s", DEFAULT_FIRST_TOKEN_TIMEOUT_S),
        ),
        state_path=Path(
            _read_str(
                problems,
                "recovery.state_path",
                recovery_raw.get("state_path", "runs/state.json"),
            )
        ),
        resume=_read_bool(problems, "recovery.resume", recovery_raw.get("resume", False)),
        min_prefill=min_prefill,
        min_decode=min_decode,
        connector=_read_str(problems, "engine.connector", engine_raw.get("connector", "nixl")),
        dialect=_read_str(problems, "engine.dialect", engine_raw.get("dialect", "vllm")),
        engine_contract=engine_contract,
        hardware=hardware,
        profile_validation=profile_validation,
    )
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    if not engines:
        raise ValueError(f"{path} declares no engines")
    seen = {e.iid for e in engines}
    if len(seen) != len(engines):
        raise ValueError(f"{path} repeats an instance id")
    cfg.validate(str(path))
    return cfg


def _read_endpoint(problems: list[str], field: str, raw: object) -> str:
    value = _read_str(problems, field, raw)
    try:
        return resolve_endpoint(value, field)
    except ValueError as exc:
        problems.append(str(exc))
        return ""


_KNOWN_KEYS = {
    "schema",
    "schema_version",
    "model",
    "engines",
    "slo",
    "controller",
    "serving",
    "engine",
    "recovery",
    "profiles",
    "engine_contract",
    "hardware",
}

_SLO_KEYS = {"ttft_s", "tpot_s"}
_ENGINE_SPEC_KEYS = {"iid", "url", "role", "pin", "attestation_url"}
_CONTROLLER_KEYS = {
    "monitor_interval_s",
    "monitor_failure_limit",
    "advisory",
    "min_prefill",
    "min_decode",
    "flip_history",
    "thresholds",
    "reactive",
}
_THRESHOLD_KEYS = {
    "expand",
    "shrink",
    "cooldown_s",
    "sustained_intervals",
    "dwell_s",
    "panic_ratio",
    "flip_resident_guard",
}
_SERVING_KEYS = {
    "admission",
    "admission_margin",
    "max_connections",
    "request_timeout_s",
    "prefill_timeout_s",
    "graceful_timeout_s",
    *(f.name for f in fields(ServingPolicy)),
}
_ENGINE_KEYS = {
    "connector",
    "dialect",
    "tokenize",
    "chars_per_token",
    "tokenize_timeout_s",
    "engine_api_key_env",
    "control_connections",
    "pool_timeout_s",
    "connect_timeout_s",
    "health_timeout_s",
    "decode_read_timeout_s",
    "first_token_timeout_s",
}
_RECOVERY_KEYS = {
    "eject_after",
    "readmit_every",
    "liveness_every",
    "liveness_misses",
    "engine_restart_policy",
    "failure_quarantine_s",
    "state_path",
    "resume",
    "health",
}
_HEALTH_KEYS = {
    "window_s",
    "drift_band",
    "min_samples",
    "probation_windows",
    "evict_windows",
    "recovery_windows",
    "probation_penalty_s",
    "relative_band",
}
_PROFILE_KEYS = {"path", "max_decode_fit_mape", "max_decode_cv_mape"}


_ENGINE_CONTRACT_KEYS = {
    "vllm_version",
    "image_digest",
    "nixl_version",
    "nixl_connector_version",
    "model_architecture",
    "model_dtype",
    "kv_heads",
    "head_size",
    "hidden_layers",
    "attention_backend",
    "kv_cache_dtype",
    "cross_layers_blocks",
    "hybrid_kv_cache_manager",
    "connector",
    "kv_role",
    "transfer_mode",
    "speculative_config",
    "enforce_handshake_compat",
}


_HARDWARE_KEYS = {"accelerator", "accelerators_per_engine", "tensor_parallel"}


def _reject_constant(name: str) -> NoReturn:
    # json's default accepts NaN/Infinity tokens; no fleet value may be
    # non-finite because NaN comparisons pass every range check.
    raise ValueError(f"non-finite JSON constant {name}")


def _read_bool(problems: list[str], name: str, value: object) -> bool:
    """Accept a JSON boolean; anything else is one named problem."""
    if not isinstance(value, bool):
        problems.append(f"{name} must be a boolean")
        return False
    return value


def _read_str(problems: list[str], name: str, value: object) -> str:
    """Accept a JSON string; anything else is one named problem."""
    if not isinstance(value, str):
        problems.append(f"{name} must be a string")
        return ""
    return value


def _read_int(problems: list[str], name: str, value: object) -> int:
    """Accept a JSON integer, excluding Python's bool subclass."""
    if not isinstance(value, int) or isinstance(value, bool):
        problems.append(f"{name} must be an integer")
        return 0
    return value


def _read_float(problems: list[str], name: str, value: object) -> float:
    """Accept a JSON number, widening integers to float and rejecting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        problems.append(f"{name} must be a number")
        return 0.0
    try:
        converted = float(value)
    except OverflowError:
        converted = math.inf
    if not math.isfinite(converted):
        problems.append(f"{name} must be finite")
        return 0.0
    return converted


def _read_section(problems: list[str], raw: dict, name: str) -> dict:
    """Descend into one nested object after a single dict check."""
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{name} must be an object")
        return {}
    return value


def _read_nested_section(problems: list[str], raw: dict, key: str, name: str) -> dict:
    """Read a nested object while reporting its complete public path."""
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{name} must be an object")
        return {}
    return value


def _check_unknown(problems: list[str], name: str, raw: dict, known: set[str]) -> None:
    """Reject unknown keys in one section while accepting annotated comments."""
    unknown = sorted(key for key in raw if key not in known and not key.startswith("_"))
    problems.extend(
        f"unknown {name} key {key!r} (a typo falls back to the default silently)" for key in unknown
    )


def _unknown_keys(raw: dict) -> list[str]:
    """Reject unknown fields while allowing underscore-prefixed annotations."""
    unknown = sorted(k for k in raw if k not in _KNOWN_KEYS and not k.startswith("_"))
    return [f"unknown key {k!r} (a typo falls back to the default silently)" for k in unknown]
