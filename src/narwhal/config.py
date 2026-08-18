"""The fleet the scheduler is pointed at, and the targets it schedules against.

One JSON file, nothing discovered at runtime, so a run is replayable. Roles in
the file are starting labels; Algorithms 2 and 3 move them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .connector import lookup as lookup_connector
from .dialect import lookup as lookup_dialect
from .scheduler import SLO, Thresholds
from .types import Role


@dataclass
class EngineSpec:
    """One engine: id, address, and the role the run opens it with."""

    iid: str
    url: str
    role: Role = Role.DECODE
    # A pinned engine keeps its configured role for the life of the run: no
    # flip path (Algorithm 1 inline, Algorithm 2, the planner, resume) may
    # move it. Off everywhere by default; an unpinned fleet leaves every
    # role to those paths.
    pin: bool = False


@dataclass
class TenantSpec:
    """One named tenant: identity by env-named key, priority by weight,
    seats by share of the pool (and an optional hard cap of its own)."""

    name: str
    api_key_env: str
    weight: float = 1.0
    # 0 = share alone.
    max_concurrent: int = 0


@dataclass
class FleetConfig:
    """Everything the server needs before it accepts a request."""

    model: str
    engines: list[EngineSpec]
    slo: SLO
    thresholds: Thresholds = field(default_factory=Thresholds)
    # Arrow §5.5's update interval.
    monitor_interval_s: float = 1.0
    # Which control loop moves roles: "reactive" is Algorithm 2 as shipped;
    # "planner" computes a destination split per plan window and moves all
    # needed instances at once.
    controller: str = "planner"
    plan_interval_s: float = 60.0
    # A plan window whose observed attainment sits under this floor
    # forces one escalation step toward the missing phase, outcomes
    # trumping the demand model. 0 disables the loop.
    plan_attainment_floor: float = 0.9
    plan_window_s: float = 120.0
    plan_confirmations: int = 2
    plan_utilization: float = 0.8
    plan_min_arrivals: int = 10
    plan_demand_floor: float = 0.5
    # A pool is starving (moves now, no confirmation) only when demand
    # exceeds its capacity by this many engines; smaller disagreements are
    # rebalances and wait for plan_confirmations. 0 disables the deadband:
    # every ceil crossing moves.
    plan_deadband: float = 0.5
    plan_fast_step_s: float = 5.0
    # Telemetry is configuration, not environment: a run's config file is its
    # record, so the W&B destination belongs in it. Empty project means no
    # exporter.
    wandb_project: str = ""
    wandb_run: str = ""
    # Availability knobs, defaults in code, values in the file: consecutive
    # failed legs before ejection, monitor intervals between readmission
    # probes, and the tokenize probe's budget.
    eject_after: int = 3
    readmit_every: int = 10
    # The breaker learns from served traffic, so an idle fleet never finds out
    # an engine died: it keeps its role and prices into every placement until
    # the first request lands on it. The liveness sweep is the traffic-free
    # path to the same verdict. Monitor intervals between sweeps of the live
    # instances, and consecutive misses before one is ejected. Set
    # `liveness_every` to 0 to sweep nothing and rely on traffic alone.
    liveness_every: int = 10
    liveness_misses: int = 2
    tokenize_timeout_s: float = 2.0
    # One admitted request holds one engine connection at a time, so the pool
    # size is also the router's admission limit; past it clients get 429. The
    # pool wait is bounded separately so exhaustion is a failed leg on the
    # journal row, not a silent stall.
    max_connections: int = 512
    pool_timeout_s: float = 5.0
    connect_timeout_s: float = 10.0
    # /health probe budget, used by readmission and the check gates.
    health_timeout_s: float = 5.0
    # Flips and refusals kept for /arrow/state; telemetry, not scheduler state.
    flip_history: int = 1000
    # Seconds uvicorn allows in-flight requests after SIGTERM. Kubernetes
    # defaults to the same 30, which covers a ~1,500-token stream's drain.
    # Zero drops every in-flight stream at the signal.
    graceful_timeout_s: float = 30.0
    profiles_path: Path = Path("runs/profiles.json")
    request_timeout_s: float = 600.0
    # A single forward pass, so it must not inherit the decode leg's budget.
    prefill_timeout_s: float = 120.0
    # Opt-in payload sidecar: a path captures every request's prompt and
    # output text (truncated per field, hard-capped in total) joined to the
    # journal by rid; empty keeps the journal's lengths-and-timings-only
    # contract. CLI flags override these.
    journal_payloads: str = ""
    journal_payloads_max_chars: int = 2048
    journal_payloads_max_mb: int = 256
    # Fallback when an engine has no /tokenize endpoint. MEASURED:
    # the reference tokenizer's ratio on English chat text; re-derive per
    # tokenizer (one /tokenize call on a representative prompt).
    chars_per_token: float = 3.8
    tokenize: bool = True
    # Decode attempts per request. A retry only happens when nothing has
    # been streamed yet, so it is transparent to the client.
    decode_attempts: int = 2
    # Longest gap between received chunks before a stream counts as stalled.
    # Set it against the TPOT target, not the healthy inter-token gap: Arrow §4.3
    # makes q2 and q3 "highly unpredictable when the decode instance is under
    # high load with limited available memory", so a legitimate gap can be long.
    # A gap this far past the target has already missed TPOT by any output
    # length, which is what makes failing it over free. CONTRACT, sized by the
    # SLO argument above; a 1M-context fleet sizes it in the hundreds.
    decode_read_timeout_s: float = 60.0
    # Deadline for the first decode token, which is Arrow §4.3's `t2`. Bounded from
    # below by the fleet's measured healthy t2, and from above by the TPOT
    # budget of the shortest output the workload contains: a retried request
    # spends this whole budget inside TPOT, so 30 tokens at a 125 ms target
    # with a 26 ms cadence allows (0.125 - 0.026) * 29 = 2.87 s.
    # MEASURED: re-derive both bounds from your own healthy t2 and your
    # workload's shortest output. Too low destroys savable work under load,
    # and long-context fleets run it an order of magnitude higher.
    first_token_timeout_s: float = 2.5
    # The affinity-ablation switch: prefill legs sharing a prompt head return
    # to the engine that last served it, unconditionally. Off is the
    # architecture's position; on reintroduces the selfish caching game
    # for measurement.
    prefill_affinity: bool = False
    # The cooperative-reuse term: a shared prefix priced inside Algorithm 1's
    # cost as a discount on the warm engine - reuse wins ties and loses any
    # conflict the resident work prices larger than the saving, so the term
    # never overrides the fleet the way the ablation does. The two games are
    # measured apart; enabling both is a config error.
    prefix_coop: bool = False
    # Half-life of the warmth the router credits an engine's prefix cache.
    # Engines evict silently, so certainty about a warm record fades on this
    # clock rather than lying forever.
    prefix_halflife_s: float = 60.0
    # Actuation-cost ablation: seconds a just-flipped instance stays out of
    # service, emulating fleets whose role change drains and relaunches the
    # worker. 0 is Narwhal's hot swap. The instance still counts toward its
    # new pool's size (it is allocated, not serving), finishes resident
    # work, and takes no new placements until the window passes.
    flip_offline_s: float = 0.0
    # The batched-placement mechanism: hold prefill placements for a short window and
    # assign the gathered batch jointly (exact min-cost matching over the
    # same per-engine cost pairs Algorithm 1 prices). "greedy" is the
    # paper's sequential behaviour and the default.
    placement: str = "greedy"
    batch_window_ms: float = 20.0
    batch_max: int = 6
    # "predictive" prices the placement the request is about to get and
    # refuses with 429 + Retry-After what the fleet already knows it cannot
    # serve inside the TTFT budget, instead of queueing that request into a
    # slow death. "open" always admits, for paired measurement. The margin
    # widens the budget by that fraction so placement noise right at the
    # boundary does not churn refusals; the deadline is the budget, the
    # margin is hysteresis.
    admission: str = "predictive"
    admission_margin: float = 0.0
    # Re-place a queued prefill leg whose staying price has missed the TTFT
    # budget onto an engine that meets it with replace_slack_s to spare.
    # A queued leg has no migrated state, so the move is a re-dispatch, not
    # a migration. Off keeps placement a life sentence, for the paired arm.
    queue_rebalance: bool = True
    replace_slack_s: float = 0.5
    # Deep queues nominate, this many live legs actually move per monitoring
    # pass. Pass prices snapshot the moment, so an unbounded pass would
    # invert the skew instead of draining it.
    replace_per_pass: int = 2
    # Predictive health: per-engine drift residuals against its own
    # trailing healthy reading. A window shorter than min_samples samples is
    # not scored; probation after probation_windows consecutive over-band
    # windows, eviction asked for after evict_windows more, probation cleared
    # by recovery_windows consecutive under-band windows. drift_band is the
    # multiplier over the engine's own baseline (longitudinal, floored at
    # 1.0 - see health.DriftTracker); relative_band's quorum vetoes verdicts
    # while a majority of scored peers are also over band; min_ttft_s floors
    # the informational prefill channel against monitor-interval voxel noise.
    # probation_penalty_s is the additive cost an engine on probation pays on
    # any new placement.
    health_window_s: float = 30.0
    health_drift_band: float = 2.0
    health_min_samples: int = 3
    health_probation_windows: int = 3
    health_evict_windows: int = 5
    health_recovery_windows: int = 3
    health_probation_penalty_s: float = 1.5
    # A window also reads its fleet: an engine only speaks when it rises this
    # far above the window's median score, so a saturated fleet - everyone
    # slow together - is a capacity story, not an engine story. 0 disables
    # the quorum, one scored engine has none by construction.
    health_relative_band: float = 1.5
    # Ratios under this floor of observed TTFT never count: a prefill that
    # costs tens of milliseconds against a coarser monitor interval is voxel
    # noise, and the deep-queue failures this instrument watches for live
    # well above it.
    health_min_ttft_s: float = 0.25
    # The control-plane handoff. A snapshot of the actuated picture
    # (roles, the breaker's holds, the counters) is rewritten atomically
    # every monitoring pass here; `resume` has a replacement router take
    # it on instead of the fleet file's opening split. Everything else
    # the router knows rebuilds from probes and traffic.
    state_path: Path = Path("runs/state.json")
    resume: bool = False
    # The tenant layer. Names, weights and env-var names only - a key
    # value never touches the file the run keeps. Empty means one implicit
    # anonymous tenant over the whole pool.
    tenants: list[TenantSpec] = field(default_factory=list)
    tenant_auth_required: bool = False
    tenant_anonymous_weight: float = 1.0
    # The prefill pool never shrinks below this. 1 is the Arrow paper's
    # behaviour (a pool is never emptied) and the default. Only the
    # controllers honor the floor; ejection is a health event and ignores it.
    min_prefill: int = 1
    # Which KV transport connects disaggregation. The expected one is a
    # name in the connector registry; the check gates are its acceptance.
    connector: str = "nixl"
    # Which engine build answers the HTTP routes - a name in the
    # dialect registry. Where the routes live and what the probe bodies carry
    # is the dialect's business; the router's protocol is unchanged.
    dialect: str = "vllm"

    @staticmethod
    def load(path: str | Path) -> FleetConfig:
        """Read and validate a fleet config, every problem named at once."""
        raw = json.loads(Path(path).read_text())
        problems = _unknown_keys(raw)
        for key in ("model", "engines", "slo"):
            if key not in raw:
                problems.append(f"missing required key {key!r}")
        if problems:
            raise ValueError(f"{path}: " + "; ".join(problems))
        engines = []
        for k, e in enumerate(raw["engines"]):
            for key in ("iid", "url"):
                if key not in e:
                    problems.append(f"engines[{k}] is missing {key!r}")
            if problems:
                continue
            engines.append(
                EngineSpec(
                    iid=e["iid"],
                    url=e["url"].rstrip("/"),
                    role=Role(e.get("role", "decode")),
                    pin=bool(e.get("pin", False)),
                )
            )
        if problems:
            raise ValueError(f"{path}: " + "; ".join(problems))
        if not engines:
            raise ValueError(f"{path} declares no engines")
        seen = {e.iid for e in engines}
        if len(seen) != len(engines):
            raise ValueError(f"{path} repeats an instance id")

        slo_raw = raw["slo"]
        thr_raw = raw.get("thresholds", {})
        cfg = FleetConfig(
            model=raw["model"],
            engines=engines,
            slo=SLO(ttft_s=float(slo_raw["ttft_s"]), tpot_s=float(slo_raw["tpot_s"])),
            thresholds=Thresholds(
                expand=float(thr_raw.get("expand", 1.0)),
                shrink=float(thr_raw.get("shrink", 0.5)),
                cooldown_s=float(thr_raw.get("cooldown_s", 10.0)),
                sustained_intervals=int(thr_raw.get("sustained_intervals", 3)),
                dwell_s=float(thr_raw.get("dwell_s", 0.0)),
                panic_ratio=float(thr_raw.get("panic_ratio", 0.0)),
            ),
            monitor_interval_s=float(raw.get("monitor_interval_s", 1.0)),
            controller=str(raw.get("controller", "planner")),
            prefill_affinity=bool(raw.get("prefill_affinity", False)),
            prefix_coop=bool(raw.get("prefix_coop", False)),
            prefix_halflife_s=float(raw.get("prefix_halflife_s", 60.0)),
            flip_offline_s=float(raw.get("flip_offline_s", 0.0)),
            placement=str(raw.get("placement", "greedy")),
            batch_window_ms=float(raw.get("batch_window_ms", 20.0)),
            batch_max=int(raw.get("batch_max", 6)),
            admission=str(raw.get("admission", "predictive")),
            admission_margin=float(raw.get("admission_margin", 0.0)),
            queue_rebalance=bool(raw.get("queue_rebalance", True)),
            replace_slack_s=float(raw.get("replace_slack_s", 0.5)),
            replace_per_pass=int(raw.get("replace_per_pass", 2)),
            plan_interval_s=float((raw.get("planner") or {}).get("interval_s", 60.0)),
            plan_attainment_floor=float((raw.get("planner") or {}).get("attainment_floor", 0.9)),
            plan_window_s=float((raw.get("planner") or {}).get("window_s", 120.0)),
            plan_confirmations=int((raw.get("planner") or {}).get("confirmations", 2)),
            plan_utilization=float((raw.get("planner") or {}).get("utilization", 0.8)),
            plan_min_arrivals=int((raw.get("planner") or {}).get("min_arrivals", 10)),
            plan_demand_floor=float((raw.get("planner") or {}).get("demand_floor", 0.5)),
            plan_deadband=float((raw.get("planner") or {}).get("deadband", 0.5)),
            plan_fast_step_s=float((raw.get("planner") or {}).get("fast_step_s", 5.0)),
            wandb_project=str((raw.get("wandb") or {}).get("project", "")),
            wandb_run=str((raw.get("wandb") or {}).get("run", "")),
            health_window_s=float((raw.get("health") or {}).get("window_s", 30.0)),
            health_drift_band=float((raw.get("health") or {}).get("drift_band", 2.0)),
            health_min_samples=int((raw.get("health") or {}).get("min_samples", 3)),
            health_probation_windows=int((raw.get("health") or {}).get("probation_windows", 3)),
            health_evict_windows=int((raw.get("health") or {}).get("evict_windows", 5)),
            health_recovery_windows=int((raw.get("health") or {}).get("recovery_windows", 3)),
            health_probation_penalty_s=float(
                (raw.get("health") or {}).get("probation_penalty_s", 1.5)
            ),
            health_relative_band=float((raw.get("health") or {}).get("relative_band", 1.5)),
            health_min_ttft_s=float((raw.get("health") or {}).get("min_ttft_s", 0.25)),
            eject_after=int(raw.get("eject_after", 3)),
            readmit_every=int(raw.get("readmit_every", 10)),
            liveness_every=int(raw.get("liveness_every", 10)),
            liveness_misses=int(raw.get("liveness_misses", 2)),
            tokenize_timeout_s=float(raw.get("tokenize_timeout_s", 2.0)),
            max_connections=int(raw.get("max_connections", 512)),
            pool_timeout_s=float(raw.get("pool_timeout_s", 5.0)),
            connect_timeout_s=float(raw.get("connect_timeout_s", 10.0)),
            health_timeout_s=float(raw.get("health_timeout_s", 5.0)),
            flip_history=int(raw.get("flip_history", 1000)),
            graceful_timeout_s=float(raw.get("graceful_timeout_s", 30.0)),
            profiles_path=Path(raw.get("profiles_path", "runs/profiles.json")),
            request_timeout_s=float(raw.get("request_timeout_s", 600.0)),
            prefill_timeout_s=float(raw.get("prefill_timeout_s", 120.0)),
            chars_per_token=float(raw.get("chars_per_token", 3.8)),
            tokenize=bool(raw.get("tokenize", True)),
            decode_attempts=int(raw.get("decode_attempts", 2)),
            decode_read_timeout_s=float(raw.get("decode_read_timeout_s", 60.0)),
            first_token_timeout_s=float(raw.get("first_token_timeout_s", 2.5)),
            state_path=Path(raw.get("state_path", "runs/state.json")),
            resume=bool(raw.get("resume", False)),
            tenants=[
                TenantSpec(
                    name=str(t["name"]),
                    api_key_env=str(t["api_key_env"]),
                    weight=float(t.get("weight", 1.0)),
                    max_concurrent=int(t.get("max_concurrent", 0)),
                )
                for t in (raw.get("tenants") or {}).get("names", [])
            ],
            tenant_auth_required=bool((raw.get("tenants") or {}).get("auth_required", False)),
            tenant_anonymous_weight=float((raw.get("tenants") or {}).get("anonymous_weight", 1.0)),
            min_prefill=int(raw.get("min_prefill", 1)),
            journal_payloads=str(raw.get("journal_payloads", "")),
            journal_payloads_max_chars=int(raw.get("journal_payloads_max_chars", 2048)),
            journal_payloads_max_mb=int(raw.get("journal_payloads_max_mb", 256)),
            connector=str(raw.get("connector", "nixl")),
            dialect=str(raw.get("dialect", "vllm")),
        )
        cfg.validate(str(path))
        return cfg

    def validate(self, source: str = "config") -> None:
        """Refuse now, with every problem named, instead of dividing later.

        `tpot_s: 0.0` is a ZeroDivisionError on the first request; a zero
        interval is a monitoring loop that never sleeps. Everything an
        operator can mistype is checked here, and all at once, because fixing
        one field per restart against a fleet is a slow afternoon.
        """
        problems = []
        positive = [
            ("slo.ttft_s", self.slo.ttft_s),
            ("slo.tpot_s", self.slo.tpot_s),
            ("thresholds.expand", self.thresholds.expand),
            ("monitor_interval_s", self.monitor_interval_s),
            ("tokenize_timeout_s", self.tokenize_timeout_s),
            ("pool_timeout_s", self.pool_timeout_s),
            ("connect_timeout_s", self.connect_timeout_s),
            ("health_timeout_s", self.health_timeout_s),
            ("request_timeout_s", self.request_timeout_s),
            ("prefill_timeout_s", self.prefill_timeout_s),
            ("chars_per_token", self.chars_per_token),
            ("decode_read_timeout_s", self.decode_read_timeout_s),
            ("first_token_timeout_s", self.first_token_timeout_s),
        ]
        for name, value in positive:
            if value <= 0:
                problems.append(f"{name} must be positive, got {value}")
        not_negative = [
            ("thresholds.shrink", self.thresholds.shrink),
            ("thresholds.cooldown_s", self.thresholds.cooldown_s),
            ("thresholds.dwell_s", self.thresholds.dwell_s),
            ("graceful_timeout_s", self.graceful_timeout_s),
        ]
        for name, value in not_negative:
            if value < 0:
                problems.append(f"{name} cannot be negative, got {value}")
        if not 0.0 <= self.plan_attainment_floor < 1.0:
            problems.append(
                f"planner.attainment_floor must be in [0, 1), got {self.plan_attainment_floor}"
            )
        if self.flip_offline_s < 0:
            problems.append(f"flip_offline_s cannot be negative, got {self.flip_offline_s}")
        if self.thresholds.panic_ratio != 0.0 and self.thresholds.panic_ratio < 1.0:
            problems.append(
                "thresholds.panic_ratio must be 0 (off) or at least 1, "
                f"got {self.thresholds.panic_ratio}"
            )
        at_least_one = [
            ("thresholds.sustained_intervals", self.thresholds.sustained_intervals),
            ("eject_after", self.eject_after),
            ("readmit_every", self.readmit_every),
            ("liveness_misses", self.liveness_misses),
            ("decode_attempts", self.decode_attempts),
            ("max_connections", self.max_connections),
            ("flip_history", self.flip_history),
        ]
        for name, value in at_least_one:
            if value < 1:
                problems.append(f"{name} must be at least 1, got {value}")
        if self.controller not in ("reactive", "planner"):
            problems.append(f"controller must be 'reactive' or 'planner', got {self.controller!r}")
        if self.placement not in ("greedy", "batched"):
            problems.append(f"placement must be 'greedy' or 'batched', got {self.placement!r}")
        if self.admission not in ("predictive", "open"):
            problems.append(f"admission must be 'predictive' or 'open', got {self.admission!r}")
        if self.admission_margin < 0:
            problems.append(f"admission_margin cannot be negative, got {self.admission_margin}")
        if self.replace_slack_s < 0:
            problems.append(f"replace_slack_s cannot be negative, got {self.replace_slack_s}")
        if self.replace_per_pass < 1:
            problems.append(f"replace_per_pass must be at least 1, got {self.replace_per_pass}")
        if self.batch_window_ms < 0:
            problems.append(f"batch_window_ms cannot be negative, got {self.batch_window_ms}")
        if self.batch_max < 1:
            problems.append(f"batch_max must be at least 1, got {self.batch_max}")
        for name, value in (
            ("planner.interval_s", self.plan_interval_s),
            ("planner.window_s", self.plan_window_s),
            ("planner.utilization", self.plan_utilization),
            ("planner.demand_floor", self.plan_demand_floor),
            ("planner.fast_step_s", self.plan_fast_step_s),
        ):
            if value <= 0:
                problems.append(f"{name} must be positive, got {value}")
        for name, value in (
            ("planner.confirmations", self.plan_confirmations),
            ("planner.min_arrivals", self.plan_min_arrivals),
        ):
            if value < 1:
                problems.append(f"{name} must be at least 1, got {value}")
        if self.plan_utilization > 1.0:
            problems.append(
                f"planner.utilization is a fraction of an instance, got {self.plan_utilization}"
            )
        if self.thresholds.shrink >= self.thresholds.expand:
            problems.append(
                f"thresholds.shrink {self.thresholds.shrink} must be below expand "
                f"{self.thresholds.expand}: the band between them is where the "
                f"pool holds still"
            )
        if self.prefix_coop and self.prefill_affinity:
            problems.append(
                "prefix_coop and prefill_affinity are different answers to the same "
                "question (the cooperative term against the affinity override); "
                "the ablation must run alone"
            )
        if self.prefix_halflife_s <= 0:
            problems.append(f"prefix_halflife_s must be positive, got {self.prefix_halflife_s}")
        # The drift instrument is a ratio against a profile, so 1.0 means
        # "as profiled": a band at or below it flags the healthy, and none of
        # its counters can move at zero.
        if self.health_drift_band <= 1.0:
            problems.append(f"health.drift_band must exceed 1.0, got {self.health_drift_band}")
        for name, value in (
            ("health.window_s", self.health_window_s),
            ("health.min_samples", self.health_min_samples),
            ("health.probation_windows", self.health_probation_windows),
            ("health.evict_windows", self.health_evict_windows),
            ("health.recovery_windows", self.health_recovery_windows),
        ):
            if value < 1:
                problems.append(f"{name} must be at least 1, got {value}")
        if self.health_probation_penalty_s < 0:
            problems.append(
                f"health.probation_penalty_s cannot be negative, got "
                f"{self.health_probation_penalty_s}"
            )
        if self.health_relative_band < 0:
            problems.append(
                f"health.relative_band cannot be negative, got {self.health_relative_band}"
            )
        if self.health_min_ttft_s <= 0:
            problems.append(f"health.min_ttft_s must be positive, got {self.health_min_ttft_s}")
        # A tenant the door cannot identify is a promise it cannot keep.
        names = [t.name for t in self.tenants]
        if len(set(names)) != len(names):
            problems.append("tenants.names repeats a tenant name")
        if "anonymous" in names:
            problems.append("'anonymous' is reserved for the tenantless door")
        for t in self.tenants:
            if t.weight <= 0:
                problems.append(f"tenants[{t.name}].weight must be positive, got {t.weight}")
            if not t.api_key_env:
                problems.append(f"tenants[{t.name}] needs api_key_env - a key's name, never a key")
            # Whether the variable is *set* is the serving door's business
            # (TenantLedger refuses to build without it): a recorded config
            # must load for checks, reports and archives on machines that
            # never hold the production keys.
            if t.max_concurrent < 0:
                problems.append(
                    f"tenants[{t.name}].max_concurrent cannot be negative, got {t.max_concurrent}"
                )
        if self.tenants and self.tenant_anonymous_weight <= 0:
            problems.append(
                f"tenants.anonymous_weight must be positive, got {self.tenant_anonymous_weight}"
            )
        try:
            lookup_connector(self.connector)
        except ValueError as exc:
            problems.append(str(exc))
        try:
            lookup_dialect(self.dialect)
        except ValueError as exc:
            problems.append(str(exc))
        if self.journal_payloads and (
            self.journal_payloads_max_chars < 1 or self.journal_payloads_max_mb < 1
        ):
            problems.append("journal_payloads caps must be positive")
        if self.min_prefill < 1:
            problems.append(f"min_prefill must be at least 1, got {self.min_prefill}")
        elif self.min_prefill > 1 and self.engines and self.min_prefill > len(self.engines) - 1:
            problems.append(
                f"min_prefill {self.min_prefill} leaves no decode engine "
                f"in a fleet of {len(self.engines)}; at most {len(self.engines) - 1}"
            )
        if problems:
            raise ValueError(f"{source}: " + "; ".join(problems))

    @staticmethod
    def from_fleet_json(path: str | Path, **overrides: object) -> FleetConfig:
        """Build a fleet config from a launcher `fleet.*.json`.

        Ports are per node, not fleet-wide. IPv6 addresses are bracketed here,
        since `http://fe80::1:8000` otherwise parses the last group as the port.
        """
        raw = json.loads(Path(path).read_text())
        nodes = raw["nodes"]
        engines = []
        for key in sorted(nodes, key=lambda k: int(k)):
            node = nodes[key]
            host = node["address"]
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            engines.append(EngineSpec(iid=f"n{key}", url=f"http://{host}:{node['port']}"))

        # narwhal's opening_split is two lists of node ids, `p` and `d`. It is
        # a starting label only: Algorithms 2 and 3 move them from there. An
        # absent split opens even.
        opening = raw.get("opening_split") or {}
        prefill_ids = {f"n{k}" for k in opening.get("p", [])}
        if prefill_ids:
            for spec in engines:
                spec.role = Role.PREFILL if spec.iid in prefill_ids else Role.DECODE
        else:
            for spec in engines[: len(engines) // 2]:
                spec.role = Role.PREFILL

        slo = raw.get("scenario", {}).get("slo", {})
        cfg = FleetConfig(
            model=raw["model"]["served_name"],
            engines=engines,
            slo=SLO(
                ttft_s=float(slo.get("ttft_ms", 10_000) / 1000.0),
                tpot_s=float(slo.get("tpot_ms", 125) / 1000.0),
            ),
        )
        for name, value in overrides.items():
            setattr(cfg, name, value)
        return cfg

    def save(self, path: str | Path) -> None:
        """Write the config as JSON: the run's replayable record."""
        out = {
            "model": self.model,
            "engines": [
                {"iid": e.iid, "url": e.url, "role": e.role.value}
                | ({"pin": True} if e.pin else {})
                for e in self.engines
            ],
            "slo": {"ttft_s": self.slo.ttft_s, "tpot_s": self.slo.tpot_s},
            "thresholds": {
                "expand": self.thresholds.expand,
                "shrink": self.thresholds.shrink,
                "cooldown_s": self.thresholds.cooldown_s,
                "dwell_s": self.thresholds.dwell_s,
                "sustained_intervals": self.thresholds.sustained_intervals,
                "panic_ratio": self.thresholds.panic_ratio,
            },
            "monitor_interval_s": self.monitor_interval_s,
            "controller": self.controller,
            "prefill_affinity": self.prefill_affinity,
            "prefix_coop": self.prefix_coop,
            "prefix_halflife_s": self.prefix_halflife_s,
            "flip_offline_s": self.flip_offline_s,
            "placement": self.placement,
            "batch_window_ms": self.batch_window_ms,
            "batch_max": self.batch_max,
            "admission": self.admission,
            "admission_margin": self.admission_margin,
            "queue_rebalance": self.queue_rebalance,
            "replace_slack_s": self.replace_slack_s,
            "replace_per_pass": self.replace_per_pass,
            "planner": {
                "interval_s": self.plan_interval_s,
                "window_s": self.plan_window_s,
                "confirmations": self.plan_confirmations,
                "utilization": self.plan_utilization,
                "min_arrivals": self.plan_min_arrivals,
                "demand_floor": self.plan_demand_floor,
                "deadband": self.plan_deadband,
                "fast_step_s": self.plan_fast_step_s,
                "attainment_floor": self.plan_attainment_floor,
            },
            "wandb": {"project": self.wandb_project, "run": self.wandb_run},
            "health": {
                "window_s": self.health_window_s,
                "drift_band": self.health_drift_band,
                "min_samples": self.health_min_samples,
                "probation_windows": self.health_probation_windows,
                "evict_windows": self.health_evict_windows,
                "recovery_windows": self.health_recovery_windows,
                "probation_penalty_s": self.health_probation_penalty_s,
                "relative_band": self.health_relative_band,
                "min_ttft_s": self.health_min_ttft_s,
            },
            "eject_after": self.eject_after,
            "readmit_every": self.readmit_every,
            "liveness_every": self.liveness_every,
            "liveness_misses": self.liveness_misses,
            "tokenize_timeout_s": self.tokenize_timeout_s,
            "max_connections": self.max_connections,
            "pool_timeout_s": self.pool_timeout_s,
            "connect_timeout_s": self.connect_timeout_s,
            "health_timeout_s": self.health_timeout_s,
            "flip_history": self.flip_history,
            "graceful_timeout_s": self.graceful_timeout_s,
            "profiles_path": str(self.profiles_path),
            "request_timeout_s": self.request_timeout_s,
            "prefill_timeout_s": self.prefill_timeout_s,
            "tokenize": self.tokenize,
            "chars_per_token": self.chars_per_token,
            "decode_attempts": self.decode_attempts,
            "decode_read_timeout_s": self.decode_read_timeout_s,
            "first_token_timeout_s": self.first_token_timeout_s,
            "state_path": str(self.state_path),
            "resume": self.resume,
            # Names, weights and env-var names are the record; never a key.
            "tenants": {
                "auth_required": self.tenant_auth_required,
                "anonymous_weight": self.tenant_anonymous_weight,
                "names": [
                    {
                        "name": t.name,
                        "api_key_env": t.api_key_env,
                        "weight": t.weight,
                        "max_concurrent": t.max_concurrent,
                    }
                    for t in self.tenants
                ],
            },
            "connector": self.connector,
            "dialect": self.dialect,
            "min_prefill": self.min_prefill,
            "journal_payloads": self.journal_payloads,
            "journal_payloads_max_chars": self.journal_payloads_max_chars,
            "journal_payloads_max_mb": self.journal_payloads_max_mb,
        }
        Path(path).write_text(json.dumps(out, indent=2) + "\n")

    @staticmethod
    def from_env() -> FleetConfig:
        """The NARWHAL_FLEET fallback for commands given no --fleet."""
        path = os.environ.get("NARWHAL_FLEET")
        if not path:
            raise RuntimeError("set NARWHAL_FLEET to a fleet config file")
        return FleetConfig.load(path)


_KNOWN_KEYS = {
    "model",
    "engines",
    "slo",
    "thresholds",
    "monitor_interval_s",
    "controller",
    "planner",
    "wandb",
    "eject_after",
    "readmit_every",
    "liveness_every",
    "liveness_misses",
    "tokenize_timeout_s",
    "max_connections",
    "pool_timeout_s",
    "connect_timeout_s",
    "health_timeout_s",
    "flip_history",
    "graceful_timeout_s",
    "profiles_path",
    "request_timeout_s",
    "prefill_timeout_s",
    "chars_per_token",
    "tokenize",
    "decode_attempts",
    "decode_read_timeout_s",
    "first_token_timeout_s",
    "prefill_affinity",
    "prefix_coop",
    "prefix_halflife_s",
    "flip_offline_s",
    "placement",
    "batch_window_ms",
    "batch_max",
    "admission",
    "admission_margin",
    "queue_rebalance",
    "replace_slack_s",
    "replace_per_pass",
    "health",
    "state_path",
    "resume",
    "tenants",
    "connector",
    "dialect",
    "min_prefill",
    "journal_payloads",
    "journal_payloads_max_chars",
    "journal_payloads_max_mb",
}


def _unknown_keys(raw: dict) -> list[str]:
    """A typo would otherwise fall back to the default without a word.

    Keys starting with `_` are the example's prose convention and pass.
    """
    unknown = sorted(k for k in raw if k not in _KNOWN_KEYS and not k.startswith("_"))
    return [f"unknown key {k!r} (a typo falls back to the default silently)" for k in unknown]
