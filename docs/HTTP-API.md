# Narwhal HTTP API reference

Index of Narwhal's HTTP routes, with links to the per-route pages. FastAPI also serves a schema at `/openapi.json` and interactive views at `/docs` and `/redoc`.

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
| Lifecycle control    | `/narwhal/lifecycle`, `/narwhal/lifecycle/drain`, `/narwhal/lifecycle/readmit` |
| Router telemetry     | `narwhal_*`                               |
| Persisted schemas    | `narwhal.*`, each versioned separately    |

The metrics version appears as `narwhal_contract_info{contract="metrics",version="1"} 1` on `/metrics`.

## HTTP contracts

- [Requests](http-api/01-Requests.md)
- [Admission and responses](http-api/02-Admission-and-Responses.md)
- [Backend and failures](http-api/03-Backend-and-Failures.md)
- [Inspection](http-api/04-Inspection.md)
- [Router state](http-api/05-Live-State.md)
- [SLO and demand](http-api/06-SLO-and-Demand.md)
- [Handoff and lifecycle](http-api/07-Handoff-and-Lifecycle.md)

## Select an endpoint

| Operator task                                               | Endpoint                                                                                                                                                                                                   |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Check router liveness                                       | [`GET /health`](http-api/04-Inspection.md#get-health)                                                                                                                                                      |
| Check readiness for new client traffic                      | [`GET /ready`](http-api/04-Inspection.md#get-ready)                                                                                                                                                        |
| Read the configured model                                   | [`GET /v1/models`](http-api/04-Inspection.md#get-v1models)                                                                                                                                                 |
| Inspect scheduler, controller, admission, and breaker state | [`GET /narwhal/state`](http-api/05-Live-State.md#get-narwhalstate)                                                                                                                                         |
| Read handoff state for a standby router                     | [`GET /narwhal/handoff`](http-api/07-Handoff-and-Lifecycle.md#get-narwhalhandoff)                                                                                                                          |
| Inspect engine drain and readmission state                  | [`GET /narwhal/lifecycle`](http-api/07-Handoff-and-Lifecycle.md#get-narwhallifecycle)                                                                                                                      |
| Drain or readmit engines                                    | [`POST /narwhal/lifecycle/drain`](http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecycledrain), [`POST /narwhal/lifecycle/readmit`](http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) |
| Scrape router metrics                                       | [`GET /metrics`](http-api/04-Inspection.md#get-metrics)                                                                                                                                                    |

`/health` returns HTTP 200 with router status and engine counts. `/ready` returns 200 while the router is admitting clients and 503 with `Retry-After: 1` otherwise.

Restrict `/narwhal/state`, `/narwhal/handoff`, and `/narwhal/lifecycle/*` to the trusted control network. The first two expose live scheduler and handoff state. The lifecycle routes drain and readmit engines.

## Research attribution

To cite Narwhal, see [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff).
