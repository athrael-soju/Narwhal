# Completion requests

## Completion routes

Both routes return OpenAI-compatible responses for the configured model.

### `POST /v1/completions`

Non-streaming calls return `object: "text_completion"`, with the text in `choices[0].text`.

### `POST /v1/chat/completions`

Non-streaming calls return `object: "chat.completion"` and the assistant message in `choices[0].message`. `content` is `null` when the reply is only reasoning or tool calls.

---

## Request contract

The body is a JSON object. Every other field goes to the engine as sent.

### Validated field types

Non-null values of these fields are type-checked before admission:

| Field        | Required type                 |
| ------------ | ----------------------------- |
| `model`      | String                        |
| `stream`     | Boolean                       |
| `n`          | Integer; booleans are invalid |
| `best_of`    | Integer; booleans are invalid |
| `max_tokens` | Integer; booleans are invalid |
| `prompt`     | String or array               |
| `messages`   | Array of objects              |

Bad JSON, a non-object body, or a wrongly typed field returns HTTP `400` in an OpenAI error envelope. The journal gets one [terminal request record](../telemetry/01-Journal.md#terminal-request-records) with `terminal: "invalid"`.

A rejected `max_tokens` looks like this:

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

A request for a different model gets HTTP `404` with `model_not_found`. Requests for the configured model are forwarded with that name.

### Sampling width

`n` and `best_of` above 1 return HTTP `400`.

### Output and tool restrictions

Non-streaming requests accept `modalities: ["text"]`, `tools` as an array of objects, and tools of type `function`. `audio` and anything else returns HTTP `400` with `invalid_request_error` before engine dispatch.

`param` names the rejected option. Streaming requests skip these checks.

---

## Request identity and authentication

| ID                 | Scope                               | Where it appears                |
| ------------------ | ----------------------------------- | ------------------------------- |
| Router request ID  | One per client request              | `x-request-id` response header  |
| Backend request ID | One per engine attempt and phase    | Engine requests and the KV handoff |
| `client_rid`       | Trusted request ID sent by ingress  | Request journal                 |

Ingress authenticates the client and strips its credentials and any internal IDs it sent. It then sets the trusted identity values.

Set `engine.engine_api_key_env` and Narwhal sends that credential on serving and control requests to the engine.

Engine credential setup is in [Engine authentication and protocol adapters](../configuration/03-Recovery-and-Validation.md#10-engine-authentication-and-protocol-adapters). Client-side setup is in [Configure the client path](../operate/01-Start-Routers.md#3-configure-the-client-path).
