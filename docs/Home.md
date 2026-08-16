<p align="center">
  <img src="../assets/narwhal.svg" alt="The narwhal logo: a black narwhal with a teal spiral tusk, above the wordmark" width="45%">
</p>

Narwhal turns a pool of stateless engines into an adaptive
disaggregated fleet. Roles are labels. A controller moves them when
traffic shifts, and a re-split settles in seconds. The fleet serves
one OpenAI-compatible endpoint. The scheduling core follows the Arrow
paper ([arXiv:2505.11916](https://arxiv.org/abs/2505.11916)).

![Adaptive hot-swap: a node's role flips in place while the weights stay resident. The price is capable engines and a tuned control loop.](../assets/architectures/hotswap.svg)

The figure shows the core mechanism. Node-03 changes phase in place,
the weights stay resident, and the pool boundary flexes in seconds.
[Architectures](Architectures.md) compares this design against the
three established fleet organizations.

## Find your page

| You want to...                                              | Page                              |
| ----------------------------------------------------------- | --------------------------------- |
| Stand it up against your engines, or the no-GPU stub fleet  | [Deploy](Deploy.md)               |
| Bring up your own hardware and model                        | [presets/README](https://github.com/athrael-soju/Narwhal/blob/main/presets/README.md) |
| See which (hardware, model) pairs are validated             | [Supported Hardware and Models](Supported-Hardware-and-Models.md) |
| Look up a config field and the validation it gets           | [Configuration](Configuration.md) |
| Call the API, or prove a run actuated from `/arrow/state`   | [Api](Api.md)                     |
| Interpret latency, goodput, and fleet-health measurements   | [Serving KPIs](KPIs.md)           |
| Look up a metric, an alert rule, or a dashboard panel       | [Observability](Observability.md) |
| Benchmark your own fleet and score the journal              | [Benchmarking](Benchmarking.md)   |
| See the four fleet designs and the case for hot-swap        | [Architectures](Architectures.md) |

The [repository README](https://github.com/athrael-soju/Narwhal#readme)
carries the design tour. The evaluation record ships with *The Price
of Order in Disaggregated Inference*.

The wiki mirrors `docs/` in the repository. Edits belong in `docs/`,
where they are reviewed and versioned with the code they describe.
`make wiki` publishes.
