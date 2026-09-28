# Stream continuation qualification

These results qualify the unreleased
[stream continuation](../concepts/04-Stream-Continuation.md) feature. They
apply to one pinned backend, tokenizer and engine process, with the
request settings and limits in the
[request boundary](../concepts/04-Stream-Continuation.md#request-boundary).
Qualify each deployment against its own engine processes, tokenizer and
settings with the
[stream continuation drill](../operate/04-Upgrade-and-Validate.md#qualify-stream-continuation).

## Summary

- Replay from a qualified byte boundary reproduced the uninterrupted output in
  token IDs and UTF-8 bytes. A replay from an incomplete-character boundary
  lost emitted text, so that boundary fails qualification.
- In the live worker-failure test, the opted-in stream recovered on a
  surviving engine and matched the healthy baseline. The ordinary stream on
  the terminated worker ended with one error.
- Seven of nine completed requests exceeded the configured router target of
  0.3 seconds per output token (TPOT). The pass criteria were client-measured
  limits declared before testing. The final worker-failure and transport-cut
  tests met them.
- Private record `nw-cont-20260927-a` retains the raw evidence.

## Replay feasibility

The [#192](https://github.com/athrael-soju/Narwhal/issues/192) feasibility
check sent raw prompt IDs containing special tokens and produced ASCII,
non-ASCII and partial UTF-8 token events. Weight files were not rehashed
during the probe, so this qualification covers only the checked running
process.

| Probe | Result |
| --- | --- |
| Replay after 11 committed IDs from a 24-token baseline | Matched in IDs and UTF-8 bytes |
| Replay after the next, incomplete-character token | Matched in IDs; emitted text lost |
| Token-stop replay after 13 committed IDs from a 16-token baseline | Matched in IDs, bytes and stop metadata |

In the first probe, the backend returned the exact augmented prompt IDs and
the 13-token suffix matched. In the second, the 12-token suffix matched in IDs
but omitted text the client had already received. In the token-stop probe,
the terminal stop ID had empty text.

The probes exercised an explicit token stop. The feasibility check compared
native EOS handling with the installed decoder and effective stop
configuration; none of those requests generated EOS.

The measured decoder converts concatenated token bytes to UTF-8 without text
cleanup. CPU checks with the installed tokenizer covered partial characters,
invalid bytes and skipped special tokens. All 13 qualifying cuts across four
adversarial sequences preserved the remaining text; all 14 unqualified cuts
failed.

The live probes did not observe a grouped frame. The feasibility check
combined two recorded live events as the backend's output collection code
would.

Synthetic serving tests cover grouped frames, failed or blocked HTTP sends,
terminal metadata before `[DONE]`, retention limits, cancellation and survivor
recovery. These tests establish router behaviour within their synthetic
inputs.

## Live router results

The [#195](https://github.com/athrael-soju/Narwhal/issues/195) test ran an
opted-in stream and an ordinary stream on the same decode worker, then
terminated that worker. The test set `recovery.failure_quarantine_s` to `30`.

| Case | Result |
| --- | --- |
| Opted-in stream on the terminated worker | Recovered; completed in 10.29 s with a longest content gap of 3.24 s |
| Ordinary stream on the terminated worker | One terminal error, no replay |
| Opted-in stream on the surviving worker | Completed without recovery; matched the healthy baseline |
| Ordinary request during recovery | First content in 2.07 s; completed in 3.84 s |

The router replayed a 564-token prompt plus 11 committed IDs and requested the
remaining 13 output tokens. The recovered response matched the healthy
baseline's 24 generated IDs and UTF-8 bytes. It kept its response identity and
ended with one finish and one `[DONE]`. The surviving decoder completed a
native KV transfer. After the test, router reservations, retained history and
surviving engine requests returned to zero.

### Transport cuts

A transport cut between a finish frame and the complete `[DONE]` delimiter
recovered. Separate cases observed the model's native EOS without a
request-level stop override. That EOS came from the engine's generation configuration; the
tokenizer's different primary EOS was not generated.

A separate transport-cut test, also with `recovery.failure_quarantine_s` set
to `30`, completed in 8.24 s with a longest content gap of 1.77 s. Its 11
committed IDs and 13 replayed IDs matched the uninterrupted baseline in IDs
and UTF-8 bytes. An ordinary request that overlapped the replacement decode
request had first content in 2.12 s and completed in 3.85 s.

After that recovery spent the router's only credit, another cut produced one
`continuation_shared_budget` error. The router kept the 11 committed IDs, sent
no replacement request and closed the response without a finish frame or
`[DONE]`. The request ended in 4.27 s, within its original deadline. Engine
transfer counters, client captures, journals and router counters agreed for
all three requests. Backend work, reservations and retained history returned
to zero.

### Earlier run without quarantine

An earlier worker-failure test recovered its opted-in stream in 9.41 s with a
longest content gap of 2.97 s. Its concurrent ordinary request failed:
with quarantine off, the router selected the terminated decoder before health
checks ejected it. That failed request remains in the reported
outcomes.

### Request outcomes

The 18 original client requests produced:

- 9 completions;
- 2 ordinary-request failures;
- 1 expected credit refusal;
- 6 cancellations by the test client in two earlier failed runs.

Router TTFT ends at prefill completion. Router TPOT includes the transfer,
queueing and recovery time that follows. All nine completed requests met the
10-second router TTFT target; seven exceeded the 0.3-second router TPOT
target. The test's interruption and completion limits used client timing.

## Known limitations

After the final worker-failure test, health checks ejected the surviving
prefill engine twice, with a successful readmission between those events. A
later direct health check returned HTTP 200 from the same engine process. The
fault-injection setup routed every engine through one HTTP proxy. An offline
reproduction showed that the proxy could drop a healthy engine's probe after a
connection error to the dead engine. The live probe errors were not captured,
so this cause remains unconfirmed.
