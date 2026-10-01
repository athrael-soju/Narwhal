---
description: Collect a private bundle of Narwhal router state and local artifacts with narwhal diagnostics.
---

# `narwhal diagnostics`

`narwhal diagnostics collect` writes a router's `/health`, `/ready`, `/narwhal/state`, `/narwhal/lifecycle`, and `/metrics` GET responses and selected local artifacts to a fresh private directory.

1. Create the parent directory.
2. Run the collector with a fresh `--out` path per router.

```bash
mkdir -p runs/diagnostics
narwhal diagnostics collect \
  --router http://127.0.0.1:8000 \
  --fleet config/fleet.json \
  --run runs/dev/run-example \
  --out runs/diagnostics/incident-001 \
  --format json
```

The collector creates the bundle directory with mode `0700` and the bundle files with mode `0600`.

## Source selection

The options name the router, the local sources to include, and the collection limits.

| Option | Default | Description |
| --- | --- | --- |
| `--router URL` | required | Router base URL (HTTP or HTTPS) with host, optional port, and optional path. |
| `--out PATH` | required | Fresh bundle directory inside an existing parent. |
| `--fleet PATH` | optional | Fleet file to include. |
| `--instance PATH` | optional | Dev instance directory with its current run, mutually exclusive with `--run`. |
| `--run PATH` | optional | Existing run directory to include, mutually exclusive with `--instance`. |
| `--artifact PATH` | optional | Additional regular file to include, repeatable. |
| `--source-timeout SECONDS` | `5` | Deadline for each source. |
| `--timeout SECONDS` | `30` | Time budget for the whole collection. |
| `--max-source-bytes BYTES` | `8388608` (8 MiB) | Maximum input bytes retained per source. |
| `--include-request-content` | `false` | Include journal and completion artifacts and request fields. |
| `--format text\|json` | `text` | Output format: `text` summary or `json` per the [command result contract](Command-Results.md). |

Each source adds these files to the bundle:

| Source | Selected by | Included files |
| --- | --- | --- |
| Dev instance | `--instance` | The instance's `instance.json`, `lifecycle.json`, and `fleet.json`, plus the run named in its lifecycle state, inside the instance directory |
| Run directory | `--run` | `fleet.json`, `profiles.json`, `teardown.json`, `router-state.json`, command and stage records, process logs, stage stdout/stderr files, and the JSON, log, and stage output files directly under `engine-*` and `verify-*` directories |
| Request content | `--include-request-content` | `journal.jsonl` and the completion artifacts |
| Files kept elsewhere | `--artifact` | Regular files on symlink-free paths, such as supervisor status, ingress logs, or deployment evidence |

A source that exceeds `--max-source-bytes` is marked `truncated` and keeps its first `--max-source-bytes` bytes.

When an endpoint stalls after its headers or first bytes, its row records the HTTP status and the body received before the deadline. For a local file, the source deadline applies between reads.

## Manifest and exit status

`manifest.json` uses `narwhal.diagnostic-bundle` version 1. It records the collection start and finish timestamps, elapsed time, package version, package source digest, inclusion policy, and one row per source.

Each source row records the source path or URL, collection timestamp, outcome, and error. HTTP rows add the status and content type. Exported files add the byte count and SHA-256 hash, and complete local reads add the original file hash and modification timestamp.

Generated filenames are a four-digit source index plus a fixed label, such as `0003-artifact.json` or `0007-metrics.txt`.

The manifest `status` is `success` when the collector collects every selected source. It is `partial` when a source fails, times out, exceeds its byte limit, or is excluded by policy.

When an artifact file write fails, its source row records `write_error`, and collection continues with the remaining sources and the manifest. A manifest file write failure exits with status `4`.

| Exit status | Operator action |
| :---: | --- |
| `0` | Inspect the completed bundle. |
| `2` | Correct the arguments or the output path. |
| `3` | Inspect individual source outcomes in the partial bundle. |
| `4` | Inspect the reported I/O failure and the retained output directory. |

The [command result contract](Command-Results.md#result-fields) defines the JSON `data` and the status for each exit code.

## Content policy

By default, the bundle excludes `journal.jsonl` and `completion.json`. It replaces structured fields named `messages`, `prompt`, `content`, `completion`, `request_body`, `response_body`, `text`, `input`, or `output` with `[REDACTED]`. `--include-request-content` includes both the files and the fields.

In either mode, credential redaction covers the values in:

- recognised credential fields, authorization headers, URL credentials, credential query parameters, and launch arguments
- `*_env` references in selected JSON sources
- environment variables whose names identify credentials

Selected credential files get an `excluded` outcome: `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `authorized_keys`, `credentials`, and `credentials.json`.

JSON and JSONL exports get field-level redaction that keeps the document structure and `*_env` reference names. In incomplete JSON and free-text logs, retained text ends at the first labelled credential or request field, including multiline and truncated values.

Filter free-text logs with site tooling when they hold credentials or request content in a custom encoding.

## Manual collection

When the installed collector is unavailable, use the [incident capture commands](Troubleshoot.md#capturing-router-and-engine-state):

- Keep each HTTP status with its response.
- Use a separate private directory per router.
