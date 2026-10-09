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

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Recognition/"><img src="https://img.shields.io/badge/%F0%9F%8F%86%20Recognition-d4a017" alt="Recognition"></a>
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

The role controller scores the current role split and each adjacent split, one engine move away. It projects each split from measured engine profiles, offered demand, and resident work. A split's score is its worst projected service-level objective (SLO) ratio across time to first token (TTFT), time per output token (TPOT), and decode queueing.

Projections use measured window demand. A decode-to-prefill candidate takes its decode demand from the larger of the short- and long-horizon estimates.

The controller moves to an adjacent split that improves the score by at least the configured margin. A decode-to-prefill move also needs stable decode demand and a closed arrival-evidence window. The window closes after `controller.reactive.evidence_span_s` with the minimum number of arrivals, or after `controller.reactive.evidence_max_span_s` under sparse traffic. A prefill-to-decode move with prefill load at or below `controller.thresholds.shrink` can proceed while the window is open.

When demand over the confirmation span shifts after a settled run, the controller moves one engine. The settled run is `controller.reactive.evidence_span_s`, or one confirmation span shorter when the shift reverses the controller's recent moves. The controller keeps moving engines in that direction on confirmation-span demand while the shift lasts: after its first move for a reversing shift, and after `controller.reactive.evidence_span_s` for any other shift. Under steady demand, the score chooses between adjacent splits once the arrival-evidence window has closed.

Every move passes guards for pinned engines, role floors, cooldown, dwell time, the resident-stream cap on decode donors, and engine lifecycle holds. While a role is below its configured floor, floor repair moves one engine per monitor pass.

New requests follow the revised split, and resident requests finish on their assigned engines.

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

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/cli/Dev/"><img src="https://img.shields.io/badge/cli-narwhal-0f766e" alt="narwhal"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Engine/"><img src="https://img.shields.io/badge/cli-narwhal--engine-0f766e" alt="narwhal-engine"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Serve/"><img src="https://img.shields.io/badge/cli-narwhal--serve-0f766e" alt="narwhal-serve"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Attest/"><img src="https://img.shields.io/badge/cli-narwhal--attest-0f766e" alt="narwhal-attest"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Profile/"><img src="https://img.shields.io/badge/cli-narwhal--profile-0f766e" alt="narwhal-profile"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Check/"><img src="https://img.shields.io/badge/cli-narwhal--check-0f766e" alt="narwhal-check"></a>
</p>

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Install-from-PyPI/"><img src="https://img.shields.io/badge/docs-Installing%20from%20PyPI-0f766e" alt="Installing from PyPI documentation"></a>
</p>

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

Run these gates in order from a management workstation:

1. Freeze inputs and discover the deployment in [Gate A](https://athrael-soju.github.io/Narwhal/deploy/01-Discover/).
2. Package and install the approved revision in [Gate B](https://athrael-soju.github.io/Narwhal/deploy/02-Install/).
3. Validate and start every engine in [Gate C](https://athrael-soju.github.io/Narwhal/deploy/03-Validate-Engines/).
4. Qualify the transfer fabric in [Gate D](https://athrael-soju.github.io/Narwhal/deploy/04-Qualify-Fabric/).
5. Attest the live engines in [Gate E](https://athrael-soju.github.io/Narwhal/deploy/05-Attest/).
6. Profile the engines and run preflight in [Gate F](https://athrael-soju.github.io/Narwhal/deploy/06-Profile-and-Preflight/).
7. Start the router and validate capacity through an SSH tunnel in [Gate G](https://athrael-soju.github.io/Narwhal/deploy/07-Serve-and-Measure/).

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Deploy/"><img src="https://img.shields.io/badge/docs-Deploying%20a%20fleet-0f766e" alt="Deploying a fleet documentation"></a>
</p>

## Documentation

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/"><img src="https://img.shields.io/badge/docs-Architecture-0f766e" alt="Architecture documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Configuration/"><img src="https://img.shields.io/badge/docs-Configuration-0f766e" alt="Configuration documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/CLI-Reference/"><img src="https://img.shields.io/badge/docs-CLI-0f766e" alt="CLI documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/HTTP-API/"><img src="https://img.shields.io/badge/docs-HTTP%20API-0f766e" alt="HTTP API documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Measure/"><img src="https://img.shields.io/badge/docs-Measurement-0f766e" alt="Measurement documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Observability/"><img src="https://img.shields.io/badge/docs-Observability-0f766e" alt="Observability documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Operate/"><img src="https://img.shields.io/badge/docs-Operations-0f766e" alt="Operations documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Troubleshoot/"><img src="https://img.shields.io/badge/docs-Troubleshooting-0f766e" alt="Troubleshooting documentation"></a>
</p>

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
