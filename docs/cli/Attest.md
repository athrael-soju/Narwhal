# `narwhal-attest`

`narwhal-attest` is a sidecar for a single vLLM engine. It reads the engine's identity and serves its attestation over HTTP at `/v1/attestation`, alongside a `/health` route. If the engine's version or process start time changes, both routes return HTTP 503 until you restart the sidecar, so it never serves an attestation for an engine that has since restarted or been upgraded.

Start it after vLLM is running.

## Options

| Option                | Default     | Description                                                                          |
| --------------------- | ----------- | ------------------------------------------------------------------------------------ |
| `--document PATH`     | required    | The attestation contract values, with a source for every field that has a value.     |
| `--engine-base URL`   | required    | Base URL of the vLLM engine. The sidecar queries `/version` and `/metrics` under it. |
| `--host HOST`         | `127.0.0.1` | Address the sidecar listens on.                                                      |
| `--port PORT`         | `8010`      | Port the sidecar listens on.                                                         |
| `--timeout-s SECONDS` | `5.0`       | How long to wait when reading the engine's identity.                                 |
| `--version`           |             | Print the installed version.                                                         |
