# Run and stop an instance

An instance records the Python interpreter it was created with, so activate
the same virtual environment for every command you run against it.

## Start it up

Use the interface you picked in [Prepare Ubuntu or WSL2](01-Prepare-Host.md):

```bash
interface=eth0  # replace with the interface from ip -brief -4 address
narwhal dev init --interface "$interface"
narwhal dev up
narwhal dev verify
narwhal dev status
```

Each command builds on the one before it:

| Command  | Reports on success | What it does                                                                                         |
| -------- | ------------------ | ---------------------------------------------------------------------------------------------------- |
| `init`   | `initialized`      | Checks the model, runtime, and GPU allocation                                                        |
| `up`     | `launched`         | Checks ports, starts the engines, captures attestations, profiles role splits, and starts the router |
| `verify` | `ready`            | Tests every eligible directed KV path, then sends an arithmetic request through the router           |
| `status` | Current state      | Reports engine health and the result of the last `verify`                                            |

Each command writes one JSON document describing the instance's state to
stdout. Progress messages, profiling output, and errors go to stderr. For
scripts, add `--format json` to get a
[versioned command result](../Command-Results.md) with stable exit codes.

If `verify` fails, `status` switches to `degraded` and keeps the reason.
Fix whatever the reason points to and run `verify` again. When it passes,
the instance is back to `ready`.

## Talk to the router

By default the router listens on `http://127.0.0.1:18000`. Its state and
metrics are the quickest way to see what the instance is doing:

```bash
curl http://127.0.0.1:18000/narwhal/state
curl http://127.0.0.1:18000/metrics
```

To use a different set of ports, pass `--port-base` to `narwhal dev init`.
To keep an instance somewhere other than `runs/dev`, pass `--instance PATH`,
and pass it to every later command as well.

## Look inside the run directory

`status` prints the path of the current run directory. The files you'll
reach for most often:

| Path                       | What's in it                                 |
| -------------------------- | -------------------------------------------- |
| `engine-*/startup.log`     | Model loading and requests for each engine   |
| `profile-*.log`            | Profiling probes and how the fits turned out |
| `verify-*/preflight.log`   | Runtime, profile and KV transfer checks      |
| `verify-*/completion.json` | The routed test response                     |
| `*-memory.jsonl`           | GPU memory samples over time                 |
| `journal.jsonl`            | Request journal                              |
| `teardown.json`            | What happened at shutdown                    |

## Stop it

```bash
narwhal dev down
narwhal dev status
```

`down` only stops process groups whose boot ID and start ticks match what
Narwhal recorded when it started them, so it won't touch an unrelated
process that happens to have reused a PID. It reports `stopped` when it's
done. The next `up` creates a new run directory and profiles the engines
from scratch.

## After changing the runtime

The profiles describe the runtime that was running when they were taken. If
you change the runtime, or an engine restarts on its own, take the instance
down and bring it back up so it gets fresh profiles, then verify it:

```bash
narwhal dev down
narwhal dev up
narwhal dev verify
```
