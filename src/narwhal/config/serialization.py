"""Versioned fleet JSON serialization with credential references only."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..contracts import FLEET, versioned

if TYPE_CHECKING:
    from .model import FleetConfig


def document(config: FleetConfig) -> dict[str, Any]:
    """Build the versioned fleet document with credential references."""
    out = {
        "model": config.model,
        "engines": [
            {"iid": e.iid, "url": e.url, "role": e.role.value}
            | ({"pin": True} if e.pin else {})
            | ({"attestation_url": e.attestation_url} if e.attestation_url else {})
            for e in config.engines
        ],
        "slo": {"ttft_s": config.slo.ttft_s, "tpot_s": config.slo.tpot_s},
        "controller": {
            "monitor_interval_s": config.monitor_interval_s,
            "monitor_failure_limit": config.monitor_failure_limit,
            "advisory": config.advisory,
            "min_prefill": config.min_prefill,
            "min_decode": config.min_decode,
            "flip_history": config.flip_history,
            "thresholds": {
                "expand": config.thresholds.expand,
                "shrink": config.thresholds.shrink,
                "cooldown_s": config.thresholds.cooldown_s,
                "dwell_s": config.thresholds.dwell_s,
                "sustained_intervals": config.thresholds.sustained_intervals,
                "panic_ratio": config.thresholds.panic_ratio,
                "flip_resident_guard": config.thresholds.flip_resident_guard,
            },
            "reactive": {
                "window_s": config.reactive_window_s,
                "confirmations": config.reactive_confirmations,
                "utilization": config.reactive_utilization,
                "min_arrivals": config.reactive_min_arrivals,
                "demand_floor": config.reactive_demand_floor,
                "movement_margin": config.reactive_movement_margin,
                "step_s": config.reactive_step_s,
                "evidence_span_s": config.reactive_evidence_span_s,
                "evidence_max_span_s": config.reactive_evidence_max_span_s,
                "evidence_min_arrivals": config.reactive_evidence_min_arrivals,
                "demand_rise_tolerance": config.reactive_demand_rise_tolerance,
                "decode_correction_min": config.reactive_decode_correction_min,
                "decode_correction_max": config.reactive_decode_correction_max,
                "decode_correction_alpha": config.reactive_decode_correction_alpha,
                "decode_correction_min_samples": config.reactive_decode_correction_min_samples,
            },
        },
        "serving": {
            "admission": config.admission,
            "admission_margin": config.admission_margin,
            "max_connections": config.max_connections,
            "request_timeout_s": config.request_timeout_s,
            "prefill_timeout_s": config.prefill_timeout_s,
            "graceful_timeout_s": config.graceful_timeout_s,
            **asdict(config.serving),
        },
        "engine": {
            "connector": config.connector,
            "dialect": config.dialect,
            "tokenize": config.tokenize,
            "chars_per_token": config.chars_per_token,
            "tokenize_timeout_s": config.tokenize_timeout_s,
            "engine_api_key_env": config.engine_api_key_env,
            "control_connections": config.control_connections,
            "pool_timeout_s": config.pool_timeout_s,
            "connect_timeout_s": config.connect_timeout_s,
            "health_timeout_s": config.health_timeout_s,
            "decode_read_timeout_s": config.decode_read_timeout_s,
            "first_token_timeout_s": config.first_token_timeout_s,
        },
        "recovery": {
            "eject_after": config.eject_after,
            "readmit_every": config.readmit_every,
            "liveness_every": config.liveness_every,
            "liveness_misses": config.liveness_misses,
            "engine_restart_policy": config.engine_restart_policy,
            "failure_quarantine_s": config.failure_quarantine_s,
            "state_path": str(config.state_path),
            "resume": config.resume,
            "health": {
                "window_s": config.health_window_s,
                "drift_band": config.health_drift_band,
                "min_samples": config.health_min_samples,
                "probation_windows": config.health_probation_windows,
                "evict_windows": config.health_evict_windows,
                "recovery_windows": config.health_recovery_windows,
                "probation_penalty_s": config.health_probation_penalty_s,
                "relative_band": config.health_relative_band,
            },
        },
        "profiles": {
            "path": str(config.profiles_path),
            "max_decode_fit_mape": config.profile_validation.max_decode_fit_mape,
            "max_decode_cv_mape": config.profile_validation.max_decode_cv_mape,
        },
    }
    if config.engine_contract is not None:
        out["engine_contract"] = config.engine_contract.fields()
    if config.hardware is not None:
        out["hardware"] = config.hardware.fields()
    return versioned(FLEET, out)


def save(config: FleetConfig, path: str | Path) -> None:
    """Write the replayable JSON configuration."""
    Path(path).write_text(json.dumps(document(config), indent=2) + "\n")
