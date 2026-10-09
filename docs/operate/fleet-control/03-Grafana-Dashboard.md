---
description: Run the fleet control console inside the Fleet control Grafana dashboard, mark actions and load jobs on its charts, and understand the framing boundary.
---

# Using the console from Grafana

`make observe` provisions a **Fleet control** dashboard with UID `narwhal-fleet-control`. The dashboard shows each console view in its own panel, beside charts from the **Narwhal Orchestrator** dashboard.

<div class="narwhal-panel-row" markdown>

![Top of the Fleet control dashboard.](../../assets/fleet-control/dashboard.png)

</div>

## Open the console in Grafana

The service must be set up as in [Setting up the control service](01-Set-Up-the-Service.md).

1. In the router shell, set the `console` section of `config/fleet-control.local.json`:

    ```json
    "console": {
      "grafana_url": "http://127.0.0.1:13000",
      "dashboard_uid": "narwhal-fleet-control",
      "embed_in_grafana": true
    }
    ```

    To connect when the page loads, also set `"auto_connect": true`. Read [Connecting automatically](02-Open-the-Console.md#connecting-automatically) first.

2. To show firing alerts in the session strip, set `prometheus_url` to `http://127.0.0.1:9090`.
3. Restart the control service.
4. If the browser reaches the console at an address other than `http://127.0.0.1:18020/console`, set `NARWHAL_CONTROL_CONSOLE_URL` to that address in the router shell.
5. To mark actions and load jobs on the charts, set up [dashboard annotations](#dashboard-annotations).
6. Run `make observe` on the router host.
7. Open the tunnel, as in [Reaching the console through the tunnel](02-Open-the-Console.md#reaching-the-console-through-the-tunnel).
8. Open `http://127.0.0.1:13000/d/narwhal-fleet-control/fleet-control`.
9. Paste the token into any console panel and select **Connect**. The other console panels connect with it.

The session strip shows **Connected**, and the **Engines** panel lists the fleet's engines.

## Dashboard layout

| Row                               | Panels                                                                                                                                                                        |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Top, without a header             | Session strip; Requests; Latency against SLO; Engines; Engine role history; Request outcomes; Fleet events; Time to first token; Time per output token; Activity              |
| **Load and configuration**        | Load job; Configuration                                                                                                                                                       |
| **Controller**, collapsed         | Pool assignments; Pool pressure                                                                                                                                               |
| **Request & Recovery**, collapsed | Request waiting time; Retries and early exits; Admission in-flight; Queue depth; Dropped requests by reason; Failed attempts by reason; Expired KV by producer; Retry credits |

Session strip, Engines, Activity, Load job and Configuration are console panels. An action in one console panel refreshes the others.

The remaining panels are charts. [Reading the dashboard](../../observability/05-Dashboard.md) describes those copied from the Narwhal Orchestrator dashboard. Three charts are specific to this dashboard:

- **Requests** shows each column of the Narwhal Orchestrator **Requests** table as a separate stat.
- **Latency against SLO** shows TTFT p95 and TPOT p95 as a percentage of their SLO targets. A bar turns yellow at 80% and red at 100%.
- **Request outcomes** shows requests offered, completed and dropped per second. Dropped requests are those refused, rejected, failed or expired.

## Dashboard annotations

The dashboard marks fleet control activity on its charts with two annotation layers:

- **Fleet control actions** draws a dashed purple line at the start of each engine action, overlay, cold restart and restore, labelled with the action and target, such as `drain n7`.
- **Load jobs** shades each load job's interval and labels it with the job ID.

<div class="narwhal-panel-row" markdown>

![Request outcomes with a load job and a drain marked.](../../assets/fleet-control/annotations.png)

</div>

Prometheus reads these events from the control service's `GET /metrics`. To add the scrape:

1. In the router shell, export the control token in `NARWHAL_CONTROL_TOKEN`.
2. Export the control service's address as Prometheus reaches it. Prometheus uses host networking, so with the default port:

    ```bash
    export NARWHAL_CONTROL_METRICS_URL=http://127.0.0.1:8020
    ```

3. Run `make observe`. It writes the `fleet-control` scrape target and passes the token to Prometheus.
4. Query the scrape status:

    ```bash
    curl -fsSG http://127.0.0.1:9090/api/v1/query \
      --data-urlencode 'query=up{job="fleet-control"}' \
      | python3 -m json.tool
    ```

    A value of `1` marks a successful scrape.

`make observe` reads the token from `NARWHAL_CONTROL_TOKEN` regardless of the service's `token_env`, and stops with an error when `NARWHAL_CONTROL_METRICS_URL` is set without it. After the service restarts with a new token, run `make observe` again.

## Framing boundary

With `console.embed_in_grafana` set, the console's `frame-ancestors` policy admits only the `console.grafana_url` origin. A framed console authenticates with the bearer token, as the standalone page does.

Each console panel loads the console in a sandboxed frame with its own origin, which keeps the token out of reach of Grafana pages. The sandbox permits downloads, for **Run record** and **Job document**, and lets **From session start** open the dashboard in the Grafana tab.

Grafana sanitizes Text panel HTML by default, which would remove the frames that load the console. `make observe` sets `GF_PANELS_DISABLE_SANITIZE_HTML` to keep them.

!!! warning
    With sanitizing off, any user who can edit a dashboard can add script that runs in other users' Grafana pages. Grafana gives anonymous users Viewer access. Grant dashboard edit rights to operators only.
