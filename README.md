<p align="center">
  <img src="https://raw.githubusercontent.com/athrael-soju/Narwhal/main/assets/social-preview.png" alt="The Narwhal logo, a black narwhal with a teal spiral tusk above the wordmark" width="100%">
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
  <a href="#getting-started">Getting started</a> |
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/docs/03-Deploy.md">Deployment</a> |
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/docs/09-API-and-Data-Reference.md">API reference</a> |
  <a href="https://github.com/athrael-soju/Narwhal/issues">Issues</a> |
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md">Contributing</a>
</p>

## About

Narwhal is a disaggregated LLM inference framework with adaptive prefill/decode scheduling, which reallocates running vLLM engines between prefill and decode as demand changes while weights remain loaded on every engine.

Narwhal provides:

- Hot-swap prefill/decode role assignment across a fixed GPU fleet.
- Separate prefill and decode routing with NIXL KV transfer.
- Latency-aware admission and placement using measured per-engine profiles.
- Streaming and non-streaming completion and chat APIs, including function tools and reasoning output where supported by the engine and model.
- Request deadlines, disconnect cancellation, bounded queues and optional retries.
- Engine health checks, transfer validation and warm-standby router failover.
- Prometheus metrics, request journals and a Grafana dashboard.

## Architecture

Each engine can execute both prefill and decode. The controller assigns roles using request demand, resident work and engine profiles, with configurable role floors, cooldowns and health checks. Role changes affect new request placement; existing requests remain tracked until completion.

![Narwhal's reactive controller changes engine roles while model weights remain resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/assets/architectures/hotswap.svg)

See [Core concepts](https://github.com/athrael-soju/Narwhal/blob/main/docs/02-Core-Concepts.md) for request flow and scheduling, and [Configuration](https://github.com/athrael-soju/Narwhal/blob/main/docs/07-Configuration.md) for controller settings.

## Benchmark snapshot

The infographic compares Narwhal, Dynamo Planner and Ray Serve LLM on two Kimi-K3 workloads measured with AlPerf v0.12.0 and prefix caching enabled.

![Completion rate, SLO-qualified requests, median time to first token and document answer quality for Narwhal, Dynamo Planner and Ray Serve LLM across chat/document and mixed-payload workloads.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/assets/infographic.png)

## Getting started

Install the router on Linux with Python 3.11+:

```bash
python -m pip install narwhal-inference
narwhal-serve --help
```

Narwhal connects to separately provisioned vLLM engines. The local walkthrough uses Git and Make:

```bash
git clone https://github.com/athrael-soju/Narwhal.git
cd Narwhal
make setup
```

`make setup` installs Narwhal and its development dependencies in `.venv`. The [getting started guide](https://github.com/athrael-soju/Narwhal/blob/main/docs/01-Get-Started.md) starts a six-engine stub fleet, profiles it, launches the router and sends an API request through the scheduling path.

The CPU walkthrough needs no credentials. For engine credentials and observability settings, copy [.env.example](https://github.com/athrael-soju/Narwhal/blob/main/.env.example) to `.env` and follow the [environment setup](https://github.com/athrael-soju/Narwhal/blob/main/docs/07-Configuration.md#environment-variables).

## GPU deployment

Narwhal supports vLLM with NIXL (`kv_both`) when every engine serves one model through a compatible KV layout and transfer topology. Operators provision and launch the engines, record the running contract in the generic fleet configuration, then collect profiles, preflight results and [deployment-load evidence](https://github.com/athrael-soju/Narwhal/blob/main/docs/06-Measure.md) for that exact hardware and tensor-parallel shape. Follow the [deployment guide](https://github.com/athrael-soju/Narwhal/blob/main/docs/03-Deploy.md) to bind the running fleet to Narwhal.

## Documentation

- [Architecture and scheduling](https://github.com/athrael-soju/Narwhal/blob/main/docs/02-Core-Concepts.md)
- [Fleet configuration](https://github.com/athrael-soju/Narwhal/blob/main/docs/07-Configuration.md)
- [API compatibility and limits](https://github.com/athrael-soju/Narwhal/blob/main/docs/09-API-and-Data-Reference.md#response-compatibility)
- [Fleet measurement](https://github.com/athrael-soju/Narwhal/blob/main/docs/06-Measure.md)
- [Ingress, monitoring and maintenance](https://github.com/athrael-soju/Narwhal/blob/main/docs/04-Operate.md)
- [Troubleshooting](https://github.com/athrael-soju/Narwhal/blob/main/docs/05-Troubleshoot.md)

## Contributing

[Contributing](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) covers checkout setup, local checks and the pull request flow. Participation follows the [code of conduct](https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md), and the [security policy](https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md) covers vulnerability reports.

## Attribution and citation

Narwhal's scheduling algorithms derive from [Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture](https://arxiv.org/abs/2505.11916) by Wu et al. (2025). Cite Arrow for those algorithms and Narwhal for this software. [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff) contains both references.

License: [Apache-2.0](https://github.com/athrael-soju/Narwhal/blob/main/LICENSE).
