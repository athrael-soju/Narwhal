# Upgrade, rollback, and release drills

## 10. Upgrade and rollback

### 10.1 Rolling upgrade with compatible handoff versions

1. Stop the standby.
2. Install the new release and matching deployment set on that host.
3. Start it as standby.
4. Confirm `/health` returns HTTP 200.
5. Confirm `/ready` returns HTTP 503.
6. Stop the old active router cleanly.
7. Confirm the upgraded router owns a higher lease epoch.
8. Confirm the upgraded router is the only ready backend.
9. Upgrade the stopped router.
10. Return it as standby.

### 10.2 Upgrade across a handoff-version change

Use a maintenance window.

1. Stop ingress.
2. Stop both routers.
3. Install one coherent deployment set on both hosts.
4. Start the active router.
5. Start its standby.
6. Restore ingress after admission state has been verified.

### 10.3 Roll back

Stop the new process before the previous build can claim the fleet.

Restore these as one unit:

- code;
- configuration;
- profiles;
- a state version supported by the restored build.

## 11. Validate every release

Run the CPU process drills against every release artifact:

```bash
make integration
```

`make integration` starts local stub engines and routers for:

- individual lifecycle recovery;
- whole-wave lifecycle recovery;
- HA failover.

The HA drill kills the primary during a stream, verifies standby takeover, and checks that the recovered primary remains fenced.

Before production admission, repeat:

- the permitted engine-restart procedure;
- router failover;

on an idle fleet using the production supervisor and load balancer.

### Release drill pass conditions

| Drill                     | Pass condition                                                                                                                                                                   |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Individual engine restart | Placement stops after drain, resident work reaches zero, and every readmission gate passes                                                                                       |
| Whole-wave restart        | Every action follows wave policy, readiness is withdrawn before stop, and the fleet is readmitted only after every restarted engine passes attestation and the KV ring completes |
| Router failover           | The load balancer selects one lease owner, roles and cumulative counters survive, and the previous primary remains fenced                                                        |
