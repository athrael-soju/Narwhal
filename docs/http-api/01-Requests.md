---
description: Request contract for POST /v1/completions and /v1/chat/completions on a Narwhal router.
---

# Completion requests

## Completion routes

### `POST /v1/completions`

Non-streaming calls return:

- `object: "text_completion"`
- The text in `choices[0].text`

### `POST /v1/chat/completions`

Non-streaming calls return:

- `object: "chat.completion"`
- The assistant message in `choices[0].message`

`content` is `null` when the reply holds only reasoning or tool calls.

## Request contract

### Validated field types

Non-null values of these fields have these required types:

| Field                   | Required type    |
| ----------------------- | ---------------- |
| `model`                 | String           |
| `stream`                | Boolean          |
| `n`                     | Integer          |
| `best_of`               | Integer          |
| `max_tokens`            | Integer          |
| `max_completion_tokens` | Integer          |
| `prompt`                | String or array  |
| `messages`              | Array of objects |

Other fields reach the engine as sent.

Invalid requests return HTTP `400` in an OpenAI error envelope:

- malformed JSON
- a JSON array or scalar body
- a field of the wrong type
- a `vllm_xargs` object that sets `kv_cache_report_mode`, `kv_transfer_params`, or `ec_transfer_params`

Each invalid request writes one [terminal request record](../telemetry/01-Journal.md#terminal-request-records) with `terminal: "invalid"`.

A rejected `max_tokens` returns:

```json
{
  "error": {
    "message": "max_tokens must be an integer",
    "type": "invalid_request_error",
    "param": "max_tokens",
    "code": null
  }
}
```

### Model handling

| Requested `model`           | Result                            |
| --------------------------- | --------------------------------- |
| Configured model, or absent | Forwarded as the configured model |
| Any other model             | HTTP `404` with `model_not_found` |

### Sampling width

`n` and `best_of` above 1 return HTTP `400`.

### Output and tool restrictions

| Option       | Streaming request   | Accepted non-streaming values         |
| ------------ | ------------------- | ------------------------------------- |
| `audio`      | Forwarded unchanged | `null`                                |
| `modalities` | Forwarded unchanged | `["text"]`                            |
| `tools`      | Forwarded unchanged | Array of objects with type `function` |

Other non-streaming values return HTTP `400` `invalid_request_error` with the option in `param`.

## Request identity and authentication

| ID                 | Scope                              | Where it appears                   |
| ------------------ | ---------------------------------- | ---------------------------------- |
| Router request ID  | One per client request             | `x-request-id` response header     |
| Backend request ID | One per engine attempt and phase   | Engine requests and the KV handoff |
| `client_rid`       | Trusted request ID sent by ingress | Request journal                    |

[Configure ingress](../operate/01-Start-Routers.md#3-configure-the-client-path) to:

1. Authenticate the client.
2. Strip the client's credentials.
3. Strip the internal IDs the client sent.
4. Set the trusted identity values.

With [`engine.engine_api_key_env`](../configuration/03-Recovery-and-Validation.md#10-engine-authentication-and-protocol-adapters) set, Narwhal sends that credential on serving and control requests to the engine.
