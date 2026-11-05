# Shared inference contract examples for POCs 08 and 09

**Status:** Illustrative design examples, not captured model responses.
Neither APIM streaming path has been live-tested. The separate
[POC 09 runtime sample](../09-model-router-runtime-sample/README.md) has local
MAF Responses tests; this document's qualification interface is OpenAI Chat
Completions, and these examples are not Responses API payloads.

## 1. Customer supplies context and tool schemas

Send `POST /llm/v1/chat/completions` over HTTPS with
`Content-Type: application/json` and
`Ocp-Apim-Subscription-Key: <customer-gateway-key>`.
This example authentication method requires a configured APIM subscription and
customer mapping. A qualified Entra/workload policy is an alternative.
The gateway strips the customer credential before calling the provider and
uses its own approved provider authentication.

```json
{
  "model": "service-default",
  "messages": [
    {
      "role": "system",
      "content": "Reserve two units of each SKU. Use lookup_stock for current stock. Never invent inventory. Do not ship a negative quantity."
    },
    {
      "role": "user",
      "content": "How many units of A-101 can ship today?"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "lookup_stock",
        "description": "Read current stock for a SKU in the caller's authorized inventory.",
        "parameters": {
          "type": "object",
          "properties": {
            "sku": {"type": "string"}
          },
          "required": ["sku"],
          "additionalProperties": false
        }
      }
    }
  ],
  "tool_choice": "auto",
  "stream": true,
  "store": false
}
```

Both POCs use this exact public request. `service-default` is a fixed protocol
placeholder, not a model or routing choice. The gateway rejects other model
values and caller-controlled route overrides, then uses verified customer
identity and operator policy to select and rewrite the backend deployment.
POC 08 selects a named deployment; POC 09 selects a router deployment that
chooses from its approved model subset. A provider-reported model name is
response metadata, not permission to select or call that model directly.

The tool schema contains no tool implementation, callback URL, or credentials.
The runtime binds `lookup_stock` to its own authorized implementation. Tenant
scope is derived from the runtime's authenticated context, never model arguments.

## 2. Model streams a tool call

For `stream: true`, the successful response has
`Content-Type: text/event-stream`. The following illustrative events are sent
incrementally, with a blank line after each event:

```text
data: {"id":"chatcmpl-example-001","object":"chat.completion.chunk","created":1793804400,"model":"<provider-reported-serving-model>","choices":[{"index":0,"delta":{"role":"assistant","content":null},"finish_reason":null}]}

data: {"id":"chatcmpl-example-001","object":"chat.completion.chunk","created":1793804400,"model":"<provider-reported-serving-model>","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_stock_001","type":"function","function":{"name":"lookup_stock","arguments":""}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-example-001","object":"chat.completion.chunk","created":1793804400,"model":"<provider-reported-serving-model>","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"sku\":\""}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-example-001","object":"chat.completion.chunk","created":1793804400,"model":"<provider-reported-serving-model>","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"A-101\"}"}}]},"finish_reason":null}]}

data: {"id":"chatcmpl-example-001","object":"chat.completion.chunk","created":1793804400,"model":"<provider-reported-serving-model>","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

data: [DONE]

```

The runtime assembles deltas by choice and tool-call index. Here the two
argument fragments form `{"sku":"A-101"}` and the tool-call ID is
`call_stock_001`. Wait for the complete `tool_calls` finish and `[DONE]`, then
validate the tool name and entire argument string against the schema, check
authorization, and execute. Handle every returned tool call, including
interleaved calls, and preserve its ID. Provider-generated arguments are not
proof of permission; no partial call may be executed.

Providers can emit additional supported events, such as empty-choice metadata,
filter results, or usage. Preserve their documented semantics without assuming
that every event contains a text or tool delta.

## 3. Customer runtime sends the result back

Send another request to the same API with the original context, the assembled
assistant tool-call message, and the matching tool result. This is a new
inference call and therefore a new stream:

```json
{
  "model": "service-default",
  "messages": [
    {
      "role": "system",
      "content": "Reserve two units of each SKU. Use lookup_stock for current stock. Never invent inventory. Do not ship a negative quantity."
    },
    {
      "role": "user",
      "content": "How many units of A-101 can ship today?"
    },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_stock_001",
          "type": "function",
          "function": {
            "name": "lookup_stock",
            "arguments": "{\"sku\":\"A-101\"}"
          }
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_stock_001",
      "content": "{\"sku\":\"A-101\",\"on_hand\":7}"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "lookup_stock",
        "description": "Read current stock for a SKU in the caller's authorized inventory.",
        "parameters": {
          "type": "object",
          "properties": {
            "sku": {"type": "string"}
          },
          "required": ["sku"],
          "additionalProperties": false
        }
      }
    }
  ],
  "tool_choice": "auto",
  "stream": true,
  "store": false
}
```

One possible text stream is:

```text
data: {"id":"chatcmpl-example-002","object":"chat.completion.chunk","created":1793804401,"model":"<provider-reported-serving-model>","choices":[{"index":0,"delta":{"role":"assistant","content":"Five units "},"finish_reason":null}]}

data: {"id":"chatcmpl-example-002","object":"chat.completion.chunk","created":1793804401,"model":"<provider-reported-serving-model>","choices":[{"index":0,"delta":{"content":"can ship today."},"finish_reason":null}]}

data: {"id":"chatcmpl-example-002","object":"chat.completion.chunk","created":1793804401,"model":"<provider-reported-serving-model>","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]

```

The expected answer is five units, not a promised exact sentence. The model
could instead request another authorized tool when appropriate. Repeated tool
requests remain subject to the runtime's turn limit and side-effect protections.

For automatic routing, a different eligible model may serve step 3. Do not
replay provider-specific encrypted reasoning or opaque state across models
without documented compatibility and qualification. If the required SDK
continuation depends on such state, mark that scenario unsupported rather than
silently discarding it.

## 4. Non-streaming JSON mode

For either inference request, set `stream` to `false` to receive one
`application/json` response instead of SSE. The fixed placeholder, identity,
routing, context, tool validation, and error rules are unchanged. For example,
the tool request in step 2 could instead return:

```json
{
  "id": "chatcmpl-example-001",
  "object": "chat.completion",
  "created": 1793804400,
  "model": "<provider-reported-serving-model>",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_stock_001",
            "type": "function",
            "function": {
              "name": "lookup_stock",
              "arguments": "{\"sku\":\"A-101\"}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

## 5. Errors, cancellation, and usage

A proposed gateway policy error body is:

```json
{
  "error": {
    "code": "invalid_model",
    "type": "invalid_request_error",
    "message": "The model field must be service-default."
  }
}
```

Before response headers, return this with HTTP 400, not 200. Authorization and
provider failures follow the distinctions in
[POC 08](README.md#6-errors-safety-and-ownership). Do not forward
credential-bearing provider error details.

After SSE headers are sent, the HTTP status cannot be changed. Preserve any
supported provider error event and mark the stream failed. A disconnect,
timeout, cancellation, missing `[DONE]`, or other premature termination is an
explicit incomplete result. A `length` or `content_filter` finish must not be
treated as a complete executable tool call. Do not transparently replay after
partial output. Attempt to cancel outstanding work when a client disconnects,
but verify backend cancellation and billing behavior rather than promising it.

The runtime owns bounded tool-argument buffering and user-facing presentation
of partial output or errors. Streaming usage is provider-dependent and may
require a supported request option. Record missing usage as unknown, not zero.
The Responses API has different event and tool-item semantics and requires a
separately qualified contract; these Chat Completions examples do not define it.
