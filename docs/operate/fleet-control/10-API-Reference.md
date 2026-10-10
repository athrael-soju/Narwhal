---
description: Routes, request fields, status codes, response bodies, and dashboard metrics for the fleet control service API.
---

# API reference

The console calls this API, and you can call it directly. Every route except `GET /` and `GET /console` needs the token. A request with a missing or invalid token receives HTTP 401 with `WWW-Authenticate: Bearer` and the body `{"detail": "missing or invalid bearer token"}`.

## Responses

A successful action returns its [run-record entry](11-Run-Record.md#action-entries). A refused or failed action returns `{"detail": "<reason>", "action": <entry>}`.

A malformed request body returns HTTP 422 with a `detail` alone, and the service records nothing. An unexpected service error returns HTTP 500 and records the action as failed.

## Sessions and status

| Route                   | Status | Meaning                                                                                                       |
| ----------------------- | ------ | ------------------------------------------------------------------------------------------------------------- |
| `POST /api/session`     | 201    | The session started. The result holds the session ID and the baseline digest                                  |
|                         | 409    | A session is already active, or an exclusive action is in progress                                            |
|                         | 500    | The baseline fleet configuration cannot be read or fails the fleet configuration loader                       |
| `POST /api/session/end` | 200    | The session closed. The result lists the changes the fleet keeps                                              |
|                         | 409    | No session is active, or an exclusive action is in progress                                                   |
| `GET /api/session`      | 200    | The active session's run record, with `changes`                                                               |
|                         | 404    | No session is active                                                                                          |
| `GET /api/health`       | 200    | `status`, the active `session`, the current or last load `job`, `in_progress` and `in_progress_since`         |
| `GET /api/console`      | 200    | `grafana` with the dashboard UID, dashboard URL and panel URLs, or `null`; `load`; and the configured `hooks` |

## Engines

| Route                             | Action                                                                                   |
| --------------------------------- | ---------------------------------------------------------------------------------------- |
| `POST /api/engines/{iid}/pause`   | Runs the `engine_pause` hook                                                             |
| `POST /api/engines/{iid}/resume`  | Runs the `engine_resume` hook, then waits until the router returns the engine to service |
| `POST /api/engines/{iid}/stop`    | Runs the `engine_stop` hook                                                              |
| `POST /api/engines/{iid}/start`   | Runs the `engine_start` hook, then waits until the router returns the engine to service  |
| `POST /api/engines/{iid}/drain`   | Drains the engine. Accepts an optional `deadline_s`, a positive number of seconds        |
| `POST /api/engines/{iid}/readmit` | Readmits the engine                                                                      |
| `GET /api/engines`                | The router state of every baseline engine                                                |

Engine actions return these statuses:

| Status | Meaning                                                                                                                                                                                                                               |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 200    | The hook exited `0`, or the router accepted the lifecycle call                                                                                                                                                                        |
| 404    | The engine ID is not in the session's baseline fleet configuration                                                                                                                                                                    |
| 409    | No session is active, or an exclusive action is in progress                                                                                                                                                                           |
| 422    | The body is not a JSON object, has a field the action does not accept, or has an invalid `deadline_s`                                                                                                                                 |
| 501    | The action's hook is not configured                                                                                                                                                                                                   |
| 502    | The hook failed to start, exited non-zero or timed out. The router was unreachable or refused the lifecycle call. After start or resume, the router blocked the engine's readmission or reported that the engine needs fresh profiles |
| 504    | Start or resume only. The engine did not return to service within `router.timeout_s`                                                                                                                                                  |

Before and after each engine action, the service reads the router's `GET /narwhal/state` and records the engine's part of it:

- `known`, `role`, `ejected`, `draining`, `quarantined`, `probation` and `pinned`
- `resident` and `breaker`
- `lifecycle` and `process_start`
- the router's `router` and `wave` lifecycle state
- `event`, the router's latest lifecycle event for the engine with its `action`, `at` and `error`, or `null`

When the state read fails, the record holds the reason in `error` and the action still runs.

`GET /api/engines` reads the engine IDs from the session's baseline, or from the `fleet` file outside a session. It returns HTTP 500 when that file is unreadable and HTTP 502 when the router state is unreadable.

## Load jobs

`POST /api/jobs` takes a JSON object with these fields:

| Field             | Meaning                                                      |
| ----------------- | ------------------------------------------------------------ |
| `workload`        | Name of a workload from `load.workloads`. Required           |
| `rate`            | Requests per second, 0.1 to 1000                             |
| `arrival`         | `steady`, `random` or `bursty`                               |
| `concurrency`     | In-flight request limit, an integer from 1 to 4096           |
| `ramp_s`          | Seconds to reach the rate and the concurrency, 1 to 3600     |
| `duration_s`      | Load duration in seconds, 1 to 86400                         |
| `requests`        | Requests to send, an integer from 1 to 1000000               |
| `warmup_requests` | Requests sent before measurement, an integer from 1 to 10000 |

[Start a job](06-Load-Jobs.md#start-a-job) lists which fields each workload kind requires, accepts or rejects.

| Route                         | Status | Meaning                                                                                                                                                                                                                                                                                           |
| ----------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /api/jobs`              | 201    | The job started                                                                                                                                                                                                                                                                                   |
|                               | 409    | No session is active, a job is running, or an exclusive action is in progress                                                                                                                                                                                                                     |
|                               | 422    | The body is not a JSON object or the parameters are invalid; the message names every problem                                                                                                                                                                                                      |
|                               | 501    | The configuration has no `load` section                                                                                                                                                                                                                                                           |
| `POST /api/jobs/current/stop` | 200    | The job stopped                                                                                                                                                                                                                                                                                   |
|                               | 409    | No job is running, no session is active, or an exclusive action is in progress                                                                                                                                                                                                                    |
| `GET /api/jobs/current`       | 200    | The running job, or the last one to finish                                                                                                                                                                                                                                                        |
|                               | 404    | No job has run                                                                                                                                                                                                                                                                                    |
| `GET /api/jobs/current/live`  | 200    | The current job's results so far, read from AIPerf's per-request records: `requests`, `elapsed_s`, `throughput`, `latency` percentiles, `slo` targets, `goodput` and a `series` over the job                                                                                                      |
|                               | 404    | No job has run                                                                                                                                                                                                                                                                                    |
| `GET /api/workloads`          | 200    | The workload library. `workloads` lists each entry with `name`, `kind`, `label`, `description`, `ignore_eos` and the workload's other keys, except `file`. `limits` maps each numeric job field to its `min`, `max`, `integer` and console `default`. The route exists only with a `load` section |

When a job finishes or stops, the service copies the job's [journal extract](11-Run-Record.md#journal-extracts) and records a `job.complete` action with the job document. The job document holds:

- `id`, `params`, `started_at`, `finished_at` and `error`
- `state`: `running`, `succeeded`, `failed` or `stopped`
- `result`: the AIPerf command, exit code, duration and workload, with the trace file's SHA-256 for a trace workload; request counts by outcome; request and output-token throughput; TTFT, inter-token latency and request latency statistics in AIPerf's reported unit; and goodput when `load.goodput` is configured
- `journal`: the job's journal extract entry, or `null` while the job runs

## Configuration

| Route                            | Action                                             |
| -------------------------------- | -------------------------------------------------- |
| `POST /api/config/overlay`       | Applies an overlay                                 |
| `POST /api/config/overlay/check` | Checks an overlay without applying or recording it |
| `POST /api/config/cold-restart`  | Runs the `cold_restart` hook                       |
| `POST /api/config/restore`       | Undoes the session's changes                       |

| Status | Meaning                                                                                                                       |
| ------ | ----------------------------------------------------------------------------------------------------------------------------- |
| 200    | The action finished. When it restarted the router, the router answered `GET /ready` with HTTP 200                             |
| 409    | No session is active, or an exclusive action is in progress                                                                   |
| 422    | Overlay only: the body is not a JSON object, changes nothing, changes another section, or fails the loader                    |
| 501    | A hook the action needs is not configured                                                                                     |
| 502    | A hook failed to start, exited non-zero or timed out, or a restore could not read or change an engine's router state          |
| 504    | The router was not ready within `router.timeout_s`, or an engine that restore started or resumed did not return to service    |

The check runs no hook. It returns HTTP 409 when no session is active, HTTP 422 when the body is not a JSON object, and otherwise HTTP 200 with:

- `errors`
- `base_digest`, for the current configuration
- `digest`, for the merged result, or `null` when it has errors
- `document`, the merged configuration

## Fleet signals

`GET /api/fleet` returns the router's readiness and, with `prometheus_url`, the firing Narwhal alerts. It returns HTTP 200 in every case and reports router or Prometheus failures in the body.

| Field                | Meaning                                                                                |
| -------------------- | -------------------------------------------------------------------------------------- |
| `router.ready`       | `true` when the router's `GET /ready` returned HTTP 200                                |
| `router.status_code` | The response status, or `null` when the router did not answer within 5 seconds         |
| `router.reason`      | The router's reason, or the request error                                              |
| `router.path`        | `/ready`                                                                               |
| `alerts`             | `null` without `prometheus_url`                                                        |
| `alerts.firing`      | Firing alerts, each with its `alertname` and `labels`, or `null` when the query failed |
| `alerts.error`       | The query error, or `null`                                                             |

The service queries `ALERTS{alertname=~"Narwhal.+",alertstate="firing",severity!="info"}`, the same alerts the Narwhal Orchestrator **Router** panel counts.

## Metrics

`GET /metrics` returns the metrics behind the [dashboard annotations](03-Grafana-Dashboard.md#dashboard-annotations) in the Prometheus text format:

| Metric                              | Labels                                                   | Value                                   |
| ----------------------------------- | -------------------------------------------------------- | --------------------------------------- |
| `narwhal_control_session_active`    | none                                                     | `1` during a session, otherwise `0`     |
| `narwhal_control_action_started_ms` | `session`, `seq`, `action`, `target`, `outcome`, `title` | Action start time, in Unix milliseconds |
| `narwhal_control_load_job_running`  | `job`, `workload`                                        | `1` while the job runs                  |

`narwhal_control_action_started_ms` has one series for each engine action, overlay, cold restart and restore that ran in the active session, failed ones included. `title` holds the chart label, such as `drain n7`.
