# Run and stop an instance

An instance records the Python interpreter it was created with. Activate the
same virtual environment for every command run against it.

## Start an instance

Use the interface selected in [Prepare Ubuntu or WSL2](01-Prepare-Host.md):

```bash
interface=eth0  # replace with the interface from ip -brief -4 address
narwhal dev init --interface "$interface"
narwhal dev up
narwhal dev verify
narwhal dev status
```

Run the commands in this order:

| Command  | Reports on success                               | What it does                                                                                         |
| -------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| `init`   | `initialized`                                    | Validates the model, runtime, and GPU allocation                                                     |
| `up`     | `launched`                                       | Checks ports, starts the engines, captures attestations, profiles role splits, and starts the router |
| `verify` | `ready`                                          | Tests every eligible directed KV path, then sends an arithmetic request through the router           |
| `status` | Current state, for example `ready` or `degraded` | Reports engine health and the result of the last `verify`                                            |

Each command writes one JSON document describing the instance's state to
stdout. Progress messages, profiling output, and errors go to stderr. For
scripts, add `--format json` to get a
[versioned command result](../Command-Results.md) with stable exit codes.

If `verify` fails, `status` reports `degraded` and keeps the reason. Fix the
reported cause and rerun `verify`. On success, status returns to `ready`.

## Talk to the router

By default the router listens on `http://127.0.0.1:18000`. These endpoints
return router state and Prometheus metrics:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

To use a different set of ports, pass `--port-base` to `narwhal dev init`.
To keep an instance in a directory other than `runs/dev`, pass `--instance PATH`
to every later command as well.

## Run directory contents

`status` prints the path of the current run directory. Common files:

| Path                       | Contents                                     |
| -------------------------- | -------------------------------------------- |
| `engine-*/startup.log`     | Model loading and requests for each engine   |
| `profile-*.log`            | Profiling probes and fit results             |
| `verify-*/preflight.log`   | Runtime, profile and KV transfer checks      |
| `verify-*/completion.json` | The routed test response                     |
| `*-memory.jsonl`           | GPU memory samples over time                 |
| `journal.jsonl`            | Request journal                              |
| `teardown.json`            | Shutdown record                              |

## Stop it

```bash
narwhal dev down
narwhal dev status
```

`down` stops only process groups whose boot ID and start ticks match the
values Narwhal recorded at launch, so a reused PID is left running. It reports
`stopped` when finished. The next `up` creates a new run directory and profiles
the engines from scratch.

## After changing the runtime

Profiles describe the runtime that was running when they were taken. After
changing the runtime or an unplanned engine restart, run `down`, `up`
and `verify` again to regenerate the profiles:

```bash
narwhal dev down
narwhal dev up
narwhal dev verify
```
