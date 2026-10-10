---
description: Files, `run.json`, action names, and router journal extracts written by a fleet control session.
---

# Run record

The service writes every record under `runs_dir`, `runs/fleet-control/` by default. It creates directories with mode `0700` and files with mode `0600`.

## Directory layout

```text
runs/fleet-control/
  actions.jsonl                  every authenticated action, one JSON object per line
  sessions/
    <session>/                   <UTC start, YYYYMMDDTHHMMSSZ>-<six hex characters>
      run.json                   the run record, rewritten after each action
      baseline.json              copy of the baseline fleet configuration
      hooks/<nnn>-<hook>.log     output of each hook run, numbered from 001
      overlays/<nnn>-fleet.json  merged fleet configuration of each applied overlay
      journal/
        job-<nnn>.jsonl          router journal rows written during each load job
        session.jsonl            router journal rows written during the session
      jobs/job-<nnn>/
        aiperf.log               AIPerf output
        aiperf/                  AIPerf artifact directory
```

`actions.jsonl` also holds actions refused outside a session, with `seq` and `session` set to `null`.

## run.json

| Field            | Meaning                                                                                  |
| ---------------- | ---------------------------------------------------------------------------------------- |
| `schema`         | `narwhal.fleet-control-run`                                                              |
| `schema_version` | `1`                                                                                      |
| `session`        | Session ID                                                                               |
| `started_at`     | Session start, ISO 8601 UTC                                                              |
| `ended_at`       | Time the session closed after its journal extract, or `null`                             |
| `baseline`       | `source`, the baseline file the service read, and `copy`, `baseline.json`                |
| `configuration`  | The configuration that currently governs the session, the last entry of `configurations` |
| `configurations` | Every applied configuration in order                                                     |
| `actions`        | Every action of the session in order                                                     |

Each `configurations` entry holds:

- `applied_at`
- `source`: `baseline` or `overlay`
- `fleet`: the configuration's file, relative to the session directory
- `digest`: canonical SHA-256 of the document
- `document`: the full fleet configuration

## Action entries

Each `actions` entry, and each line of `actions.jsonl`, holds:

| Field         | Meaning                                                       |
| ------------- | ------------------------------------------------------------- |
| `seq`         | Position in the session from `1`, or `null` outside a session |
| `session`     | Session ID, or `null`                                         |
| `action`      | Action name                                                   |
| `params`      | Request parameters                                            |
| `started_at`  | Start time, ISO 8601 UTC                                      |
| `finished_at` | Finish time, ISO 8601 UTC                                     |
| `outcome`     | `ok`, `refused` or `failed`                                   |
| `result`      | The action's effect, or `null`                                |
| `error`       | Reason for a refused or failed action, or `null`              |

The `result` depends on the action:

| Action                                                         | `result`                                                                                   |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `session.start`                                                | `session`, `baseline_digest`                                                               |
| `session.end`                                                  | `changes` and `journal`                                                                    |
| `engine.pause`, `engine.stop`                                  | `engine`, `before`, `hook`, `after`                                                        |
| `engine.resume`, `engine.start`                                | `engine`, `before`, `hook`, `service` with `in_service` and `waited_s`, `after`            |
| `engine.drain`, `engine.readmit`                               | `engine`, `before`, `router` with the call's `path`, `body`, `status` and `error`, `after` |
| `job.start`, `job.stop`                                        | `job`, the job document                                                                    |
| `job.complete`                                                 | The job document                                                                           |
| `config.overlay`                                               | `fleet`, `digest`, `base_digest`, `hook`, `readiness`                                      |
| `config.cold_restart`                                          | `fleet`, `digest`, `hook`, `readiness`                                                     |
| `config.restore`                                               | `changes`, `steps`, and `hook` and `readiness` when the configuration changed              |

`changes` holds `configuration`, whether the configuration differs from the baseline, and `engines`, the changes each engine still carries: `paused` or `stopped`, then `drained`. Each `steps` entry names the `engine`, the `action` that undid one change and that action's `seq`, or `null` when the engine was already in service.

A hook run holds `hook`, `argv`, `exit_code`, `timed_out`, `duration_s`, `log`, and `tail`, the last 4096 bytes of output.

`readiness` holds `path`, `ready`, `attempts`, `waited_s`, `status_code` and `reason`.

## Journal extracts

Each load job and each session keeps a copy of the router journal rows written while it ran. Journal times are relative to the router process, so the service selects rows by file position:

1. When the run starts, the service records the journal's size.
2. When the run ends, it copies the bytes appended after that offset.

- Rows written after the copy starts are left out.
- An incomplete last row is left out.
- The copy stops before it would exceed `router.journal_max_bytes`.
- If the journal was replaced or truncated during the run, or was created during the run, the copy starts from the beginning of the current file.

If the router restarted during a session, the extract holds rows from more than one router process. Each row's `run` field names its process.

The service copies the extract while it handles the request, so a large extract delays other requests.

The `journal` entry in a job document or a `session.end` result has:

| Field          | Meaning                                                                                                            |
| -------------- | ------------------------------------------------------------------------------------------------------------------ |
| `extract`      | The extract file, or `null` when nothing was copied                                                                |
| `start_offset` | Journal byte offset where the copy started                                                                         |
| `end_offset`   | Journal byte offset after the last copied row                                                                      |
| `lines`        | Rows copied                                                                                                        |
| `bytes`        | Bytes copied                                                                                                       |
| `terminal`     | Count of finished requests by outcome, such as `completed` or `refused`                                            |
| `notes`        | Conditions that affected the copy, such as a missing or replaced journal, a dropped partial row, or the size limit |

[Request journal](../../telemetry/01-Journal.md) describes the journal rows.
