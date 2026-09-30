# Completion requests

## Completion routes

Both routes return OpenAI-compatible responses for the configured model.

### `POST /v1/completions`

Non-streaming calls return `object: "text_completion"`, with the text in `choices[0].text`.

### `POST /v1/chat/completions`

Non-streaming calls return `object: "chat.completion"` and the assistant message in `choices[0].message`. `content` is `null` when the reply is only reasoning or tool calls.

---

## Request contract

The body is a JSON object. The router passes fields outside the validated set to the engine as sent.

### Validated field types

The router type-checks non-null values of these fields before admission:

| Field        | Required type                 |
| ------------ | ----------------------------- |
| `model`      | String                        |
| `stream`     | Boolean                       |
| `n`          | Integer; booleans are invalid |
| `best_of`    | Integer; booleans are invalid |
| `max_tokens` | Integer; booleans are invalid |
| `prompt`     | String or array               |
| `messages`   | Array of objects              |

These requests return HTTP `400` in an OpenAI error envelope and write one [terminal request record](../telemetry/01-Journal.md#terminal-request-records) with `terminal: "invalid"`:

- malformed JSON
- a JSON array or scalar body
- a field of the wrong type

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

| Requested `model` | Result                            |
| ----------------- | --------------------------------- |
| Configured model  | Forwarded with that name          |
| Any other model   | HTTP `404` with `model_not_found` |

### Sampling width

`n` and `best_of` above 1 return HTTP `400`.

### Output and tool restrictions

Streaming requests pass these options to the engine as sent. Non-streaming requests accept:

- `modalities: ["text"]`
- `tools` as an array of objects
- tools of type `function`

A non-streaming request with an `audio` value, or any other value for these options, returns HTTP `400` with `invalid_request_error` before engine dispatch. `param` names the rejected option.

---

## Request identity and authentication

| ID                 | Scope                               | Where it appears                |
| ------------------ | ----------------------------------- | ------------------------------- |
| Router request ID  | One per client request              | `x-request-id` response header  |
| Backend request ID | One per engine attempt and phase    | Engine requests and the KV handoff |
| `client_rid`       | Trusted request ID sent by ingress  | Request journal                 |

[Configure ingress](../operate/01-Start-Routers.md#3-configure-the-client-path) to:

1. Authenticate the client.
2. Strip the client's credentials and any internal IDs it sent.
3. Set the trusted identity values.

With [`engine.engine_api_key_env`](../configuration/03-Recovery-and-Validation.md#10-engine-authentication-and-protocol-adapters) set, Narwhal sends that credential on serving and control requests to the engine.
