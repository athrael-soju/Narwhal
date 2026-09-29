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

Narwhal is an adaptive disaggregated LLM inference framework that reassigns prefill and decode roles as demand changes.

Features:

- Hot-swap prefill/decode role assignment across a fixed GPU fleet.
- Separate prefill and decode routing with NIXL key-value (KV) transfer.
- Latency-aware admission and placement driven by measured per-engine profiles.
- Streaming and buffered completion and chat APIs.
- Function tools and reasoning output on supporting engines and models.
- Request deadlines and disconnect cancellation.
- Bounded queues and optional retries.
- Engine health checks and transfer validation.
- Warm-standby router failover.
- Prometheus metrics and request journals.
- Grafana dashboard.

## Architecture

Each role controller pass:

- Estimates prefill and decode pressure against service-level objectives (SLOs) for the current role split and each adjacent split.
- Reads engine curves, offered demand, and resident work.
- Moves an engine when a candidate split improves the worst projected SLO ratio by the configured margin.
- Checks the candidate split against the role floor, cooldown, and health.

New requests follow the revised split, while resident requests finish on their assigned engines.

![Role controller changing engine roles with weights resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

## Evaluation snapshot

- Tool: AIPerf v0.12.0
- Workloads: Kimi-K3 chat/document and mixed-payload
- Prefix caching: enabled
- Systems: Narwhal, Dynamo Planner, Ray Serve LLM

![Evaluation results for Narwhal, Dynamo Planner, and Ray Serve LLM.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/infographic.png)

## Install from PyPI

Install on Linux with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

The wheel installs these commands:

- `narwhal`
- `narwhal-engine`
- `narwhal-serve`
- `narwhal-attest`
- `narwhal-profile`
- `narwhal-check`

Version steps:

1. Record the `narwhal-serve --version` output with the fleet configuration, engine image, and profiles.
2. Pin that version on every router host.

Guide: [PyPI installation](https://athrael-soju.github.io/Narwhal/Install-from-PyPI/)

## Narwhal dev

[Narwhal dev](https://athrael-soju.github.io/Narwhal/Dev-Runtime/) runs a local NVIDIA CUDA fleet on Ubuntu or Ubuntu under WSL2.

```bash
narwhal dev init/up/verify/status/down
```

| Template                      | Target                         | Check                              |
| ----------------------------- | ------------------------------ | ---------------------------------- |
| Installed two-engine template | GPUs with 8 GB of VRAM or less | Available memory at initialization |
| RTX 5090 template             | Four-engine configuration      | Measured configuration             |

## Deploy a fleet

[Deploy a fleet](https://athrael-soju.github.io/Narwhal/Deploy/) steps from a management workstation:

1. Inspect the target hardware and model.
2. Install an approved source revision.
3. Validate the running vLLM processes and KV paths.
4. Profile the engines and run preflight.
5. Run a capacity trial through an SSH tunnel to the router.

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

- [Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md)
- [Code of conduct](https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md)
- [Security policy](https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md)

## Attribution and citation

Narwhal's scheduling algorithms derive from [Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture](https://arxiv.org/abs/2505.11916) by Wu et al. (2025).

Citations: Arrow (algorithms) and Narwhal (software), both in [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff).

License: [Apache-2.0](https://github.com/athrael-soju/Narwhal/blob/main/LICENSE).