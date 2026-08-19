# API

Narwhal serves seven routes. The two completion routes are the data plane, and the other five are observational. The served `/docs` page renders the typed shapes (`/v1/models`, `/health`, `/arrow/state`) from the same Pydantic models the schema test pins. The completion routes, `/metrics` and `/arrow/handoff` pass through untyped.

## POST /v1/completions, POST /v1/chat/completions

OpenAI-compatible. The request body is the base of both engine legs, and the router overwrites `model` in it with the config's served model. A request naming any other model is answered `404` with code `model_not_found`. The router refuses `n` and `best_of` above 1 with `400`, because the prefill leg runs with `max_tokens: 1` and the two legs would disagree on sampling width.

The router adds this behavior around the pass-through:

- Past `max_connections` in-flight requests, or past the tenant's own weighted share when tenants are configured, the reply is `429` with `retry-after: 1`. Under predictive admission (the default), a request whose cheapest placement already prices past the TTFT budget is also answered `429`, with `retry-after` set to the priced overrun - except a prompt whose own prefill alone prices past the budget, which gets the `429` with no `retry-after`. These refusals count separately as `refused` rather than `rejected`, so door refusals are reported apart from pool exhaustion. `admission: "open"` restores always-admit.
- With tenants configured and `tenants.auth_required` true, a request whose bearer resolves to no tenant is answered `401` with type `authentication_error`, and is counted as `rejected` on the anonymous book.
- Failed legs return their true status: `504` for anything time-shaped and `502` for a faulted engine. A streaming request whose decode leg fails instead gets the failure as a terminal `data: {"error": ...}` frame, whether or not any tokens were sent first; a prefill-leg failure returns the status, the stream not yet begun.
- A non-streaming response includes `usage`, and `finish_reason` is the engine's own. Both routes reassemble into completions-shaped choices, with the content in `choices[0].text` on the chat route too.
- Only `authorization`, `x-api-key` and `x-request-id` request headers are forwarded, and the first two also resolve the tenant.

## GET /v1/models

The one model Narwhal fronts, in the OpenAI list shape.

## GET /health

`{"status": "ok", "instances": 6}` reports liveness and the fleet size the router was configured with.

## GET /metrics

Prometheus text: The response includes served, failed and unserved counters, pool sizes, pool loads, flip counters, the ejection gauges (`arrow_ejected_instances`, and `arrow_ejected{iid=...}` per held-out engine), and TTFT and TPOT histograms, plus the rest of the register (per-instance roles and resident counts, the refusal and rejection counters, probation, placement regret, the load regime, flip reversals and in-flight flips). [Observability](Observability.md) catalogues every series. `tools/grafana-narwhal.json` is the standard board over these, and `tools/prometheus-alerts.yml` contains the alert rules.

## GET /arrow/handoff

The control-plane handoff document, generated at request time: It contains the fleet's engine ids, every current role, the breaker's held-out instances, any open relaunch windows with their remaining seconds, and the counters the run speaks over. A version stamp, the wall clock, the run id and the model go first in the document, so a reader knows which router the picture came from. This is exactly what `--resume` reads from disk after a crash. It is served over HTTP so a warm standby (`narwhal-serve --standby-of <primary>`) can shadow it without touching the primary's filesystem. A standby polls it every quarter second by default and keeps the freshest copy. When the primary stops posting, the standby applies its copy.

While a router runs as standby, `/health` answers `{"status": "standby", "instances": 6}` and both completion routes answer `503` with `retry-after: 1` and error code `standby`.

## GET /arrow/state

The live scheduler picture, and the actuation record of an adaptive run. The following output was captured from the stub fleet after three requests:

```json
{
    "served": 3,
    "failed": 0,
    "controller": "planner",
    "admission": {
        "inflight": 0,
        "limit": 512,
        "rejected": 0,
        "refused": 0
    },
    "tenants": {
        "anonymous": {
            "served": 3,
            "failed": 0,
            "rejected": 0,
            "inflight": 0,
            "weight": 1.0,
            "cap": 512
        }
    },
    "pools": {
        "prefill": [
            "e0",
            "e1",
            "e2"
        ],
        "decode": [
            "e3",
            "e4",
            "e5"
        ]
    },
    "load": {
        "prefill": 0.0,
        "decode": 0.0
    },
    "thresholds": {
        "expand": 1.0,
        "shrink": 0.5,
        "cooldown_s": 10.0,
        "sustained_intervals": 3,
        "dwell_s": 0.0,
        "panic_ratio": 0.0
    },
    "slo": {
        "ttft_s": 10.0,
        "tpot_s": 0.125
    },
    "first_token_timeout_s": 2.5,
    "resident": {
        "e0": {
            "prefill": 0,
            "decode": 0
        },
        "...": { "..." : 0 },
        "e5": {
            "prefill": 0,
            "decode": 0
        }
    },
    "pinned": [],
    "min_prefill": 1,
    "ejected": [],
    "probation": [],
    "unserved": 0,
    "panic_bypasses": 0,
    "poa": {
        "regret": null,
        "regime": "subcritical",
        "samples": 0
    },
    "flips_refused": [],
    "flips": []
}
```

The fields in detail:

| Field                                        | Meaning                                                                                                                                                                                                                                                                                                                                                                            |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `served`, `failed`                           | Requests completed and requests lost, since start.                                                                                                                                                                                                                                                                                                                                 |
| `controller`                                 | Which loop moves roles, either `planner` (the default) or `reactive` (Algorithm 2). Flip records from the planner have `by` set to `"planner"`.                                                                                                                                                                                                                                    |
| `admission`                                  | In-flight count against the limit, and the two refusal counters. A rising `rejected` means the pool had no room, or - with tenant auth on - the door turned away an unidentified bearer; these reach the client as 429 and 401 respectively. A rising `refused` means the cost model priced every placement over the TTFT budget before dispatch, always a 429.                                                                                                            |
| `tenants`                                    | Each tenant's served/failed/rejected/inflight counts, with its weight and cap. A tenant from the config always appears, and the `anonymous` book appears once it has any activity. Empty only when no tenants are configured and no traffic has arrived.                                                                                                                           |
| `pools`                                      | Which instances have each role right now. Roles move, and this is the actuation record.                                                                                                                                                                                                                                                                                            |
| `load`                                       | The Arrow §5.5 pool loads, relative to the SLO. 0 is idle, 1.0 is exactly at target. These two numbers drive Algorithm 2.                                                                                                                                                                                                                                                          |
| `thresholds`, `slo`, `first_token_timeout_s` | The config values the run actually uses, echoed so a state snapshot describes itself.                                                                                                                                                                                                                                                                                              |
| `resident`                                   | Per-instance in-flight prefill and decode counts.                                                                                                                                                                                                                                                                                                                                  |
| `pinned`, `min_prefill`                      | The config's standing constraints on the controllers: engines whose role no flip path may move, and the floor under the prefill pool. `[]` and `1` when the feature is unused.                                                                                                                                                                                                 |
| `ejected`                                    | Instances the breaker keeps out of scheduling and out of both pool loads. They keep their pool label but receive no dispatch, and `/health` probes drive readmission.                                                                                                                                                                                                              |
| `probation`                                  | Engines the predictive-health loop has deprioritized after drifting past `health.drift_band` for sustained windows. They still serve if nothing else can, but every placement prices them `health.probation_penalty_s` higher, and sustained drift on probation escalates to ejection.                                                                                             |
| `unserved`                                   | Requests Algorithm 1 could not place within the SLO.                                                                                                                                                                                                                                                                                                                               |
| `panic_bypasses`                             | P→D flips the sustained two-sided panic condition let through the cooldown. Setting `thresholds.panic_ratio` to 0 disables the bypass and the counter stays at 0.                                                                                                                                                                                                                  |
| `poa`                                        | Observation-only efficiency gauge: `regret` is the median per-placement cost regret against that placement's own best option (`null` before data, and the windowed matching optimum is the offline replay's estimator). `regime` classifies the load as `subcritical`, `transitional` or `saturated`. `samples` counts recorded placements. Nothing reads these values for control. |
| `flips_refused`                              | The last 20 refusals, each with the reason (cooldown, pool size, load band). The scheduler retains `flip_history` of them, and the response truncates.                                                                                                                                                                                                                             |
| `flips`                                      | The role changes, capped at the config's `flip_history`.                                                                                                                                                                                                                                                                                                                           |

A `flips` entry from a run where the controller moved an instance:

```json
{ "at": 1712.4, "iid": "e2", "to": "decode", "by": "algorithm2",
  "prefill_inflight": 0, "decode_inflight": 0, "drained_s": 0.0 }
```

`by` is `algorithm1` for inline request-path flips and `algorithm2` for the monitoring loop. The inflight counts are how much work the instance had when relabeled, because a flip that strands work costs more than the label write. `drained_s` records how long that work took to finish, and it is `null` until it has.

The W&B watcher, the stress and walk scorers, and `narwhal-bench` all read this endpoint. `narwhal-bench` refuses to drive a fleet that is already serving someone else's traffic.

## The journal

The journal is the router's other output, written to the path `--journal` names rather than served over HTTP. It is one JSONL row per request, appended as the request completes. `narwhal-report`, `narwhal-bench --score-journal`, the oracle and `examples/read_journal.py` all read this file, so its shape is a contract.

Content, the actual prompt and response text, is deliberately absent. The journal records lengths and timings only. When a debugging session needs content too, the opt-in payload sidecar (`journal_payloads` in the config, or `--journal-payloads`) writes `{rid, prompt, prompt_truncated, output, output_truncated}` rows to a separate file, with each text field truncated at a configured cap, the truncation flagged, and the whole file capped in size. Join it to the journal by `rid`.

The file opens with one `{"meta": ...}` row per process that wrote it, naming the package version and commit. Readers skip rows with a `meta` key. Every request row has these fields:

| Field                       | Meaning                                                                                                                                                                                         |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run`                       | Id minted per router process. Runs appended to one file are separated by it.                                                                                                                    |
| `rid`                       | The request id, also returned to the client as `x-request-id`.                                                                                                                                  |
| `client_rid`                | The client's own `x-request-id`, when one was sent. `null` otherwise.                                                                                                                           |
| `arrived`                   | Arrival time on the router's clock (monotonic, so compare within a run only).                                                                                                                   |
| `input_len`                 | Prompt tokens, exact when `/tokenize` answered, estimated otherwise.                                                                                                                            |
| `output_len`                | Tokens the decode leg produced.                                                                                                                                                                 |
| `wanted_len`                | The request's `max_tokens`, `0` when the request set none. Failure and refusal rows record `0`. On a row that asked for tokens, `output_len` short of it means a severed stream.                |
| `ttft_s`                    | The Arrow §4.2 cut: queue plus prefill, ending at o1 on the prefill instance.                                                                                                                   |
| `tpot_s`                    | Mean same-instance token gap over `output_len - 1`. `null` under two tokens.                                                                                                                    |
| `first_byte_s`              | The client's wait for its first byte, which differs from `ttft_s` by the KV transfer and decode queue.                                                                                          |
| `prefill_iid`, `decode_iid` | Which instance served each leg.                                                                                                                                                                 |
| `crossed`                   | Whether the KV cache moved between instances.                                                                                                                                                   |
| `attempts`                  | The decode instances tried, in order, ending with the one that answered. A first-try success reads `["e3"]`. Rows that never reached a decode leg have `[]`.                                    |
| `tenant`                    | The tenant the request resolved to, `anonymous` when none.                                                                                                                                      |
| `refused`                   | Present and `true` when admission refused the request at the door. The row then has the priced reason in `error`. Scorers drop these rows from the attainment denominator and count them apart. |
| `refused_cause`             | On a refused row, `"prompt"` when the prompt's own prefill alone priced past the budget (no `retry-after`), `"queue"` when a priced queue did.                                                   |
| `error`                     | `null` on success, else the failed leg's verdict.                                                                                                                                               |
