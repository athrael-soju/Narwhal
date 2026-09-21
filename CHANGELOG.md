# Changelog

## 0.1.0 (2026-09-20)

Narwhal's initial public release routes LLM inference across prefill and decode engines and changes their roles while model weights remain loaded.

### Serving and admission

- OpenAI-compatible completion and chat endpoints with streaming, input validation, and vLLM engine support.
- Profile-based request placement with predictive admission and bounded queueing.
- Optional transient retries restart the complete prefill/decode attempt before visible output. One deadline and a shared retry budget bound every original request.
- Deployment-owned engine credentials authenticate serving, profiling, preflight and lifecycle probes.
- Client cancellation releases capacity and bypasses the engine-failure counters. First-token deadlines remain active through metadata-only stream frames.
- Engine health monitoring, quarantine, ejection, and recovery of the configured prefill and decode floors.

### Fleet control

- One reactive controller moves engines between prefill and decode roles. Each decision scores the current split and its adjacent alternatives. Role pinning and minimum pool sizes constrain those changes.
- Decode capacity estimates account for active sequences and resident KV tokens. Profile coverage, demand evidence, resident work, and consolidation safety constrain role changes.
- Warm-standby control-plane handoff preserves router state across takeover.

### Deployment and validation

- Generic vLLM/NIXL `kv_both` fleet support binds one model, hardware shape and KV layout through fleet configuration, engine attestations and measured profiles. Operators own image selection and process launch.
- Kubernetes, Ansible, Terraform or site automation provisions hosts, distributes artifacts, configures routes and launches engines; Narwhal validates the running engine and KV-transfer contracts.
- Preflight checks cover engine health, process-bound compatibility, generation, profiles, SLOs, and the transfer fabric.
- Fleet schema 1 groups controller, serving, engine I/O, recovery and profile policy under
  their runtime owners. Ingress owns client identity and content capture; Narwhal owns one
  global admission budget and retains timings, identifiers, placements and outcomes.
- Profile and configuration validation reject invalid types, non-finite values, incomplete measured domains, and mismatched engine sets.
- Deployments record source identity and file digests.

### Measurement and operations

- Profiling measures per-engine prefill and decode curves; correctness canaries track exact output across live controller events.
- CPU lifecycle and failover drills run under `make check`.
- Per-request journals, controller state, Prometheus metrics, a Grafana dashboard, and
  engine-down alerts. One startup command derives router and engine scrape targets from the
  deployed fleet, then verifies target health and populated dashboard queries.

### Deployment qualification

- Collect per-engine profiles, live runtime evidence, exact-output canaries and deployment load results before serving client traffic.
- Validate prefix caching and speculative decoding against the selected model, engine build and KV layout before enabling them. Prefix caching requires exact-replay and continuation checks. Use whole-wave restart policy when the engine build shares peer registrations across the fleet.

See the [configuration reference](docs/07-Configuration.md) for defaults and the [measurement guide](docs/06-Measure.md) for deployment requirements.
