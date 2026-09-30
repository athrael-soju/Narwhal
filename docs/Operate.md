# Operate Narwhal

Each model fleet runs one router pair in a shared lease domain:

| Router | Role |
| --- | --- |
| Lease holder | Admits and places requests |
| Peer | Standby |

```text
clients
  |
TLS, authentication, WAF, model routing
  |---------------- router pair A ---------------- fleet A, model A
  |---------------- router pair B ---------------- fleet B, model B
  `---------------- router pair C ---------------- fleet C, model C
```

## Operator tasks

- [Production boundary and router pair](operate/01-Start-Routers.md)
- [Router state and placement monitoring](operate/02-Monitor.md)
- [Engine restart and process replacement](operate/03-Restart-Engines.md)
- [Upgrade, rollback, and release drills](operate/04-Upgrade-and-Validate.md)
- [Troubleshoot a fleet](Troubleshoot.md)
- [Measure a fleet](Measure.md)

## Production startup checklist

For a new or replaced production deployment:

1. Assemble one [deployment set](operate/01-Start-Routers.md#2-keep-one-deployment-set).
2. Confirm state handoff compatibility with `narwhal-check --print-contract-versions`.
3. Configure private control interfaces on the [client path](operate/01-Start-Routers.md#3-configure-the-client-path).
4. Configure trusted ingress rewriting on the client path.
5. Confirm both router hosts share the required [lease domain](operate/01-Start-Routers.md#4-start-a-router-pair).
6. Start the intended primary.
7. Start its standby from the same deployment set.
8. Verify `/ready` returns HTTP 200 on the primary.
9. Verify `/ready` returns HTTP 503 on the standby.
10. Verify `/health` reports `status: ok` on the active router.
11. Run the deployment workload through production ingress.
12. Verify dashboard collection.
13. Verify paging thresholds.
14. Run the [engine restart drill](operate/04-Upgrade-and-Validate.md#11-validate-every-release) for the configured restart policy.
15. Run the router failover drill through the production load balancer.
16. Open client admission.
