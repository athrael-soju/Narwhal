---
description: Roll out and roll back Narwhal upgrades with compatible state handoff versions.
---

# Upgrade and rollback

## Upgrade and rollback

### Rolling upgrade with compatible handoff versions

#### Upgrading the standby

1. Check that both releases share a [handoff version](01-Start-Routers.md#keeping-one-deployment-set).
2. Record the active router's `ha.epoch` from `/narwhal/state`.
3. Stop the standby.
4. Install the new deployment set on that host.
5. Start the upgraded router as standby.
6. Check that `/health` returns HTTP 200 on the new standby.
7. Check that `/ready` returns HTTP 503 on the new standby.

#### Handing off to the upgraded router

1. Stop the old active router gracefully.
2. Verify the upgraded router's `ha.epoch` exceeds the epoch recorded in [Upgrading the standby](#upgrading-the-standby).
3. Check that it is the only backend returning HTTP 200 from `/ready`.

#### Upgrading the former active router

1. Install the same deployment set on the stopped router's host.
2. Start that router as standby.
3. Wait for its `/ready` to return HTTP 503.

### Upgrading across a handoff-version change

Incompatible handoff versions need a maintenance window.

1. Stop ingress.
2. Stop both routers.
3. Install the same deployment set on both hosts.
4. Start the intended primary.
5. Start its standby.
6. Confirm the primary holds the lease.
7. Confirm the primary is the only backend returning HTTP 200 from `/ready`.
8. Restore ingress.

### Rolling back

1. Remove the router you are rolling back from the load balancer.
2. Stop the new router gracefully.
3. Inspect the rollback build's contract support:

    ```bash
    narwhal-check --print-contract-versions
    ```

4. Restore these as one unit:
    - the code
    - the configuration
    - the profiles
    - the first-token calibration artifact for the live process generations
    - a state handoff the restored build supports
5. If process generations changed, regenerate the profiles and [first-token calibration](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline) before preflight.
6. For fresh roles and zeroed counters, set `recovery.resume: false` and drop `--resume`.
7. Confirm exactly one router holds the lease: the rollback router or its fenced peer.
8. Start the rollback build.
9. Check that `/health` reports `status: ok`.
10. Check that `/ready` returns HTTP 200.
11. Check that roles and cumulative counters are present.
12. Send one completion request and confirm it succeeds.
13. Return the router to service.
14. Restore its standby.

### Upgrading across the contract field rename

| Earlier name                                | Current name               |
| ------------------------------------------- | -------------------------- |
| `vllm_version` contract field               | `engine_version`           |
| `nixl_version` contract field               | `transfer_version`         |
| `nixl_connector_version` contract field     | `connector_version`        |
| `engine.vllm_version` attestation field     | `engine.version`           |
| `capture-nixl` contract tool action         | `capture-connector`        |
| `nixl_transfer_*` preflight evidence fields | `transfer_*`               |
| `ports.nixl_first` dev template port        | `ports.side_channel_first` |

Fleet files, attestation documents and dev templates with the earlier names still load. The rename changes the contract fingerprint and the profile generation digests. Upgrade in this order:

1. Upgrade the routers as in [Rolling upgrade with compatible handoff versions](#rolling-upgrade-with-compatible-handoff-versions).
2. Restart every `narwhal-attest` sidecar on the upgraded build.
3. Regenerate the profiles and [first-token calibration](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline).
4. Run preflight.

### Upgrading to engine-neutral launch settings

Discovery and `narwhal dev` write [engine-neutral launch settings](../configuration/05-Engine-Launch.md#engine-neutral-launch-settings) in `runtime` in place of these `extra_args` flags:

| Earlier `extra_args` flag                                                                       | Current `runtime` field |
| ----------------------------------------------------------------------------------------------- | ----------------------- |
| vLLM `--max-model-len`, SGLang `--context-length`                                               | `context_length`        |
| vLLM `--max-num-seqs`, SGLang `--max-running-requests`                                          | `max_sequences`         |
| vLLM `--tokenizer`, SGLang `--tokenizer-path`                                                   | `tokenizer`             |
| vLLM `--enable-prefix-caching` or `--no-enable-prefix-caching`, SGLang `--disable-radix-cache`  | `prefix_caching`        |
| vLLM `--kv-events-config` with `{"enable_kv_cache_events": false}`                              | `kv_events: false`      |

The side-channel port variable `NARWHAL_KV_SIDE_CHANNEL_PORT` replaces `NARWHAL_NIXL_SIDE_CHANNEL_PORT` and `NARWHAL_SGLANG_BOOTSTRAP_PORT`, which still apply while it is unset.

Launch records with the earlier flags still load, and the launcher passes the same arguments for them. A record written with the new fields passes those flags in another position, which gives the engine a new [`launch_digest`](../configuration/01-Fleet-Schema.md#attestation). After you regenerate launch records, regenerate the profiles and [first-token calibration](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline), then run preflight.

### Upgrading to engine-neutral evidence names

`narwhal-engine` writes `checked.json` and `shared-start.json` with these field names:

| File                | Earlier names                        | Current name     |
| ------------------- | ------------------------------------ | ---------------- |
| `checked.json`      | `vllm_api_version`, `sglang_version` | `engine_version` |
| `shared-start.json` | `vllm_version`, `sglang_version`     | `engine_version` |
| `shared-start.json` | `vllm_args`, `sglang_args`           | `engine_args`    |

Launch directories with the earlier names still load, and a repeated `check` accepts a `checked.json` that records the same values under them. A file that holds both an earlier and a current name for one field is rejected. The attestation document and its `launch_digest` do not change.
