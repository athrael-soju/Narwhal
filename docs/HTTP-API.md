# Narwhal HTTP API reference

## Interface map

| Interface            | Public namespace                          |
| -------------------- | ----------------------------------------- |
| Python distribution  | `narwhal-inference`                       |
| Python import        | `narwhal`                                 |
| Operator commands    | `narwhal-*`                               |
| Completion API       | `/v1/completions`, `/v1/chat/completions` |
| Model inspection     | `/v1/models`                              |
| Health and readiness | `/health`, `/ready`                       |
| Metrics              | `/metrics`                                |
| Router state         | `/narwhal/state`                          |
| State handoff        | `/narwhal/handoff`                        |
| Lifecycle control    | `/narwhal/lifecycle` and its actions      |
| Router telemetry     | `narwhal_*`                               |
| Persisted schemas    | `narwhal.*`, versioned per document       |
| OpenAPI schema       | `/openapi.json`                           |
| Schema browser       | `/docs`                                   |

The `/metrics` response includes `narwhal_contract_info{contract="metrics",version="1"} 1`.

## HTTP contracts

- [Completion requests](http-api/01-Requests.md): request fields, validation, model selection, output restrictions, and request IDs.
- [Admission and responses](http-api/02-Admission-and-Responses.md): refusal conditions and status codes, admission counters, response assembly, and token-ID accounting.
- [Backend execution and failures](http-api/03-Backend-and-Failures.md): prefill and decode legs, input sizing, engine-error mapping, timeouts, breaker readmission, and retries.
- [Model, health, and metrics inspection](http-api/04-Inspection.md): `/v1/models`, `/health`, `/ready`, and `/metrics`.
- [Live router and scheduler state](http-api/05-Live-State.md): the fields of `/narwhal/state`.
- [SLO attainment and demand accounting](http-api/06-SLO-and-Demand.md): service-level objective (SLO) outcome buckets, demand history, and consolidation evidence in `/narwhal/state`.
- [State handoff and engine lifecycle](http-api/07-Handoff-and-Lifecycle.md): the `/narwhal/handoff` document and the routes that drain and readmit engines.

## Which endpoint do I need?

| If you want to...                                             | Call                                                                                                                                                                                                       |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Confirm the router is alive                                   | [`GET /health`](http-api/04-Inspection.md#get-health)                                                                                                                                                      |
| Know whether it's ready for client traffic                    | [`GET /ready`](http-api/04-Inspection.md#get-ready)                                                                                                                                                        |
| See which model is configured                                 | [`GET /v1/models`](http-api/04-Inspection.md#get-v1models)                                                                                                                                                 |
| Look inside the scheduler, controller, admission, and breaker | [`GET /narwhal/state`](http-api/05-Live-State.md#get-narwhalstate)                                                                                                                                         |
| Hand state over to a standby router                           | [`GET /narwhal/handoff`](http-api/07-Handoff-and-Lifecycle.md#get-narwhalhandoff)                                                                                                                          |
| Check whether engines are draining or awaiting readmission    | [`GET /narwhal/lifecycle`](http-api/07-Handoff-and-Lifecycle.md#get-narwhallifecycle)                                                                                                                      |
| Drain or readmit engines                                      | [`POST /narwhal/lifecycle/drain`](http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecycledrain), [`POST /narwhal/lifecycle/readmit`](http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) |
| Scrape metrics                                                | [`GET /metrics`](http-api/04-Inspection.md#get-metrics)                                                                                                                                                    |

Probe responses:

| Probe     | Response                                                                                                  |
| --------- | --------------------------------------------------------------------------------------------------------- |
| `/health` | HTTP 200 in every state, with the router's status and engine counts                                       |
| `/ready`  | HTTP 200 while the router accepts client requests                                                         |
| `/ready`  | HTTP 503 with `Retry-After: 1` while admission is closed                                                  |

!!! warning
    Keep `/narwhal/state`, `/narwhal/handoff`, `/narwhal/lifecycle`, and the lifecycle action routes on your trusted control network.

## Citing this work

Arrow research attribution is in [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff).