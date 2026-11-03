# Agent Runtime - Model Service Contract

**Version:** 1 (initial interface)

**Scope:** the [minimal customer model-service design](MINIMAL-DESIGN.md).
The [core contract is embedded in that design](MINIMAL-DESIGN.md#6-agent-runtime---model-service-contract);
this document retains the expanded interface reference.
This documents the implemented, locally tested routing API. The new service
and Azure Table deployment are not yet live-qualified.
The contract is runtime-neutral: **MAF / Copilot SDK** are alternative callers.
MAF is the Functions runtime default; Copilot SDK is an explicit preview option.
Only the Copilot SDK client path has a local native smoke test; the external
MAF adapter remains pending.

## 1. Responsibilities

| Agent Runtime (MAF / Copilot SDK) | Model Service |
| --- | --- |
| Own instructions, messages, framework-specific context/compaction, tools, and runtime-state persistence | Authenticate the customer and authorize agent/model/session access |
| Create or retrieve a binding before configuring the runtime's client adapter | Atomically persist the selected model/deployment for 24 hours |
| Send complete runtime-packaged context on each inference request | Forward that context without adding another transcript |
| Handle failures, partial streams, and any explicitly budgeted retries | Validate the pinned endpoint/model and return response/usage or an explicit error |

The service does not execute tools, host an agent loop, or restore framework
state. The runtime must not silently start an empty conversation when required
state is missing. Compaction mechanisms and persistence are framework-specific,
not interchangeable guarantees.

## 2. Authentication and Authorization

Every session and inference request uses HTTPS and:

```http
Authorization: Bearer <customer-api-key>
```

The service derives the customer from the validated key, not from caller JSON
or a tenant header. The key must remain in the customer backend/runtime's
secret storage. The runtime client uses this **service credential**, not the
upstream model-provider credential. For Copilot SDK, supply it as the
custom-provider `api_key`.

The service checks the customer's allowed agents/models and the binding's
ownership and expiry on every protected request. Scope is
`(application, customer, agent, public session)`. Session IDs and provider URLs
are identifiers, not authorization tokens.

Customer credentials are never forwarded to model endpoints; the service uses
its own provider credentials. Revoked access is not overridden by an otherwise
unexpired binding.

## 3. Session Binding API

| Method and path | Success |
| --- | --- |
| `POST /sessions` | 201 for a new binding; 200 for the same existing binding |
| `GET /sessions/{agent_id}/{session_id}` | 200 with the owning, unexpired binding |
| `GET /sessions/{agent_id}/{session_id}/v1/models` | 200 with the session's one allowed model alias |

### Create or retrieve a binding

`POST /sessions`, with `Content-Type: application/json`:

```json
{
  "session_id": "conversation-001",
  "agent_id": "assistant",
  "model_alias": "economy"
}
```

`session_id` and `model_alias` are required; `agent_id` defaults to `assistant`.
Identifiers use 1-64 ASCII letters, digits, underscores, or hyphens.
Additional binding-request fields, including tenant, endpoint, or credential
fields, are rejected.

Illustrative response, also returned by the owning session GET:

```json
{
  "tenant_id": "customer-a",
  "agent_id": "assistant",
  "session_id": "conversation-001",
  "sdk_session_id": "8ecde19dbfb341b292ee3bf5af54ada3",
  "model_alias": "economy",
  "model_name": "gpt-4.1-mini",
  "model_version": "2025-04-14",
  "created_at": "2026-11-03T14:30:00+00:00",
  "expires_at": "2026-11-04T14:30:00+00:00",
  "provider_base_url": "https://models.example.com/sessions/assistant/conversation-001/v1",
  "wire_api": "completions",
  "provider_limits": {
    "max_prompt_tokens": 32768,
    "max_output_tokens": 4096
  },
  "context_owner": "caller_sdk"
}
```

The selected runtime's adapter uses the returned binding, alias, base URL,
protocol and limits with its service credential. Copilot SDK maps these to its
session/provider configuration; see the [SDK setup example](07-session-affinity/README.md#copilot-sdk-configuration).
A MAF adapter must explicitly map the binding to its session/chat client and
typed message/tool contract; that adapter is not yet implemented.

`sdk_session_id` and `context_owner: "caller_sdk"` retain their existing wire
names. They represent an opaque caller-side session reference and caller-owned
context; they do not mandate a Copilot engine. On resume, retrieve the same
binding and restore the selected framework's tenant-isolated state. Do not
silently change frameworks and assume their saved state is compatible.

### Binding rules

- Expiry is fixed at creation plus 24 hours; requests do not extend it.
- Repeating creation with the same model preserves the binding and caller-side ID.
- A different permitted model on the same session returns 409.
- Inference cannot create a missing session.
- Expired sessions return 410; the runtime must explicitly create a new public
  session ID rather than silently rebind the old one.
- Endpoint removal/configuration drift or provider model mismatch fails
  explicitly. There is no model fallback.
- Affinity is checked when admitting inference; an already admitted stream may
  finish under its configured deadline after the binding expires.

## 4. Inference API

```http
POST /sessions/{agent_id}/{session_id}/v1/chat/completions
Authorization: Bearer <customer-api-key>
Content-Type: application/json
```

Use the returned provider base URL; do not construct a backend/provider URL.
Example non-streaming request:

```json
{
  "model": "economy",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Summarize this synthetic information."}
  ],
  "max_tokens": 128,
  "stream": false,
  "store": false
}
```

| Field | Contract |
| --- | --- |
| `model` | Required; must equal the binding's public model alias |
| `messages` | Required, nonempty runtime-packaged context; the service adds no history |
| `tools`, tool-call messages/results | Optional function-tool definitions and messages; execution stays with the runtime |
| `stream` | Optional boolean, default false; true requests SSE |
| `max_tokens` or `max_completion_tokens` | Positive integer within the endpoint limit; if neither is supplied, the service supplies its configured `max_tokens` |
| `store` | Omit or set false; provider-managed conversation/response references are not supported |

The service substitutes the pinned deployment for `model`, validates supported
fields/limits, and makes one upstream attempt. The caller cannot supply a
tenant, endpoint, or provider credential.

**JSON success:** HTTP 200, OpenAI-compatible response. For example:

```json
{
  "id": "chatcmpl-example",
  "object": "chat.completion",
  "model": "gpt-4.1-mini-2025-04-14",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "A concise summary."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
}
```

Response `model` is the concrete provider-reported model, not the public alias.
Function-tool responses can have null text content and `tool_calls`. Preserve
tool-call IDs, finish reasons, and usage. Usage may be absent; absence does not
mean zero consumption.

**Streaming success:** HTTP 200 with `Content-Type: text/event-stream`;
OpenAI-compatible `data:` events, ending in `data: [DONE]`.
The service validates model identity before the first data event. A later
failure emits an explicit error event without a successful `[DONE]`; HTTP status
cannot change after streaming starts. The runtime must treat that as a failed
or partial outcome, not a completed answer.

Inference responses include `X-Model-Session-Expires-At` and `Cache-Control:
no-store`. Configured byte/output limits and an absolute inference deadline
apply; the default deadline is 25 seconds. `max_prompt_tokens` is a client
context-budget hint whose use must be qualified for the selected framework,
not a server-enforced token or financial quota.

## 5. Errors and Retry Rules

Errors use this shape; the same object is placed in an SSE `data:` event when
failure occurs after streaming starts:

```json
{
  "error": {
    "code": "session_expired",
    "type": "model_service_error",
    "message": "session_expired"
  }
}
```

| HTTP | Example code / meaning | Runtime action |
| --- | --- | --- |
| 401 | `unauthenticated` | Correct the service credential |
| 403 | `agent_not_allowed`, `model_not_allowed` | Do not bypass the access policy |
| 404 | `session_not_found` | Check the customer/session; do not silently reset history |
| 409 | `session_model_conflict` | Use the original binding or explicitly start a new session |
| 410 | `session_expired` | Explicitly start a new public session |
| 413 / 422 | `request_too_large`, invalid/unsupported input or output limit | Correct the request |
| 502 | `provider_model_mismatch`, invalid provider response | Fail; never substitute a model |
| 503 | Pinned endpoint or routing registry unavailable/changed, live inference disabled | Surface the failure; retain the binding |
| 504 | `endpoint_timeout` | Outcome and charges may be unknown |

Provider HTTP errors are sanitized as `endpoint_http_<status>` and preserve
4xx/5xx status, including 429. Inspect the error code: a provider 401 is not the
same as `unauthenticated` at the service. Unexpected service failures return 500.

Session creation is idempotent for its scoped identifiers and model.
**Inference is not guaranteed idempotent.** The service does not automatically
retry; framework/runtime retries require an explicit budget and may incur extra
charges. Disconnect/cancellation does not guarantee provider rollback.

## 6. Scope and Extensions

Initial protocol: OpenAI-compatible Chat Completions, JSON/SSE, and runtime-owned
function tools. This is not a Responses/native Anthropic adapter or durable
agent-job API.

Future Entra/workload identity, per-user permissions, cost-aware admission,
shared budgets, additional protocols, and cross-agent affinity are described in
the [minimal design](MINIMAL-DESIGN.md#7-future-extensions). None changes the
initial rule: the selected runtime owns context; the service owns authorized
model affinity.
