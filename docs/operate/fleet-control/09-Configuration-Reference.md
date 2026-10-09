---
description: Every key in the fleet control service's private configuration, the hook contract, and the environment each hook command receives.
---

# Configuration reference

The fleet control service reads its private configuration from `config/fleet-control.local.json`, or from the file named by `--config` or `NARWHAL_CONTROL_CONFIG`. [Setting up the control service](01-Set-Up-the-Service.md) shows how to create it.

The service validates the whole file at startup and reports every problem in one message. It rejects unknown keys at every level. Relative paths resolve against the directory where the service starts.

## Top-level keys

| Key              | Default                  | Meaning                                                                                             |
| ---------------- | ------------------------ | --------------------------------------------------------------------------------------------------- |
| `host`           | `127.0.0.1`              | Listener address. Must be a loopback address or `localhost`                                         |
| `port`           | `8020`                   | Listener port, `1` through `65535`                                                                  |
| `token_env`      | `NARWHAL_CONTROL_TOKEN`  | Environment variable that holds the bearer token                                                    |
| `runs_dir`       | `runs/fleet-control`     | Directory for the action log and session run records                                                |
| `fleet`          | `NARWHAL_FLEET` when set | Baseline fleet configuration file. Required when `NARWHAL_FLEET` is unset                           |
| `hooks`          | none; required           | Hook commands by name. Must include `restore`                                                       |
| `router`         | see [Router](#router)    | The router whose state, lifecycle and readiness routes the service calls                            |
| `prometheus_url` | absent                   | Prometheus base URL, `http` or `https`, that the service asks for firing alerts                     |
| `load`           | absent                   | AIPerf settings and the workload library. Without it, load-job routes refuse requests or are absent |
| `console`        | absent                   | Grafana settings for the console. Without it, the console shows no charts or dashboard links        |

The bearer token in `token_env` must be at least 32 characters long, without leading or trailing whitespace.

## Hooks

Each entry under `hooks` names one command:

| Key         | Default | Meaning                                            |
| ----------- | ------- | -------------------------------------------------- |
| `argv`      | none    | Command and arguments, a non-empty list of strings |
| `timeout_s` | `900`   | Seconds before the service stops the command       |

The service runs these hooks:

| Hook             | Runs for                                 | Must                                                                                        |
| ---------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------- |
| `engine_pause`   | Engine pause                             | Pause the engine named by `NARWHAL_CONTROL_ENGINE`                                          |
| `engine_resume`  | Engine resume, restoring the baseline    | Resume that engine                                                                          |
| `engine_stop`    | Engine stop                              | Stop that engine                                                                            |
| `engine_start`   | Engine start, restoring the baseline     | Start that engine                                                                           |
| `router_restart` | Overlay, restoring the baseline          | Restart the router with the fleet configuration at `NARWHAL_CONTROL_FLEET`                  |
| `cold_restart`   | Cold restart                             | Restart every engine and the router with the fleet configuration at `NARWHAL_CONTROL_FLEET` |

### How hooks run

A hook succeeds when it exits `0` within its timeout. The service runs each hook in its own process group with standard input closed, and writes the hook's standard output and standard error to a log in the session directory. At the timeout, the service sends SIGTERM to the process group, then SIGKILL 5 seconds later.

Each hook inherits the service's environment, minus the bearer token variable, plus these variables:

| Variable                     | Set for                          | Value                                                                      |
| ---------------------------- | -------------------------------- | -------------------------------------------------------------------------- |
| `NARWHAL_CONTROL_SESSION`    | Every hook                       | Session ID                                                                 |
| `NARWHAL_CONTROL_RUN_DIR`    | Every hook                       | Absolute path of the session directory                                     |
| `NARWHAL_CONTROL_BASELINE`   | Every hook                       | Absolute path of the session's copy of the baseline fleet configuration    |
| `NARWHAL_CONTROL_ACTION`     | Engine hooks                     | `pause`, `resume`, `stop` or `start`                                       |
| `NARWHAL_CONTROL_ENGINE`     | Engine hooks                     | Engine ID                                                                  |
| `NARWHAL_CONTROL_ENGINE_URL` | Engine hooks                     | The engine's `url` from the baseline fleet configuration, or empty         |
| `NARWHAL_CONTROL_FLEET`      | `router_restart`, `cold_restart` | Absolute path of the fleet configuration the restarted processes must load |

## Router

| Key                        | Default                 | Meaning                                                                                                                                   |
| -------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `router.url`               | `http://127.0.0.1:8000` | Router base URL, `http` or `https`. AIPerf load jobs also send their requests here                                                        |
| `router.timeout_s`         | `600`                   | Limit for lifecycle calls, for the readiness wait after a restart or restore, and for an engine's return to service after start or resume |
| `router.journal`           | absent                  | Absolute path of the router's request journal on this host. The file need not exist at startup                                            |
| `router.journal_max_bytes` | `268435456` (256 MiB)   | Largest journal extract copied for one session or load job, a positive number of bytes                                                    |

Engine state reads use the shorter of 10 seconds and `router.timeout_s`.

Set `router.journal` to the path the router writes: the value of `narwhal-serve --journal`, or by default `journal.jsonl` beside `profiles.path`. Sessions and load jobs record a [journal extract](11-Run-Record.md#journal-extracts) only when `router.journal` is set.

## Load

| Key                   | Default | Meaning                                                             |
| --------------------- | ------- | ------------------------------------------------------------------- |
| `load.aiperf`         | none    | AIPerf executable                                                   |
| `load.model`          | none    | Served model name that AIPerf requests                              |
| `load.tokenizer`      | none    | Tokenizer name or path for AIPerf                                   |
| `load.endpoint_type`  | `chat`  | `chat` or `completions`                                             |
| `load.streaming`      | `true`  | Whether AIPerf streams responses                                    |
| `load.goodput`        | `{}`    | AIPerf metric tag mapped to its positive goodput threshold          |
| `load.grace_period_s` | none    | Seconds AIPerf waits for in-flight responses after a timed job ends |
| `load.extra_args`     | `[]`    | Further arguments appended to the AIPerf command                    |
| `load.workloads`      | none    | The workload library: at least one named workload                   |

### Workloads

Workload names use letters, digits, `.`, `_` and `-`, up to 64 characters. Each workload has a `kind`:

| Kind                | Requests                                                                                                              |
| ------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `synthetic`         | AIPerf generates prompts with one input and output length distribution                                                |
| `mixed`             | AIPerf generates prompts in several sizes, each with a share of the requests                                          |
| `multi_turn`        | AIPerf generates conversations. Each turn resends the conversation so far                                             |
| `public_dataset`    | AIPerf sends recorded chat prompts from a public dataset                                                              |
| `timestamped_trace` | AIPerf replays a `mooncake_trace` JSONL file at its recorded timestamps                                               |
| `prefix_trace`      | AIPerf replays a `mooncake_trace` JSONL file whose records share prompt prefixes through `hash_ids`, paced by the job |

Every kind accepts these keys:

| Key              | Default           | Meaning                                                                                                                                                                         |
| ---------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `label`          | the workload name | Text in the console's workload list, at most 60 characters                                                                                                                      |
| `description`    | none              | Text in the console hint, at most 300 characters. The hint summarizes the workload's settings in its place                                                                      |
| `ignore_eos`     | `false`           | When `true`, each request sets `ignore_eos`, and the engine generates the full requested output length                                                                          |
| `cache_bust`     | none              | Adds a marker unique to each session at `system_prefix`, `system_suffix`, `first_turn_prefix` or `first_turn_suffix`. A prefix marker stops prefix-cache reuse between sessions |
| `cancel_percent` | none              | Percentage of requests that the client cancels, above 0 and at most 100                                                                                                         |
| `cancel_after_s` | `0`               | Seconds after sending at which the client cancels a request. Requires `cancel_percent`                                                                                          |

The other keys depend on the kind:

| Key                    | Kinds                               | Default  | Meaning                                                         |
| ---------------------- | ----------------------------------- | -------- | --------------------------------------------------------------- |
| `isl`                  | `synthetic`, `multi_turn`           | required | Mean input tokens of each request or turn                       |
| `osl`                  | `synthetic`, `multi_turn`           | required | Mean output tokens requested by each request or turn            |
| `isl_stddev`           | `synthetic`, `multi_turn`           | `0`      | Standard deviation of the input tokens                          |
| `osl_stddev`           | `synthetic`, `multi_turn`           | `0`      | Standard deviation of the output tokens                         |
| `mix`                  | `mixed`                             | required | Request sizes, described below                                  |
| `turns`                | `multi_turn`                        | required | Mean turns per conversation, an integer of at least 2           |
| `turns_stddev`         | `multi_turn`                        | `0`      | Standard deviation of the turns per conversation                |
| `turn_delay_s`         | `multi_turn`                        | `0`      | Mean seconds between turns                                      |
| `turn_delay_stddev_s`  | `multi_turn`                        | `0`      | Standard deviation of the seconds between turns                 |
| `dataset`              | `public_dataset`                    | required | `sharegpt`                                                      |
| `file`                 | `timestamped_trace`, `prefix_trace` | required | Path of a `mooncake_trace` JSONL file                           |
| `block_size`           | `timestamped_trace`, `prefix_trace` | none     | Tokens each hash ID stands for. Required for `prefix_trace`     |
| `system_prompt_tokens` | `synthetic`, `mixed`, `multi_turn`  | none     | Length of one system prompt that every request shares           |
| `prefix_prompts`       | `synthetic`, `mixed`, `multi_turn`  | none     | Number of shared prefixes. Each request starts with one of them |
| `prefix_tokens`        | `synthetic`, `mixed`, `multi_turn`  | none     | Length of each shared prefix                                    |

Each `mix` entry holds:

| Key          | Default  | Meaning                                        |
| ------------ | -------- | ---------------------------------------------- |
| `isl`        | required | Mean input tokens                              |
| `osl`        | required | Mean output tokens requested                   |
| `isl_stddev` | `0`      | Standard deviation of the input tokens         |
| `osl_stddev` | `0`      | Standard deviation of the output tokens        |
| `percent`    | required | Share of the requests, above 0 and at most 100 |

The service checks these rules when it starts:

- The `percent` values of a `mix` add up to 100.
- `prefix_prompts` and `prefix_tokens` appear together.
- A workload sets `system_prompt_tokens` or `prefix_prompts`, but not both.
- With `load.endpoint_type` set to `completions`, the library holds no `multi_turn` or `public_dataset` workload and no workload that sets `system_prompt_tokens` or `cache_bust`. AIPerf sends those through chat messages.
- When a workload sets `ignore_eos`, `load.extra_args` leaves out `--extra-inputs`.

Each input and output length must fit the served model's context length.

AIPerf downloads the ShareGPT dataset from Hugging Face on the first `public_dataset` job and keeps it in `.cache/aiperf/` under the service's working directory. Later jobs reuse that copy.

`aiperf synthesize agentic-code` writes a `mooncake_trace` file of coding-agent sessions. Use it as a `prefix_trace` workload with `block_size` 512.

[Start a job](06-Load-Jobs.md#start-a-job) lists the job inputs each kind accepts.

## Console

| Key                        | Default          | Meaning                                                                                                                                          |
| -------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `console.grafana_url`      | none; required   | Grafana base URL as the operator's browser reaches it, without credentials, query or fragment                                                    |
| `console.dashboard_uid`    | `narwhal-router` | Dashboard UID for embedded panels and dashboard links                                                                                         |
| `console.panels`           | none             | Non-empty list of distinct panel IDs that the standalone console embeds, in display order                                                        |
| `console.from`             | `now-15m`        | Panel time range start, `now` or `now-<n><unit>` with unit `s`, `m`, `h`, `d`, `w`, `M` or `y`                                                   |
| `console.refresh`          | `5s`             | Panel refresh interval, `<n><unit>` with unit `s`, `m`, `h` or `d`                                                                               |
| `console.embed_in_grafana` | `false`          | Whether pages from the `console.grafana_url` origin and the console's own origin may frame the console. See [Framing boundary](03-Grafana-Dashboard.md#framing-boundary)      |
| `console.auto_connect`     | `false`          | Whether the console page carries the token and connects on load. See [Connecting automatically](02-Open-the-Console.md#connecting-automatically) |
| `console.trusted_hosts`    | none             | Host names or IPv4 addresses, besides loopback, for which the console page carries the token. Requires `console.auto_connect` |

`console.grafana_url` is Grafana's address as your workstation browser sees it, usually the tunnel's local Grafana port such as `http://127.0.0.1:13000`.

The example configuration embeds the shipped dashboard's **Requests**, **Latency**, **Time to first token**, **Time per output token**, **Engine role history** and **Fleet events** panels. [Reading the dashboard](../../observability/05-Dashboard.md) describes each panel. If those panels show as empty frames, see [Charts in the standalone console are empty frames](08-Troubleshooting.md#charts-in-the-standalone-console-are-empty-frames).

## Environment variables

| Variable                      | Read by                     | Meaning                                                                                                                      |
| ----------------------------- | --------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `NARWHAL_CONTROL_CONFIG`      | The service                 | Configuration file when `--config` is not given                                                                              |
| `NARWHAL_FLEET`               | The service                 | Baseline fleet configuration file when `fleet` is unset                                                                      |
| `NARWHAL_CONTROL_TOKEN`       | The service, `make observe` | Bearer token. The service reads the variable that `token_env` names; `make observe` always reads `NARWHAL_CONTROL_TOKEN`     |
| `NARWHAL_CONTROL_CONSOLE_URL` | `make observe`              | Console address that the dashboard's **Fleet control** link opens, when it differs from `http://127.0.0.1:18020/console`     |
| `NARWHAL_CONTROL_METRICS_URL` | `make observe`              | Control service address as Prometheus reaches it. See [Dashboard annotations](03-Grafana-Dashboard.md#dashboard-annotations) |
