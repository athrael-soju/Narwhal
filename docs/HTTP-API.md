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
| List, join, or remove engines | [`GET /narwhal/engines`](http-api/08-Engine-Membership.md#get-narwhalengines), [`POST /narwhal/engines`](http-api/08-Engine-Membership.md#post-narwhalengines), [`DELETE /narwhal/engines/{iid}`](http-api/08-Engine-Membership.md#delete-narwhalenginesiid) |
| Browse the OpenAPI schema | `GET /openapi.json`, `GET /docs` |

A completion's refusals and status codes are in [Admission and responses](http-api/02-Admission-and-Responses.md), and its engine legs, timeouts and retries in [Backend execution and failures](http-api/03-Backend-and-Failures.md). [SLO attainment and demand accounting](http-api/06-SLO-and-Demand.md) covers the SLO and demand fields of `/narwhal/state`.

The `/metrics` response includes `narwhal_contract_info{contract="metrics",version="3"} 1`.

!!! warning
    Keep `/narwhal/state`, `/narwhal/targets`, `/narwhal/handoff`, `/narwhal/lifecycle`, `/narwhal/engines`, and the lifecycle action routes on your trusted control network.

## Control token

The routes that change membership or lifecycle need the router's control token in an `Authorization: Bearer <token>` header:

- `POST /narwhal/engines` and `DELETE /narwhal/engines/{iid}`
- `POST /narwhal/lifecycle/drain` and `POST /narwhal/lifecycle/readmit`

A request without the token, or with another one, gets HTTP `401` with an empty body and `WWW-Authenticate: Bearer`, before the router checks whether it controls the fleet or reads the body. The router compares the token in constant time. Every request other than a `GET`, `HEAD` or `OPTIONS` outside the completion routes is a control request and needs the token, so an unknown control path without it also gets `401`. With the token, a control request's body may hold at most 64 KiB; a longer one gets HTTP `413` once the router has read past that limit. Reads, completions, `/health`, `/ready` and `/metrics` need no token.

The router reads the token from `NARWHAL_ROUTER_TOKEN`. It must hold at least 32 random bytes, as 64 hex digits or 43 base64url characters, and must differ from the engine API key, which every engine holds; the router refuses to start with a shorter token or with the engine key. Generate one with:

```bash
python -c 'import secrets; print(secrets.token_hex(32))'
```

A router without `NARWHAL_ROUTER_TOKEN` serves completions and reads, and answers every control request, and `GET /narwhal/handoff`, with HTTP `403` and a plain-text body that names the setting. A standby needs the token to verify its primary's [signed handoff](http-api/07-Handoff-and-Lifecycle.md#get-narwhalhandoff) and refuses to start without it, so both routers of a pair take the same token. The [fleet control service](operate/fleet-control/01-Set-Up-the-Service.md) sends the token from its own `NARWHAL_ROUTER_TOKEN`.

!!! danger "0.13.0 and earlier"
    Up to 0.13.0, anyone who could reach the router port could join, remove, drain or readmit engines. A join also sent the engine API key to the URL in the request before the router checked that engine's attestation, so the caller could collect the key and call every engine directly. Upgrade, and keep the router port, which HAProxy forwards in full, off untrusted networks. A join now reads the candidate's attestation before any request carries the engine key, and health checks carry no key.
