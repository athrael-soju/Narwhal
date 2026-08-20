# Observability

The router reports through three read paths, all off the serving path. `/metrics` emits Prometheus text. `/arrow/state` returns the same state as JSON, and [Api](Api.md) documents it field by field. When W&B is configured, an exporter streams each monitoring pass to a run. [Deploy](Deploy.md) §7 has the Prometheus and Grafana setup.

## The metric catalog

Counters reset when the router restarts. With `resume: true` or a warm-standby takeover ([Deploy](Deploy.md) §5) the handoff restores them, and they continue instead. Gauges read the current monitoring pass.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `arrow_served_total` | counter | | Requests completed without error. |
| `arrow_failed_total` | counter | | Requests that ended in an error. |
| `arrow_unserved_total` | counter | | Requests that reached Algorithm 1 step 3 with no placement meeting the SLO. |
| `arrow_refused_total` | counter | | The predictive door priced the request over the TTFT budget and turned it away. The router answers 429. |
| `arrow_rejected_total` | counter | | Requests refused at the pool limit (`max_connections`), or - with tenant auth on - an unidentified bearer turned away at the door; answered 429 and 401 respectively. `arrow_refused_total` counts cost-model declines, so graph the two apart. |
| `arrow_pool_instances` | gauge | `role` | Instances in each pool. |
| `arrow_pool_load` | gauge | `role` | Pool load as a ratio against the pool's own SLO target, where 1.0 is at target. The reactive controller reads the pair. |
| `arrow_instance_role` | gauge | `iid`, `role` | 1 while the engine has that role. An aggregated fleet (no prefill pool configured) reports every engine in both roles, since both are the function the engines perform. |
| `arrow_resident_requests` | gauge | `iid`, `phase` | Requests resident on each instance, per phase. |
| `arrow_ejected_instances` | gauge | | Instances the breaker is keeping out of scheduling right now. The availability signal to alarm on. |
| `arrow_ejected` | gauge | `iid` | 1 while this instance is ejected. |
| `arrow_probation_instances` | gauge | `iid` | 1 while the drift instrument has this engine deprioritized. Probation raises the engine's price on every placement. The engine still takes traffic when nothing else can. |
| `arrow_flips_total` | counter | `to`, `by` | Role changes, by target pool and which loop asked (`algorithm1` inline, `algorithm2` monitoring, `planner`). |
| `arrow_flip_reversals_total` | counter | | Role changes that put an instance back where it came from. High values mean the controller is oscillating. |
| `arrow_flips_refused_total` | counter | | Flips declined by the load condition, the cooldown, or the pool-size guard. |
| `arrow_flip_inflight_total` | counter | `phase` | Requests resident on an instance at the moment it was relabeled. These are the requests a flip can disturb. |
| `arrow_placement_regret` | gauge | | Median per-placement cost regret against that placement's own floor. Nothing in the control loop reads it. Absent until data exists. |
| `arrow_regime` | gauge | `regime` | One series per load regime, labeled `subcritical`, `transitional`, or `saturated`. The active regime reads 1 and the other two read 0. Absent until data exists. |
| `arrow_ttft_seconds` | histogram | | Time to first token (Arrow §4.2 cut: queue plus prefill). |
| `arrow_tpot_seconds` | histogram | | Time per output token (Arrow §4.3). |

The histogram buckets derive from the configured SLOs. Each bucket edge is a fixed fraction of the target, most of them under 1.0 because attainment is decided just below the target, so quantile resolution follows your own SLOs without configuration. After an SLO change, re-derive the dashboards. Bucket counts are not comparable across configs.

## The alert rules

`tools/prometheus-alerts.yml` defines six rules. The engines job scrapes every declared engine directly, independent of the router's config. The engine alerts fire with or without a router, and an engine removed from serving still alarms.

| Alert | Fires when | Read it as |
| --- | --- | --- |
| `NarwhalEngineDown` | `up{job="engines"} == 0` for 30 s | An engine stopped answering its scrape. Treat it as a fault even when the router reports healthy pools. |
| `NarwhalEngineEjected` | `arrow_ejected_instances > 0` for 1 m | The breaker is scheduling around a live engine, so capacity is down while the process stays up. `arrow_ejected{iid=...}` names the engines. Readmission probes `/health`. If the ejection persists, the engine process is down. |
| `NarwhalRouterDown` | `up == 0` for 2 m | A scrape target has answered nothing for two minutes. The expression has no job qualifier, so a dead engine fires this alert alongside `NarwhalEngineDown`. When it fires on its own, the router is down, and a warm standby, if deployed, has already taken over. |
| `NarwhalErrorBurst` | `rate(arrow_failed_total[5m]) > 0.5` for 5 m | Requests failing faster than background noise explains. A sustained burst with zero ejections usually means a leg-level fault the breaker cannot see yet. The journal's error column names the leg. |
| `NarwhalUnservedRising` | `rate(arrow_unserved_total[10m]) > 0.2` for 10 m | The cost model finds no placement that meets the SLO. The fleet is undersized for the offered load, or the pools are in the wrong split. |
| `NarwhalPoolStarved` | `arrow_pool_instances < 1` for 2 m | A pool has no engines. Ejected engines keep their label and still count here, so this is the role split collapsing, not the breaker; `min_prefill` floors the prefill pool, so a disaggregated fleet's zero points at the decode side or a misconfiguration. |

## The standard board

`tools/grafana-narwhal.json` provisions itself when the compose stack starts ([Deploy](Deploy.md) §7). The role cards repeat over an `iid` variable (display label `engine`) that reads `label_values(up{job="engines"}, iid)`, so the board follows whatever fleet the scrape targets name and needs no editing per fleet.

- One role card per engine (prefill, decode, or P+D on an aggregated fleet). A card reads STANDBY in orange when the engine scrape is up and the router reports no role, and OFFLINE in red when scrape and role are both absent.
- Pool load against SLO, with 1.0 at target.
- Pool sizes with ejections and probation.
- Throughput and failures.
- Flip totals.
- TTFT and TPOT quantiles from the histograms.
- Resident requests per instance.
- The node role timeline.
- A health timeline with the fleet's ejection count next to per-node probation.

## W&B, per run

Name the destination in the fleet config (`"wandb": {"project": ..., "run": ...}`) and the router streams the pools, both loads, and the served, failed, unserved, and flip counters on each monitoring pass. The wandb library prints its run URL to the console when the exporter initializes. A W&B outage drops points and disables the exporter. Serving is unaffected.

When the in-process exporter is not an option, `tools/wandb_watch.py` polls `/arrow/state` from any host. Nothing has to be installed on the router. Without a credential the script exits with an error. A credential can come from `WANDB_API_KEY`, from an `api.wandb.ai` entry in `~/.netrc`, or from `WANDB_MODE` set to `offline` or `disabled` for a connectionless run. The run URL is the first line it prints. `--once` performs that startup check and exits. Assert on the URL before leaving a campaign unattended.

## Watching a long run

Start `tools/run_watchdog.sh <run-dir>` beside the router, on the same node. Every 15 s it writes a verdict line covering processes, ejections, counters, and every engine's `/health`. It exits when the run's status file gains the `COOLDOWN WALK DONE` marker; a run that never writes it leaves the watchdog looping. Keep it local. An SSH watch loop can stop with its session and write nothing.
