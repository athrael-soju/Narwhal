---
description: Diagnose fleet control console problems from the first visible symptom, including empty console panels, missing chart markers, and engines stuck after a drain or stop.
---

# Troubleshooting the console

## A console panel shows a browser error page

The browser refused to show the console in a Grafana frame. From the router shell, read the console's framing headers:

```bash
curl -sS -D - -o /dev/null http://127.0.0.1:8020/console | grep -iE 'x-frame-options|content-security-policy'
```

- `X-Frame-Options: DENY` or `frame-ancestors 'none'`: `console.embed_in_grafana` is off. Set it to `true` and restart the service.
- `frame-ancestors` with another origin: `console.grafana_url` differs from the Grafana address in the browser. Set it to the origin in the browser's address bar and restart the service.

Reload the dashboard. Each console panel shows **Control token** or connects.

## Charts in the standalone console are empty frames

Grafana was started before `make observe` enabled embedding with `GF_SECURITY_ALLOW_EMBEDDING`.

Run `make observe` on the router host to recreate Grafana, then reload the console.

## Chart markers are missing for actions and load jobs

Query the scrape status of the control service:

```bash
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{job="fleet-control"}' \
  | python3 -m json.tool
```

- Empty result: `NARWHAL_CONTROL_METRICS_URL` was unset when `make observe` last ran. Set it and `NARWHAL_CONTROL_TOKEN`, as in [Dashboard annotations](03-Grafana-Dashboard.md#dashboard-annotations), and run `make observe` again.
- Value `0`: open Prometheus `/targets` and read the `fleet-control` scrape error. For HTTP 401, Prometheus holds an old token. Export the current token and run `make observe` again. For a refused connection, `NARWHAL_CONTROL_METRICS_URL` names the wrong address.

Run an engine action to confirm the fix. Its marker appears on **Request outcomes** after the action finishes.

## Readmit stays disabled after a drain

**Readmit** shows `<engine> must restart before readmission: stop and start it, or restart the engine wave.`

The router readmits this engine only after its process restarts.

1. Restart the engine with **Stop**, then **Start**. Without those hooks, restart it through its process manager, as in [Replacing the process](../03-Restart-Engines.md#72-replacing-the-process). Under the `whole_wave` restart policy, restart the engine wave, as in [Restarting an engine wave](../03-Restart-Engines.md#8-restarting-an-engine-wave).
2. Wait until **Readmit** is enabled.
3. Select **Readmit**. The engine returns to `in service`.

## Start fails and the engine stays ejected

**Start** or **Resume** fails with one of these errors. The engine's row shows the router's latest lifecycle event in the **Readmit** tooltip.

| Error                                                        | Cause                                                                                   | Fix                                                                                                                                |
| ------------------------------------------------------------ | --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `<engine> did not return to service within <n>s`             | The engine process failed to come up, or it answers health checks too slowly            | Read the start hook's output in `hooks/<nnn>-engine_start.log` in the session directory, then the engine's startup log. See [An engine fails to start after a stop](#an-engine-fails-to-start-after-a-stop) |
| `<engine> needs fresh profiles before the router readmits it` | The restarted process no longer matches the engine's profiles                           | Profile the engine and restart the router with the new profiles, as in [Activating replacement profiles](../03-Restart-Engines.md#activating-replacement-profiles) |
| `the router blocked readmission of <engine>`                  | The router's readmission validation failed. The error ends with the router's reason     | Fix the reason, then select **Readmit**                                                                                            |

## An engine fails to start after a stop

**Start** fails, and the engine's startup log reports less free GPU memory than the engine requires. The start hook's output is in `hooks/<nnn>-engine_start.log` in the session directory.

On a host shared with other KV-transfer engines, another engine still holds the stopped engine's KV memory, as described in [Peer memory release](../../concepts/03-Failure-and-State.md#peer-memory-release). Restart the engine wave, as in [Restarting an engine wave](../03-Restart-Engines.md#8-restarting-an-engine-wave).

To let **Start** succeed without a wave restart, set `UCX_CUDA_IPC_CACHE` to `n` in `runtime.environment` and use UCX 1.22 or later in the engine image. Peers then release a stopped engine's GPU memory. [Peer memory release](../../concepts/03-Failure-and-State.md#peer-memory-release) describes the trade-off.
