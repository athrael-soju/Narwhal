# `narwhal-attest`

`narwhal-attest` is a sidecar for a single vLLM engine. It reads the engine's version and process start time and serves an attestation over HTTP at `/v1/attestation`, alongside a `/health` route.

Start it after vLLM is running. It queries the engine at startup and exits with an error if the engine is unreachable or its version differs from the attestation document.

If the engine's version or process start time changes, both routes return HTTP 503 until you restart the sidecar.

## Options

| Option                | Default     | Description                                                                          |
| --------------------- | ----------- | ------------------------------------------------------------------------------------ |
| `--document PATH`     | required    | Path to the attestation document (JSON). Each populated field must name its source.  |
| `--engine-base URL`   | required    | Base URL of the vLLM engine. The sidecar queries `/version` and `/metrics` under it. |
| `--host HOST`         | `127.0.0.1` | Address the sidecar listens on.                                                      |
| `--port PORT`         | `8010`      | Port the sidecar listens on.                                                         |
| `--timeout-s SECONDS` | `5.0`       | How long to wait when reading the engine's identity.                                 |
| `--version`           |             | Print the installed version.                                                         |
