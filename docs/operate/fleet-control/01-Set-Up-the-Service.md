---
description: Write the fleet control service's private configuration, set its bearer token, and start and check the service on the router host.
---

# Setting up the control service

Configure and start the fleet control service on the router host.

## Prerequisites

- A deployed fleet with its router serving on the router host, as in [Starting and locally verifying the router](../../deploy/07-Serve-and-Measure.md#starting-and-locally-verifying-the-router).
- The router shell: the router host's Narwhal checkout with `.venv` active, opened with `deploy_hosts.py shell`.
- The monitoring stack, started with `make observe` on the router host.
- The baseline fleet configuration file that the router is running.
- A hook command for each action you plan to run. See [Hooks](09-Configuration-Reference.md#hooks).
- For load jobs, AIPerf on the router host. [Ordered benchmark points](../../measure/05-Benchmark-Runner.md) names the AIPerf client version.

## Write the private configuration

In the router shell, copy the example configuration:

```bash
cp config/fleet-control.example.json config/fleet-control.local.json
```

Edit the copy for your deployment. Each hook, and the `load` and `console` sections, enables one feature. [Configuration reference](09-Configuration-Reference.md) lists every key.

The service reads `config/fleet-control.local.json` by default. To use another file, pass `--config` or set `NARWHAL_CONTROL_CONFIG`.

## Set the bearer token

In the router shell, generate a token:

```bash
export NARWHAL_CONTROL_TOKEN="$(openssl rand -hex 32)"
```

Keep the token in your secret store. The console and every API request need it.

## Start the service

In the router shell, from the checkout root, run:

```bash
.venv/bin/python -m tools.fleet_control.cli --config config/fleet-control.local.json
```

The service runs in the foreground. To change the log level, add `--log-level` with `critical`, `error`, `warning`, `info` (default) or `debug`.

If the configuration or token is invalid, or the listener address is unavailable, the service exits with status `2` and lists every problem after `fleet control failed:`.

## Check the service

From another router-host shell with the token exported, run:

```bash
curl -fsS -H "Authorization: Bearer $NARWHAL_CONTROL_TOKEN" http://127.0.0.1:8020/api/health
```

An idle service returns:

```json
{"status": "ok", "session": null, "job": null, "in_progress": null, "in_progress_since": null}
```

Next, [open the console](02-Open-the-Console.md).

## Stop the service

Press Ctrl+C in the service's shell. This also stops any running load job.
