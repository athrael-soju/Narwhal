# `narwhal diagnostics`

`narwhal diagnostics collect` sends GET requests to a router's `/health`, `/ready`, `/narwhal/state`, `/narwhal/lifecycle`, and `/metrics`. It writes each response and selected local artifact to a fresh private directory.

Collect one router's bundle:

```bash
mkdir -p runs/diagnostics
narwhal diagnostics collect \
  --router http://127.0.0.1:8000 \
  --fleet config/fleet.json \
  --run runs/dev/run-example \
  --out runs/diagnostics/incident-001 \
  --format json
```

Output location:

1. Create the parent directory.
2. Give each router its own fresh `--out` path.

| Path             | Mode   |
| ---------------- | ------ |
| Bundle directory | `0700` |
| Bundle files     | `0600` |

## Source selection

| Option                      | Default           | Description                                                                                             |
| --------------------------- | ----------------- | ------------------------------------------------------------------------------------------------------- |
| `--router URL`              | required          | HTTP or HTTPS base URL of the router, with a host, port, and optional path.                             |
| `--out PATH`                | required          | Fresh bundle directory inside an existing parent.                                                       |
| `--fleet PATH`              | optional          | Fleet file to include.                                                                                  |
| `--instance PATH`           | optional          | Dev instance directory to include, with its current run.                                                |
| `--run PATH`                | optional          | Existing run directory to include.                                                                      |
| `--artifact PATH`           | optional          | Additional regular file to include; repeatable.                                                         |
| `--source-timeout SECONDS`  | `5`               | Deadline for each source.                                                                               |
| `--timeout SECONDS`         | `30`              | Time budget for the whole collection.                                                                   |
| `--max-source-bytes BYTES`  | `8388608` (8 MiB) | Maximum input bytes retained per source.                                                                |
| `--include-request-content` | `false`           | Include journal and completion artifacts and request fields.                                            |
| `--format text\|json`       | `text`            | Output format, either a `text` summary or `json` for the [command result contract](Command-Results.md). |

Pass at most one of `--run` and `--instance`.

| Source                | Selected by                 | Included files                                                                                                                                                                                                                          |
| --------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dev instance          | `--instance`                | The instance's `instance.json`, `lifecycle.json`, and `fleet.json`, plus the run its lifecycle state records. That run must be inside the instance directory.                                                                          |
| Run directory         | `--run`                     | `fleet.json`, `profiles.json`, `teardown.json`, `router-state.json`, command and stage records, process logs, stage stdout/stderr files, and the JSON, log, and stage output files directly under `engine-*` and `verify-*` directories. |
| Request content       | `--include-request-content` | `journal.jsonl` and the completion artifacts.                                                                                                                                                                                           |
| Files kept elsewhere  | `--artifact`                | Supervisor status, ingress logs, or deployment evidence. Each must be a regular file on a path free of symbolic links.                                                                                                                  |

Source limits:

| Case                                                | Result                                                                                                                  |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| A source exceeds `--max-source-bytes`               | The first `--max-source-bytes` bytes are kept, the source is marked `truncated`, and collection continues within the overall budget. |
| An endpoint stalls after its headers or first bytes | The deadline covers response streaming. The row keeps the HTTP status and the body received so far.                     |
| A local file read                                   | The deadline is checked between bounded reads.                                                                          |

Use local files for incident collection.

## Manifest and exit status

`manifest.json` uses `narwhal.diagnostic-bundle` version 1.

| Scope                | Recorded fields                                                                                                                   |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Manifest             | Collection start and finish timestamps, elapsed time, package version, package source digest, inclusion policy, and one row per source |
| Every source row     | Source path or URL, collection timestamp, outcome, and error                                                                      |
| HTTP rows            | Status and content type                                                                                                           |
| Exported files       | Byte count and SHA-256 hash                                                                                                       |
| Complete local reads | Original file hash and modification timestamp                                                                                     |

Generated filenames are a four-digit source index plus a fixed label, such as `0003-artifact.json` or `0007-metrics.txt`.

| Manifest `status` | Condition                                                                         |
| ----------------- | --------------------------------------------------------------------------------- |
| `success`         | Every selected source was collected.                                              |
| `partial`         | A source failed, timed out, exceeded its byte limit, or was excluded by policy.   |

| Write failure  | Result                                                                                           |
| -------------- | ------------------------------------------------------------------------------------------------ |
| Artifact write | A `write_error` row. Collection continues with the remaining sources and the manifest.           |
| Manifest write | The command's I/O error status.                                                                  |

| Exit status | Operator action                                                   |
| ----------- | ----------------------------------------------------------------- |
| `0`         | Inspect the completed bundle.                                     |
| `2`         | Correct arguments, the output path, or required directory access. |
| `3`         | Inspect individual source outcomes in the partial bundle.         |
| `4`         | Inspect the reported I/O failure and retained output directory.   |

JSON command results report the bundle path, manifest path, collection status, and source count. The manifest's artifact entry records whether this invocation created it. Exit `3` maps to command status `degraded` and error code `collection_partial`.

## Content policy

| Content                                                                                                                                              | Default      | With `--include-request-content` |
| ---------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ | -------------------------------- |
| `journal.jsonl` and `completion.json`                                                                                                                | Excluded     | Included                         |
| Structured fields named `messages`, `prompt`, `content`, `completion`, `request_body`, `response_body`, `text`, `input`, or `output`                  | `[REDACTED]` | Included                         |
| Credentials                                                                                                                                          | Redacted     | Redacted                         |

Credential redaction covers the values in:

- recognised credential fields, authorization headers, URL credentials, credential query parameters, and launch arguments;
- `*_env` references in selected JSON sources (exported JSON keeps the reference names);
- environment variables whose names identify credentials.

Explicitly selected credential files get an `excluded` outcome: `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `authorized_keys`, `credentials`, and `credentials.json`.

| Export                              | Redaction                                                                                                                                    |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| JSON and JSONL                      | Structured fields stay intact after redaction.                                                                                               |
| Incomplete JSON and free-text logs  | A labelled credential or request field ends the retained text at that point, including multiline and truncated values. Unlabelled operational lines stay. |

When credentials or request content use an application-specific encoding, supply free-text logs filtered by site tooling.

## Manual collection

When the collector's installed command is unavailable, use the [incident capture commands](Troubleshoot.md#capture-router-and-engine-state):

- Keep each HTTP status with its response.
- Use a separate private directory per router.
