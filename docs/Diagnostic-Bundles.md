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

Make sure the parent directory exists before you run the collector, and give each router its own `--out` path that doesn't exist yet. The collector creates the bundle directory itself (mode `0700`) and writes its files as `0600`. If the output path is already there, it stops before collecting anything. Relative paths are resolved against the current working directory.

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

Pass at most one of `--run` and `--instance`. The `--instance` option includes the instance's `instance.json`, `lifecycle.json`, and `fleet.json` along with the run recorded by its lifecycle state, and that run must live inside the instance directory. Selecting a run brings in `fleet.json`, `profiles.json`, `teardown.json`, `router-state.json`, command and stage records, process logs, and stage stdout/stderr files, plus any JSON, log, or stage output files directly under `engine-*` and `verify-*` directories. Adding `--include-request-content` pulls in `journal.jsonl` and the completion artifacts as well. For supervisor status, ingress logs, or deployment evidence kept elsewhere, pass them with `--artifact`, which requires regular files and rejects symbolic links in the selected path.

When a source exceeds the byte limit, the collector retains the first portion up to `--max-source-bytes`, marks the source `truncated`, and carries on within the remaining overall budget. Endpoint deadlines cover response streaming, so a response that stalls after its headers or first bytes still keeps its HTTP status and whatever body has arrived. Local file reads check the deadline between bounded reads, and because filesystem operations inherit their mount's I/O behaviour, local files remain the right choice for incident collection.

## Manifest and exit status

`manifest.json` uses `narwhal.diagnostic-bundle` version 1 and records the collection start and finish timestamps, elapsed time, package version, package source digest, inclusion policy, and one row per source. Each row identifies the source path or URL, its collection timestamp, outcome, and any error. HTTP rows keep the status and content type, while exported files carry byte counts and SHA-256 hashes. Complete local reads additionally record the original file hash and modification timestamp. Generated filenames combine a four-digit source index with a fixed label, such as `0003-artifact.json` or `0007-metrics.txt`, so that source names and credential values never appear in output paths.

The manifest's `status` reads `success` when every selected source was collected and `partial` when a source failed, timed out, exceeded its byte limit, or was excluded by policy. A failed artifact write contributes a `write_error` row while collection proceeds to the remaining sources and the manifest itself, whereas a failure to write the manifest returns the command's I/O error status.

| Exit status | Operator action                                                   |
| ----------- | ----------------------------------------------------------------- |
| `0`         | Inspect the completed bundle.                                     |
| `2`         | Correct arguments, the output path, or required directory access. |
| `3`         | Inspect individual source outcomes in the partial bundle.         |
| `4`         | Inspect the reported I/O failure and retained output directory.   |

JSON command results report the bundle path, manifest path, collection status, and source count, and their artifact entry tracks whether this invocation created the manifest. Exit `3` maps to command status `degraded` and error code `collection_partial`.

## Content policy

Default exports exclude `journal.jsonl` and `completion.json`, and structured fields named `messages`, `prompt`, `content`, `completion`, `request_body`, `response_body`, `text`, `input`, or `output` become `[REDACTED]`. Passing `--include-request-content` enables these artifacts and fields while credential redaction stays active in both modes.

Credential values are removed from recognised credential fields, authorization headers, URL credentials, credential query parameters, and launch arguments. The collector resolves `*_env` references from selected JSON sources only to strip those values, retaining the reference names in exported JSON, and it also scrubs the values of environment variables whose names identify credentials. Credential files (`.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `authorized_keys`, `credentials`, and `credentials.json`) receive an `excluded` outcome when explicitly selected.

JSON and JSONL exports preserve their structured fields after redaction. For incomplete JSON or free-text logs, a labelled credential or request field ends the retained text at that point, covering multiline and truncated values, while unlabelled operational lines remain in the export. Site tooling supplies filtered free-text logs when credentials or request content use an application-specific encoding.

## Manual collection

Use the [incident capture commands](Troubleshoot.md#capture-router-and-engine-state) when the collector's installed command is unavailable. Preserve each HTTP status alongside its response and use a separate private directory per router.
