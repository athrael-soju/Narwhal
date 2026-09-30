# Completion requests

## Completion API

Narwhal serves one model per fleet, the one set in its configuration, on two OpenAI-compatible routes.

### `POST /v1/completions`

A non-streaming request returns `object: "text_completion"`, with the generated text in `choices[0].text`.

### `POST /v1/chat/completions`

A non-streaming request returns `object: "chat.completion"`, with the assistant's reply in `choices[0].message`. If the model produced only reasoning or tool calls, `content` is `null`.

## Request contract

The request body must be a JSON object. Before reserving an admission seat or any engine capacity, the router checks the fields it reads itself. When one of these fields is present and not null, it must have the type shown:

| Field        | Required type                 |
| ------------ | ----------------------------- |
| `model`      | String                        |
| `stream`     | Boolean                       |
| `n`          | Integer; booleans are invalid |
| `best_of`    | Integer; booleans are invalid |
| `max_tokens` | Integer; booleans are invalid |
| `prompt`     | String or array               |
| `messages`   | Array of objects              |

Anything else in the body goes to the engine untouched.

If the JSON is invalid, the body has the wrong shape, or one of these fields has the wrong type, the client gets HTTP `400` in an OpenAI error envelope, and the journal gets a [terminal request record](../telemetry/01-Journal.md#terminal-request-records) with `terminal: "invalid"`. For example:

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

If `model` names anything other than the configured model, the request fails with HTTP `404` and `model_not_found` before dispatch. Accepted requests are forwarded with `model` set to the configured model.

### Sampling width

Values of `n` or `best_of` above 1 return HTTP `400`. Prefill runs as a separate one-token request, and it has to use the same sampling width as the decode leg that follows it.

### Output and tool restrictions

Non-streaming requests can ask for text output and function tools. A non-streaming request is rejected with HTTP `400` before it reaches an engine if it sets `audio`, sets `modalities` to anything other than `["text"]`, or includes a tool whose type isn't `function`. The error type is `invalid_request_error`, and `param` names the option that was rejected.

Passing these checks doesn't guarantee the feature works. Support for particular input formats, reasoning, and function tools depends on the model and engine configuration.

## Request identity and authentication

Narwhal gives every completion request its own router request ID when it arrives and returns it to the client in the `x-request-id` response header. From it, Narwhal derives a separate backend ID for each engine attempt and execution phase, which is how it tracks KV ownership. If the request arrived with its own `x-request-id` header, the request journal keeps that value as `client_rid` so you can correlate the two.

Narwhal doesn't authenticate clients itself. Your ingress in front of it authenticates the client, strips client credentials and any internal IDs the client supplied, then installs the trusted values Narwhal uses to identify the client. To attach the deployment's engine credential to serving and control requests, set `engine.engine_api_key_env`.

The engine authentication boundary is covered in [Configure Narwhal](../configuration/03-Recovery-and-Validation.md#10-engine-authentication-and-protocol-adapters), and ingress requirements in [Operate Narwhal](../operate/01-Start-Routers.md#3-configure-the-client-path).
