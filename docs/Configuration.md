# Fleet configuration reference

One JSON file holds everything the router needs before it accepts a request.
`narwhal-check --print-example-config` prints an annotated example. Every value
is validated on load, and all problems are named at once. Keys starting with `_` are
prose and ignored. The commands require `--fleet`; `NARWHAL_FLEET` names the
file only for the ASGI-factory path, where `create_app()` is called with no
config. Beyond that variable, the router reads from the environment only the
tenant keys that `tenants.names` points at.

Sections first, then scalars. Defaults are the values the schema carries when
the file omits the key. *Contracts* are
protocol behaviour that holds on any fleet. *Measurements* are values a
fleet derives from its own profile, such as `chars_per_token`,
`first_token_timeout_s`, and the SLOs themselves. A new fleet re-derives
measurements instead of inheriting them, and the field comments in
`config.py` mark which is which. The router's Prometheus histogram buckets are derived from the
configured SLOs, so quantile resolution follows the targets automatically.

| Field | Default | What it sets |
| --- | --- | --- |
| `model` | required | Served model name; every engine must serve exactly this one, or KV routes between incompatible caches. |
| `engines` | required | One entry per vLLM instance: `iid`, `url`, and a starting `role` that Algorithms 2 and 3 relabel from there. An entry may add `"pin": true` to keep its configured role for the life of the run: no flip path (inline, monitoring, planner, or `--resume`) moves a pinned engine, and it never appears among flip candidates. One use case is a healthy but role-constrained node, such as one whose NIC prefills well but cannot carry decode-side KV egress at peak. Another is the failover anchor: the study's fleet pinned one prefill engine so a control-plane takeover always lands on a known prefill seat, because a pinned engine keeps its configured role while a resume reapplies every other role from the handoff. A config that pins nothing behaves exactly as before. |
| `journal_payloads` | `""` | Opt-in payload sidecar: a path captures each request's prompt and output text as JSONL, joined to the journal by `rid`. Empty (the default) keeps the journal's lengths-and-timings-only contract - content is never recorded unless asked for. `--journal-payloads` overrides. |
| `journal_payloads_max_chars` / `_max_mb` | `2048` / `256` | Each field truncates at `max_chars` (full lengths are already in the journal). Capture stops when the file reaches `max_mb`, with serving unaffected and one warning logged. |
| `min_prefill` | `1` | The prefill pool never shrinks below this many engines; counted over the whole live pool, so pinned and dwelling engines hold seats. `1` is the Arrow paper's never-empty guard and changes nothing. It must leave at least one decode engine (`min_prefill <= engines - 1`). A config with a pinned prefill engine must set it to at least `2`, so a role-constrained engine is never left carrying the fleet's entire KV egress alone; validation enforces this. |
| `slo.ttft_s` | required | Time-to-first-token target; every load and every flip is priced against it. |
| `slo.tpot_s` | required | Per-token target; must sit above the profiled floor or no fleet size can meet it. |
| `thresholds.expand` | `1.0` | Load ratio at which Algorithm 2 grows a pool; loads are SLO-relative, so 1.0 is exactly at target. |
| `thresholds.shrink` | `0.5` | Load ratio below which a pool gives an instance away; must stay below `expand`, the band between them is where the pool holds still. |
| `thresholds.cooldown_s` | `10.0` | Seconds between P→D flips; one-sided by design, D→P recovery is never throttled. Stationary work earns a long cooldown: a measured ablation recovered 23.9 points of attainment on a stationary trace at `180`, the value the study's reference fleet ran. A moving optimum pays for that damping in lag, about 140 s per transition under the reactive controller at that setting. |
| `thresholds.panic_ratio` | `0.0` | Regime-flip bypass of the cooldown: a P→D flip passes when decode load holds at this multiple of `expand` while prefill sits at or below `shrink`, sustained for `sustained_intervals` monitoring passes. Two-sided by measurement: a global spike raises both loads and must not fire it. `0` disables, and any other value must be at least `1`. Shipped off, and measured inert on the study's reference fleet: the ungated D→P recovery absorbed every flood constructed to trigger it. |
| `thresholds.sustained_intervals` | `3` | Consecutive over-threshold passes before Algorithm 2 fires, so a single spike cannot move an instance. |
| `thresholds.dwell_s` | `0.0` | Pins a just-flipped instance in both directions. Keep it 0 on workloads whose optimum moves. |
| `prefill_affinity` | `false` | Ablation switch: prefill legs sharing a prompt head return to the engine that last served it, unconditionally. Off is the architecture's position; on reintroduces the selfish caching game for measurement. Measured, the game loses: at 90-100% warm-engine pinning the affinity arm lost on completion at every tested rate and destroyed a quarter of the offered load that stateless placement completed. |
| `prefix_coop` | `false` | Cooperative prefix reuse: the shared head priced inside Algorithm 1's cost as a discount on the engine whose cache holds it, so reuse wins ties and loses conflicts with queued work. Engines evict silently, so the credit fades on `prefix_halflife_s`. Cannot compose with `prefill_affinity`; the two games are measured apart. |
| `prefix_halflife_s` | `60.0` | Half-life of the warmth credited to an engine's prefix cache; must be positive. |
| `flip_offline_s` | `0.0` | Actuation-cost ablation: seconds a just-flipped instance stays out of service, emulating a fleet whose role change requires a drain and relaunch. The instance still counts toward its new pool's size. It finishes resident work and takes no new placements until the window passes. `0` is Narwhal's hot swap. |
| `placement` | `"greedy"` | Prefill-leg placement: `greedy` places each request as it arrives (the Arrow paper's sequential behaviour); `batched` holds arrivals for `batch_window_ms` (or until `batch_max` gather) and assigns the window jointly by exact min-cost matching over the same cost pairs. |
| `batch_window_ms` | `20.0` | The batched mode's gathering window; a lone request in a quiet window pays it once. |
| `batch_max` | `6` | Window closes early at this many gathered placements. |
| `admission` | `"predictive"` | The door's policy. `predictive` prices the placement a request is about to get and answers 429 with a priced `Retry-After` when the fleet already knows it cannot serve inside the TTFT budget. `open` always admits, for paired measurement. |
| `admission_margin` | `0.0` | Widens the admission budget by this fraction, so placement noise right at the boundary does not churn refusals. The deadline is the budget, and the margin is hysteresis. |
| `queue_rebalance` | `true` | Re-places a queued prefill leg whose staying price has missed the TTFT budget onto an engine that meets it with `replace_slack_s` to spare. A queued leg has no migrated state, so the move is a plain re-dispatch. Off keeps a placement final, for the paired arm. |
| `replace_slack_s` | `0.5` | Seconds the destination must beat the TTFT budget by before a queued leg moves. |
| `replace_per_pass` | `2` | Legs moved per monitoring pass. Deep queues nominate, and this bounds what actually moves, because pass prices snapshot the moment and an unbounded pass would invert the skew instead of draining it. |
| `monitor_interval_s` | `1.0` | Arrow §5.5's update interval: one control pass, then the decode load window closes. |
| `controller` | `"planner"` | Which loop moves roles. `planner` (default) computes a destination split per window and moves all needed instances in one pass. `reactive` is Algorithm 2 as the Arrow paper specifies, the choice for paper-faithful behaviour. *The Price of Order* measures the two against each other. The planner prices demand against the fleet-mean profile, so mixed fleets are priced by what the fleet can do in aggregate. |
| `planner.interval_s` / `window_s` | `60` / `120` | Plan cadence and the demand window it reads. |
| `planner.confirmations` | `2` | Consecutive same-target plans before a pure rebalance moves; a starving pool moves immediately. |
| `planner.attainment_floor` | `0.9` | A plan window whose observed attainment sits under this floor forces one escalation step toward the missing phase, outcomes trumping the demand model. The loop exists because a demand model can be exact on average and still miss the tail (burst headroom is invisible to mean-capacity arithmetic); feeding verdicts back raised a measured prefill wall from 49.9% to 70.9% attainment on the same arrivals. Must sit in `[0, 1)`, and `0` disables the loop. |
| `planner.utilization` | `0.8` | The fraction of an instance each pool's demand is divided by: `need = ceil(demand / utilization)`. Must sit in `(0, 1]`. |
| `planner.deadband` | `0.5` | A pool is starving (moves without confirmation) only when demand exceeds its capacity by this many engines; ceil-wobble disagreements at demand boundaries wait for `confirmations` instead. Without it the planner hunts when demand sits between two optima; `0` restores that behaviour. |
| `planner.min_arrivals` / `demand_floor` | `10` / `0.5` | No moves on a cold or idle signal, so the planner never acts on warmup noise. |
| `planner.fast_step_s` | `5.0` | Between plans, a pool below its computed need may grow one step this often; shrinking stays with the plan loop. |
| `wandb.project` / `wandb.run` | `""` | Telemetry destination; empty project means no exporter. |
| `eject_after` | `3` | Consecutive failed legs before the breaker acts. Connection-shaped failures (refused, unreachable) eject; timeout-shaped failures only trigger a `/health` verify, ejecting when that also fails, because a timeout tail under load does not mean the engine died. |
| `readmit_every` | `10` | Monitor intervals between `/health` probes of ejected instances; readmission is the only way back. |
| `liveness_every` | `10` | Monitor intervals between `/health` sweeps of the live instances. The breaker learns from served traffic, so an idle fleet never dispatches the request that would reveal a dead engine, and it keeps its role until one arrives. `0` sweeps nothing and restores that traffic-only behaviour. |
| `liveness_misses` | `2` | Consecutive silent sweeps before a live instance is ejected. One silence is a blip and never enough; answering resets the count. The last live instance is never ejected, as with every other path. |
| `tokenize_timeout_s` | `2.0` | Budget for the exact-length probe, charged to a request the router has not placed yet. |
| `max_connections` | `512` | Engine pool size and the admission limit; the request past it is answered 429 rather than queueing inside the pool. |
| `pool_timeout_s` | `5.0` | Bound on the wait for a connection slot, so exhaustion fails a leg loudly instead of stalling for the request timeout. |
| `connect_timeout_s` | `10.0` | TCP handshake budget to an engine. |
| `health_timeout_s` | `5.0` | Budget for the `/health` probes that readmission and the check gates run. |
| `flip_history` | `1000` | Flips and refusals kept for `/arrow/state`. The scheduler never reads it. |
| `graceful_timeout_s` | `30.0` | Seconds uvicorn allows in-flight requests after SIGTERM; `--graceful-timeout` overrides per process. |
| `profiles_path` | `runs/profiles.json` | Where `narwhal-profile` wrote the per-instance curves; the router refuses to start without them. |
| `request_timeout_s` | `600.0` | End-to-end budget for one request. |
| `prefill_timeout_s` | `120.0` | Budget for the prefill leg alone; a single forward pass must not inherit the decode budget. |
| `chars_per_token` | `3.8` | Fallback prefill-cost estimate when an engine has no `/tokenize`; its error squares through the quadratic. |
| `tokenize` | `true` | Ask an engine for exact input length before scheduling. |
| `decode_attempts` | `2` | Decode legs tried per request; a retry only happens before anything streamed, so it is invisible to the client. |
| `decode_read_timeout_s` | `60.0` | Longest silent gap before a stream counts as stalled. Set it against the TPOT target rather than the healthy cadence. |
| `first_token_timeout_s` | `2.5` | Deadline for the decode leg's first token (Arrow §4.3's t2). Set it above the crossed-handoff p99 in `narwhal-report`'s KV table for your context sizes. Long-context fleets need it far higher than the packaged example: on the study's six-node reference fleet at 12-16k input tokens, that p99 ran 4.3-5.4 s against this default's 2.5. |
| `health.window_s` | `30.0` | Per-engine decode residuals against the engine's own profile curve (at the batch it carries), scored drift-window by drift-window at this length; a window with fewer than `min_samples` observations carries no verdict. |
| `health.drift_band` | `2.0` | A window counts as drift when its residual rises past `band` times the engine's own trailing healthy reading (learned live, floored at 1.0) - hardware and drivers carry constant offsets from any static profile, so the band is read against the engine's own history. Ratios under it move the baseline along. Must exceed `1.0`. |
| `health.relative_band` | `1.5` | The fleet-surge veto and its override. A drift verdict is vetoed when a majority of the other engines scoring in the same window are over *their* own bands, because a fleet rising together is out of capacity, not sick. The veto yields when the engine's residual exceeds this multiple of the surging fleet's median, so an engine that is its own story is still convicted. `0` disables the veto, and a window with under three scored engines has none by construction. |
| `health.min_ttft_s` | `0.25` | TTFT observations under this floor are ignored, because a twenty-millisecond prefill cannot be scored against a one-second monitor interval, and the deep-queue failures this instruments sit above the floor. TTFT scores stay informational. Verdicts ride the decode channel, whose occupancy terms the scheduler prices identically. |
| `health.min_samples` | `3` | Least observations for a window to be scored at all, so a busy or idle minute cannot convict an engine. |
| `health.probation_windows` / `evict_windows` | `3` / `5` | Consecutive drifting windows before probation, then before the tracker asks the breaker to eject. Probation prices the engine `probation_penalty_s` above itself on every placement so argmin drains new work away from it; ejection reuses the reactive path's ejection - probes readmit the moment /health answers. |
| `health.recovery_windows` | `3` | Consecutive under-band windows that clear probation: the recover-in-place path, which is what the fleet-recovery finding expects most drifters to do. |
| `health.probation_penalty_s` | `1.5` | The placement-cost penalty an engine on probation pays - seconds on a prefill leg, converted to tokens at the TPOT SLO rate on a decode leg. |
| `state_path` | `runs/state.json` | Where the control-plane handoff lives. It records each engine's actual pool, the breaker's holds, offline relaunch windows, and the served/failed/unserved counters. Rewritten atomically every monitoring pass, with one more flush at shutdown. |
| `resume` | `false` | Start from that handoff instead of the fleet file's opening split, so the replacement router inherits the previous router's actuated picture. A handoff from a different fleet is refused. A handoff listing every engine as ejected lands as none. `narwhal-serve --resume` forces it on, and the measured restart gap prints on the resume line. |
| `tenants.names` | `[]` | Tenants: each entry takes `name`, `api_key_env` (the environment variable holding that tenant's key - a key value never enters the recorded config; an unset variable refuses at the serving door, so checks and reports load the config keyless), `weight` as its fair share of the admission limit, and an optional `max_concurrent` hard cap below the share. An empty list runs one implicit anonymous tenant over the whole pool. |
| `tenants.auth_required` | `false` | Which requests the door admits when names exist: true means an unresolvable bearer is 401'd; false means it lands in the `anonymous` bucket protected by `anonymous_weight`. |
| `tenants.anonymous_weight` | `1.0` | The anonymous bucket's share, when it is open. |
| `connector` | `"nixl"` | Which KV transport carries the split, named in the connector registry. The check gates (`narwhal-check`, every ordered pair by default (`--ring` narrows it)) are its acceptance test: a connector counts as supported when the gates pass on reference hardware. |
| `dialect` | `"vllm"` | Which engine build answers the HTTP routes, named in the dialect registry: where `/health` and exact token counting live and what a one-token prefill cannot carry, plus the probe body's exit-hold keys. A build with no exact-count route makes the router fall back to `chars_per_token` per request, and the profiler size prompts off it instead of refusing - announced per instance, because the fitted x axis is then estimated. |

(Hardware, model) pair bundles live under
[presets](https://github.com/athrael-soju/Narwhal/blob/main/presets/README.md),
with `presets/_template/` as the skeleton. `narwhal-check --preset <name>`
gates against `presets/<name>/fleet.json` directly. Secrets stay in the
environment (`.env` for the fleet tool). The config file is the run's
record and belongs in the experiment's directory, with engine URLs
elided.
