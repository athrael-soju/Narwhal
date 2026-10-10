---
description: Open the fleet control console from a workstation through the operator tunnel.
---

# Opening the console

The console runs on the router host. Open it from a workstation through the operator tunnel, described in [Tunnelling router, Prometheus, and Grafana to the workstation](../../deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation).

The service must be running, as in [Setting up the control service](01-Set-Up-the-Service.md).

## Reaching the console through the tunnel

1. On the workstation, open a terminal in the checkout with `.env` loaded.
2. Open the tunnel with the control port and Grafana, and keep the terminal open:

    ```bash
    python3 tools/deployment/deploy_hosts.py tunnel --role router \
      --forward 18020:8020 --forward 13000:3000
    ```

3. Open `http://127.0.0.1:18020/console` in a browser on the workstation.
4. Paste the bearer token into **Control token** and select **Connect**.

If you forward Grafana to a workstation port other than `13000`, set `console.grafana_url` to that address and restart the service.

If the control service or Grafana listens on a router-host address other than `127.0.0.1`, open a separate tunnel for that address with `--remote-address`, as in [Accessing dashboards and isolating listeners](../../observability/02-Access.md#isolating-a-second-monitoring-stack).

## Console authentication

**Connect** keeps the token in the tab's session storage. Closing the tab or selecting **Forget token** clears it. If the service rejects the token, the page asks for it again.

Times are in UTC. To switch between light and dark mode, select the theme button in the header. To open the console in one mode, add `theme=light` or `theme=dark` to the URL.

The **?** beside each panel title describes the panel.

## Connecting automatically

With `console.auto_connect` set to `true`, the service embeds the token in the console page, and the page connects when it loads.

The service embeds the token when the request's `Host` header names `127.0.0.1`, `localhost`, `::1` or a name in `console.trusted_hosts`. At any other address, the page asks for the token.

!!! warning
    Anyone who can load the page at one of those names can read the token. That includes every user and process on the router host, every local process on a workstation with the tunnel open, and every machine that can reach a trusted name. Enable `auto_connect` only on single-user machines, and list in `trusted_hosts` only names that other people's machines cannot reach.

To connect from other machines, add the host names their browsers use:

```json
"console": {
  "grafana_url": "/",
  "auto_connect": true,
  "trusted_hosts": ["ops-host", "ops-host.example.ts.net"]
}
```

A reverse proxy in front of the service must pass the browser's `Host` header through, as nginx does with `proxy_set_header Host $host;`. A proxy that sends its upstream address instead makes every request look local, and every client receives the token.

After the service restarts with a new token, reload the page.
