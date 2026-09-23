<p align="center">
  <img src="https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/social-preview.png" alt="The Narwhal logo, a black narwhal with a teal spiral tusk above the wordmark" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0 license">
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue" alt="Python 3.11 through 3.13">
  <a href="https://pypi.org/project/narwhal-inference/"><img src="https://img.shields.io/pypi/v/narwhal-inference" alt="Latest PyPI version"></a>
</p>

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/">Documentation</a> |
  <a href="https://athrael-soju.github.io/Narwhal/Deploy/">Deploy a fleet</a> |
  <a href="https://athrael-soju.github.io/Narwhal/HTTP-API/">HTTP API</a> |
  <a href="https://github.com/athrael-soju/Narwhal/issues">Issues</a> |
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md">Contributing</a>
</p>

# Narwhal

Narwhal routes disaggregated LLM inference across a fleet of dual-capability vLLM engines. For each request, it prices eligible prefill engines from measured profiles and resident work, then keeps decode local or transfers KV over NIXL to another engine. A controller shifts logical prefill and decode roles as demand changes while model weights stay loaded.

## Install the router

On Linux with Python 3.11 or newer, install the `narwhal-inference` distribution:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
python -m pip show narwhal-inference
narwhal-check --help
```

The wheel installs `narwhal-serve`, `narwhal-attest`, `narwhal-profile`, and `narwhal-check`. A serving fleet also needs compatible vLLM engines, a fleet configuration, engine attestation, and profiles measured on the deployed hardware. The [PyPI installation guide](https://athrael-soju.github.io/Narwhal/Install-from-PyPI/) covers version pinning and those deployment inputs.

## Request flow and role control

Narwhal assigns an available serving seat or queues the request in a bounded FIFO, then selects a prefill engine by projected time to first token. The selected engine processes the prompt and returns a typed KV handoff. Decode runs on that engine or consumes the handoff on another eligible engine; the router tracks deadlines, retries, token timing, and the final outcome in its request journal.

Clients use streaming or non-streaming `/v1/completions` and `/v1/chat/completions`. Router health checks and warm-standby handoff govern availability; `/metrics` exposes request and fleet telemetry.

On controller passes, Narwhal prices the current and adjacent prefill/decode splits against offered demand and measured engine curves. It moves an eligible engine when the candidate improves the worst projected SLO ratio and passes role-floor, cooldown, resident-work, and health checks. New requests use the revised split while resident requests finish on their assigned engines.

![Narwhal's reactive controller changes engine roles while model weights remain resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

[Core concepts](https://athrael-soju.github.io/Narwhal/Core-Concepts/) covers request placement and role changes; [Configuration](https://athrael-soju.github.io/Narwhal/Configuration/) defines the controller guards and serving limits.

## Deploy a fleet

From a management workstation, an operator follows [Deploy a fleet](https://athrael-soju.github.io/Narwhal/Deploy/) to inspect the target hardware and model, prepare router and engine hosts from an approved source revision, check each running vLLM process and KV path, then profile and preflight the fleet before routing traffic. The final gate measures the workload through the private path and reconciles client outcomes with the router journal while Prometheus and Grafana monitor the fleet.

## Benchmark snapshot

AlPerf v0.12.0 ran chat/document and mixed-payload Kimi-K3 workloads with prefix caching enabled across Narwhal, Dynamo Planner, and Ray Serve LLM.

![Completion rate, SLO-qualified requests, median time to first token and document answer quality for Narwhal, Dynamo Planner and Ray Serve LLM across chat/document and mixed-payload workloads.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/infographic.png)

## Documentation

| Task | Guide |
| --- | --- |
| Install and pin the Python distribution | [Install from PyPI](https://athrael-soju.github.io/Narwhal/Install-from-PyPI/) |
| Bring up and validate a GPU fleet | [Deploy a fleet](https://athrael-soju.github.io/Narwhal/Deploy/) |
| Configure roles, admission, and engine contracts | [Configuration](https://athrael-soju.github.io/Narwhal/Configuration/) |
| Run router, attestation, profiling, and preflight commands | [CLI reference](https://athrael-soju.github.io/Narwhal/CLI-Reference/) |
| Send requests and inspect router state | [HTTP API](https://athrael-soju.github.io/Narwhal/HTTP-API/) |
| Measure workload capacity and reconcile results | [Measure a fleet](https://athrael-soju.github.io/Narwhal/Measure/) |
| Monitor and maintain the service | [Observability](https://athrael-soju.github.io/Narwhal/Observability/), [Operations](https://athrael-soju.github.io/Narwhal/Operate/), [Troubleshooting](https://athrael-soju.github.io/Narwhal/Troubleshoot/) |

## Contributing and citation

[Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) covers checkout setup, local checks and the pull request flow. Participation follows the [code of conduct](https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md); the [security policy](https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md) covers vulnerability reports.

Narwhal's scheduling algorithms derive from [Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture](https://arxiv.org/abs/2505.11916) by Wu et al. (2025). Cite Arrow for those algorithms and Narwhal for this software. [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff) contains both references.

License: [Apache-2.0](https://github.com/athrael-soju/Narwhal/blob/main/LICENSE).
