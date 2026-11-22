---
description: Operate a Narwhal fleet with a warm-standby router pair in a shared lease domain.
---

# Operating Narwhal

Each model fleet runs one router pair in a shared lease domain. The lease holder admits and places requests, and its peer runs as standby.

```text
clients
  |
TLS, authentication, WAF, model routing
  |---------------- router pair A ---------------- fleet A, model A
  |---------------- router pair B ---------------- fleet B, model B
  `---------------- router pair C ---------------- fleet C, model C
```

## Operator tasks

- [Production boundary and router pair](operate/01-Start-Routers.md): set the production boundary, configure the client path, and start a router pair.
- [Router state and placement monitoring](operate/02-Monitor.md): read router state and watch placement and control.
- [Engine restart and process replacement](operate/03-Restart-Engines.md): restart one engine or a wave, activate replacement profiles, and detect process replacement.
- [Upgrade and rollback](operate/04-Upgrade-and-Validate.md): upgrade routers across compatible or changed handoff versions, and roll back.
- [Release drills](operate/05-Release-Drills.md): run restart, whole-wave, and failover drills for every release.
- [Controlling the fleet](operate/06-Controlling-the-Fleet.md): run engine actions, configuration overlays, and load jobs from the control service and its console.
- [Admission, queue and retry settings](operate/07-Admission-Queue-and-Retry-Settings.md): choose each setting from the client outcomes it produces.
- [Troubleshooting a fleet](Troubleshoot.md): start from the first visible symptom.
- [Measuring a fleet](Measure.md): profile the fleet, calibrate SLOs, and measure a target workload.
