"""Cross-field fleet constraints, including derived connection budgets."""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from ..engines.connector import lookup as lookup_connector
from ..engines.dialect import lookup as lookup_dialect

if TYPE_CHECKING:
    from .model import FleetConfig


def validate(config: FleetConfig, source: str = "config") -> None:
    """Validate cross-field constraints and report all failures together."""
    problems = []
    try:
        config.serving.validate()
    except ValueError as exc:
        problems.append(str(exc))
    positive = [
        ("slo.ttft_s", config.slo.ttft_s),
        ("slo.tpot_s", config.slo.tpot_s),
        ("controller.thresholds.expand", config.thresholds.expand),
        ("controller.monitor_interval_s", config.monitor_interval_s),
        ("engine.tokenize_timeout_s", config.tokenize_timeout_s),
        ("engine.pool_timeout_s", config.pool_timeout_s),
        ("engine.connect_timeout_s", config.connect_timeout_s),
        ("engine.health_timeout_s", config.health_timeout_s),
        ("serving.request_timeout_s", config.request_timeout_s),
        ("serving.prefill_timeout_s", config.prefill_timeout_s),
        ("engine.chars_per_token", config.chars_per_token),
        ("engine.first_token_timeout_s", config.first_token_timeout_s),
    ]
    for name, value in positive:
        if value <= 0:
            problems.append(f"{name} must be positive, got {value}")
    not_negative = [
        ("controller.thresholds.shrink", config.thresholds.shrink),
        ("controller.thresholds.cooldown_s", config.thresholds.cooldown_s),
        ("controller.thresholds.dwell_s", config.thresholds.dwell_s),
        ("controller.reactive.movement_margin", config.reactive_movement_margin),
        ("serving.graceful_timeout_s", config.graceful_timeout_s),
        # 0 disables the gap bound between decode chunks.
        ("engine.decode_read_timeout_s", config.decode_read_timeout_s),
        # 0 disables the guard; a positive value caps donor residency on flips.
        ("controller.thresholds.flip_resident_guard", config.thresholds.flip_resident_guard),
    ]
    for name, value in not_negative:
        if value < 0:
            problems.append(f"{name} must be nonnegative, got {value}")

    if config.thresholds.panic_ratio != 0.0 and config.thresholds.panic_ratio < 1.0:
        problems.append(
            "controller.thresholds.panic_ratio must be 0 (off) or at least 1, "
            f"got {config.thresholds.panic_ratio}"
        )
    at_least_one = [
        ("controller.thresholds.sustained_intervals", config.thresholds.sustained_intervals),
        ("controller.monitor_failure_limit", config.monitor_failure_limit),
        ("recovery.eject_after", config.eject_after),
        ("recovery.readmit_every", config.readmit_every),
        ("recovery.liveness_misses", config.liveness_misses),
        ("serving.max_connections", config.max_connections),
        ("controller.flip_history", config.flip_history),
    ]
    for name, value in at_least_one:
        if value < 1:
            problems.append(f"{name} must be at least 1, got {value}")
    # Resolve the derived control-pool budget; the stored int is what
    # save() and /narwhal/state then report.
    if config.control_connections < 0:
        problems.append(
            f"engine.control_connections must be nonnegative, got {config.control_connections}"
        )
    elif config.control_connections == 0:
        config.control_connections = config.resolved_control_connections()

    if config.admission not in ("predictive", "open"):
        problems.append(
            f"serving.admission must be 'predictive' or 'open', got {config.admission!r}"
        )
    if config.admission_margin < 0:
        problems.append(
            f"serving.admission_margin must be nonnegative, got {config.admission_margin}"
        )
    if config.failure_quarantine_s < 0:
        problems.append(
            f"recovery.failure_quarantine_s must be nonnegative, got {config.failure_quarantine_s}"
        )
    for name, value in (
        ("controller.reactive.window_s", config.reactive_window_s),
        ("controller.reactive.utilization", config.reactive_utilization),
        ("controller.reactive.demand_floor", config.reactive_demand_floor),
        ("controller.reactive.step_s", config.reactive_step_s),
        ("controller.reactive.evidence_span_s", config.reactive_evidence_span_s),
        ("controller.reactive.evidence_max_span_s", config.reactive_evidence_max_span_s),
    ):
        if value <= 0:
            problems.append(f"{name} must be positive, got {value}")
    for name, value in (
        ("controller.reactive.confirmations", config.reactive_confirmations),
        ("controller.reactive.min_arrivals", config.reactive_min_arrivals),
        ("controller.reactive.evidence_min_arrivals", config.reactive_evidence_min_arrivals),
    ):
        if value < 1:
            problems.append(f"{name} must be at least 1, got {value}")
    if config.reactive_utilization > 1.0:
        problems.append(
            "controller.reactive.utilization is a fraction of an instance, got "
            f"{config.reactive_utilization}"
        )
    if config.reactive_movement_margin >= 1.0:
        problems.append(
            "controller.reactive.movement_margin must be below 1, got "
            f"{config.reactive_movement_margin}"
        )
    if config.reactive_demand_rise_tolerance < 0:
        problems.append(
            "controller.reactive.demand_rise_tolerance must be nonnegative, got "
            f"{config.reactive_demand_rise_tolerance}"
        )
    if config.reactive_evidence_span_s > config.reactive_evidence_max_span_s:
        problems.append(
            f"controller.reactive.evidence_span_s {config.reactive_evidence_span_s} exceeds "
            "controller.reactive.evidence_max_span_s "
            f"{config.reactive_evidence_max_span_s}: the "
            "minimum evidence duration cannot outlast the bounded lookback"
        )
    # The demand window must retain arrivals for the full evidence lookback.
    if config.reactive_evidence_max_span_s > config.reactive_window_s:
        problems.append(
            "controller.reactive.evidence_max_span_s "
            f"{config.reactive_evidence_max_span_s} exceeds controller.reactive.window_s "
            f"{config.reactive_window_s}: a demand window shorter "
            "than the bounded evidence lookback cannot collect it"
        )
    if config.reactive_decode_correction_min <= 0:
        problems.append(
            "controller.reactive.decode_correction_min must be positive, got "
            f"{config.reactive_decode_correction_min}"
        )
    if config.reactive_decode_correction_max < config.reactive_decode_correction_min:
        problems.append(
            "controller.reactive.decode_correction_max must be at least "
            "controller.reactive.decode_correction_min"
        )
    if not 0 < config.reactive_decode_correction_alpha <= 1:
        problems.append(
            "controller.reactive.decode_correction_alpha must be in (0, 1], got "
            f"{config.reactive_decode_correction_alpha}"
        )
    if config.reactive_decode_correction_min_samples < 1:
        problems.append("controller.reactive.decode_correction_min_samples must be at least 1")
    if config.thresholds.shrink >= config.thresholds.expand:
        problems.append(
            f"controller.thresholds.shrink {config.thresholds.shrink} must be below "
            "controller.thresholds.expand "
            f"{config.thresholds.expand}: the band between them is where the "
            f"pool holds still"
        )
    # A drift band at 1.0 would classify the profiled baseline as degraded.
    if config.health_drift_band <= 1.0:
        problems.append(
            f"recovery.health.drift_band must exceed 1.0, got {config.health_drift_band}"
        )
    for name, value in (
        ("recovery.health.window_s", config.health_window_s),
        ("recovery.health.min_samples", config.health_min_samples),
        ("recovery.health.probation_windows", config.health_probation_windows),
        ("recovery.health.evict_windows", config.health_evict_windows),
        ("recovery.health.recovery_windows", config.health_recovery_windows),
    ):
        if value < 1:
            problems.append(f"{name} must be at least 1, got {value}")
    # A window opens on the first observation and closes once window_s has
    # passed. The monitor contributes one residual per engine per pass.
    # Bound the threshold by the configured cadence. Slow passes and missing
    # observations can leave an actual window below this nominal count.
    if config.monitor_interval_s > 0 and config.health_window_s >= 1:
        nominal_samples = math.floor(config.health_window_s / config.monitor_interval_s)
        if nominal_samples < config.health_min_samples:
            problems.append(
                f"recovery.health.min_samples {config.health_min_samples} exceeds the nominal "
                f"{nominal_samples} samples per recovery.health.window_s "
                f"{config.health_window_s} at controller.monitor_interval_s "
                f"{config.monitor_interval_s}"
            )
    # Ejection requires the streak to reach probation first.
    if config.health_evict_windows < config.health_probation_windows:
        problems.append(
            f"recovery.health.evict_windows {config.health_evict_windows} is below "
            "recovery.health.probation_windows "
            f"{config.health_probation_windows}: ejection "
            "is only reachable after probation"
        )
    if config.health_probation_penalty_s < 0:
        problems.append(
            "recovery.health.probation_penalty_s must be nonnegative, got "
            f"{config.health_probation_penalty_s}"
        )
    if config.health_relative_band < 0:
        problems.append(
            f"recovery.health.relative_band must be nonnegative, got {config.health_relative_band}"
        )
    try:
        lookup_connector(config.connector)
    except ValueError as exc:
        problems.append(str(exc))
    try:
        lookup_dialect(config.dialect)
    except ValueError as exc:
        problems.append(str(exc))
    if not isinstance(config.advisory, bool):
        problems.append("controller.advisory must be a boolean")
    if not isinstance(config.min_prefill, int) or isinstance(config.min_prefill, bool):
        problems.append("controller.min_prefill must be an integer")
    elif config.min_prefill < 1:
        problems.append(f"controller.min_prefill must be at least 1, got {config.min_prefill}")
    if not isinstance(config.min_decode, int) or isinstance(config.min_decode, bool):
        problems.append("controller.min_decode must be an integer")
    elif config.min_decode < 1:
        problems.append(f"controller.min_decode must be at least 1, got {config.min_decode}")
    if (
        isinstance(config.min_prefill, int)
        and not isinstance(config.min_prefill, bool)
        and isinstance(config.min_decode, int)
        and not isinstance(config.min_decode, bool)
        and config.min_prefill >= 1
        and config.min_decode >= 1
        and config.engines
        and config.min_prefill + config.min_decode > len(config.engines)
        and not (len(config.engines) == 1 and config.min_prefill == 1 and config.min_decode == 1)
    ):
        if config.min_decode == 1:
            problems.append(
                f"controller.min_prefill {config.min_prefill} permits zero decode engines "
                f"in a fleet of {len(config.engines)}; at most {len(config.engines) - 1}"
            )
        else:
            problems.append(
                f"controller.min_prefill {config.min_prefill} plus controller.min_decode "
                f"{config.min_decode} "
                f"exceeds the fleet size {len(config.engines)}"
            )
    if config.engine_restart_policy not in ("individual", "whole_wave"):
        problems.append("recovery.engine_restart_policy must be 'individual' or 'whole_wave'")
    if config.engine_restart_policy == "whole_wave":
        if config.engine_contract is None or config.engine_contract.missing():
            problems.append(
                "recovery.engine_restart_policy whole_wave requires a complete engine_contract"
            )
        if config.liveness_every <= 0:
            problems.append(
                "recovery.engine_restart_policy whole_wave requires recovery.liveness_every > 0"
            )
    if config.engine_contract is not None:
        contract = config.engine_contract
        if not contract.vllm_version:
            problems.append("engine_contract.vllm_version is required")
        if not contract.connector:
            problems.append("engine_contract.connector is required")
        if not contract.enforce_handshake_compat:
            problems.append(
                "engine_contract.enforce_handshake_compat must stay true; "
                "disabling vLLM's NIXL compatibility hash permits silent corruption"
            )
        if contract.image_digest and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", contract.image_digest
        ):
            problems.append(
                "engine_contract.image_digest must be an immutable sha256:<64 hex> digest"
            )
        problems.extend(
            f"engine_contract.{name} must be nonnegative"
            for name in (
                "nixl_connector_version",
                "kv_heads",
                "head_size",
                "hidden_layers",
            )
            if getattr(contract, name) < 0
        )
    if config.hardware is not None:
        if not config.hardware.accelerator:
            problems.append("hardware.accelerator is required")
        if config.hardware.accelerators_per_engine < 1:
            problems.append("hardware.accelerators_per_engine must be at least 1")
        if config.hardware.tensor_parallel < 1:
            problems.append("hardware.tensor_parallel must be at least 1")
        elif config.hardware.tensor_parallel > config.hardware.accelerators_per_engine:
            problems.append(
                "hardware.tensor_parallel cannot exceed hardware.accelerators_per_engine"
            )
    for name, value in (
        (
            "profiles.max_decode_fit_mape",
            config.profile_validation.max_decode_fit_mape,
        ),
        (
            "profiles.max_decode_cv_mape",
            config.profile_validation.max_decode_cv_mape,
        ),
    ):
        if value <= 0:
            problems.append(f"{name} must be positive, got {value}")
    # Accepted fit error beyond the movement margin lets profile noise
    # masquerade as a projected improvement.
    if config.profile_validation.max_decode_fit_mape > config.reactive_movement_margin:
        problems.append(
            "profiles.max_decode_fit_mape "
            f"{config.profile_validation.max_decode_fit_mape} "
            "exceeds controller.reactive.movement_margin "
            f"{config.reactive_movement_margin}"
        )
    if problems:
        raise ValueError(f"{source}: " + "; ".join(problems))
