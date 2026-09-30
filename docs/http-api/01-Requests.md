# Completion requests

## Completion API

Narwhal serves one model per fleet, the one set in its configuration, on two OpenAI-compatible routes.

### `POST /v1/completions`

A non-streaming request returns `object: "text_completion"`, with the generated text in `choices[0].text`.

### `POST /v1/chat/completions`

A non-streaming request returns `object: "chat.completion"`, with the assistant's reply in `choices[0].message`. When the model produces only reasoning or tool calls, `content` is `null`.

## Request contract

The request body must be a JSON object. The router validates the fields it reads before it reserves admission or engine capacity. A field that is present and not null must have the listed type:

| Field        | Required type                 |
| ------------ | ----------------------------- |
| `model`      | String                        |
| `stream`     | Boolean                       |
| `n`          | Integer; booleans are invalid |
| `best_of`    | Integer; booleans are invalid |
| `max_tokens` | Integer; booleans are invalid |
| `prompt`     | String or array               |
| `messages`   | Array of objects              |

All other fields are forwarded to the engine unchanged, except `model` (see below).

Invalid JSON, a body that is not an object, or a wrongly typed field returns HTTP `400` in an OpenAI error envelope and writes a [terminal request record](../telemetry/01-Journal.md#terminal-request-records) with `terminal: "invalid"`. For example:

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

A `model` other than the configured model fails with HTTP `404` and `model_not_found` before dispatch. Accepted requests are forwarded with `model` set to the configured model.

### Sampling width

Values of `n` or `best_of` above 1 return HTTP `400`. Prefill runs as a separate one-token request and uses the same sampling width as the decode request that follows it.

### Output and tool restrictions

Non-streaming requests can ask for text output and function tools. A non-streaming request is rejected with HTTP `400` before it reaches an engine if it sets `audio`, sets `modalities` to anything other than `["text"]`, or includes a tool whose type is not `function`. The error type is `invalid_request_error`, and `param` names the rejected option.

Passing these checks does not guarantee the feature works. Support for input formats, reasoning, and function tools depends on the model and engine configuration.

## Request identity and authentication

Every completion request gets a router request ID on arrival. The router returns it in the `x-request-id` response header.

From the router request ID, Narwhal derives a separate backend ID for each engine attempt and execution phase. These IDs track KV ownership.

If the request arrived with its own `x-request-id` header, the request journal records that value as `client_rid`.

Narwhal does not authenticate clients. The ingress must authenticate clients, strip client credentials and any client-supplied internal IDs, and install the trusted values Narwhal uses to identify the client.

Set `engine.engine_api_key_env` to attach the deployment's engine credential to serving and control requests.

See [Configure Narwhal](../configuration/03-Recovery-and-Validation.md#10-engine-authentication-and-protocol-adapters) for engine authentication and [Operate Narwhal](../operate/01-Start-Routers.md#3-configure-the-client-path) for ingress requirements.
