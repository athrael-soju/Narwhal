---
description: Engine monitoring, failure handling, readmission and durable control-plane state in Narwhal.
---

# Failure, readmission, and state

The router learns that an engine is failing from the requests it sends and from a monitoring pass that runs every `controller.monitor_interval_s`. Failures build evidence against the engine, the evidence leads to a hold or an ejection, and a probe or a validated readmission returns the engine to placement. The router also saves the state that a restarted or standby router needs to take over.

## Monitoring and readiness

A monitoring pass runs the stages below. When a stage fails, the router records the failure and carries on with the rest of the pass.

| Stage              | Work                                                                                  |
| ------------------ | ------------------------------------------------------------------------------------- |
| `controller`       | Role-controller evaluation                                                            |
| `health`           | Decode performance-drift checks against each engine's profile                         |
| `drains`           | Closing the drain of each role change whose remaining work has finished               |
| `rollover`         | Closing the interval's telemetry windows and opening the next                         |
| `readmission`      | Readmission and [peer release](#peer-memory-release) rounds                           |
| `transfer_refresh` | [Transfer state refresh](#transfer-state-refresh) of SGLang decode engines            |
| `liveness`         | [Liveness](#liveness) probes every `recovery.liveness_every` intervals                |
| `residency`        | Prefix residency refresh from attestation sidecars, on its own loop once per interval |
| `handoff`          | Writing the [state handoff](#durable-control-plane-state)                             |
| `telemetry`        | Floor-state refresh and loop logging                                                  |

Consecutive passes with a failed stage form the monitor-failure streak. When the streak reaches `controller.monitor_failure_limit`, the router degrades. It stops admitting requests and placing prefill legs, so new requests and requests waiting for prefill receive HTTP 503, while requests past prefill keep decoding. `/ready` returns HTTP 503 with the reason `monitoring degraded: <stage> <class>`, naming the stage and error class of the streak's first failure. Monitoring continues, and a warm standby counts each degraded readiness answer as a missed probe toward its takeover threshold.

The first pass in which every stage succeeds ends the streak and reopens admission. A router restart resets the monitor-failure counters.

## Engine failure handling

### Connection pools

The router reaches engines through two kinds of connection pool:

| Pool | Scope | Carries | Bound | Client |
| --- | --- | --- | --- | --- |
| Data | One pool per engine | Token counting, prefill and decode | `serving.max_connections` open connections to each engine, and up to half that number kept alive | HTTP/1.1 on asyncio transports with the `httptools` response parser |
| Control | One pool shared by all engines | Health probes and inference probes | `engine.control_connections` open connections across all engines, and up to half that number kept alive | HTTPX |

A leg that times out waiting for a connection in the router's pool leaves the engine's failure streaks unchanged.

### Failure evidence

The router sorts each failed leg into a class and keeps a separate streak of consecutive failures per class for each engine. When a streak reaches `recovery.eject_after`, the router acts on the class:

| Failure | Class | Action |
| --- | --- | --- |
| Connect failure or connect timeout | `connection` | Eject the engine |
| Read timeout on a decode leg, including one before the response headers | `stream` | Run an inference probe under a placement hold |
| Any other transport timeout | `timeout` | Run a health probe |
| [Dropped connection](../telemetry/04-Failures.md#dropped-connections) after the request was sent | `timeout` | Run a health probe |
| First-token deadline while the engine emits other output | `overload` | Run a health probe |
| First-token deadline from a silent engine, silence between tokens, or a stream that ends without `[DONE]` or before any token | `stream` | Run an inference probe under a placement hold |
| HTTP 408 or 429 | `overload` | Run a health probe |
| Other HTTP 5xx response | `inference_status` | Run an inference probe under a placement hold |
| vLLM prefill response without a readable KV handoff | `kv_handoff` | Run an inference probe under a placement hold |

A 4xx response other than 408 or 429 counts as a successful answer and [clears failure evidence](#clearing-failure-evidence).

Whether a failure can take an engine out of placement depends on coverage. An engine is covered when the other live engines can still place every role it places. On vLLM, a pinned engine places its own role, so it is covered when another live engine holds that role or is unpinned, and an unpinned engine places both roles, so it is covered when prefill and decode each have another live engine that holds the role or is unpinned. On SGLang, only an engine that holds a role places it, so a pinned engine is covered when another live engine holds its role, and an unpinned engine when other live engines hold both roles. An uncovered engine has [last-engine protection](#last-engine-protection).

A recovery probe clears evidence and lifts holds once the loaded profiles match the engine's live process generation. Under this profile-match rule, the router ejects an engine whose generation differs from its profiles or cannot be read.

### Inference probes

An inference probe sends one request through a prefill leg and a decode leg to retest the path that failed. Each `stream` failure on a decode leg records the leg's producer against the decode engine, and the probe retests every recorded path. A failed producer leg leaves the held engine untested, so the next producer runs the probe.

On vLLM, the [prefill leg of each path](../telemetry/04-Failures.md#inference-probe-producers) runs on the recorded producer while it is live, then on one other live producer, and last in a standalone probe that runs both legs on the held engine. An engine held without a recorded producer, such as one held for `inference_status` or `kv_handoff` evidence, takes the standalone probe.

On SGLang, each engine serves only its live role, so the probe pairs the held engine with a live peer in the other role, and the two legs run together. A held prefill engine produces for each live decode engine in turn, moving to the next when a decode leg fails. A held decode engine takes the prefill leg from its recorded producer while it is live, and then from one other live prefill engine. A held decode engine without a recorded producer, such as one held for `inference_status` evidence, takes it from that other engine alone. When no peer in the other role is live, the probe is `inconclusive` with the reason `no_producer` or `no_consumer`.

A passing probe that meets the profile-match rule clears the recorded inference failures and lifts the hold. A failed leg on the held engine ejects it when it is covered, and returns it to placement when it is uncovered. An inconclusive probe keeps the hold, and engine monitoring probes again every `recovery.readmit_every` monitor intervals.

### Liveness

Every `recovery.liveness_every` monitor intervals, the router probes `/health` on each engine except ejected and draining ones, and ejects an engine after `recovery.liveness_misses` consecutive misses. A probe that waits out the control pool leaves the miss count unchanged. A successful probe resets the miss count and clears the connection, timeout and overload streaks. On an engine in timed quarantine that meets the profile-match rule, it also ends the quarantine.

On a fleet with `engine_contract`, the same sweep checks the process identity, attestation and profile generation of each engine that answers, and ejects an engine whose process start changed or whose identity it cannot verify. The router reads a vLLM engine's version from `/version` and its process start from `/metrics`. It reads an SGLang engine's version from `/server_info` and its process start from the attestation sidecar.

### Last-engine protection

Coverage keeps the last engine for a role serving. An uncovered engine stays in placement when it drifts past its profile, when a failure would start a timed quarantine or an inference hold, and after a failed health or inference probe. When another engine's ejection or drain leaves a held engine uncovered, the hold ends and the engine returns to placement.

These ejections take an engine out of placement whether or not it is covered: a connection-class streak, missed liveness probes, a changed or unverifiable process identity, a profile generation mismatch, and a failed SGLang role switch.

At zero serving capacity, `/ready` and new completion requests return HTTP 503.

### Failure quarantine

With `recovery.failure_quarantine_s` above `0`, a [covered](#failure-evidence) engine whose request leg fails leaves placement until the quarantine deadline passes, and then candidate selection releases it. Every failed leg starts a quarantine except a 4xx response other than 408 or 429 and a wait for a connection in the router's pool. A successful health or inference probe that meets the profile-match rule ends the quarantine early, and so does another engine's ejection or drain that leaves the engine uncovered.

### Clearing failure evidence

A successful answer resets the streaks of the classes it shows to be healthy:

| Successful response | Failure evidence cleared |
| --- | --- |
| 4xx response other than 408 or 429 | Connection and inference-status |
| Health 200 | Connection, timeout, overload, and liveness |
| Prefill with an extracted KV handoff | Connection, inference-status, and KV-handoff |
| Decode stream reaching its terminal marker | Connection, inference-status, and stream |
| Passing inference probe or validated readmission | Connection, inference-status, KV-handoff, and stream |

A health 200 or a passing inference probe that meets the profile-match rule also lifts the engine's ejection and hold. An engine awaiting an inference probe keeps its hold until that probe passes.

### Peer memory release

Peer release applies to vLLM engines that share a host and exchange KV through CUDA IPC. Each engine maps the KV memory of its host peers, and a stopped engine's GPU memory stays allocated while a peer holds its mapping. SGLang engines need no release.

With the CUDA IPC cache off and UCX 1.22 or later in the engine image, a consumer unmaps the memory of each producer idle for longer than `engine_ttl` when it starts a handshake or sends a KV lease heartbeat. The engine launcher sets `engine_ttl` to 60 seconds for an engine with CUDA IPC peers when `runtime.environment` turns `UCX_CUDA_IPC_CACHE` off with `n`, `no` or `0`. The router's release rounds trigger this check by sending each live KV consumer a transfer probe from another live producer.

`UCX_CUDA_IPC_CACHE` in `runtime.environment` chooses how CUDA IPC engines behave:

- At `y`, the UCX default, a stopped peer's GPU memory stays mapped until a wave restart, and transfers from a restarted or idle-evicted producer go through the cached mapping.
- At `n`, with UCX 1.22 or later, release rounds free that memory, and each transfer from a restarted or idle-evicted producer maps and unmaps the producer's KV memory. Transfers and decoding slow as the engines serve, until the engines restart.

On an eight-engine fleet with the cache off, decode engines lost up to 64% of their decode rate, and mean NIXL transfer time per decode engine was 105 to 124 ms after about 4,300 transfers, against 15 to 22 ms after about 3,350 transfers with the cache on.

`launch.peer_release` in a vLLM engine's attestation, and the same value in the image check's [`checked.json`](../configuration/05-Engine-Launch.md#runtime-launch-records-and-image-verification), report whether the engine releases a stopped peer's memory. It is `true` for an engine with zero CUDA IPC peers and for an engine with UCX 1.22 or later and the CUDA IPC cache off, and `false` for every other CUDA IPC engine.

#### Release rounds

On a vLLM fleet under `recovery.engine_restart_policy` `individual`, the router runs release rounds for each engine out of placement, at 65, 125, 245, 485, 965, 1925 and 3845 seconds after a start point. The start point is the engine's ejection, or its entry into lifecycle state `drained`, `deadline_exceeded`, `validating` or `blocked`, and each change between those states starts the schedule again.

When a consumer's probe fails on the producer leg, cannot connect to the consumer, waits out the control pool, or raises an error, the router retries that consumer once, 5 seconds later, through the next producer. A consumer with zero other live producers gets a logged warning to recover with a whole-wave restart, and waits for the next round. `/narwhal/state` reports each engine's rounds in [`peer_release`](../http-api/05-Live-State.md#peer_release).

#### Startup wait

A vLLM engine started by `narwhal-engine` gives its peers time to release memory before vLLM measures the GPU. It checks free memory every 5 seconds, logging `waiting for KV peers to release it`, until free memory reaches the share that `--gpu-memory-utilization` requests or 180 seconds pass. That wait spans the first two release rounds after an ejection.

#### Whole-wave fallback

With `engine_contract` set and the `individual` restart policy, `narwhal-check` warns about each producer that shares a host with a KV consumer when that consumer cannot release a stopped producer's memory: its `launch.peer_release` attestation is `false`, missing or unreadable, or the fleet has no other producer to send it a release probe. Recover a crashed engine in either layout with a [wave restart](../operate/03-Restart-Engines.md#restart-an-engine-wave).

### Transfer state refresh

On SGLang, a decode engine keeps the transfer endpoint it learned from each prefill engine until the decode engine itself switches roles, and a prefill engine opens a new endpoint every time it enters prefill. Once a producer has re-entered prefill, a decode engine that took handoffs from its earlier prefill period holds a stale endpoint, and handoffs between the two time out after 30 seconds.

A decode engine becomes stale for a producer when it takes a handoff from an earlier prefill period of that producer, and starts out stale for every producer until the router first switches it. The router refreshes a stale decode engine by switching it to prefill and back:

- At startup, after checking engine identities, the router refreshes each idle stale decode engine.
- Each monitoring pass refreshes one idle stale decode engine. When a producer has no decode engine that is fresh for it, the pass holds the stale decode engine with the fewest decode requests out of new placements and refreshes it once its requests finish.

Placement steers around stale pairs in the meantime. A decode leg goes to a decode engine that is stale for its producer only when every other decode engine is stale for that producer too, and prefill prefers producers that a fresh decode engine can take.

## Readmission and drains

How an ejected engine returns depends on the fleet's engine contract and restart policy. The router probes `/health` on each ejected engine every `recovery.readmit_every` monitor intervals.

With `engine_contract` set and `recovery.engine_restart_policy` `individual`, automatic recovery starts once an ejected engine's `/health` returns HTTP 200 and its attestation sidecar responds. It runs the [lifecycle readmission checks](../http-api/07-Handoff-and-Lifecycle.md#post-narwhallifecyclereadmit) in order, which need a complete `engine_contract`. On SGLang, the fabric check pairs the engine with the first live engine in the opposite role and fails when there is none. An engine that fails the checks stays blocked until an operator requests readmission, and the other ejected engines keep recovering one at a time while at least one engine remains in placement. When zero engines remain in placement, recovery runs as one [whole wave](../operate/03-Restart-Engines.md#recover-loss-of-every-placement-peer), and a blocked member holds the wave. Under the `whole_wave` policy, any ejection holds the fleet for a managed wave restart.

With `engine_contract` unset, automatic recovery checks health, or reruns the inference probe for an inference suspect, and matches profiles to the live process identity.

Profile checks cover every loaded variant. With `engine_contract` set, profile digests must match the verified attestation's `launch_digest` when the sidecar reports one, and its `attestation_digest` otherwise. A profile mismatch excludes the engine from recovery, readmission and takeover. The running router keeps its loaded profiles until it restarts.

An operator drain holds through healthy responses, resume and takeover until readmission clears it.

## Retries

An admitted request that fails transiently before it shows any output retries on engines whose legs have not failed for it, up to `serving.max_attempts` attempts, within its original deadline. Each retry spends one credit from a router-wide pool of `serving.retry_budget` credits, and each successful original request adds `serving.retry_replenish` credits. Under the defaults, a request retries once from a pool of 10 credits. [Retries](../http-api/03-Backend-and-Failures.md#retries) gives the placement rules and the failures that retry.

## Durable control-plane state

Each monitoring pass writes a state handoff that records engine roles, ejections, lifecycle holds, inference-verification holds with their recorded producers, consolidation risk, counters, and the lease holder. The router replaces the handoff file atomically, so a failed write leaves the previous handoff in place.

`narwhal-serve --resume` applies a saved handoff when its schema, engine set and engine restart policy match the configured fleet. With `engine_contract` configured, the handoff must also hold an accepted process identity for every engine it counts as available. Pinned engines take their configured roles, and saved ejections are probed for readmission at once. A saved inference suspect that another engine covers returns as an ejected engine due for a readmission probe. An engine whose readmission validation was running when the handoff was written resumes as `blocked`.

[Activating replacement profiles](../operate/03-Restart-Engines.md#activating-replacement-profiles) loads fresh measurements and keeps lifecycle holds and drain identities.

A warm standby router serves traffic while it holds the shared lease, and load balancers find the serving router through `/ready`.
