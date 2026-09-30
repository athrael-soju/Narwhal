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

Narwhal is the first open-source LLM inference framework that automatically hot-swaps prefill and decode roles on both NVIDIA and AMD GPUs. It moves engines between roles as demand changes, on a fixed GPU fleet with model weights already loaded. It scales from a [single GPU](https://athrael-soju.github.io/Narwhal/Dev-Runtime/) to [distributed multi-node deployments](https://athrael-soju.github.io/Narwhal/Deploy/).

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
      <td>Reassigns prefill and decode roles across a fixed GPU fleet, with NIXL key-value (KV) transfer between them.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/"><img src="https://img.shields.io/badge/docs-Core%20concepts-0f766e" alt="Core concepts documentation"></a></td>
    </tr>
    <tr>
      <td>Serving</td>
      <td>Serves streaming and buffered completion and chat requests with latency-aware admission.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/HTTP-API/"><img src="https://img.shields.io/badge/docs-HTTP%20API%20reference-0f766e" alt="HTTP API reference documentation"></a></td>
    </tr>
    <tr>
      <td>Fault tolerance</td>
      <td>Fails over to a warm-standby router and readmits engines against their live process generation.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Operate/"><img src="https://img.shields.io/badge/docs-Operate%20Narwhal-0f766e" alt="Operate Narwhal documentation"></a></td>
    </tr>
    <tr>
      <td>Measurement</td>
      <td>Profiles engines and runs ordered benchmark points with retained evidence.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Measure/"><img src="https://img.shields.io/badge/docs-Measure%20a%20fleet-0f766e" alt="Measure a fleet documentation"></a></td>
    </tr>
    <tr>
      <td>Observability</td>
      <td>Exports router and engine metrics to Prometheus and a provisioned Grafana dashboard.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Observability/"><img src="https://img.shields.io/badge/docs-Set%20up%20observability-0f766e" alt="Set up observability documentation"></a></td>
    </tr>
    <tr>
      <td>Operator tooling</td>
      <td>Validates fleet files offline and collects private diagnostic bundles.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/CLI-Reference/"><img src="https://img.shields.io/badge/docs-CLI%20reference-0f766e" alt="CLI reference documentation"></a></td>
    </tr>
    <tr>
      <td>Development mode</td>
      <td>Runs two to eight engines on one NVIDIA CUDA GPU under Ubuntu or WSL2.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Dev-Runtime/"><img src="https://img.shields.io/badge/docs-Narwhal%20dev-0f766e" alt="Narwhal dev documentation"></a></td>
    </tr>
  </tbody>
</table>

## How engines change roles

<table width="100%" align="center">
  <thead>
    <tr>
      <th align="left" width="25%">Role controller pass</th>
      <th align="left" width="75%">Behavior</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Inputs</td>
      <td>Measured engine profiles, offered demand, and resident work</td>
    </tr>
    <tr>
      <td>Candidates</td>
      <td>The current role split and each adjacent split, one engine move away</td>
    </tr>
    <tr>
      <td>Score</td>
      <td>The worst projected service-level objective (SLO) ratio across time to first token (TTFT), time per output token (TPOT), and decode queueing</td>
    </tr>
    <tr>
      <td>Demand</td>
      <td>Measured window demand, using the larger of the short- and long-horizon decode estimates for decode-to-prefill candidates</td>
    </tr>
    <tr>
      <td>Evidence window</td>
      <td>Closes after <code>controller.reactive.evidence_span_s</code> and the minimum arrivals, or after <code>controller.reactive.evidence_max_span_s</code> under sparse traffic</td>
    </tr>
    <tr>
      <td>Move</td>
      <td>To the adjacent split that improves the score by at least the configured margin</td>
    </tr>
    <tr>
      <td>Decode to prefill</td>
      <td>Requires a closed evidence window and stable decode demand</td>
    </tr>
    <tr>
      <td>Prefill to decode</td>
      <td>Proceeds with the evidence window open</td>
    </tr>
    <tr>
      <td>Guards</td>
      <td>Pinned engines, role floors, cooldown, dwell time, the resident-stream ceiling on decode donors, and engine lifecycle holds</td>
    </tr>
    <tr>
      <td>Floor repair</td>
      <td>One engine per monitor pass while a phase sits below its configured floor</td>
    </tr>
    <tr>
      <td>New requests</td>
      <td>Follow the revised split</td>
    </tr>
    <tr>
      <td>Resident requests</td>
      <td>Finish on their assigned engines</td>
    </tr>
  </tbody>
</table>

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/concepts/02-Role-Control/"><img src="https://img.shields.io/badge/docs-Role%20control%20and%20capacity%20floors-0f766e" alt="Role control and capacity floors documentation"></a>
</p>

![Role controller changing engine roles with weights resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

## Narwhal against Dynamo Planner and Ray Serve LLM

<p align="center">
  <a href="https://github.com/ai-dynamo/aiperf/releases/tag/v0.12.0"><img src="https://img.shields.io/badge/client-AIPerf%20v0.12.0-0f766e" alt="AIPerf v0.12.0"></a>
  <a href="https://huggingface.co/moonshotai/Kimi-K3"><img src="https://img.shields.io/badge/model-Kimi--K3-0f766e" alt="Kimi-K3 model"></a>
  <a href="https://athrael.net/narwhal-evaluation-2026-09/chat-document-results/"><img src="https://img.shields.io/badge/workload-chat%2Fdocument-0f766e" alt="Chat/document workload results"></a>
  <a href="https://athrael.net/narwhal-evaluation-2026-09/mixed-workload-results/"><img src="https://img.shields.io/badge/workload-mixed%20payload-0f766e" alt="Mixed-payload workload results"></a>
  <a href="https://athrael.net/posts/evaluating-narwhal#prefix-caching"><img src="https://img.shields.io/badge/prefix%20caching-enabled-0f766e" alt="Prefix caching enabled"></a>
</p>

<p align="center">
  <a href="https://github.com/athrael-soju/Narwhal"><img src="https://img.shields.io/badge/framework-Narwhal%20v0.1.0-0f766e" alt="Narwhal v0.1.0"></a>
  <a href="https://github.com/ai-dynamo/dynamo"><img src="https://img.shields.io/badge/framework-Dynamo%20Planner-0f766e" alt="Dynamo Planner"></a>
  <a href="https://github.com/ray-project/ray"><img src="https://img.shields.io/badge/framework-Ray%20Serve%20LLM-0f766e" alt="Ray Serve LLM"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/infographic.png" alt="Evaluation results for Narwhal, Dynamo Planner, and Ray Serve LLM.">
</p>

<p align="center">
  <a href="https://athrael.net/posts/evaluating-narwhal/"><img src="https://img.shields.io/badge/write--up-Evaluating%20Narwhal-0f766e" alt="Evaluating Narwhal write-up"></a>
</p>

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

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/cli/Dev/"><img src="https://img.shields.io/badge/cli-narwhal-0f766e" alt="narwhal"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Engine/"><img src="https://img.shields.io/badge/cli-narwhal--engine-0f766e" alt="narwhal-engine"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Serve/"><img src="https://img.shields.io/badge/cli-narwhal--serve-0f766e" alt="narwhal-serve"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Attest/"><img src="https://img.shields.io/badge/cli-narwhal--attest-0f766e" alt="narwhal-attest"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Profile/"><img src="https://img.shields.io/badge/cli-narwhal--profile-0f766e" alt="narwhal-profile"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Check/"><img src="https://img.shields.io/badge/cli-narwhal--check-0f766e" alt="narwhal-check"></a>
</p>

For each deployment:

1. Record the `narwhal-serve --version` output with the fleet configuration, engine image, and profiles.
2. Pin that version on every router host.

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Install-from-PyPI/"><img src="https://img.shields.io/badge/docs-Install%20from%20PyPI-0f766e" alt="Install from PyPI documentation"></a>
</p>

## Try it on one GPU

Narwhal dev runs a local NVIDIA CUDA fleet on Ubuntu or Ubuntu under WSL2.

```bash
narwhal dev init
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

<table width="100%" align="center">
  <thead>
    <tr>
      <th align="left" width="55%">GPU</th>
      <th align="center" width="15%">Engines</th>
      <th align="left" width="30%">Template</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>NVIDIA GPU with 8 GB of VRAM or less</td>
      <td align="center">2</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Dev-Runtime/"><img src="https://img.shields.io/badge/docs-Installed%20template-0f766e" alt="Installed template documentation"></a></td>
    </tr>
    <tr>
      <td>RTX 5090</td>
      <td align="center">4</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/dev/RTX-5090-Reference/"><img src="https://img.shields.io/badge/docs-RTX%205090%20reference-0f766e" alt="RTX 5090 reference documentation"></a></td>
    </tr>
  </tbody>
</table>

## Bring up a fleet

Run these gates from a management workstation:

<table width="100%" align="center">
  <thead>
    <tr>
      <th align="left" width="80%">Step</th>
      <th align="left" width="20%">Gate</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Freeze inputs and discover the deployment</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/deploy/01-Discover/"><img src="https://img.shields.io/badge/docs-Gate%20A-0f766e" alt="Gate A documentation"></a></td>
    </tr>
    <tr>
      <td>Package and install the approved revision</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/deploy/02-Install/"><img src="https://img.shields.io/badge/docs-Gate%20B-0f766e" alt="Gate B documentation"></a></td>
    </tr>
    <tr>
      <td>Validate and start every engine</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/deploy/03-Validate-Engines/"><img src="https://img.shields.io/badge/docs-Gate%20C-0f766e" alt="Gate C documentation"></a></td>
    </tr>
    <tr>
      <td>Qualify the transfer fabric</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/deploy/04-Qualify-Fabric/"><img src="https://img.shields.io/badge/docs-Gate%20D-0f766e" alt="Gate D documentation"></a></td>
    </tr>
    <tr>
      <td>Attest the live engines</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/deploy/05-Attest/"><img src="https://img.shields.io/badge/docs-Gate%20E-0f766e" alt="Gate E documentation"></a></td>
    </tr>
    <tr>
      <td>Profile the engines and run preflight</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/deploy/06-Profile-and-Preflight/"><img src="https://img.shields.io/badge/docs-Gate%20F-0f766e" alt="Gate F documentation"></a></td>
    </tr>
    <tr>
      <td>Start the router and validate capacity through an SSH tunnel</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/deploy/07-Serve-and-Measure/"><img src="https://img.shields.io/badge/docs-Gate%20G-0f766e" alt="Gate G documentation"></a></td>
    </tr>
  </tbody>
</table>

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Deploy/"><img src="https://img.shields.io/badge/docs-Deploy%20a%20fleet-0f766e" alt="Deploy a fleet documentation"></a>
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
