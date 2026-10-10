---
description: Capture diagnostic bundles and recover a Narwhal fleet from router, admission and overload failures.
---

# Troubleshooting a fleet

## Capturing router and engine state

Collect one bundle per incident router, with:

- a fresh output path
- that router's fleet configuration
- that router's run directory

Collect a bundle:

```bash
mkdir -p runs/diagnostics
narwhal diagnostics collect \
  --router http://router:8000 \
  --fleet config/fleet.json \
  --run runs/dev/run-example \
  --out runs/diagnostics/router-incident-001
```

On exit status `3`, inspect the source rows in the partial bundle's [manifest](Diagnostic-Bundles.md).

Add `--artifact PATH` to include ingress and supervisor status, engine boot logs, profiles, or deployment load results from outside the selected run. Add `--include-request-content` to include journal and completion content.

Manual capture for releases before 0.3.0:

```bash
umask 077
mkdir -p runs/diagnostics
incident_dir=$(mktemp -d runs/diagnostics/router.XXXXXX)

for endpoint in health ready narwhal/state narwhal/lifecycle metrics; do
  name=${endpoint##*/}
  curl --connect-timeout 2 --max-time 5 -sS \
    -o "$incident_dir/$name.body" -w '%{http_code}\n' \
    "http://router:8000/$endpoint" > "$incident_dir/$name.status"
done
```

Local artifacts:

1. Filter them with site tooling per the [Content policy](Diagnostic-Bundles.md#content-policy).
2. Place them beside these snapshots.

Before stopping an engine, capture evidence per the [planned restart or unplanned-failure procedure](troubleshoot/01-Engine-Recovery.md).

## Router, admission, and lifecycle signals

Start from the router's health and readiness:

- If a request to `/health` fails, query the peer's `/ready` to identify the lease holder. Inspect the router process, host, and network path.
- If `/health` reports `standby`, send traffic and lifecycle actions to the active lease holder.
- If `/health` reports `fenced`, identify the current lease holder and remove the fenced router from the load balancer.
- If `/health` reports `maintenance`, follow `/narwhal/lifecycle` through the engine wave until readiness returns.
- If both routers return HTTP 503 from `/ready`, compare their refusal reasons. Inspect backend health, lifecycle holds, engine monitoring, the lease holder, and state handoff freshness.

For rising client errors:

- If HTTP 429 responses increase, [classify them by outcome reason](#classify-outcomes-by-reason) before changing capacity.
- If HTTP 502 responses increase, inspect engine failures, ejection, quarantine, and in-flight work.
- If HTTP 504 responses increase, separate queue and request expiry from engine timeouts with the response error and terminal journal row.
- If a stream ends with an error frame after the HTTP 200 response starts, inspect the failed attempts, final outcome, and participating engines.

If the lifecycle state is `blocked`, repair the failed drain identity capture or readmission check, and retry that operation.

Procedures by path:

<div class="grid cards" markdown>

-   [Fleet overload with healthy engines](#fleet-overload-with-healthy-engines)

    ---

    HTTP 429, 503, or 504 increases, classified by outcome reason.

-   [Engine and whole-wave recovery](troubleshoot/01-Engine-Recovery.md)

    ---

    An engine failed unexpectedly, or the fleet needs a whole-wave restart.

-   [Router failover and rollback](troubleshoot/02-Router-Recovery.md)

    ---

    The primary router failed, or a standby reports a stale or incompatible state handoff.

</div>

## Fleet overload with healthy engines

Use this procedure when HTTP 429, 503, or 504 responses rise while engines pass health checks. If the router has ejected or held engines, follow [Engine and whole-wave recovery](troubleshoot/01-Engine-Recovery.md) first.

### Classify outcomes by reason

Each rejected, refused, expired, or failed request increments one outcome counter on the router's `/metrics`, labelled with the [outcome reason](telemetry/01-Journal.md#outcome-reasons) that ended it. Query each counter over the incident window, grouped by its label:

```text
sum by (reason) (increase(narwhal_rejected_total[5m]))
sum by (cause) (increase(narwhal_refused_total[5m]))
sum by (reason) (increase(narwhal_expired_total[5m]))
sum by (reason) (increase(narwhal_failed_total[5m]))
```

The [drop reasons panels](observability/05-Dashboard.md#drop-reasons-failed-attempts-and-expired-kv) chart the same series.

To classify individual requests, count the router journal's terminal rows by outcome. Set `JOURNAL` to the router's journal path, `journal.jsonl` beside `profiles.path` unless `narwhal-serve --journal` sets another path:

```bash
jq -r 'select(.terminal != null and .terminal != "completed")
  | [.terminal, .reason, .status, .error_type, .error_code] | @tsv' "$JOURNAL" \
  | sort | uniq -c | sort -rn
```

Each output line counts one combination of terminal state, reason, HTTP status, error type, and error code. A null field prints as an empty column.

Match the largest counts to a class:

| Counter and label | Journal `terminal` | Client response | Class |
| --- | --- | --- | --- |
| `narwhal_rejected_total{reason="inflight_limit"}` | `rejected` | HTTP 429, message `router in-flight limit reached` | [In-flight limit](#in-flight-limit-rejections) |
| `narwhal_rejected_total{reason="saturated"}` | `rejected` | HTTP 429, message starting `router saturated` | [Router saturation](#router-saturation-rejections) |
| `narwhal_rejected_total{reason="not_ready"}` | `rejected` | HTTP 503 `standby` or `backend_unavailable` | Router readiness, not overload. Read `readiness_reason` and follow [Router, admission, and lifecycle signals](#router-admission-and-lifecycle-signals). |
| `narwhal_refused_total{cause="queue"}` or `{cause="prompt"}` | `refused` | HTTP 429 | [TTFT refusals](#ttft-refusals) |
| `narwhal_refused_total{cause="aggregate_unpriced"}` | `refused` | HTTP 429 | [TTFT refusals](#ttft-refusals) |
| `narwhal_refused_total{cause="slot_wait"}`, `{cause="kv_capacity"}`, or `{cause="tpot"}` | `refused` | HTTP 429 | [Decode capacity refusals](#decode-capacity-refusals) |
| `narwhal_expired_total{reason="queue_timeout"}` or `{reason="deadline"}` | `expired` | HTTP 504 | [Wait and deadline expiries](#wait-and-deadline-expiries) |
| `narwhal_expired_total{reason="handoff"}` | `expired` | HTTP 504 `handoff_expired` | [KV handoff expiries](#kv-handoff-expiries) |
| `narwhal_failed_total{reason="engine_overloaded"}`, `{reason="engine_timeout"}`, or `{reason="local_pool"}` | `failed` | HTTP 502 or 504 | [Engine-side overload](#engine-side-overload) |

[Admission and refusal semantics](http-api/02-Admission-and-Responses.md#admission-and-refusal-semantics) and [Engine failure handling](http-api/03-Backend-and-Failures.md#engine-failure-handling) list the status, error type, and code of each response.

### Diagnose the class

#### In-flight limit rejections

Check: `narwhal_http_retained` reaches `narwhal_http_retained_limit`, which is the [in-flight limit](configuration/02-Serving-and-Role-Control.md#in-flight-limit) plus `serving.queue_capacity`. `narwhal_admission_inflight` stays at `narwhal_admission_inflight_limit`.

Cause: requests arrive faster than admitted requests release their seats. The router rejects each arrival before it reads the body, so the predictive checks never run for these requests.

Action: reduce ingress traffic, or add engines that passed deployment validation. Before raising `serving.max_connections`, measure the in-flight load the fleet sustains while healthy.

#### Router saturation rejections

Check: compare `narwhal_router_loop_lag_seconds` and `narwhal_request_sizing_delay_seconds` with `narwhal_saturation_threshold_seconds`. The signal at or above the threshold caused the rejection.

Cause: a late router event loop, or slow token counting and prefix hashing. With `engine.tokenize` on, sizing includes the exact count from an engine.

Action: for loop lag, inspect CPU load on the router host and `rate(narwhal_event_loop_busy_seconds_total[5m])`. For sizing delay, inspect the latency of the engines' exact-count requests. Reduce ingress traffic to this router until the signal falls below the threshold.

#### TTFT refusals

Check: read `admission_price` on the refused journal rows. Its parts are `backlog_s`, `own_prefill_s`, and `elapsed_s`, and `price_s` is their sum. The [admission price](telemetry/01-Journal.md#admission-price) section defines each part.

| `cause` | Cause | Action |
| --- | --- | --- |
| `queue` | Prefill work resident on the cheapest prefill engine, `backlog_s`, plus the time the request already waited, `elapsed_s`, pushes `price_s` past the TTFT budget. | Reduce the offered rate, or add prefill capacity. Clients can retry after `Retry-After`, which excludes the time the request already waited. |
| `prompt` | The prompt's own prefill, `own_prefill_s`, exceeds the TTFT budget. | Shorten the prompt, or raise `slo.ttft_s`. Draining the prefill backlog does not clear this cause. |
| `aggregate_unpriced` | Zero prefill engines are live, and every candidate engine carries decode work. | Check `pools.prefill` and `ejected` in `/narwhal/state`, then restore a live prefill engine. `controller.min_prefill` sets the [prefill role floor](configuration/02-Serving-and-Role-Control.md#role-floors). |

#### Decode capacity refusals

Check: the journal rows carry `refused_cause: decode`, and `reason` names the failed [decode admission check](configuration/02-Serving-and-Role-Control.md#decode-admission-check): `slot_wait`, `kv_capacity`, or `tpot`. Compare `resident` decode counts in `/narwhal/state` with `narwhal_engine_seats{phase="decode"}`.

Cause: projected decode load exceeds the live decode engines' seats, KV capacity, or `slo.tpot_s`.

Action: reduce the offered rate, or add decode capacity.

#### Wait and deadline expiries

Check: read `queue_waits` on the expired journal rows, and `narwhal_queue_wait_seconds` by `stage`.

| `reason` | Stage with the longest wait | Cause | Action |
| --- | --- | --- | --- |
| `queue_timeout` | `admission` | Every admission seat stayed held for `serving.queue_timeout_s`. | Treat as [in-flight limit](#in-flight-limit-rejections) pressure. |
| `queue_timeout` | `prefill` | Every prefill engine's [seats](configuration/02-Serving-and-Role-Control.md#engine-seats) stayed full for the rest of `serving.queue_timeout_s`. | Reduce the offered rate, or add prefill capacity. |
| `deadline` | any | The request reached `serving.request_timeout_s`. | Compare `queue_waits`, `ttft_s`, and `upstream_seconds` to find the stage that used the time. |

Raising `serving.queue_timeout_s` lengthens waits without adding seats.

#### KV handoff expiries

Check: `narwhal_waiting_decode` stays above `0`, and the expired rows' `queue_waits.decode` approaches the producer's `narwhal_handoff_bound_seconds`.

Cause: every decode engine's seats stayed full until prefilled requests reached their [KV handoff bound](configuration/02-Serving-and-Role-Control.md#kv-handoff-bound). Past the bound, the producer's KV lease can expire before a decode engine pulls the blocks, so the router ends the attempt instead of dispatching its decode leg.

Action: reduce the offered rate, or add decode capacity. With `serving.max_attempts` above `1`, as in the default of `2`, an expiry with attempts left retries with a fresh prefill, which adds prefill load.

#### Engine-side overload

Check: read `narwhal_attempt_failures_total` by `phase` and `reason`, and each journal row's `attempt_failures`. Each entry's `retry_reason` records why the router retried the attempt or ended the request. `shared_budget` with a rising `narwhal_retry_denied_total` means the retry credits ran out.

| `reason` | Meaning |
| --- | --- |
| `engine_overloaded` | The engine returned HTTP 408 or 429. |
| `engine_timeout` | The engine returned HTTP 504, or a prefill, first-token, or between-token timeout expired. |
| `local_pool` | A wait for a router data connection to the engine reached `engine.pool_timeout_s`. |

Cause: admitted load exceeds what the engines serve within their timeouts. In `open` admission mode, the router skips the predictive checks. With `serving.queue_capacity` at `0`, the router places requests without checking seats, and an engine queues requests above its seats.

Action: reduce the offered rate, or add capacity. In `open` mode, compare a run in [`predictive` mode](configuration/02-Serving-and-Role-Control.md#global-admission).

### Confirm the binding limit and verify recovery

1. Keep the request mix, `serving.max_connections`, queue settings, and timeouts fixed.
2. Test two offered rates under those conditions.
3. Compare completed throughput and the share of requests meeting the service-level objective, from `narwhal_served_total` and `narwhal_slo_met_total`.
4. Reduce ingress traffic, or add a fleet that passed deployment validation, before raising a limit.

Before you change an admission, queue, retry or in-flight setting, read [Choosing admission, queue and retry settings](operate/07-Admission-Queue-and-Retry-Settings.md) for the client outcomes of each value.

The class is resolved when the counter series that identified it stops increasing at the offered rate, and the journal shows no new terminal rows with that reason.

The load-test router outcome ratio divides router completions by admitted requests. [Deployment attainment](measure/04-Reconcile-and-Accept.md#joining-client-offers-to-the-router-journal) divides the client completions that meet the service-level objective by all scheduled offers, including cancellations, predictive refusals, and unsent scheduling misses.

## Validating recovery

Run the [release validation drills](operate/05-Release-Drills.md#validating-every-release).
