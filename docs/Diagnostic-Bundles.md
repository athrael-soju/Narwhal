# Collect diagnostic bundles

When something goes wrong, `narwhal diagnostics collect` gathers what you need to investigate into one private directory. It queries the router's `/health`, `/ready`, `/narwhal/state`, `/narwhal/lifecycle`, and `/metrics` endpoints, and copies the local files you point it at.

It only reads. Every HTTP call is a GET, and local files are copied without being changed. It won't restart or manage the router or engines either. Whatever supervises them at your site is still responsible for that.

```bash
mkdir -p runs/diagnostics
narwhal diagnostics collect \
  --router http://127.0.0.1:8000 \
  --fleet config/fleet.json \
  --run runs/dev/run-example \
  --out runs/diagnostics/incident-001 \
  --format json
```

The parent directory has to exist already, and the `--out` directory must not. If `--out` already exists, the command stops before collecting anything, so use a new path for each router. The bundle directory is created with mode `0700` and its files with `0600`, which keeps them readable only by you. Relative paths resolve from your working directory.

## Source selection

| Option                      | What it collects                                                                                                                                                                                        |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--router URL`              | Required. The router's HTTP or HTTPS base URL: host, port, and optionally a path.                                                                                                                       |
| `--out PATH`                | Required. A new directory, inside a parent that already exists.                                                                                                                                         |
| `--fleet PATH`              | A fleet JSON file.                                                                                                                                                                                      |
| `--instance PATH`           | A dev instance's `instance.json`, `lifecycle.json`, and `fleet.json`, plus the run recorded in its lifecycle state. That run has to live inside the instance directory. Can't be combined with `--run`. |
| `--run PATH`                | One existing run directory. Can't be combined with `--instance`.                                                                                                                                        |
| `--artifact PATH`           | Any other regular file. Repeat the option to add more.                                                                                                                                                  |
| `--source-timeout SECONDS`  | How long to wait for each source. Default `5`.                                                                                                                                                          |
| `--timeout SECONDS`         | Time limit for the whole collection. Default `30`.                                                                                                                                                      |
| `--max-source-bytes BYTES`  | How much to keep from each source. Default `8388608` (8 MiB).                                                                                                                                           |
| `--include-request-content` | Also export prompts, completions, and journal entries. See [Content policy](#content-policy).                                                                                                           |
| `--format text\|json`       | A readable summary, or a [command result](Command-Results.md).                                                                                                                                          |

When you select a run, the bundle gets `fleet.json`, `profiles.json`, `teardown.json`, `router-state.json`, the command and stage records, process logs, and each stage's stdout and stderr. It also picks up JSON files, logs, and stage output sitting directly inside any `engine-*` or `verify-*` directory. `journal.jsonl` and completion files are only included with `--include-request-content`.

For anything kept elsewhere, such as supervisor status, ingress logs, or deployment evidence, use `--artifact`. Each one has to be a regular file, and the collector refuses any path that passes through a symbolic link.

### Size and time limits

If a source is larger than `--max-source-bytes`, the collector keeps that many bytes, marks the source `truncated`, and moves on to the next one while there's time left in the overall budget.

The per-source timeout covers the whole HTTP response, body included. If a response stalls halfway through, you still get its status code and whatever part of the body had arrived.

> **Warning:** Collect from local disks where you can. The collector checks the deadline between reads, but it can't interrupt a read that's already blocked. If a network mount stops responding, a single read can hang well past `--source-timeout` and `--timeout`.

## Manifest and exit status

Every bundle contains a `manifest.json` (format `narwhal.diagnostic-bundle`, version 1). It records when collection started and finished, how long it took, the package version and source digest, and which inclusion policy was in effect. It then has one row per source with the path or URL, when it was collected, the outcome, and the error if there was one. Rows for HTTP sources also carry the status code and content type.

Every exported file has its size and SHA-256 hash recorded. Each local file's row also stores the original's modification time and, if the file was read all the way through, the original's hash, so you can tell later whether the file has changed since. Output filenames are generated rather than copied from the source, which stops a path or credential from leaking out through a filename.

The manifest's `status` is `success` only if every selected source was collected in full. If anything failed, returned an HTTP error status, timed out, hit the byte limit, or was excluded by the content policy, the status is `partial`. A router that isn't ready answers `/ready` with `503`, so a bundle collected from it is always partial. A single oversized log is enough to make a bundle partial, so check the individual rows before assuming something is broken.

If writing one exported file fails, that source gets a `write_error` row and collection carries on with the rest. If the manifest itself can't be written, the command exits with status `4`.

| Exit status | Meaning                                                             | What to do                                                                                           |
| ----------- | ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `0`         | The bundle is complete.                                             | Start investigating, or hand the bundle over.                                                        |
| `2`         | The arguments are invalid, `--out` already exists, or its parent directory doesn't. | Fix the arguments. Check that the parent directory exists and `--out` doesn't.         |
| `3`         | The bundle is partial.                                              | Look at each source's outcome in `manifest.json` to see what's missing and why.                      |
| `4`         | An I/O error, such as a permission error, stopped the command.      | Read the error, check that you can write to the parent directory, then look at what was left in the output directory. |

With `--format json`, the command result gives you the bundle path, the manifest path, the collection status, and the number of sources. Its artifact entry tells you whether this particular run created the manifest. A partial bundle (exit `3`) is reported with command status `degraded` and error code `collection_partial`.

## Content policy

By default, bundles leave out request content. `journal.jsonl` and `completion.json` aren't exported. Any structured field named `messages`, `prompt`, `content`, `completion`, `request_body`, `response_body`, `text`, `input`, or `output` is replaced with `[REDACTED]`. Pass `--include-request-content` if you need them. Credentials are redacted either way.

The collector removes credential values from known credential fields, authorization headers, URLs with embedded credentials, credential query parameters, and launch arguments. When a JSON source refers to a secret with a `*_env` field, the collector looks up that variable so it knows which value to remove, then leaves the variable's name in the export. It also scrubs the value of any environment variable whose name marks it as a credential.

Some files are never exported, even if you name them explicitly: `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa`, `id_ed25519`, `authorized_keys`, `credentials`, and `credentials.json`. They appear in the manifest with the outcome `excluded`.

JSON and JSONL files keep their structure after redaction. Free-text logs and incomplete JSON are harder to redact safely, so the collector is blunter with them. When it finds a labeled credential or request field, the retained text stops at that point, and nothing after it in that source is exported. That way a value spread over several lines, or cut off by truncation, can't slip through. Ordinary log lines before that point are kept.

> **Warning:** Redaction only recognizes the labeled fields and formats described above. If your application writes credentials or request content into logs some other way, the collector won't catch them. Filter those logs yourself and pass the filtered copies with `--artifact`.

## Manual collection

If the `narwhal` command isn't installed where you need it, use the [incident capture commands](Troubleshoot.md#capture-state-first) instead. Save each response's HTTP status alongside it, and use a separate private directory for each router.
