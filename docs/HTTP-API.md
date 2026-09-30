# Narwhal HTTP API reference

This reference covers Narwhal's HTTP routes. FastAPI also generates a schema at `/openapi.json` and interactive views at `/docs` and `/redoc`.

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
| Persisted schemas    | `narwhal.*`, each versioned separately    |

`/metrics` includes `narwhal_contract_info{contract="metrics",version="1"} 1`.

## HTTP contracts

- [Completion requests](http-api/01-Requests.md)
- [How requests are admitted and answered](http-api/02-Admission-and-Responses.md)
- [Running requests on engines](http-api/03-Backend-and-Failures.md)
- [Inspection endpoints](http-api/04-Inspection.md)
- [Router state](http-api/05-Live-State.md)
- [How Narwhal tracks SLOs and demand](http-api/06-SLO-and-Demand.md)
- [Failover handoff and engine restarts](http-api/07-Handoff-and-Lifecycle.md)

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

`/health` always returns HTTP 200 with the router's status and engine counts. `/ready` returns 200 while the router is admitting clients and 503 with `Retry-After: 1` when it isn't.

Keep `/narwhal/state`, `/narwhal/handoff`, and `/narwhal/lifecycle` (action routes included) on the trusted control network. They expose live scheduler and handoff state, and the lifecycle routes can drain and readmit engines.

## Research attribution

For Arrow research attribution, use [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff).
