# Router failover and rollback

## Router failover

### Primary router failed

Query `/ready` and `/narwhal/lifecycle` on both routers, then identify the lease holder, whose `/narwhal/lifecycle` reports `router.controls_fleet: true`. Send load-balancer traffic to that router once its `/ready` returns HTTP 200.

Check the active router against the last persisted state handoff. Its lease epoch should be higher than the failed primary's last epoch, and its roles and cumulative counters should match the handoff.

Restart the old primary by adding `--standby-of <active-router>` to its launch command, substituting the active router's URL for the placeholder. The recovered process returns HTTP 503 from `/ready`. It refuses direct completion requests.

When both routers report HTTP 503 from `/ready`, read the refusal reasons and fix what they name. For lease storage or clock-bound refusals, repair without changing the lease holder or fencing.

### State handoff is stale or incompatible

| Cause | Standby `/ready` |
| --- | --- |
| The previous lease epoch's state handoff expired, or the previous router exited before persisting one | HTTP 503, reason `no fresh handoff` |
| Contract version mismatch or wrong epoch in the state handoff | HTTP 503 |

Keep client traffic stopped while you restore a compatible router release and the [deployment set](../operate/01-Start-Routers.md#2-keep-one-deployment-set). If the handoff cannot be restored, open a maintenance window and set `recovery.resume: false` in the fleet configuration. Start one router from its opening roles by dropping `--resume` and `--standby-of` from the launch command. Admit traffic only after the old router has stopped or fenced itself.

## Router rollback

Follow [Roll back](../operate/04-Upgrade-and-Validate.md#103-roll-back).

When the router returns to service, run the drills in [Validate every release](../operate/04-Upgrade-and-Validate.md#11-validate-every-release).
