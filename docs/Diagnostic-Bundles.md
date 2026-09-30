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

| Path | Mode |
| --- | :---: |
| Bundle directory | `0700` |
| Bundle files | `0600` |

## Source selection

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

| Source | Selected by | Included files |
| --- | --- | --- |
| Dev instance | `--instance` | The instance's `instance.json`, `lifecycle.json`, and `fleet.json`, plus the run named in its lifecycle state, inside the instance directory |
| Run directory | `--run` | `fleet.json`, `profiles.json`, `teardown.json`, `router-state.json`, command and stage records, process logs, stage stdout/stderr files, and the JSON, log, and stage output files directly under `engine-*` and `verify-*` directories |
| Request content | `--include-request-content` | `journal.jsonl` and the completion artifacts |
| Files kept elsewhere | `--artifact` | Regular files on symlink-free paths, such as supervisor status, ingress logs, or deployment evidence |

| Case | Result |
| --- | --- |
| A source exceeds `--max-source-bytes` | The source is marked `truncated`. |
| Content of a truncated source | The first `--max-source-bytes` bytes. |
| An endpoint stalls after its headers or first bytes | The row records the HTTP status and the body received before the deadline. |
| A local file read | The source deadline applies between reads. |

## Manifest and exit status

`manifest.json` uses `narwhal.diagnostic-bundle` version 1.

| Scope | Recorded fields |
| --- | --- |
| Manifest | Collection start and finish timestamps, elapsed time, package version, package source digest, inclusion policy, and one row per source |
| Every source row | Source path or URL, collection timestamp, outcome, and error |
| HTTP rows | Status and content type |
| Exported files | Byte count and SHA-256 hash |
| Complete local reads | Original file hash and modification timestamp |

Generated filenames are a four-digit source index plus a fixed label, such as `0003-artifact.json` or `0007-metrics.txt`.

| Manifest `status` | Condition |
| --- | --- |
| `success` | Every selected source was collected. |
| `partial` | A source failed, timed out, exceeded its byte limit, or was excluded by policy. |

| Write failure | Result |
| --- | --- |
| Artifact file | The source row records `write_error`. |
| Artifact file | Collection continues with the remaining sources and the manifest. |
| Manifest file | Exit status `4`. |

| Exit status | Operator action |
| :---: | --- |
| `0` | Inspect the completed bundle. |
| `2` | Correct the arguments or the output path. |
| `3` | Inspect individual source outcomes in the partial bundle. |
| `4` | Inspect the reported I/O failure and the retained output directory. |

JSON command results:

- `data` holds `bundle`, `manifest`, `collection_status`, and the `sources` count.
- Exit `3` maps to command status `degraded` and error code `collection_partial`.

## Content policy

| Content | Default | With `--include-request-content` |
| --- | --- | --- |
| `journal.jsonl` and `completion.json` | Excluded | Included |
| Structured fields named `messages`, `prompt`, `content`, `completion`, `request_body`, `response_body`, `text`, `input`, or `output` | `[REDACTED]` | Included |
| Credentials | Redacted | Redacted |

Credential redaction covers the values in:

- recognised credential fields, authorization headers, URL credentials, credential query parameters, and launch arguments
- `*_env` references in selected JSON sources
- environment variables whose names identify credentials

Selected credential files get an `excluded` outcome: `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `authorized_keys`, `credentials`, and `credentials.json`.

| Export | Redaction |
| --- | --- |
| JSON and JSONL | Field-level redaction that keeps the document structure and `*_env` reference names. |
| Incomplete JSON and free-text logs | Retained text ends at the first labelled credential or request field, including multiline and truncated values. |

Filter free-text logs with site tooling when they hold credentials or request content in a custom encoding.

## Manual collection

When the installed collector is unavailable, use the [incident capture commands](Troubleshoot.md#capture-router-and-engine-state):

- Keep each HTTP status with its response.
- Use a separate private directory per router.
