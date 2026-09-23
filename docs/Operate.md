# Narwhal Production Operations

Narwhal controls admission and placement for one model fleet through one active router. Each fleet runs a router pair against a shared lease domain. The lease grants admission authority to one router and fences its peer as standby.

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

## Production startup checklist

For a new or replaced production deployment:

1. Assemble one release, fleet configuration, profile store, and evidence set.
2. Confirm router handoff compatibility with `narwhal-check --print-contract-versions`.
3. Configure private control interfaces and trusted ingress rewriting.
4. Confirm both router hosts share the required lease domain.
5. Start the intended primary.
6. Start its standby from the same deployment set.
7. Verify lease ownership through `/ready`.
8. Verify process and fleet state through `/health`.
9. Run the deployment workload through production ingress.
10. Verify dashboard collection and paging thresholds.
11. Run the engine lifecycle procedure permitted by the configured restart policy.
12. Run router failover through the production load balancer.
13. Open client admission.

Use [Troubleshoot a fleet](Troubleshoot.md) for failure procedures.

Use [Measure a fleet](Measure.md) for production evidence and deployment validation.
