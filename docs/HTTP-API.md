# Narwhal HTTP API reference

FastAPI generates `/openapi.json` from Narwhal v0.1.0's routes and serves its interactive view at `/docs`.

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
| HA handoff           | `/narwhal/handoff`                        |
| Lifecycle control    | `/narwhal/lifecycle` and its actions      |
| Router telemetry     | `narwhal_*`                               |
| Persisted schemas    | `narwhal.*`, versioned per document       |

The `/metrics` response includes `narwhal_contract_info{contract="metrics",version="1"} 1`.

For Arrow research attribution, use [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff).

## HTTP contracts

- [Completion requests](http-api/01-Requests.md)
- [Admission and responses](http-api/02-Admission-and-Responses.md)
- [Backend execution and failures](http-api/03-Backend-and-Failures.md)
- [Model, health, and metrics inspection](http-api/04-Inspection.md)
- [Live router and scheduler state](http-api/05-Live-State.md)
- [SLO attainment and demand accounting](http-api/06-SLO-and-Demand.md)
- [HA handoff and engine lifecycle](http-api/07-Handoff-and-Lifecycle.md)

## Select an endpoint

| Operator task | Endpoint |
| ------------- | -------- |
| Check router liveness | [`GET /health`](http-api/04-Inspection.md#get-health) |
| Check readiness for new client traffic | [`GET /ready`](http-api/04-Inspection.md#get-ready) |
| Read the configured model | [`GET /v1/models`](http-api/04-Inspection.md#get-v1models) |
| Inspect scheduler, controller, admission, and breaker state | [`GET /narwhal/state`](http-api/05-Live-State.md#get-narwhalstate) |
| Read handoff state for a standby router | [`GET /narwhal/handoff`](http-api/07-Handoff-and-Lifecycle.md#get-narwhalhandoff) |
| Inspect engine drain and readmission state | [`GET /narwhal/lifecycle`](http-api/07-Handoff-and-Lifecycle.md#get-narwhallifecycle) |
| Drain or readmit engines | [`POST /narwhal/lifecycle/drain`](http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecycledrain), [`POST /narwhal/lifecycle/readmit`](http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) |
| Scrape router metrics | [`GET /metrics`](http-api/04-Inspection.md#get-metrics) |

`/health` returns HTTP 200 with router status and engine counts; `/ready` returns 200 during client admission and 503 with `Retry-After: 1` when admission is closed.

Restrict `/narwhal/state`, `/narwhal/handoff`, and `/narwhal/lifecycle` (including its action routes) to the trusted control network; these routes publish live scheduler and handoff state and can drain or readmit engines.
