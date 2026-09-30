# Gate G: Start the service and validate capacity through the private path

The last gate starts the router, brings up monitoring, and runs a first capacity trial from your workstation over an SSH tunnel.

## Start and locally verify the router

On the router host:

```bash
.venv/bin/narwhal-serve \
  --fleet runs/deployment/fleet.json \
  --host 127.0.0.1 \
  --port 8000
```

The router listens only on loopback. Trial traffic from your workstation reaches it through an SSH tunnel. If you later expose it publicly, put TLS, authentication, a WAF policy, request limits, and model routing in front of it.

From another shell on the router host (replace `<served-model>` with the fleet's model):

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/narwhal/state | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/metrics
curl -fsS http://127.0.0.1:8000/v1/completions \
  -H 'content-type: application/json' \
  -d '{"model":"<served-model>","prompt":"Return one sentence about narwhals.","max_tokens":32}'
```

Save these responses before you put any load on the router. They are your baseline:

- `/health` shows liveness and the cached instance counts;
- `/ready` shows the admission state;
- `/narwhal/state` shows the engine inventory and role split;
- `/metrics`;
- one successful completion, which should increment `served`.

## Start observability on the router

In the router shell, point monitoring at the fleet document and the router, then start it:

```bash
export NARWHAL_FLEET=runs/deployment/fleet.json
export NARWHAL_ROUTER_URL=http://127.0.0.1:8000
make observe
```

Save the target-discovery and dashboard-verification output. Prometheus scrapes the router locally and finds the engine targets from the fleet document.

## Tunnel router, Prometheus and Grafana to the workstation

From a management-checkout terminal with `.env` loaded:

```bash
python3 tools/deployment/deploy_hosts.py tunnel --role router \
  --forward 18000:8000 --forward 19090:9090 --forward 13000:3000
```

The helper binds ports on your workstation's loopback, checks the router's recorded SSH host key, and logs in with whatever password, key, or agent the router role is configured for. Leave this terminal open. Ctrl-C closes all the forwards it opened.

From another workstation shell:

```bash
export NARWHAL_TRIAL_URL=http://127.0.0.1:18000
curl -fsS "$NARWHAL_TRIAL_URL/health"
curl -fsS "$NARWHAL_TRIAL_URL/ready"
curl -fsSG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=up{job=~"narwhal-router|engines"}' \
  | python3 -m json.tool
```

The Grafana dashboard is at:

```text
http://127.0.0.1:13000/d/narwhal-router/narwhal-orchestrator
```

Point your workload client at `$NARWHAL_TRIAL_URL`. Because the traffic goes through SSH, the trial's numbers include the tunnel's network and encryption overhead.

The tunnel connects to `127.0.0.1` on the router and writes a log under `runs/access-<id>/`. If a workstation port is already taken, change the local side of that `--forward` and the matching client URL, but keep the remote port the same.

If SSH connects but forwarded requests fail, check the listener on the router host. If a service is bound somewhere other than loopback, use `--remote-address`. That option applies to every forward in the same invocation, so services on different addresses need separate tunnels.

Note which host has the router role, the workstation's hostname, the source revision, and each local-to-remote port mapping.

## Run the initial capacity trial

1. Create a deployment identifier and put together the evidence set described in [Measure a fleet](../Measure.md).
2. Add Gate F's passing preflight output to it.
3. Add the monitoring startup output and evidence that Prometheus is successfully scraping the router and the engines.
4. From the workstation, run the initial synthetic workload through `$NARWHAL_TRIAL_URL`: two runs of 200 requests, at 0.5 and at 1 request/s, each with 8,192 input tokens and 128 output tokens. Keep the workload definition, the per-request records, and the summaries.

    Treat 2 s TTFT, 33.3 ms TPOT, and 95% attainment as provisional thresholds until measured performance and the service requirements settle the real acceptance criteria.

    Also record the client's CPU, memory, network, and scheduler behavior. If the trial falls short, this lets you tell whether the workstation or the SSH path was the bottleneck rather than the fleet.

5. Let resident work drain. Reconcile every offered request against the terminal outcomes recorded by the client and by the router. Use Grafana's provisioned data source to query the engine, request, token, role, and pool-load series. Then run the post-load KV ring:

```bash
.venv/bin/narwhal-check --fleet runs/deployment/fleet.json --ring
```

The trial passes when all of the following are true:

- the router served the measured workload;
- the client's records reconcile with the router journal;
- Prometheus scraped the router and the engines successfully;
- Grafana has the required series;
- the post-load KV ring passed.

Store the service locations, the approved source revision, the fleet configuration, the profiles, the router journal, and the monitoring endpoints with the private deployment record.

Once the client records and the ring result are saved, stop the client and close the tunnel when you no longer need private access. Leave the engines, attestation sidecars, router, and monitoring stack running. For a later planned engine drain or shutdown, see [Operate Narwhal](../Operate.md).

When you record the final gate, use the [evidence and recovery index](../Deploy.md#evidence-and-recovery-index).
