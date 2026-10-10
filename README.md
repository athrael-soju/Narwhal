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
  <a href="https://athrael-soju.github.io/Narwhal/"><img src="https://img.shields.io/badge/Documentation-0f766e" alt="Documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Deploy/"><img src="https://img.shields.io/badge/Deployment-0f766e" alt="Deployment"></a>
  <a href="https://athrael-soju.github.io/Narwhal/HTTP-API/"><img src="https://img.shields.io/badge/API%20reference-0f766e" alt="API reference"></a>
  <a href="https://github.com/athrael-soju/Narwhal/issues"><img src="https://img.shields.io/badge/Issues-0f766e" alt="Issues"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md"><img src="https://img.shields.io/badge/Contributing-0f766e" alt="Contributing"></a>
</p>

## What is Narwhal?

Narwhal is an adaptive, disaggregated inference framework that automatically hot-swaps prefill and decode roles as demand changes, and without having to reload model weights. It can scale from a [single GPU](https://athrael-soju.github.io/Narwhal/Dev-Runtime/) to [multi-node deployments](https://athrael-soju.github.io/Narwhal/Deploy/).

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
      <td>Reassigns prefill and decode roles across a fixed GPU fleet, with NIXL key-value (KV) transfer between them</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/"><img src="https://img.shields.io/badge/docs-Core%20concepts-0f766e" alt="Core concepts documentation"></a></td>
    </tr>
    <tr>
      <td>Serving</td>
      <td>Serves completion and chat requests, streamed or buffered, with latency-aware admission</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/HTTP-API/"><img src="https://img.shields.io/badge/docs-HTTP%20API%20reference-0f766e" alt="HTTP API reference documentation"></a></td>
    </tr>
    <tr>
      <td>Fault tolerance</td>
      <td>Fails over to a warm-standby router and readmits engines against their live process generation</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Operate/"><img src="https://img.shields.io/badge/docs-Operating%20Narwhal-0f766e" alt="Operating Narwhal documentation"></a></td>
    </tr>
    <tr>
      <td>Measurement</td>
      <td>Profiles engines, runs ordered benchmark points, and keeps the evidence from each point</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Measure/"><img src="https://img.shields.io/badge/docs-Measuring%20a%20fleet-0f766e" alt="Measuring a fleet documentation"></a></td>
    </tr>
    <tr>
      <td>Observability</td>
      <td>Exports router and engine metrics to Prometheus and a provisioned Grafana dashboard</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Observability/"><img src="https://img.shields.io/badge/docs-Setting%20up%20observability-0f766e" alt="Setting up observability documentation"></a></td>
    </tr>
    <tr>
      <td>Operator tooling</td>
      <td>Validates fleet files offline and collects private diagnostic bundles</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/CLI-Reference/"><img src="https://img.shields.io/badge/docs-CLI%20reference-0f766e" alt="CLI reference documentation"></a></td>
    </tr>
    <tr>
      <td>Development mode</td>
      <td>Runs two to eight engines on one NVIDIA CUDA GPU under Ubuntu or WSL2</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Dev-Runtime/"><img src="https://img.shields.io/badge/docs-Narwhal%20dev-0f766e" alt="Narwhal dev documentation"></a></td>
    </tr>
  </tbody>
</table>

## How engines change roles

The role controller scores the current role split and each adjacent split, one engine move away. It uses measured engine profiles, offered demand, and resident work, and moves one engine only when the worst projected SLO ratio improves and every role, lifecycle, and resident-work guard passes. New requests follow the revised split; resident requests finish on their assigned engines.

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/concepts/02-Role-Control/"><img src="https://img.shields.io/badge/docs-Role%20control%20and%20capacity%20floors-0f766e" alt="Role control and capacity floors documentation"></a>
</p>

![Role controller changing engine roles with weights resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

## Evaluation

<h3 align="center"><a href="https://athrael.net/posts/evaluating-narwhal/">Read the full evaluation: Evaluating Narwhal</a></h3>

## Getting the commands

Install on Linux with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

The wheel installs these commands:

`narwhal`, `narwhal-engine`, `narwhal-serve`, `narwhal-attest`, `narwhal-profile`, and `narwhal-check`. See [Installing from PyPI](https://athrael-soju.github.io/Narwhal/Install-from-PyPI/) and the [CLI reference](https://athrael-soju.github.io/Narwhal/CLI-Reference/) for their options.

## Trying it on one GPU

Narwhal dev runs a local NVIDIA CUDA fleet on Ubuntu, either directly or under WSL2.

```bash
narwhal dev init
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

The installed template starts two engines on an NVIDIA GPU with 8 GB of VRAM or less. The RTX 5090 reference template starts four engines on an RTX 5090.

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Dev-Runtime/"><img src="https://img.shields.io/badge/docs-Installed%20template-0f766e" alt="Installed template documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/dev/RTX-5090-Reference/"><img src="https://img.shields.io/badge/docs-RTX%205090%20reference-0f766e" alt="RTX 5090 reference documentation"></a>
</p>

## Bringing up a fleet

Run the deployment gates in [Deploying a fleet](https://athrael-soju.github.io/Narwhal/Deploy/) from a management workstation.

## Documentation

The [MkDocs site](https://athrael-soju.github.io/Narwhal/) routes installation, deployment, operation, measurement, and reference tasks.

## Contributing

<p align="center">
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md"><img src="https://img.shields.io/badge/community-Contributing-0f766e" alt="Contributing"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md"><img src="https://img.shields.io/badge/community-Code%20of%20conduct-0f766e" alt="Code of conduct"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md"><img src="https://img.shields.io/badge/community-Security%20policy-0f766e" alt="Security policy"></a>
</p>

## Built on Arrow

Narwhal's scheduling algorithms derive from Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture by Wu et al. (2025).

<p align="center">
  <a href="https://arxiv.org/abs/2505.11916"><img src="https://img.shields.io/badge/paper-Arrow%20%C2%B7%20arXiv%202505.11916-0f766e" alt="Arrow paper on arXiv"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff"><img src="https://img.shields.io/badge/cite-CITATION.cff-0f766e" alt="Citation metadata for Arrow and Narwhal"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-0f766e" alt="Apache-2.0 license"></a>
</p>
