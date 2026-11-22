---
description: Reference for the OpenAI-compatible completion routes and the health, metrics and control endpoints of a Narwhal router.
---

# Narwhal HTTP API reference

| To | Call |
| --- | --- |
| Send a completion request | [`POST /v1/completions`, `POST /v1/chat/completions`](http-api/01-Requests.md) |
| See which model is configured | [`GET /v1/models`](http-api/04-Inspection.md#get-v1models) |
| Confirm the router is alive | [`GET /health`](http-api/04-Inspection.md#get-health) |
| Check readiness for client traffic | [`GET /ready`](http-api/04-Inspection.md#get-ready) |
| Scrape metrics | [`GET /metrics`](http-api/04-Inspection.md#get-metrics) |
| Discover engine scrape targets | [`GET /narwhal/targets`](http-api/04-Inspection.md#get-narwhaltargets) |
| Look inside the scheduler, controller, admission, and breaker | [`GET /narwhal/state`](http-api/05-Live-State.md#get-narwhalstate) |
| Hand state over to a standby router | [`GET /narwhal/handoff`](http-api/07-Handoff-and-Lifecycle.md#get-narwhalhandoff) |
| Check whether engines are draining or awaiting readmission | [`GET /narwhal/lifecycle`](http-api/07-Handoff-and-Lifecycle.md#get-narwhallifecycle) |
| Drain or readmit engines | [`POST /narwhal/lifecycle/drain`](http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecycledrain), [`POST /narwhal/lifecycle/readmit`](http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) |
| Browse the OpenAPI schema | `GET /openapi.json`, `GET /docs` |

A completion's refusals and status codes are in [Admission and responses](http-api/02-Admission-and-Responses.md), and its engine legs, timeouts and retries in [Backend execution and failures](http-api/03-Backend-and-Failures.md). [SLO attainment and demand accounting](http-api/06-SLO-and-Demand.md) covers the SLO and demand fields of `/narwhal/state`.

The `/metrics` response includes `narwhal_contract_info{contract="metrics",version="2"} 1`.

!!! warning
    Keep `/narwhal/state`, `/narwhal/targets`, `/narwhal/handoff`, `/narwhal/lifecycle`, and the lifecycle action routes on your trusted control network.
