# Engine and whole-wave recovery

Lifecycle requests come from the management host described in [Engine restart and process replacement](../operate/03-Restart-Engines.md). The fleet needs a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract).

## Engine failure

### One engine failed unexpectedly

Check that the router ejected or quarantined the engine and that the other engines still get work. Save the engine boot log and the supervisor exit reason before you restart the process.

The steps depend on `recovery.engine_restart_policy`.

For `individual` recovery:

1. Start the engine and verify its endpoints per [Replace the process](../operate/03-Restart-Engines.md#72-replace-the-process).
2. Restart its attestation sidecar through the configured process manager and read the sidecar log.
3. Repair any named endpoint or contract failure.
4. If the engine process changed, [activate fresh profiles while preserving its hold](../operate/03-Restart-Engines.md#activate-replacement-profiles), then request readmission. A stale profile holds the engine under the `individual` policy until fresh profiles are active.
5. Follow `/narwhal/lifecycle` while the router readmits the engine.
6. Confirm `accepts_new: true`.

If the lifecycle state becomes `blocked`, repair the failure named in `engines.<id>.error`. Then send a `POST` request to `/narwhal/lifecycle/readmit`.

For `whole_wave`, follow [Whole-wave recovery](#whole-wave-recovery).

For a planned restart, see [Restart one engine](../operate/03-Restart-Engines.md#7-restart-one-engine).

## Whole-wave recovery

Use whole-wave recovery when `recovery.engine_restart_policy` is `whole_wave`, or after a stale-peer assertion, a transfer stall that kills a peer, or a process replacement that invalidates shared peer state.

1. [Start a whole-wave drain](../operate/03-Restart-Engines.md#81-drain-the-wave).
2. If drain identity capture fails, follow [Recover an unplanned whole-wave hold](../operate/03-Restart-Engines.md#83-recover-an-unplanned-whole-wave-hold).
3. Wait until `wave.ready_to_stop` is `true` and `/ready` returns HTTP 503.
4. Stop every engine process tree through the external supervisor and verify accelerator memory is held only by the intended worker processes.
5. Launch every engine from the same immutable image and launch contract, each with a fresh attestation sidecar.
6. While the wave remains held, [activate the replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles).
7. Request whole-wave readmission.
8. Wait for fabric validation to pass.
9. Confirm `/ready` returns HTTP 200.
10. Restore ingress.

Then run the drills in [Validate every release](../operate/04-Upgrade-and-Validate.md#11-validate-every-release).
