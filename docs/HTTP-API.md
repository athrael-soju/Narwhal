# Narwhal HTTP API reference

The Narwhal router uses FastAPI. It builds its OpenAPI schema from the router's routes and serves it at `/openapi.json`. Open `/docs` if you'd rather browse it interactively.

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

The `/metrics` response includes `narwhal_contract_info{contract="metrics",version="1"} 1`.

## HTTP contracts

Each page below covers one part of the API in detail.

- [Completion requests](http-api/01-Requests.md): what a request may contain, how it's validated, how the model is chosen, which output options are restricted, and how request IDs work.
- [Admission and responses](http-api/02-Admission-and-Responses.md): why the router refuses a request and with which status code, what the admission counters track, how responses are put together, and how token IDs are counted.
- [Backend execution and failures](http-api/03-Backend-and-Failures.md): how a request moves through the prefill and decode legs, how input is sized, how engine errors become HTTP errors, and what happens on timeouts, breaker readmission, and retries.
- [Model, health, and metrics inspection](http-api/04-Inspection.md): `/v1/models`, `/health`, `/ready`, and `/metrics`.
- [Live router and scheduler state](http-api/05-Live-State.md): every field in `/narwhal/state`.
- [SLO attainment and demand accounting](http-api/06-SLO-and-Demand.md): how requests fall into service-level objective (SLO) outcome buckets, plus the demand history and consolidation evidence that `/narwhal/state` reports.
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

The two probes behave differently. `/health` always returns HTTP 200, with the router's status and engine counts. `/ready` returns 200 only while the router is accepting client requests. Once admission closes, it returns 503 with `Retry-After: 1`.

!!! warning
    Keep `/narwhal/state`, `/narwhal/handoff`, `/narwhal/lifecycle`, and the lifecycle action routes on your trusted control network. They expose live scheduler and handoff state, and they can drain or readmit engines.

## Citing this work

For Arrow research attribution, see [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff).