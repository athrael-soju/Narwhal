<p align="center">
  <img src="https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/social-preview.png" alt="The Narwhal logo, a black narwhal with a teal spiral tusk above the wordmark" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0 license">
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue" alt="Python 3.11 through 3.13">
  <img src="https://img.shields.io/badge/style-ruff-261230" alt="Lint and format by ruff">
  <img src="https://img.shields.io/badge/types-mypy-blue" alt="Types checked with mypy">
  <a href="https://pypi.org/project/narwhal-inference/"><img src="https://img.shields.io/pypi/v/narwhal-inference" alt="Latest PyPI version"></a>
</p>

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/">Documentation</a> |
  <a href="https://athrael-soju.github.io/Narwhal/Deploy/">Deployment</a> |
  <a href="https://athrael-soju.github.io/Narwhal/HTTP-API/">API reference</a> |
  <a href="https://github.com/athrael-soju/Narwhal/issues">Issues</a> |
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md">Contributing</a>
</p>

## About

Narwhal is a disaggregated LLM inference framework, which reallocates prefill and decode roles as demand changes while model weights stay loaded.

Narwhal provides:

- Hot-swap prefill/decode role assignment across a fixed GPU fleet.
- Separate prefill and decode routing with NIXL KV transfer.
- Latency-aware admission and placement using measured per-engine profiles.
- Streaming and non-streaming completion and chat APIs, including function tools and reasoning output where supported by the engine and model.
- Request deadlines, disconnect cancellation, bounded queues and optional retries.
- Engine health checks, transfer validation and warm-standby router failover.
- Prometheus metrics, request journals and a Grafana dashboard.

## Architecture

On regular controller passes, Narwhal prices the current and adjacent prefill/decode splits from measured engine curves, offered demand, and resident work, moving an eligible engine when a candidate improves the worst projected SLO ratio by the configured margin and passes role-floor, cooldown, and health checks. New requests follow the revised split while resident requests finish on their assigned engines.

![Narwhal's reactive controller changes engine roles while model weights remain resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

See [Core concepts](https://athrael-soju.github.io/Narwhal/Core-Concepts/) for request flow and scheduling, and [Configuration](https://athrael-soju.github.io/Narwhal/Configuration/) for controller settings.

## Benchmark snapshot

AlPerf v0.12.0 ran chat/document and mixed-payload Kimi-K3 workloads with prefix caching enabled across Narwhal, Dynamo Planner, and Ray Serve LLM.

![Completion rate, SLO-qualified requests, median time to first token and document answer quality for Narwhal, Dynamo Planner and Ray Serve LLM across chat/document and mixed-payload workloads.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/infographic.png)

## Install from PyPI

Install the router commands on Linux with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
python -m pip show narwhal-inference
narwhal-check --help
```

The wheel installs `narwhal-serve`, `narwhal-attest`, `narwhal-profile`, and `narwhal-check`. Record the version reported by `pip show` with the fleet configuration and engine image, then pin it across router hosts. The [PyPI installation guide](https://athrael-soju.github.io/Narwhal/Install-from-PyPI/) covers the engine and profile inputs required before serving requests.

## Run locally on an RTX 5090

Follow [Set up the WSL2 GPU runtime](https://athrael-soju.github.io/Narwhal/Dev-Runtime/) to install the pinned runtime and run four engines on one GPU with `narwhal dev init/up/verify/status/down`.

## Deploy a fleet

From a management workstation, an operator follows [Deploy a fleet](https://athrael-soju.github.io/Narwhal/Deploy/) to inspect the target hardware and model, install an approved source revision, validate the running vLLM processes and KV paths, then profile and preflight before routing traffic. The final gate measures the workload through the private path, reconciles client outcomes with the router journal, and checks Prometheus and Grafana.

## Reference

- [Architecture and scheduling](https://athrael-soju.github.io/Narwhal/Core-Concepts/)
- [Fleet configuration](https://athrael-soju.github.io/Narwhal/Configuration/)
- [CLI reference](https://athrael-soju.github.io/Narwhal/CLI-Reference/)
- [HTTP API](https://athrael-soju.github.io/Narwhal/HTTP-API/)
- [Fleet measurement](https://athrael-soju.github.io/Narwhal/Measure/)
- [Prometheus and Grafana](https://athrael-soju.github.io/Narwhal/Observability/)
- [Ingress and maintenance](https://athrael-soju.github.io/Narwhal/Operate/)
- [Troubleshooting](https://athrael-soju.github.io/Narwhal/Troubleshoot/)

## Contributing

[Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) covers checkout setup, local checks and the pull request flow. Participation follows the [code of conduct](https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md), and the [security policy](https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md) covers vulnerability reports.

## Attribution and citation

Narwhal's scheduling algorithms derive from [Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture](https://arxiv.org/abs/2505.11916) by Wu et al. (2025). Cite Arrow for those algorithms and Narwhal for this software. [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff) contains both references.

License: [Apache-2.0](https://github.com/athrael-soju/Narwhal/blob/main/LICENSE).
