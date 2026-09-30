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

## What is Narwhal?

Narwhal is an adaptive LLM inference framework that moves engines between prefill and decode roles as demand changes, on a fixed GPU fleet with model weights already loaded.

<table width="100%" align="center">
  <thead>
    <tr>
      <th align="left" width="18%">Capability</th>
      <th align="left" width="52%">Behavior</th>
      <th align="left" width="30%">Guide</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Role hot-swap</td>
      <td>Reassigns prefill and decode roles across a fixed GPU fleet.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/">Core concepts</a></td>
    </tr>
    <tr>
      <td>Split routing</td>
      <td>Routes prefill and decode separately with NIXL key-value (KV) transfer.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/">Core concepts</a></td>
    </tr>
    <tr>
      <td>Latency-aware admission</td>
      <td>Admits and places requests from measured per-engine profiles.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Measure/">Measure a fleet</a></td>
    </tr>
    <tr>
      <td>Completion APIs</td>
      <td>Serves streaming and buffered completion and chat requests.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/HTTP-API/">HTTP API</a></td>
    </tr>
    <tr>
      <td>Router failover</td>
      <td>Promotes a warm-standby router.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Operate/">Ingress and maintenance</a></td>
    </tr>
    <tr>
      <td>Observability</td>
      <td>Exports Prometheus metrics and request journals to a Grafana dashboard.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Observability/">Prometheus and Grafana</a></td>
    </tr>
    <tr>
      <td>Generation-bound readmission</td>
      <td>Readmits an engine only against its live process generation.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/operate/03-Restart-Engines/">Restart engines</a></td>
    </tr>
    <tr>
      <td>Offline validation</td>
      <td>Validates fleet files and collects diagnostic bundles.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Config-Inspection/"><code>narwhal config</code></a>, <a href="https://athrael-soju.github.io/Narwhal/Diagnostic-Bundles/"><code>narwhal diagnostics</code></a></td>
    </tr>
    <tr>
      <td>Benchmark runs</td>
      <td>Runs ordered benchmark points with retained evidence.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/measure/05-Benchmark-Runner/">Ordered benchmark points</a></td>
    </tr>
    <tr>
      <td>Development mode</td>
      <td>Runs four engines on one NVIDIA CUDA GPU under Ubuntu or WSL2.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Dev-Runtime/">Narwhal dev</a></td>
    </tr>
  </tbody>
</table>

## How engines change roles

Each role controller pass:

- Estimates prefill and decode pressure against service-level objectives (SLOs) for the current role split and each adjacent split.
- Reads engine curves, offered demand, and resident work.
- Moves an engine when a candidate split improves the worst projected SLO ratio by the configured margin.
- Checks the candidate split against the role floor, cooldown, and health.

New requests follow the revised split.

Resident requests finish on their assigned engines.

![Role controller changing engine roles with weights resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

## Narwhal against Dynamo Planner and Ray Serve LLM

<p align="center">
  <a href="https://github.com/ai-dynamo/aiperf/releases/tag/v0.12.0">AIPerf v0.12.0</a> |
  <a href="https://huggingface.co/moonshotai/Kimi-K3">Kimi-K3</a> <a href="https://athrael.net/narwhal-evaluation-2026-09/chat-document-results/">chat/document</a> and <a href="https://athrael.net/narwhal-evaluation-2026-09/mixed-workload-results/">mixed-payload</a> workloads |
  <a href="https://athrael.net/posts/evaluating-narwhal#prefix-caching">Prefix caching enabled</a> |
  <a href="https://github.com/athrael-soju/Narwhal">Narwhal</a>, <a href="https://github.com/ai-dynamo/dynamo">Dynamo Planner</a>, <a href="https://github.com/ray-project/ray">Ray Serve LLM</a>
</p>

![Evaluation results for Narwhal, Dynamo Planner, and Ray Serve LLM.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/infographic.png)

## Get the commands

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

## Try it on one GPU

[Narwhal dev](https://athrael-soju.github.io/Narwhal/Dev-Runtime/) runs a local NVIDIA CUDA fleet on Ubuntu or Ubuntu under WSL2.

```bash
narwhal dev init/up/verify/status/down
```

| Template                      | Target                         | Check                              |
| ----------------------------- | ------------------------------ | ---------------------------------- |
| Installed two-engine template | GPUs with 8 GB of VRAM or less | Available memory at initialization |
| RTX 5090 template             | Four-engine configuration      | Measured configuration             |

## Bring up a fleet

The [Deploy a fleet](https://athrael-soju.github.io/Narwhal/Deploy/) guide runs these steps from a management workstation:

1. Inspect the target hardware and model.
2. Install an approved source revision.
3. Validate the running vLLM processes and KV paths.
4. Profile the engines and run preflight.
5. Run a capacity trial through an SSH tunnel to the router.

## Go deeper

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

## Built on Arrow

Narwhal's scheduling algorithms derive from [Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture](https://arxiv.org/abs/2505.11916) by Wu et al. (2025).

Citations: Arrow (algorithms) and Narwhal (software), both in [CITATION.cff](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff).

License: [Apache-2.0](https://github.com/athrael-soju/Narwhal/blob/main/LICENSE).