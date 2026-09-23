# `narwhal-attest`

Start `narwhal-attest` after vLLM. The sidecar reads one engine's identity, serves its attestation over HTTP, and stops when the engine version or process start time changes.

| Option                | Default     | Contract                                                |
| --------------------- | ----------- | ------------------------------------------------------- |
| `--document PATH`     | required    | Contract values plus a source for every populated field |
| `--engine-base URL`   | required    | vLLM base URL queried at `/version` and `/metrics`      |
| `--host HOST`         | `127.0.0.1` | Sidecar bind address                                    |
| `--port PORT`         | `8010`      | Sidecar port                                            |
| `--timeout-s SECONDS` | `5.0`       | Time budget for reading engine identity                 |
