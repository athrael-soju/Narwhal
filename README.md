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
  <a href="https://github.com/athrael-soju/Narwhal/tree/main/docs">Documentation</a> |
  <a href="https://athrael-soju.github.io/Narwhal/Deploy/">Deployment</a> |
  <a href="https://athrael-soju.github.io/Narwhal/HTTP-API/">API reference</a> |
  <a href="https://github.com/athrael-soju/Narwhal/issues">Issues</a> |
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md">Contributing</a>
</p>

## About

Narwhal routes disaggregated LLM inference across vLLM engines, reallocating prefill and decode roles as demand changes while model weights stay loaded.

Narwhal provides:

- Hot-swap prefill/decode role assignment across a fixed GPU fleet.
- Separate prefill and decode routing with NIXL KV transfer.
- Latency-aware admission and placement using measured per-engine profiles.
- Streaming and non-streaming completion and chat APIs, including function tools and reasoning output where supported by the engine and model.
- Request deadlines, disconnect cancellation, bounded queues and optional retries.
- Engine health checks, transfer validation and warm-standby router failover.
- Prometheus metrics, request journals and a Grafana dashboard.

## Architecture

The controller prices adjacent fleet splits from request demand, resident work, and measured engine profiles. Role floors, cooldowns, and health checks govern moves; new requests follow the resulting split while resident requests complete on their assigned engines.

![Narwhal's reactive controller changes engine roles while model weights remain resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

See [Core concepts](https://athrael-soju.github.io/Narwhal/Core-Concepts/) for request flow and scheduling, and [Configuration](https://athrael-soju.github.io/Narwhal/Configuration/) for controller settings.

## Install from PyPI

Install the router commands on Linux with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-check --help
```

The [PyPI installation guide](https://github.com/athrael-soju/Narwhal/blob/main/docs/Install-from-PyPI.md) covers version checks and the fleet inputs needed after installation. A production deployment also uses an approved source checkout for host preparation and engine launch.

## Deploy a fleet

From a management workstation, [Deploy a fleet](https://athrael-soju.github.io/Narwhal/Deploy/) uses the private `.env` and host inspection to prepare router and GPU engine hosts, launch vLLM with NIXL, and verify a completion through Narwhal. Measure the workload through the private SSH route, reconcile the results, and inspect the fleet through Prometheus and Grafana.

## Documentation

- [Architecture and scheduling](https://athrael-soju.github.io/Narwhal/Core-Concepts/)
- [Fleet configuration](https://athrael-soju.github.io/Narwhal/Configuration/)
- [HTTP API](https://athrael-soju.github.io/Narwhal/HTTP-API/)
- [Fleet measurement](https://athrael-soju.github.io/Narwhal/Measure/)
- [Ingress, monitoring and maintenance](https://athrael-soju.github.io/Narwhal/Operate/)
- [Troubleshooting](https://athrael-soju.github.io/Narwhal/Troubleshoot/)

## Contributing

[Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) covers checkout setup, local checks and the pull request flow. Participation follows the [code of conduct](https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md), and the [security policy](https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md) covers vulnerability reports.

## Attribution and citation

Narwhal's scheduling algorithms derive from [Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture](https://arxiv.org/abs/2505.11916) by Wu et al. (2025). Cite Arrow for those algorithms and Narwhal for this software. [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff) contains both references.

License: [Apache-2.0](https://github.com/athrael-soju/Narwhal/blob/main/LICENSE).
