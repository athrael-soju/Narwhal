---
description: Reach the fleet control console from a workstation through the operator tunnel, and choose between pasting the bearer token and connecting automatically.
---

# Opening the console

The control service serves the console on the router host. You open it from a workstation through the operator tunnel, which forwards workstation loopback ports to `127.0.0.1` on the router host. [Tunnelling router, Prometheus, and Grafana to the workstation](../../deploy/07-Serve-and-Measure.md#tunnelling-router-prometheus-and-grafana-to-the-workstation) covers the tunnel's host-key check and authentication.

The service must be running, as in [Setting up the control service](01-Set-Up-the-Service.md).

## Reaching the console through the tunnel

1. On the workstation, open a terminal in the checkout with `.env` loaded.
2. Open the tunnel with the control port and Grafana:

    ```bash
    python3 tools/deployment/deploy_hosts.py tunnel --role router \
      --forward 18020:8020 --forward 13000:3000
    ```

3. Keep the tunnel terminal open.
4. Open `http://127.0.0.1:18020/console` in a browser on the workstation.
5. Paste the bearer token into **Control token** and select **Connect**.

The local Grafana port must match the origin of `console.grafana_url`. If you forward Grafana to another workstation port, change `console.grafana_url` to match and restart the service.

If the control service or Grafana listens on a router-host address other than `127.0.0.1`, open a separate tunnel for that address with `--remote-address`, as in [Accessing dashboards and isolating listeners](../../observability/02-Access.md#isolating-a-second-monitoring-stack).

## Console authentication

`GET /console` serves the console page, and `GET /` redirects to it. Both are open. The page is static, and fleet data reaches it through authenticated API calls. Every other route returns HTTP 401 to a request with a missing or invalid token.

After **Connect**, the page stores the token in the tab's session storage and sends it in an `Authorization: Bearer` header on each API request. Closing the tab or selecting **Forget token** clears it. Sending the token in a header rather than a cookie keeps other sites from making authenticated requests through your browser. If the service returns 401, the page clears the token and asks for it again.

The console shows times in UTC and follows the browser's light or dark preference. To override the theme, add `theme=light` or `theme=dark` to the URL.

## Connecting automatically

With `console.auto_connect` set to `true`, the service embeds the token in the console page, and the page connects when it loads.

!!! warning
    Any process that can reach the service's port can then read the token from the page. That includes every user and process on the router host and every local process on a workstation with the tunnel open. Enable `auto_connect` only on single-user machines.

The service serves this page only to requests whose `Host` header is `127.0.0.1`, `localhost` or `::1`, and returns HTTP 421 to others. This blocks DNS rebinding, where a site points its own domain name at a loopback address to read the page.

After the service restarts with a new token, reload the page.
