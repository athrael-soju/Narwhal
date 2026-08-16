# Observability

The router reports through three read paths, all off the serving path.
`/metrics` serves Prometheus text. `/arrow/state` serves the same
picture as JSON ([Api](Api.md) documents it field by field). An
optional W&B exporter streams the run live. [Deploy](Deploy.md) §7
covers standing the Prometheus and Grafana stack up. This page is the
reference for what the numbers mean.

## The metric catalog

Counters reset when the router restarts. Gauges read the current
monitoring pass.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `arrow_served_total` | counter | | Requests completed without error. |
| `arrow_failed_total` | counter | | Requests that ended in an error. |
| `arrow_unserved_total` | counter | | Requests that reached Algorithm 1 step 3 with no placement meeting the SLO. |
| `arrow_refused_total` | counter | | Requests the predictive door turned away, priced over the TTFT budget. Answered 429. |
| `arrow_rejected_total` | counter | | Requests refused at the pool limit (`max_connections`). Also 429, and a different story: `refused` is the cost model declining, `rejected` is the pool full. |
| `arrow_pool_instances` | gauge | `role` | Instances in each pool. |
| `arrow_pool_load` | gauge | `role` | Pool load as a ratio against its own SLO target. 1.0 is exactly at target, and these two numbers drive the reactive controller. |
| `arrow_instance_role` | gauge | `iid`, `role` | 1 while the engine carries the role. An aggregated fleet (no prefill pool configured) reports every engine in both roles, because that is the function the engines actually serve. |
| `arrow_resident_requests` | gauge | `iid`, `phase` | Requests resident on each instance, per phase. |
| `arrow_ejected_instances` | gauge | | Instances the breaker holds out of scheduling right now. The alarm-worthy availability signal. |
| `arrow_ejected` | gauge | `iid` | 1 while this instance is ejected. |
| `arrow_probation_instances` | gauge | `iid` | 1 while the drift instrument has this engine deprioritized. Probation prices the engine higher on every placement; it still serves when nothing else can. |
| `arrow_flips_total` | counter | `to`, `by` | Role changes, by target pool and which loop asked (`algorithm1` inline, `algorithm2` monitoring, `planner`). |
| `arrow_flip_reversals_total` | counter | | Role changes that put an instance back where it came from. The thrash signal. |
| `arrow_flips_refused_total` | counter | | Flips the load condition declined. |
| `arrow_flip_inflight_total` | counter | `phase` | Requests resident on an instance at the moment it was relabeled. Work exposed to a flip. |
| `arrow_placement_regret` | gauge | | Median per-placement cost regret against that placement's own floor. Observation only; nothing reads it for control. Absent until data exists. |
| `arrow_regime` | gauge | `regime` | 1 for the current load regime and 0 for the other two: `subcritical`, `transitional`, or `saturated`. Absent until data exists. |
| `arrow_ttft_seconds` | histogram | | Time to first token (Arrow §4.2 cut: queue plus prefill). |
| `arrow_tpot_seconds` | histogram | | Time per output token (Arrow §4.3). |

The histogram buckets derive from the configured SLOs: edges sit at
fixed fractions of each target (most of them under 1.0, because
attainment is decided just below the target), so quantile resolution
follows your own SLOs without configuration. Re-derive dashboards
after an SLO change rather than comparing bucket counts across
configs.

## The alert rules

`tools/prometheus-alerts.yml` ships six rules. The engines job scrapes
every engine directly, so engine alerts fire with or without a router.

| Alert | Fires when | Read it as |
| --- | --- | --- |
| `NarwhalEngineDown` | `up{job="engines"} == 0` for 30 s | An engine stopped answering its scrape. Never routine, whatever the router thinks. |
| `NarwhalEngineEjected` | `arrow_ejected_instances > 0` for 1 m | The breaker is serving around a live engine. Capacity is down and nobody may have noticed. |
| `NarwhalRouterDown` | `up == 0` for 2 m | A scrape went dark for two minutes. The expression is unqualified, so a dead engine fires this alongside `NarwhalEngineDown`; when it fires alone, the router itself is down, and a running standby has taken over already. |
| `NarwhalErrorBurst` | `rate(arrow_failed_total[5m]) > 0.5` for 5 m | Requests are dying faster than background noise explains. |
| `NarwhalUnservedRising` | `rate(arrow_unserved_total[10m]) > 0.2` for 10 m | The cost model finds no SLO-meeting placement. The fleet is undersized for what is arriving. |
| `NarwhalPoolStarved` | `arrow_pool_instances < 1` for 2 m | A pool has no engines. With `min_prefill` at its default this means every engine of one role is ejected. |

## The standard board

`tools/grafana-narwhal.json` provisions itself when the compose stack
starts ([Deploy](Deploy.md) §7). Its panels, top to bottom: role
one role card per engine (prefill, decode, or P+D on an aggregated
fleet; STANDBY in orange when the engine scrape is up but the router
reports no role; OFFLINE in red when both are gone). The cards repeat
over an `engine` variable reading `label_values(up{job="engines"},
iid)`, so the board follows whatever fleet the scrape targets name and
needs no editing per fleet; pool load against SLO
(1.0 = at target); pool sizes with ejections and probation;
throughput and failures; flips; TTFT and TPOT quantiles off the
histograms; resident requests per instance; the node role timeline;
and a health timeline plotting the fleet's ejection count beside
per-node probation.

## W&B, per run

Name the destination in the fleet config (`"wandb": {"project": ...,
"run": ...}`) and the router streams the pools, both loads, and the
served, failed, unserved and flip counters on every monitoring pass.
The wandb library prints its own run URL to the console when the
exporter initializes. A W&B outage drops points and disables the
exporter without touching serving.

`tools/wandb_watch.py` is the external fallback: it polls
`/arrow/state` and needs nothing installed on the router. It refuses
to start without a credential (`WANDB_API_KEY`, an `api.wandb.ai`
entry in `~/.netrc`, or `WANDB_MODE=offline`/`disabled` for a
connectionless run), prints its run URL on the first line, and
`--once` runs exactly that smoke check and exits. Assert on the URL
before leaving a campaign unattended.

## Watching a long run

`tools/run_watchdog.sh <run-dir>` runs beside the router and writes a
verdict line every 15 s: processes, ejections, counters, and every
engine's `/health`. It exits with the run. Run it on the router's own
node rather than over SSH from elsewhere, because remote watch loops
fail silently.
