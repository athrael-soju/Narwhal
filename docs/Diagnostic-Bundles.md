# Collect diagnostic bundles

`narwhal diagnostics collect` writes a private directory containing the router's `/health`, `/ready`, `/narwhal/state`, `/narwhal/lifecycle`, and `/metrics` responses, plus copies of the local files you name.

The command is read-only. It issues only GET requests, copies local files unchanged, and does not restart or manage the router or engines.

```bash
mkdir -p runs/diagnostics
narwhal diagnostics collect \
  --router http://127.0.0.1:8000 \
  --fleet config/fleet.json \
  --run runs/dev/run-example \
  --out runs/diagnostics/incident-001 \
  --format json
```

`--out` must not exist, and its parent must. If `--out` exists, the command stops before collecting anything. Use a new path for each router. The bundle directory is created with mode `0700` and its files with `0600`. Relative paths resolve from the working directory.

## Source selection

| Option                      | What it collects                                                                                                                                                                                        |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--router URL`              | Required. The router's HTTP or HTTPS base URL: host, port, and optionally a path.                                                                                                                       |
| `--out PATH`                | Required. A new directory, inside a parent that already exists.                                                                                                                                         |
| `--fleet PATH`              | A fleet JSON file.                                                                                                                                                                                      |
| `--instance PATH`           | A dev instance's `instance.json`, `lifecycle.json`, and `fleet.json`, plus the run recorded in its lifecycle state. That run must live inside the instance directory. Can't be combined with `--run`. |
| `--run PATH`                | One existing run directory. Can't be combined with `--instance`.                                                                                                                                        |
| `--artifact PATH`           | Any other regular file. Repeat the option to add more.                                                                                                                                                  |
| `--source-timeout SECONDS`  | How long to wait for each source. Default `5`.                                                                                                                                                          |
| `--timeout SECONDS`         | Time limit for the whole collection. Default `30`.                                                                                                                                                      |
| `--max-source-bytes BYTES`  | How much to keep from each source. Default `8388608` (8 MiB).                                                                                                                                           |
| `--include-request-content` | Also export prompts, completions, and journal entries. See [Content policy](#content-policy).                                                                                                           |
| `--format text\|json`       | A readable summary, or a [command result](Command-Results.md).                                                                                                                                          |

A selected run contributes:

- `fleet.json`, `profiles.json`, `teardown.json`, and `router-state.json`
- the command and stage records, process logs, and each stage's stdout and stderr
- JSON files, logs, and stage output directly inside any `engine-*` or `verify-*` directory

`journal.jsonl` and `completion.json` are included only with `--include-request-content`.

For anything kept elsewhere, such as supervisor status, ingress logs, or deployment evidence, use `--artifact`. Each one must be a regular file. The collector refuses any path that passes through a symbolic link.

### Size and time limits

If a source is larger than `--max-source-bytes`, the collector keeps that many bytes, marks the source `truncated`, and moves on to the next source while time remains in the overall budget.

The per-source timeout covers the whole HTTP response, body included. If a response stalls partway through, the bundle keeps its status code and the part of the body that arrived.

> **Warning:** Timeouts are checked between reads and cannot interrupt a read that is already blocked. A hung network mount can exceed `--source-timeout` and `--timeout`. Collect from local disks.

## Manifest and exit status

Every bundle contains a `manifest.json` (format `narwhal.diagnostic-bundle`, version 1). It records:

- when collection started and finished, and how long it took
- the package version and source digest
- which inclusion policy was in effect
- one row per source: path or URL, collection time, outcome, and the error if there was one. HTTP rows also carry the status code and content type.

Every exported file has its size and SHA-256 hash recorded. Each local file's row also stores the original's modification time and, if the file was read completely, the original's hash, so you can tell later whether the file has changed. Output filenames are generated, which keeps source paths and credentials out of filenames.

The manifest's `status` is `success` only if every selected source was collected in full. Any of the following makes it `partial`:

- a source failed or timed out
- an HTTP source returned an error status
- a source hit the byte limit (`truncated`)
- a source was `excluded` (see [Content policy](#content-policy))

A router that isn't ready answers `/ready` with `503`, so a bundle collected from it is always partial.

If writing one exported file fails, that source gets a `write_error` row and collection continues. If the manifest itself can't be written, the command exits with status `4`.

| Exit status | Meaning                                                                             | What to do                                                                  |
| ----------- | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `0`         | The bundle is complete.                                                             | None.                                                                       |
| `2`         | The arguments are invalid, `--out` already exists, or its parent doesn't.           | Fix the arguments.                                                          |
| `3`         | The bundle is partial.                                                              | Check each source's outcome in `manifest.json` to see what's missing and why. |
| `4`         | An I/O error, such as a permission error, stopped the command.                      | Read the error, check write access to the parent directory, then inspect the output directory. |

With `--format json`, the command result reports the bundle path, manifest path, collection status, and source count. The artifact entry indicates whether this run created the manifest. A partial bundle (exit `3`) is reported with command status `degraded` and error code `collection_partial`.

## Content policy

By default, bundles leave out request content. `journal.jsonl` and `completion.json` aren't exported. Any structured field named `messages`, `prompt`, `content`, `completion`, `request_body`, `response_body`, `text`, `input`, or `output` is replaced with `[REDACTED]`. Pass `--include-request-content` to keep them. Credentials are redacted either way.

The collector removes credential values from known credential fields, authorization headers, URLs with embedded credentials, credential query parameters, and launch arguments. When a JSON source refers to a secret with a `*_env` field, the collector looks up that variable to identify the value to remove, and leaves the variable's name in the export. It also scrubs the value of any environment variable whose name marks it as a credential.

Some files are never exported, even when you name them explicitly: `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `authorized_keys`, `credentials`, and `credentials.json`. They appear in the manifest with the outcome `excluded`.

JSON and JSONL files keep their structure after redaction. For free-text logs and incomplete JSON, output is cut at the first labeled credential or request field, and nothing after it in that source is exported. This covers values that span several lines or are cut off by truncation. Ordinary log lines before that point are kept.

> **Warning:** Redaction covers only the fields and formats above. The collector does not catch credentials or request content that an application writes into logs in other ways. Filter those logs yourself and pass the filtered copies with `--artifact`.

## Manual collection

If the `narwhal` command isn't installed where you need it, use the [incident capture commands](Troubleshoot.md#capture-state-first) instead. Save each response's HTTP status alongside it, and use a separate private directory for each router.
