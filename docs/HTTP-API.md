# Narwhal HTTP API reference

Narwhal exposes an OpenAI-compatible completion API, operational inspection endpoints, and lifecycle controls for disaggregated inference fleets.

FastAPI publishes the generated schema at:

- `/docs`
- `/openapi.json`

This reference describes the HTTP contract for Narwhal v0.1.0, including request validation, response assembly, admission behaviour, engine failure handling, scheduler state, HA handoff, and engine lifecycle operations.

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

`narwhal_contract_info` identifies metrics contract version 1.

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

Use the endpoints according to the question being answered:

| Need                                                                   | Endpoint             |
| ---------------------------------------------------------------------- | -------------------- |
| Is the router process alive?                                           | [`/health`](http-api/04-Inspection.md#get-health)                       |
| Should a load balancer send new client traffic here?                   | [`/ready`](http-api/04-Inspection.md#get-ready)                         |
| Which model is served?                                                 | [`/v1/models`](http-api/04-Inspection.md#get-v1models)                 |
| What are current scheduler, controller, admission, and breaker states? | [`/narwhal/state`](http-api/05-Live-State.md#get-narwhalstate)         |
| What state should a warm standby inherit?                              | [`/narwhal/handoff`](http-api/07-Handoff-and-Lifecycle.md#get-narwhalhandoff) |
| Can an engine be stopped or readmitted?                                | [`/narwhal/lifecycle`](http-api/07-Handoff-and-Lifecycle.md#get-narwhallifecycle) |
| What time-series data should monitoring scrape?                        | [`/metrics`](http-api/04-Inspection.md#get-metrics)                     |

`/health` is a liveness endpoint.

`/ready` is the client-traffic admission signal.

`/narwhal/state`, `/narwhal/handoff`, and `/narwhal/lifecycle` expose internal control-plane state and should be treated as trusted operational interfaces.
