# Engine and whole-wave recovery

Prerequisites:

- the management host from [Engine restart and process replacement](../operate/03-Restart-Engines.md) for lifecycle requests;
- a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) in the fleet configuration.

## Engine failure

### One engine failed unexpectedly

1. Check that the router ejected or quarantined the engine.
2. Check that the other engines receive work.
3. Save the engine boot log and the supervisor exit reason before you restart the process.

Choose the procedure:

| Case | Procedure |
| --- | --- |
| `recovery.engine_restart_policy` is `individual` | The steps below |
| `recovery.engine_restart_policy` is `whole_wave` | [Whole-wave recovery](#whole-wave-recovery) |
| Planned restart | [Restart one engine](../operate/03-Restart-Engines.md#7-restart-one-engine) |

For `individual` recovery:

1. Start the engine.
2. Verify its endpoints per [Replace the process](../operate/03-Restart-Engines.md#72-replace-the-process).
3. Restart its attestation sidecar through the configured process manager.
4. Read the sidecar log.
5. Repair each named endpoint or contract failure.
6. If the engine process changed:
    1. [Activate fresh profiles while preserving its hold](../operate/03-Restart-Engines.md#activate-replacement-profiles).
    2. Request readmission.
7. Follow `/narwhal/lifecycle` while the router readmits the engine.
8. Confirm `accepts_new: true`.

If the lifecycle state becomes `blocked`:

1. Repair the failure named in `engines.<id>.error`.
2. Send a `POST` request to `/narwhal/lifecycle/readmit`.

## Whole-wave recovery

Use whole-wave recovery when `recovery.engine_restart_policy` is `whole_wave`, or after one of these events:

- a stale-peer assertion;
- a transfer stall that kills a peer;
- a process replacement that invalidates shared peer state.

1. [Start a whole-wave drain](../operate/03-Restart-Engines.md#81-drain-the-wave).
2. If drain identity capture fails, follow [Recover an unplanned whole-wave hold](../operate/03-Restart-Engines.md#83-recover-an-unplanned-whole-wave-hold).
3. Wait until `wave.ready_to_stop` is `true` and `/ready` returns HTTP 503.
4. Stop every engine process tree through the external supervisor.
5. Verify that only the intended worker processes hold accelerator memory.
6. Launch every engine from the same immutable image and launch contract, each with a fresh attestation sidecar.
7. While the wave remains held, [activate the replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles).
8. Request whole-wave readmission.
9. Wait for fabric validation to pass.
10. Confirm `/ready` returns HTTP 200.
11. Restore ingress.
12. Run the drills in [Validate every release](../operate/04-Upgrade-and-Validate.md#11-validate-every-release).
