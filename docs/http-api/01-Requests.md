# Completion requests

## Completion API

Narwhal supports one sequence per request and serves one configured model. Client requests use OpenAI-compatible request and response shapes where supported.

### `POST /v1/completions`

Accepts an OpenAI completions request.

For a non-streaming response:

- `object` is `"text_completion"`.
- generated text is returned in `choices[0].text`.

### `POST /v1/chat/completions`

Accepts an OpenAI chat completions request.

For a non-streaming response:

- `object` is `"chat.completion"`.
- the generated message is returned in `choices[0].message`.
- `choices[0].message.role` is `"assistant"`.
- `content` is `null` when the response contains only reasoning or tool-call output.

---

## Request contract

Both completion routes require a JSON object.

Narwhal validates router-interpreted fields before admission or engine dispatch.

### Validated field types

Non-null values must use the following types:

| Field        | Required type                 |
| ------------ | ----------------------------- |
| `model`      | String                        |
| `stream`     | Boolean                       |
| `n`          | Integer; booleans are invalid |
| `best_of`    | Integer; booleans are invalid |
| `max_tokens` | Integer; booleans are invalid |
| `prompt`     | String or array               |
| `messages`   | Array of objects              |

Invalid JSON, an invalid top-level body shape, or an invalid router-interpreted field type returns HTTP `400` in an OpenAI error envelope.

Example:

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

The diagnostic identifies the affected field and violated rule.

Fields outside the router validation set pass through unchanged.

A validation failure records one invalid terminal outcome before Narwhal reserves admission or engine capacity.

### Model handling

Narwhal validates the submitted model name before dispatch.

If it matches the configured model, Narwhal replaces the request's `model` value with the configured served model when constructing the engine request.

A different requested model returns:

- HTTP `404`
- error code `model_not_found`

### Sampling width

Narwhal supports one sequence per request.

Either of the following returns HTTP `400`:

```text
n > 1
best_of > 1
```

This keeps prefill and decode sampling widths aligned.

### Output and tool restrictions

Non-streaming requests support:

- text output
- function tools

Narwhal returns HTTP `400` before engine dispatch when a non-streaming request asks for:

- `audio`
- an output `modalities` value other than `["text"]`
- a tool type other than `function`

The error uses `invalid_request_error` and identifies the rejected option in `param`.

Model and engine configuration determine actual support for input formats, reasoning, and function tools.

---

## Request identity and authentication

Narwhal assigns `x-request-id` when a request reaches the router.

Each engine attempt and each execution phase receives its own backend request ID for KV ownership. The request journal records the original client request identifier as `client_rid`, preserving correlation across the full request lifecycle.

Ingress owns client authentication.

It must remove client credentials before forwarding a request to Narwhal.

Set:

```text
engine.engine_api_key_env
```

to attach the deployment's engine credential to serving and control requests.

Ingress should also remove client-supplied internal credentials and request IDs before installing trusted replacements. Narwhal derives client identity from those trusted values.

See [Configure Narwhal](../configuration/03-Recovery-and-Validation.md#10-engine-authentication-and-protocol-adapters) for the engine authentication boundary and [Operate Narwhal](../operate/01-Start-Routers.md#3-configure-the-client-path) for ingress requirements.
