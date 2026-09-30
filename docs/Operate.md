# Operate Narwhal

Each model fleet runs one router pair in a shared lease domain. The lease holder admits and places requests. Its peer is the standby.

```text
clients
  |
TLS, authentication, WAF, model routing
  |---------------- router pair A ---------------- fleet A, model A
  |---------------- router pair B ---------------- fleet B, model B
  `---------------- router pair C ---------------- fleet C, model C
```

## Operator tasks

- [Production boundary and router pair](operate/01-Start-Routers.md): component responsibilities, the deployment set, the client path, and router pair startup.
- [Router state and placement monitoring](operate/02-Monitor.md): `/health` and `/ready`, the dashboard, and paging thresholds.
- [Engine restart and process replacement](operate/03-Restart-Engines.md): draining, replacing, and readmitting one engine or a whole wave, and activating replacement profiles.
- [Upgrade, rollback, and release drills](operate/04-Upgrade-and-Validate.md): router pair upgrade and rollback, and the release drills.
- [Troubleshoot a fleet](Troubleshoot.md): failure procedures.
- [Measure a fleet](Measure.md): production evidence and deployment validation.

## Production startup checklist

For a new or replaced production deployment:

1. Assemble one [deployment set](operate/01-Start-Routers.md#2-keep-one-deployment-set).
2. Confirm state handoff compatibility with `narwhal-check --print-contract-versions`.
3. Configure the [client path](operate/01-Start-Routers.md#3-configure-the-client-path): private control interfaces and trusted ingress rewriting.
4. Confirm both router hosts share the required [lease domain](operate/01-Start-Routers.md#4-start-a-router-pair).
5. Start the intended primary.
6. Start its standby from the same deployment set.
7. Verify `/ready`: HTTP 200 on the primary, HTTP 503 on the standby.
8. Verify `/health` on the active router reports `status: ok`.
9. Run the deployment workload through production ingress.
10. Verify dashboard collection and paging thresholds.
11. Run the [engine restart drill](operate/04-Upgrade-and-Validate.md#11-validate-every-release) for the configured restart policy.
12. Run the router failover drill through the production load balancer.
13. Open client admission.
