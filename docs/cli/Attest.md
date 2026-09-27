# `narwhal-attest`

Start `narwhal-attest` after vLLM. The sidecar reads one engine's identity and
serves its attestation over HTTP. Its health and attestation routes return HTTP
503 when the engine version or process start time changes.

| Option                | Default     | Contract                                                |
| --------------------- | ----------- | ------------------------------------------------------- |
| `--version`           |             | Print the installed distribution version.               |
| `--document PATH`     | required    | Contract values plus a source for every populated field |
| `--continuation-document PATH` | unset | Private replay capture for the measured engine process |
| `--engine-base URL`   | required    | vLLM base URL queried at `/version` and `/metrics`      |
| `--host HOST`         | `127.0.0.1` | Sidecar bind address                                    |
| `--port PORT`         | `8010`      | Sidecar port                                            |
| `--timeout-s SECONDS` | `5.0`       | Time budget for reading engine identity                 |

## Continuation capture

`--continuation-document` adds `GET /v1/attestation/continuation`. The capture
must use schema `narwhal.replay-capture`, version `1`, with these fields:

| Field | Required value |
| --- | --- |
| `contract_sha256` | SHA-256 of the canonical approved replay contract, as 64 lowercase hexadecimal characters |
| `attestation_digest` | Digest returned by the ordinary attestation for the same process, including its `sha256:` prefix |
| `engine.vllm_version` | Measured backend version |
| `engine.process_start_time_seconds` | Positive, finite process start marker from the engine's `/metrics` route |

Startup rejects a capture whose process identity or ordinary attestation digest
differs from the running engine. Restarting the sidecar with an old capture does
not qualify a replacement engine. Omit this option when the engine has no
approved replay capture; the continuation route then returns HTTP 404.

Fleet qualification produces and reviews the capture together with the private
qualification artifact pinned by the router. The sidecar verifies the supplied
binding and checks the live process on each request. Source review and replay
measurements establish the decoder property described in the
[continuation capability contract](../concepts/04-Stream-Continuation.md#capability-identity).
Keep the captures and their source evidence under ignored deployment paths.
