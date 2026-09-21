"""Fleet configuration values, defaults, and public construction API."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from ..scheduling.control import SLO, Thresholds
from ..serving.policy import ServingPolicy
from ..types import Role


@dataclass
class EngineSpec:
    """Engine address and opening scheduler role."""

    iid: str
    url: str
    role: Role = Role.DECODE
    # Pinning excludes the engine from every role-change path, including resume.
    pin: bool = False
    # Full URL served by the engine-side attestation process.
    attestation_url: str = ""


@dataclass(frozen=True)
class EngineContract:
    """Declared engine fields that must agree across a NIXL fleet.

    Attestation binds runtime fields to the engine's current process start.
    """

    vllm_version: str = ""
    image_digest: str = ""
    nixl_version: str = ""
    nixl_connector_version: int = 0
    model_architecture: str = ""
    model_dtype: str = ""
    kv_heads: int = 0
    head_size: int = 0
    hidden_layers: int = 0
    attention_backend: str = ""
    kv_cache_dtype: str = ""
    cross_layers_blocks: bool | None = None
    hybrid_kv_cache_manager: bool | None = None
    connector: str = "NixlConnector"
    kv_role: str = ""
    transfer_mode: str = ""
    speculative_config: str = ""
    enforce_handshake_compat: bool = True

    def fields(self) -> dict[str, str | int | bool | None]:
        """Return the stable representation hashed by preflight."""
        return {
            "vllm_version": self.vllm_version,
            "image_digest": self.image_digest,
            "nixl_version": self.nixl_version,
            "nixl_connector_version": self.nixl_connector_version,
            "model_architecture": self.model_architecture,
            "model_dtype": self.model_dtype,
            "kv_heads": self.kv_heads,
            "head_size": self.head_size,
            "hidden_layers": self.hidden_layers,
            "attention_backend": self.attention_backend,
            "kv_cache_dtype": self.kv_cache_dtype,
            "cross_layers_blocks": self.cross_layers_blocks,
            "hybrid_kv_cache_manager": self.hybrid_kv_cache_manager,
            "connector": self.connector,
            "kv_role": self.kv_role,
            "transfer_mode": self.transfer_mode,
            "speculative_config": self.speculative_config,
            "enforce_handshake_compat": self.enforce_handshake_compat,
        }

    def fingerprint(self) -> str:
        """Return a short digest of the complete declared representation."""
        raw = json.dumps(self.fields(), sort_keys=True, separators=(",", ":")).encode()
        return sha256(raw).hexdigest()[:16]

    def missing(self) -> list[str]:
        """List undeclared compatibility fields in the engine contract."""
        optional = {
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
            "kv_role",
            "transfer_mode",
            "speculative_config",
        }
        fields = self.fields()
        return sorted(
            key
            for key in optional
            if fields[key] is None
            or fields[key] == ""
            or (not isinstance(fields[key], bool) and fields[key] == 0)
        )


@dataclass(frozen=True)
class HardwareSpec:
    """Site-neutral hardware identity for one fleet shape."""

    accelerator: str
    accelerators_per_engine: int
    tensor_parallel: int

    def fields(self) -> dict[str, str | int]:
        """Return the stable public representation."""
        return {
            "accelerator": self.accelerator,
            "accelerators_per_engine": self.accelerators_per_engine,
            "tensor_parallel": self.tensor_parallel,
        }


@dataclass(frozen=True)
class ProfileValidationPolicy:
    """Decode-profile error limits enforced by fleet preflight.

    `max_decode_fit_mape` caps in-sample error; `max_decode_cv_mape` caps
    leave-one-cell-out cross-validation error. The fit limit must be at or
    below the controller's movement margin.
    """

    max_decode_fit_mape: float = 0.05
    max_decode_cv_mape: float = 0.13

    def __post_init__(self) -> None:
        for name in ("max_decode_fit_mape", "max_decode_cv_mape"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value}")


@dataclass
class FleetConfig:
    """Validated inputs for serving and controller construction."""

    model: str
    engines: list[EngineSpec]
    slo: SLO
    thresholds: Thresholds = field(default_factory=Thresholds)
    monitor_interval_s: float = 1.0
    # Consecutive failed monitoring passes before the router stops admitting
    # new requests. Any monitor stage failure counts, even if control succeeds.
    monitor_failure_limit: int = 5
    # Compute and expose controller decisions without changing engine roles.
    advisory: bool = False
    reactive_window_s: float = 120.0
    # Minimum elapsed arrival-evidence span before D-to-P consolidation. The
    # shipped default is half the shipped 120 s demand window.
    reactive_evidence_span_s: float = 60.0
    # Close the evidence window when arrivals or time since the last risk
    # event span this duration, allowing consolidation under sparse traffic.
    reactive_evidence_max_span_s: float = 120.0
    # Minimum arrival samples inside the evidence span.
    reactive_evidence_min_arrivals: int = 10
    # D-to-P consolidation is refused while the short-horizon decode estimate
    # exceeds the long-horizon estimate by more than this fraction.
    reactive_demand_rise_tolerance: float = 0.25
    reactive_confirmations: int = 2
    reactive_utilization: float = 0.8
    reactive_min_arrivals: int = 10
    reactive_demand_floor: float = 0.5
    # Minimum reduction in the worst projected SLO ratio before a role change.
    reactive_movement_margin: float = 0.05
    reactive_step_s: float = 5.0
    reactive_decode_correction_min: float = 0.5
    reactive_decode_correction_max: float = 2.0
    reactive_decode_correction_alpha: float = 0.2
    reactive_decode_correction_min_samples: int = 8
    # Reactive breaker threshold and readmission cadence.
    eject_after: int = 3
    readmit_every: int = 10
    # Liveness sweeps detect dead engines while traffic is idle. Zero disables sweeps.
    liveness_every: int = 10
    liveness_misses: int = 2
    # Whole-wave recovery protects engines that retain stale peer registrations.
    engine_restart_policy: str = "individual"
    tokenize_timeout_s: float = 2.0
    # Data-pool capacity also bounds admitted originals; phase waits hold no connection.
    max_connections: int = 512
    serving: ServingPolicy = field(default_factory=ServingPolicy)
    # Reserved connections for health and recovery probes. Validation resolves
    # zero to max(4, 2 per engine) and stores the result here.
    control_connections: int = 0
    pool_timeout_s: float = 5.0
    connect_timeout_s: float = 10.0
    # Shared by breaker readmission and preflight checks.
    health_timeout_s: float = 5.0
    # Retained telemetry rows exposed through `/narwhal/state`.
    flip_history: int = 1000
    # Uvicorn drain time after SIGTERM. Zero terminates active streams immediately.
    graceful_timeout_s: float = 30.0
    profiles_path: Path = Path("runs/profiles.json")
    request_timeout_s: float = 600.0
    # Prefill has a separate deadline because it is one forward pass.
    prefill_timeout_s: float = 120.0
    # Character-ratio fallback; calibrate against the served tokenizer.
    chars_per_token: float = 3.8
    tokenize: bool = True
    # Environment variable supplying the credential attached to each engine leg.
    engine_api_key_env: str = ""
    # Maximum gap between decode chunks. Size this from the TPOT failure
    # budget. 0 disables the bound, leaving only the request deadline.
    decode_read_timeout_s: float = 60.0
    # Set above the measured crossed-handoff p99 for the served context range.
    first_token_timeout_s: float = 2.5
    # Hold a failed engine out of placement while health checks catch up.
    failure_quarantine_s: float = 0.0
    # Predictive admission returns 429 when every placement exceeds the TTFT
    # budget. The margin supplies hysteresis at the boundary.
    admission: str = "predictive"
    admission_margin: float = 0.0
    # Drift windows compare each engine with its trailing healthy baseline.
    # Probation adds placement cost; sustained drift requests ejection.
    health_window_s: float = 30.0
    health_drift_band: float = 2.0
    health_min_samples: int = 3
    health_probation_windows: int = 3
    health_evict_windows: int = 5
    health_recovery_windows: int = 3
    health_probation_penalty_s: float = 1.5
    # Peer-relative outliers bypass the fleet-wide surge veto. Zero disables it.
    health_relative_band: float = 1.5
    # Atomic handoff for role, breaker, lifecycle, and counter state.
    state_path: Path = Path("runs/state.json")
    resume: bool = False
    # Role changes preserve this live-prefill floor. Breaker ejections may breach it.
    min_prefill: int = 1
    # Role changes preserve this live-decode floor. Breaker ejections may breach it.
    min_decode: int = 1
    connector: str = "nixl"
    dialect: str = "vllm"
    engine_contract: EngineContract | None = None
    hardware: HardwareSpec | None = None
    # Decode error limits applied by preflight.
    profile_validation: ProfileValidationPolicy = field(default_factory=ProfileValidationPolicy)

    def __post_init__(self) -> None:
        # Programmatic construction gets the same finiteness gate as the loader.
        problems = [
            f"{name} must be finite, got {getattr(self, name)}"
            for name in _FINITE_FLOAT_FIELDS
            if not math.isfinite(getattr(self, name))
        ]
        if problems:
            raise ValueError("; ".join(problems))

    @staticmethod
    def load(path: str | Path) -> FleetConfig:
        """Load a fleet config and report all detectable schema errors."""
        from .loading import load

        return load(path)

    def resolved_control_connections(self) -> int:
        """Control-pool budget: the explicit value, or the fleet-size derivation."""
        if self.control_connections > 0:
            return self.control_connections
        return max(4, 2 * len(self.engines))

    def engine_auth_mode(self) -> str:
        """Return the engine-authentication mode."""
        if self.engine_api_key_env:
            return "engine-credential"
        return "boundary"

    def resolve_engine_key(self) -> str | None:
        """Resolve the engine credential when constructing a network client."""
        if not self.engine_api_key_env:
            return None
        key = os.environ.get(self.engine_api_key_env)
        if not key:
            raise ValueError(
                f"engine_api_key_env {self.engine_api_key_env} is not set in the environment"
            )
        return key

    def engine_headers(self) -> dict[str, str]:
        """Build authentication headers for engine endpoints, excluding sidecars."""
        key = self.resolve_engine_key()
        return {"authorization": f"Bearer {key}"} if key is not None else {}

    def validate(self, source: str = "config") -> None:
        """Validate cross-field constraints and report all failures together."""
        from .validation import validate

        validate(self, source)

    def save(self, path: str | Path) -> None:
        """Write the replayable JSON configuration."""
        from .serialization import save

        save(self, path)

    @staticmethod
    def from_env() -> FleetConfig:
        """Load the config named by `NARWHAL_FLEET`."""
        path = os.environ.get("NARWHAL_FLEET")
        if not path:
            raise RuntimeError("set NARWHAL_FLEET to a fleet config file")
        return FleetConfig.load(path)


_FINITE_FLOAT_FIELDS = (
    "monitor_interval_s",
    "reactive_window_s",
    "reactive_evidence_span_s",
    "reactive_evidence_max_span_s",
    "reactive_demand_rise_tolerance",
    "reactive_utilization",
    "reactive_demand_floor",
    "reactive_movement_margin",
    "reactive_step_s",
    "reactive_decode_correction_min",
    "reactive_decode_correction_max",
    "reactive_decode_correction_alpha",
    "tokenize_timeout_s",
    "pool_timeout_s",
    "connect_timeout_s",
    "health_timeout_s",
    "graceful_timeout_s",
    "request_timeout_s",
    "prefill_timeout_s",
    "chars_per_token",
    "decode_read_timeout_s",
    "first_token_timeout_s",
    "failure_quarantine_s",
    "admission_margin",
    "health_window_s",
    "health_drift_band",
    "health_probation_penalty_s",
    "health_relative_band",
)
