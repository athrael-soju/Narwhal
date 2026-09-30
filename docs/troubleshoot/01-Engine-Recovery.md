---
description: Recover failed vLLM engines and whole engine waves in a Narwhal fleet.
---

# Engine and whole-wave recovery

Prerequisites:

- the management host from [Engine restart and process replacement](../operate/03-Restart-Engines.md) for lifecycle requests
- a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) in the fleet configuration

## Engine failure

### One engine failed unexpectedly

1. Check that the router ejected or quarantined the engine.
2. Check that the other engines receive work.
3. Save the engine boot log.
4. Save the supervisor exit reason.

| Case | Procedure |
| --- | --- |
| `recovery.engine_restart_policy` is `individual` | The steps below |
| `recovery.engine_restart_policy` is `whole_wave` | [Whole-wave recovery](#whole-wave-recovery) |
| Planned restart | [Restart one engine](../operate/03-Restart-Engines.md#7-restart-one-engine) |

Steps for `individual` recovery:

1. Start the engine.
2. Verify its endpoints per [Replace the process](../operate/03-Restart-Engines.md#72-replace-the-process).
3. Restart its attestation sidecar through the configured process manager.
4. Read the sidecar log.
5. Repair each named endpoint or contract failure.
6. If the attested `launch_digest` changed or the sidecar reports an attestation digest only:
    1. [Activate fresh profiles while preserving its hold](../operate/03-Restart-Engines.md#activate-replacement-profiles).
    2. Request readmission.
7. Follow `/narwhal/lifecycle` until readmission completes.
8. Confirm `accepts_new: true`.

If the lifecycle state becomes `blocked`:

1. Repair the failure named in `engines.<id>.error`.
2. Send `POST /narwhal/lifecycle/readmit` with body `{"engines":["<id>"]}`.

## Whole-wave recovery

Triggers for whole-wave recovery:

- `recovery.engine_restart_policy` is `whole_wave`
- A stale-peer assertion
- A transfer stall that kills a peer
- A process replacement that invalidates shared peer state

1. [Start a whole-wave drain](../operate/03-Restart-Engines.md#81-drain-the-wave).
2. If drain identity capture fails, follow [Recover an unplanned whole-wave hold](../operate/03-Restart-Engines.md#83-recover-an-unplanned-whole-wave-hold).
3. Wait until `wave.ready_to_stop` is `true`.
4. Confirm `/ready` returns HTTP 503.
5. Stop every engine process tree through the external supervisor.
6. Verify that only the intended worker processes hold accelerator memory.
7. Launch every engine from the same immutable image and launch contract.
8. Start a fresh attestation sidecar for each engine.
9. While the wave is held, [activate one replacement profile store](../operate/03-Restart-Engines.md#activate-replacement-profiles) covering every engine whose attested `launch_digest` changed or whose sidecar reports an attestation digest only.
10. Request whole-wave readmission.
11. Wait for fabric validation to pass.
12. Confirm `/ready` returns HTTP 200.
13. Restore ingress.
14. Run the drills in [Validate every release](../operate/05-Release-Drills.md#11-validate-every-release).
