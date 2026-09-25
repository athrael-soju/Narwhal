# Collect diagnostic bundles

`narwhal diagnostics collect` reads a router's `/health`, `/ready`, `/narwhal/state`, `/narwhal/lifecycle` and `/metrics` through GET requests, then records each response and selected local artifact in a fresh private directory. Collection reads sources in place; site tooling owns router and engine supervision.

```bash
mkdir -p runs/diagnostics
narwhal diagnostics collect \
  --router http://127.0.0.1:8000 \
  --fleet config/fleet.json \
  --run runs/dev/run-example \
  --out runs/diagnostics/incident-001 \
  --format json
```

Create the parent directory first and choose a fresh `--out` path for each router. The collector creates the bundle directory with mode `0700` and its files with mode `0600`; an existing output path fails before collection starts. Relative paths resolve from the working directory.

## Source selection

| Option | Collection scope |
| --- | --- |
| `--router URL` | Required HTTP or HTTPS base URL; accepts a host, port and optional path. |
| `--out PATH` | Required fresh directory inside an existing parent. |
| `--fleet PATH` | Explicit fleet JSON. |
| `--instance PATH` | The selected dev instance's `instance.json`, `lifecycle.json` and `fleet.json`, plus the run recorded by its lifecycle state. That run must belong to the instance directory. |
| `--run PATH` | One existing run directory; choose either `--run` or `--instance`. |
| `--artifact PATH` | Additional regular file, repeatable. |
| `--source-timeout SECONDS` | Per-source deadline, default `5`. |
| `--timeout SECONDS` | Overall collection budget, default `30`. |
| `--max-source-bytes BYTES` | Maximum input bytes retained per source, default `8388608`. |
| `--include-request-content` | Include journal and completion artifacts and request fields. |
| `--format text\|json` | Text summary or the [command result contract](Command-Results.md). |

Run selection includes `fleet.json`, `profiles.json`, `teardown.json`, `router-state.json`, command and stage records, process logs and stage stdout/stderr files. It also includes JSON, log and stage output files directly under `engine-*` and `verify-*` directories. `--include-request-content` adds `journal.jsonl` and completion artifacts. Use `--artifact` for supervisor status, ingress logs or deployment evidence stored elsewhere. Artifact reads require regular files and reject symbolic links in the selected path.

The collector retains up to the byte limit, marks the source `truncated`, and continues within the remaining overall budget. Endpoint deadlines include response streaming; a response that stalls after its headers or first bytes retains its HTTP status and available body. Local file reads check the deadline between bounded reads. Use local files for incident collection; filesystem operations inherit their mount's I/O behaviour.

## Manifest and exit status

`manifest.json` uses `narwhal.diagnostic-bundle` version 1. It records collection start and finish timestamps, elapsed time, package version, package source digest, inclusion policy and one row per source. Each row identifies the source path or URL, collection timestamp, outcome and error when applicable. HTTP rows retain status and content type. Exported files have byte counts and SHA-256 hashes; complete local reads also record the original file hash and modification timestamp. Generated filenames avoid placing source names or credential values in output paths.

The manifest's `status` is `success` when every selected source was collected, or `partial` when a source failed, timed out, exceeded its byte limit or was explicitly excluded by policy. A failed artifact write contributes a `write_error` row while collection continues to the remaining sources and manifest. A failure to write the manifest returns the command's I/O error status.

| Exit status | Operator action |
| --- | --- |
| `0` | Inspect the completed bundle. |
| `2` | Correct arguments, the output path or required directory access. |
| `3` | Inspect individual source outcomes in the partial bundle. |
| `4` | Inspect the reported I/O failure and retained output directory. |

JSON command results report the bundle path, manifest path, collection status and source count. Their artifact entry tracks whether this invocation created the manifest. Exit `3` uses command status `degraded` and error code `collection_partial`.

## Content policy

Default exports exclude `journal.jsonl` and `completion.json`. Structured fields named `messages`, `prompt`, `content`, `completion`, `request_body`, `response_body`, `text`, `input` or `output` become `[REDACTED]`. `--include-request-content` enables these artifacts and fields while credential redaction stays active.

Credential values are removed from recognised credential fields, authorization headers, URL credentials, credential query parameters and launch arguments. The collector resolves `*_env` references from selected JSON sources only to remove those values, retaining the reference names in exported JSON. It also scrubs values of environment variables whose names identify credentials. Credential files (`.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `authorized_keys`, `credentials` and `credentials.json`) receive an `excluded` outcome when explicitly selected.

JSON and JSONL exports preserve their structured fields after redaction. For incomplete JSON or free-text logs, a labelled credential or request field ends the retained text at that point, covering multiline and truncated values. Unlabelled operational lines remain in the export. Site tooling supplies filtered free-text logs when credentials or request content use an application-specific encoding.

## Manual collection

Use the [incident capture commands](Troubleshoot.md#capture-router-and-engine-state) when the collector's installed command is unavailable. Preserve each HTTP status alongside its response and use a separate private directory per router.
