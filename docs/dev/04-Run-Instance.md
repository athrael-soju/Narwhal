# Run and stop an instance

Keep one Python environment active for every lifecycle command on an
instance.

## Start and verify

Select the IPv4 interface from [Prepare Ubuntu or WSL2](01-Prepare-Host.md):

```bash
interface=eth0  # replace with the interface reported by ip
narwhal dev init --interface "$interface"
narwhal dev up
narwhal dev verify
narwhal dev status
```

| Command  | Success status | Checks                                                                 |
| -------- | -------------- | ---------------------------------------------------------------------- |
| `init`   | `initialized`  | Model, runtime and GPU allocation                                      |
| `up`     | `launched`     | Ports, engine startup, profiles, attestations and router startup       |
| `verify` | `ready`        | Preflight across eligible directed KV paths, routed arithmetic request |
| `status` | Current status | Engine health and the retained verification result                    |

| Output                        | Content                                                                    |
| ----------------------------- | -------------------------------------------------------------------------- |
| stdout                        | One JSON lifecycle document                                                |
| stderr                        | Preparation, profiling and error diagnostics                               |
| stdout with `--format json`   | [Versioned command result](../Command-Results.md) with automation exit codes |

A failed `verify` sets `status` to `degraded` with the retained reason.
Restore `ready`:

1. Repair the failing input.
2. Run `narwhal dev verify`.

## Inspect the router

The default router listens at `http://127.0.0.1:18000`:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

| Change             | Flag                                 |
| ------------------ | ------------------------------------ |
| Port layout        | `narwhal dev init --port-base`       |
| Instance directory | `--instance PATH` on every command   |

## Inspect the run directory

`status` prints the current run directory.

| Path                      | Records                                   |
| ------------------------- | ----------------------------------------- |
| `engine-*/startup.log`    | Model loading and engine requests         |
| `profile-*.log`           | Probe and fit outcomes                    |
| `verify-*/preflight.log`  | Runtime, profile and transfer checks      |
| `verify-*/completion.json`| Routed response                           |
| `*-memory.jsonl`          | Device memory samples                     |
| `journal.jsonl`           | Request journal                           |
| `teardown.json`           | Teardown result                           |

## Stop the instance

```bash
narwhal dev down
narwhal dev status
```

`down` reports `stopped` after stopping the process groups that match the
recorded boot ID and start ticks. The next `up` creates a fresh run directory
with new profiles.

## Restart after a runtime change

After a runtime change or engine restart:

1. Run `narwhal dev down`.
2. Run `narwhal dev up`.
3. Run `narwhal dev verify`.
