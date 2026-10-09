---
description: Run engine actions, configuration overlays and AIPerf load jobs against a running fleet from the fleet control service and console, with a private run record per session.
---

# Controlling the fleet

The fleet control service runs operator test actions against a running Narwhal fleet:

- engine actions
- configuration overlays
- cold restarts
- baseline restores
- AIPerf load jobs

It records each action with its time and effect in a private run record. Its console page shows every control beside embedded Grafana dashboard panels.

The service runs on the router host, listens only on loopback, and refuses every API request that lacks its bearer token. Operators reach the service and Grafana from a workstation through the operator tunnel.

The service controls one deployed fleet. Deployment-specific commands perform each change, such as stopping an engine or restarting the router. The private configuration names these commands as hooks.

## Prerequisites

- A deployed fleet with its router serving on the router host, as in [Gate G](../deploy/07-Serve-and-Measure.md#starting-and-locally-verifying-the-router).
- The router shell: the router host's Narwhal checkout with `.venv` active, opened with `deploy_hosts.py shell`.
- The monitoring stack started with `make observe` on the router host, for the console's dashboard panels.
- The baseline fleet configuration file that the router is running.
- One hook command for each action you plan to run, described under [Hooks](#hooks).
- For load jobs, an AIPerf installation on the router host. [Ordered benchmark points](../measure/05-Benchmark-Runner.md) names the standard AIPerf client version.

## Setup

### 1. Write the private configuration

The service reads `config/fleet-control.local.json` by default. Another file can be named with `--config` or the `NARWHAL_CONTROL_CONFIG` environment variable. Git ignores every `config/fleet-control.*.json` file except the shipped example, and `make publication` refuses to publish one.

In the router shell, copy the example and replace its placeholders:

```bash
cp config/fleet-control.example.json config/fleet-control.local.json
```

The service validates the whole file at startup and reports every problem in one message. It refuses unknown keys at every level.

| Key         | Default                          | Meaning                                                                                                       |
| ----------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `host`      | `127.0.0.1`                      | Listener address. Must be a loopback address or `localhost`                                                   |
| `port`      | `8020`                           | Listener port, `1` through `65535`                                                                            |
| `token_env` | `NARWHAL_CONTROL_TOKEN`          | Environment variable that holds the bearer token                                                              |
| `runs_dir`  | `runs/fleet-control`             | Directory for the action log and session run records                                                          |
| `fleet`     | `NARWHAL_FLEET` when set         | Baseline fleet configuration file. Required when `NARWHAL_FLEET` is unset                                     |
| `hooks`     | none; required                   | Hook commands by name. Must include `restore`                                                                 |
| `router`    | see [Router](#router)            | The router whose state, lifecycle and readiness routes the service calls                                      |
| `load`      | absent                           | AIPerf settings and the workload library. Without it, load-job routes refuse requests or are absent           |
| `console`   | absent                           | Grafana panels the console embeds. Without it, the console shows its controls without panels                  |

Relative paths resolve against the directory where the service starts.

#### Hooks

Each entry under `hooks` names one command:

| Key         | Default | Meaning                                            |
| ----------- | ------- | -------------------------------------------------- |
| `argv`      | none    | Command and arguments, a non-empty list of strings |
| `timeout_s` | `900`   | Seconds before the service stops the command       |

The service runs these hook names:

| Hook             | Runs for                                    | Must                                                                                                     |
| ---------------- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `restore`        | Ending a session, restoring the baseline    | Return the deployment to the baseline fleet configuration at `NARWHAL_CONTROL_BASELINE`                  |
| `engine_pause`   | Engine pause                                | Pause the engine named by `NARWHAL_CONTROL_ENGINE`                                                       |
| `engine_resume`  | Engine resume                               | Resume that engine                                                                                       |
| `engine_stop`    | Engine stop                                 | Stop that engine                                                                                         |
| `engine_start`   | Engine start                                | Start that engine                                                                                        |
| `router_restart` | Configuration overlay                       | Restart the router with the fleet configuration at `NARWHAL_CONTROL_FLEET`                               |
| `cold_restart`   | Cold restart                                | Restart every engine and the router with the fleet configuration at `NARWHAL_CONTROL_FLEET`              |

Only `restore` is required. An action whose hook is not configured is refused with HTTP 501.

A hook succeeds when it exits `0` within its timeout. The service runs each hook in its own process group with standard input closed, and writes its standard output and standard error to a log in the session directory. At the timeout, the service sends SIGTERM to the process group, then SIGKILL after 5 seconds.

Each hook inherits the service's environment without the bearer token variable, plus:

| Variable                     | Set for                     | Value                                                                   |
| ---------------------------- | --------------------------- | ----------------------------------------------------------------------- |
| `NARWHAL_CONTROL_SESSION`    | Every hook                  | Session ID                                                              |
| `NARWHAL_CONTROL_RUN_DIR`    | Every hook                  | Absolute path of the session directory                                  |
| `NARWHAL_CONTROL_BASELINE`   | Every hook                  | Absolute path of the session's copy of the baseline fleet configuration |
| `NARWHAL_CONTROL_ACTION`     | Engine hooks                | `pause`, `resume`, `stop` or `start`                                    |
| `NARWHAL_CONTROL_ENGINE`     | Engine hooks                | Engine ID                                                               |
| `NARWHAL_CONTROL_ENGINE_URL` | Engine hooks                | The engine's `url` from the baseline fleet configuration, or empty      |
| `NARWHAL_CONTROL_FLEET`      | `router_restart`, `cold_restart` | Absolute path of the fleet configuration the restarted processes must load |

#### Router

| Key                | Default                 | Meaning                                                                                                    |
| ------------------ | ----------------------- | ---------------------------------------------------------------------------------------------------------- |
| `router.url`       | `http://127.0.0.1:8000` | Router base URL, `http` or `https`. AIPerf load jobs also send their requests here                         |
| `router.timeout_s` | `600`                   | Limit for lifecycle calls and for the readiness wait after a restart or restore                            |

Engine state reads use the shorter of 10 seconds and `router.timeout_s`.

#### Load

| Key                   | Default  | Meaning                                                                         |
| --------------------- | -------- | ------------------------------------------------------------------------------- |
| `load.aiperf`         | none     | AIPerf executable                                                               |
| `load.model`          | none     | Served model name that AIPerf requests                                          |
| `load.tokenizer`      | none     | Tokenizer name or path for AIPerf                                               |
| `load.endpoint_type`  | `chat`   | `chat` or `completions`                                                         |
| `load.streaming`      | `true`   | Whether AIPerf streams responses                                                |
| `load.goodput`        | `{}`     | AIPerf metric tag mapped to its positive goodput threshold                      |
| `load.grace_period_s` | none     | Seconds AIPerf waits for in-flight responses after a timed job ends             |
| `load.extra_args`     | `[]`     | Further arguments appended to the AIPerf command                                |
| `load.workloads`      | none     | The workload library: at least one named workload                               |

Workload names use letters, digits, `.`, `_` and `-`, up to 64 characters. Each workload has a `kind`:

| Kind                | Keys                                                                                     | Replay                                                                                                |
| ------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `synthetic`         | `isl` and `osl` in tokens, required; `isl_stddev` and `osl_stddev`, default `0`         | AIPerf generates prompts with these input and output length distributions                             |
| `timestamped_trace` | `file`, a `mooncake_trace` JSONL file, required; `block_size` in tokens, optional         | AIPerf replays the records at their recorded timestamps                                               |
| `prefix_trace`      | `file`, a `mooncake_trace` JSONL file, required; `block_size` in tokens, required         | AIPerf reuses prompt prefixes through each record's `hash_ids`, paced by the job's rate and concurrency |

#### Console

| Key                     | Default          | Meaning                                                                                              |
| ----------------------- | ---------------- | ---------------------------------------------------------------------------------------------------- |
| `console.grafana_url`   | none             | Grafana base URL as the operator's browser reaches it, without credentials, query or fragment        |
| `console.dashboard_uid` | `narwhal-router` | Dashboard UID. The default names the shipped Narwhal Orchestrator dashboard                          |
| `console.panels`        | none             | Non-empty list of distinct dashboard panel IDs, in display order                                     |
| `console.from`          | `now-15m`        | Panel time range start, `now` or `now-<n><unit>` with unit `s`, `m`, `h`, `d`, `w`, `M` or `y`        |
| `console.refresh`       | `5s`             | Panel refresh interval, `<n><unit>` with unit `s`, `m`, `h` or `d`                                   |

`console.grafana_url` is a workstation address, usually the tunnel's local Grafana port, and not Grafana's address on the router host. The example configuration embeds the shipped dashboard's **Requests**, **Latency**, **Time to first token**, **Time per output token**, **Engine role history** and **Fleet events** panels. [Reading the dashboard](../observability/05-Dashboard.md) describes each panel.

Grafana must allow framing. The `make observe` Compose file sets `GF_SECURITY_ALLOW_EMBEDDING` to `true`. Grafana keeps anonymous Viewer access, so a framed panel needs no Grafana sign-in. A Grafana instance started before this setting existed still sends `X-Frame-Options: deny`, and the browser shows an empty frame. Run `make observe` again on the router host to recreate it.

### 2. Set the bearer token

The token must hold at least 32 characters without surrounding whitespace. In the router shell, generate one and keep it in the configured variable:

```bash
export NARWHAL_CONTROL_TOKEN="$(openssl rand -hex 32)"
```

Store the token in the deployment's private secret store. The service never passes it to hooks or to AIPerf.

### 3. Start the service

In the router shell, from the checkout root, run:

```bash
.venv/bin/python -m tools.fleet_control.cli --config config/fleet-control.local.json
```

The service runs in the foreground. `--log-level` sets the uvicorn log threshold: `critical`, `error`, `warning`, `info` (default) or `debug`.

The service exits with status `2` and a `fleet control failed:` message when the configuration is invalid, the token is missing or too short, or the listener address is unavailable.

To check the service from another router-host shell with the token exported, run:

```bash
curl -fsS -H "Authorization: Bearer $NARWHAL_CONTROL_TOKEN" http://127.0.0.1:8020/api/health
```

A running service with no session returns:

```json
{"status": "ok", "session": null, "job": null, "in_progress": null}
```

Press Ctrl+C to stop the service. On shutdown it stops a running load job and records the stop.

## Reaching the console through the tunnel

The operator tunnel forwards workstation loopback ports to `127.0.0.1` on the router host. [Tunnelling router, Prometheus, and Grafana to the workstation](../deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) describes its host-key check and authentication.

1. On the workstation, open a terminal in the checkout with `.env` loaded.
2. Open the tunnel with the control port and Grafana:

    ```bash
    python3 tools/deployment/deploy_hosts.py tunnel --role router \
      --forward 18020:8020 --forward 13000:3000
    ```

3. Keep the tunnel terminal open.
4. Open `http://127.0.0.1:18020/console` in a browser on the workstation.
5. Paste the bearer token into **Control token** and select **Connect**.

The local Grafana port must match the origin of `console.grafana_url`. If you forward Grafana to another workstation port, change `console.grafana_url` to match and restart the service. When the control service or Grafana listens on a router-host address other than `127.0.0.1`, open a separate tunnel for that address with `--remote-address`, as in [Accessing dashboards and isolating listeners](../observability/02-Access.md#isolating-a-second-monitoring-stack).

### Console authentication

`GET /console` serves a static page without the token. The page holds no fleet data, configuration values or token. `GET /` redirects to `/console`. Every other request without a valid token receives HTTP 401, including every `/api/` route.

After **Connect**, the page keeps the token in the browser tab's session storage and sends it as an `Authorization: Bearer` header on each API request. The token stays in that tab until the tab closes or the operator selects **Forget token**. No cookie carries the token, so another site cannot make the browser send an authenticated request. When the service answers 401, the page discards the token and asks for it again.

After it connects, the page reads `GET /api/console`. The response holds `grafana`, with the dashboard UID, the full dashboard URL and each panel's ID and URL, or `null` without a `console` section. It also holds `load`, which is `true` when load jobs are configured.

The page's content security policy admits only its own inline script and style, requests to the control service, and frames from the `console.grafana_url` origin. The page cannot itself be framed.

## Sessions

A session is one operator test run. Every action except session start requires an active session, and actions outside a session are refused with HTTP 409.

**Start session** (`POST /api/session`) loads the baseline fleet configuration through the fleet configuration loader, copies it into a new session directory, and records it as the session's first configuration. It returns HTTP 201 with the `session.start` action entry, whose result holds the session ID and the baseline digest.

**End session and restore** (`POST /api/session/end`) stops a running load job, runs the `restore` hook, and closes the session. It records `job.stop` when a job ran, `baseline.restore`, and `session.end`. Ending a session does not wait for router readiness. When the restore fails, the session stays open so the operator can retry.

The session section shows the session ID, its start time, any exclusive action in progress, and the full run record from `GET /api/session`. The console polls `GET /api/health` every 3 seconds.

| Route                   | Status | Meaning                                                                                  |
| ----------------------- | ------ | ---------------------------------------------------------------------------------------- |
| `POST /api/session`     | 201    | The session started                                                                      |
|                         | 409    | A session is already active, or an exclusive action is in progress                      |
|                         | 500    | The baseline fleet configuration cannot be read or fails the fleet configuration loader |
| `POST /api/session/end` | 200    | The restore hook succeeded and the session closed                                        |
|                         | 409    | No session is active, or an exclusive action is in progress                              |
|                         | 502    | The restore hook failed to start, exited non-zero or timed out; the session stays open  |
| `GET /api/session`      | 200    | The active session's run record                                                          |
|                         | 404    | No session is active                                                                     |
| `GET /api/health`       | 200    | `status`, the active `session`, the current or last load `job`, and `in_progress`        |

### Exclusive actions

Session start, session end, configuration overlays, cold restarts and baseline restores are exclusive. While one runs, the service refuses every other action with HTTP 409 and the message `<action> is in progress`. Engine actions and load jobs are not exclusive, so an engine action can run while a load job is running. Status reads such as `GET /api/health` continue during an exclusive action.

## Engine actions

The engines table lists each engine from the session's baseline fleet configuration with its role and router state. Each row has **pause**, **resume**, **stop**, **start**, **drain** and **readmit**.

| Action    | Route                                | Effect                                                                                   |
| --------- | ------------------------------------ | ---------------------------------------------------------------------------------------- |
| `pause`   | `POST /api/engines/{iid}/pause`      | Runs the `engine_pause` hook                                                             |
| `resume`  | `POST /api/engines/{iid}/resume`     | Runs the `engine_resume` hook                                                            |
| `stop`    | `POST /api/engines/{iid}/stop`       | Runs the `engine_stop` hook                                                              |
| `start`   | `POST /api/engines/{iid}/start`      | Runs the `engine_start` hook                                                             |
| `drain`   | `POST /api/engines/{iid}/drain`      | Calls the router's [`POST /narwhal/lifecycle/drain`](../http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecycledrain) for the engine |
| `readmit` | `POST /api/engines/{iid}/readmit`    | Calls the router's [`POST /narwhal/lifecycle/readmit`](../http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) for the engine |

`drain` accepts an optional `deadline_s` body field, a positive number of seconds, which the console takes from **Drain deadline (s)**. Without it, the router applies its default deadline of 300 seconds. The other actions accept no body fields.

Before and after the action, the service reads the router's `GET /narwhal/state` and records the engine's part of it:

- `known`, `role`, `ejected`, `draining`, `quarantined`, `probation` and `pinned`
- `resident` and `breaker`
- `lifecycle` and `process_start`
- the router's `router` and `wave` lifecycle state

When the state cannot be read, the record holds the reason in `error` and the action still runs. **Last engine action** shows the before and after values side by side and highlights the fields that changed.

`GET /api/engines` returns the same state for every baseline engine. It returns HTTP 409 without a session and HTTP 502 when the router state cannot be read; the console then lists the baseline engines with their state unavailable.

| Status | Meaning                                                                                                            |
| ------ | ------------------------------------------------------------------------------------------------------------------ |
| 200    | The hook exited `0`, or the router accepted the lifecycle call                                                     |
| 404    | The engine ID is not in the session's baseline fleet configuration                                                 |
| 409    | No session is active, or an exclusive action is in progress                                                        |
| 422    | The body is not a JSON object, has a field the action does not accept, or has an invalid `deadline_s`             |
| 501    | The action's hook is not configured                                                                                |
| 502    | The hook failed to start, exited non-zero or timed out; or the router was unreachable or refused the lifecycle call |

## Load jobs

The service runs one AIPerf job at a time against `router.url`. A different rate or duration requires a new job.

**Start job** (`POST /api/jobs`) takes a JSON object with these fields:

| Field         | Meaning                                                     |
| ------------- | ----------------------------------------------------------- |
| `workload`    | Name of a workload from `load.workloads`; required          |
| `rate`        | Requests per second, a positive number                      |
| `concurrency` | In-flight request limit, a positive integer                 |
| `duration_s`  | Load duration in seconds, a positive number                 |

A `synthetic` or `prefix_trace` workload requires `duration_s` and at least one of `rate` and `concurrency`. A `timestamped_trace` workload keeps its recorded arrival times, so it refuses `rate`; `concurrency` and `duration_s` optionally cap and shorten its replay. The console fills the workload list from `GET /api/workloads`, which omits trace file paths.

**Stop job** (`POST /api/jobs/current/stop`) stops the running job's whole AIPerf process group and returns the stopped job. **Refresh status** reads `GET /api/jobs/current`, which returns the running job or the last one to finish, or HTTP 404 when no job has run.

When AIPerf finishes, the service records a `job.complete` action with the job document. The job document holds:

- `id`, `params`, `started_at`, `finished_at` and `error`
- `state`: `running`, `succeeded`, `failed` or `stopped`
- `result`, the client results

The client results hold:

- the AIPerf command, exit code, duration and workload, with the trace file's SHA-256 for a trace workload
- request counts by outcome
- request and output-token throughput
- TTFT, inter-token latency and request latency statistics in AIPerf's reported unit
- goodput, when `load.goodput` is configured

A job fails when its trace file cannot be read, AIPerf cannot start or exits non-zero, or AIPerf writes no readable summary export.

| Route                         | Status | Meaning                                                                                       |
| ----------------------------- | ------ | --------------------------------------------------------------------------------------------- |
| `POST /api/jobs`              | 201    | The job started                                                                               |
|                               | 409    | No session is active, a job is running, or an exclusive action is in progress                 |
|                               | 422    | The body is not a JSON object or the parameters are invalid; the message names every problem |
|                               | 501    | The configuration has no `load` section                                                       |
| `POST /api/jobs/current/stop` | 200    | The job stopped                                                                               |
|                               | 409    | No job is running, no session is active, or an exclusive action is in progress                |
| `GET /api/jobs/current`       | 200    | The current or last job                                                                       |
|                               | 404    | No job has run                                                                                |
| `GET /api/workloads`          | 200    | The workload library; the route exists only with a `load` section                             |

## Configuration overlays, cold restarts and restores

The configuration section shows the configuration that currently governs the session: its source, its file in the session directory, its digest and when it was applied.

**Apply overlay** (`POST /api/config/overlay`) takes a partial fleet document and merges it onto the current fleet configuration as a JSON merge patch (RFC 7386):

- An object merges key by key.
- `null` removes a key, so the field returns to its default.
- Any other value, arrays included, replaces the current value.

An overlay may change only the `slo`, `controller`, `serving` and `recovery` sections, plus keys that start with `_`. The service checks the merged document with the fleet configuration loader that `narwhal config validate` runs before any hook runs. It then writes the merged file to the session directory and runs the `router_restart` hook with `NARWHAL_CONTROL_FLEET` naming that file. [Serving and role control](../configuration/02-Serving-and-Role-Control.md) and [Recovery and validation](../configuration/03-Recovery-and-Validation.md) define the overlay sections' fields.

For example, this overlay adds 10% of the TTFT target to the admission budget. The value is illustrative:

```json
{"serving": {"admission_margin": 0.1}}
```

**Cold restart** (`POST /api/config/cold-restart`) runs the `cold_restart` hook with the current fleet configuration file.

**Restore baseline** (`POST /api/config/restore`) runs the `restore` hook and records the baseline as the current configuration. It records the hook run as a separate `baseline.restore` action.

After each of these operations, the service polls the router's `GET /ready` every second until it returns HTTP 200 or `router.timeout_s` passes. After a successful `router_restart` hook, the overlay governs the session even if readiness then fails.

| Status | Meaning                                                                                                     |
| ------ | ----------------------------------------------------------------------------------------------------------- |
| 200    | The hook succeeded and the router answered `GET /ready` with HTTP 200                                       |
| 409    | No session is active, or an exclusive action is in progress                                                 |
| 422    | Overlay only: the body is not a JSON object, changes nothing, changes another section, or fails the loader |
| 501    | The `router_restart` or `cold_restart` hook is not configured                                               |
| 502    | The hook failed to start, exited non-zero or timed out                                                      |
| 504    | The router was not ready within `router.timeout_s`                                                          |

## Responses and the action log

Every API route needs the token. A request without a valid token receives HTTP 401 with `WWW-Authenticate: Bearer` and the body `{"detail": "missing or invalid bearer token"}`.

A successful action returns its run-record entry. A refused or failed action returns `{"detail": "<reason>", "action": <entry>}`. A malformed request body is refused with HTTP 422 and a `detail` alone, and no action is recorded. An unexpected service error returns HTTP 500 and records the action as failed.

The **Action log** section lists the active session's actions from its run record, newest first, with each result available under **view**. **Responses in this tab** lists the HTTP status and reason of each request made from this browser tab, including refusals recorded outside a session.

## Run record

The service writes every record under `runs_dir`, default `runs/fleet-control/`, which Git ignores. It creates directories with mode `0700` and files with mode `0600`.

```text
runs/fleet-control/
  actions.jsonl                  every authenticated action, one JSON object per line
  sessions/
    <session>/                   <UTC start, YYYYMMDDTHHMMSSZ>-<six hex characters>
      run.json                   the run record, rewritten after each action
      baseline.json              copy of the baseline fleet configuration
      hooks/<nnn>-<hook>.log     output of each hook run, numbered from 001
      overlays/<nnn>-fleet.json  merged fleet configuration of each applied overlay
      jobs/job-<nnn>/
        aiperf.log               AIPerf output
        aiperf/                  AIPerf artifact directory
```

`actions.jsonl` also holds actions refused outside a session, with `seq` and `session` set to `null`.

`run.json` has these fields:

| Field            | Meaning                                                                                             |
| ---------------- | --------------------------------------------------------------------------------------------------- |
| `schema`         | `narwhal.fleet-control-run`                                                                         |
| `schema_version` | `1`                                                                                                 |
| `session`        | Session ID                                                                                          |
| `started_at`     | Session start, ISO 8601 UTC                                                                         |
| `ended_at`       | Time the restore completed while ending the session, or `null`                                      |
| `baseline`       | `source`, the baseline file the service read, and `copy`, `baseline.json`                           |
| `configuration`  | The configuration that currently governs the session, the last entry of `configurations`           |
| `configurations` | Every applied configuration in order                                                                |
| `actions`        | Every action of the session in order                                                                |

Each `configurations` entry holds:

| Field        | Meaning                                                    |
| ------------ | ---------------------------------------------------------- |
| `applied_at` | Time the configuration took effect                         |
| `source`     | `baseline` or `overlay`                                    |
| `fleet`      | The configuration's file, relative to the session directory |
| `digest`     | Canonical SHA-256 digest of the document                   |
| `document`   | The full fleet configuration                               |

Each `actions` entry, and each line of `actions.jsonl`, holds:

| Field         | Meaning                                                      |
| ------------- | ------------------------------------------------------------ |
| `seq`         | Position in the session from `1`, or `null` outside a session |
| `session`     | Session ID, or `null`                                        |
| `action`      | Action name                                                  |
| `params`      | Request parameters                                           |
| `started_at`  | Start time, ISO 8601 UTC                                     |
| `finished_at` | Finish time, ISO 8601 UTC                                    |
| `outcome`     | `ok`, `refused` or `failed`                                  |
| `result`      | The action's effect, or `null`                               |
| `error`       | Reason for a refused or failed action, or `null`             |

The service records these action names:

| Action                                                     | `result`                                                                                                   |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `session.start`                                            | `session` and `baseline_digest`                                                                            |
| `session.end`                                              | `restore_seq`, the `seq` of the restore it ran                                                             |
| `baseline.restore`                                         | The hook run                                                                                               |
| `engine.pause`, `engine.resume`, `engine.stop`, `engine.start` | `engine`, `before`, `hook` and `after`                                                                 |
| `engine.drain`, `engine.readmit`                           | `engine`, `before`, `router` and `after`; `router` holds the lifecycle call's `path`, `body`, `status` and `error` |
| `job.start`, `job.stop`                                    | `job`, the job document                                                                                    |
| `job.complete`                                             | The job document                                                                                           |
| `config.overlay`                                           | `fleet`, `digest`, `base_digest`, `hook` and `readiness`                                                   |
| `config.cold_restart`                                      | `fleet`, `digest`, `hook` and `readiness`                                                                  |
| `config.restore`                                           | `restore_seq`, `fleet`, `digest` and `readiness`                                                           |

A hook run holds `hook`, `argv`, `exit_code`, `timed_out`, `duration_s`, `log` relative to the session directory, and `tail`, the last 4096 bytes of its output. `readiness` holds the readiness `path`, `ready`, `attempts`, `waited_s`, the last `status_code` and the router's stated `reason`.

The run record does not include an extract of the router's request journal. Retain the router journal for the session's time range separately, as described in [Request journal](../telemetry/01-Journal.md).
