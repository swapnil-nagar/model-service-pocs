# Shared inference contract examples for POCs 08 and 09

**Status:** Illustrative design examples, not captured model responses.
The first qualification interface is OpenAI Chat Completions. These examples
are not Responses API payloads.

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
  "model": "explicit-standard",
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
  "stream": false,
  "store": false
}
```

In POC 09, replace `explicit-standard` with `auto-balanced` in **every**
inference request. The gateway maps that alias to a configured router deployment.
The word `auto` is not a special API instruction without that configuration.

The tool schema contains no tool implementation, callback URL, or credentials.
The runtime binds `lookup_stock` to its own authorized implementation. Tenant
scope is derived from the runtime's authenticated context, never model arguments.

## 2. Model requests a tool call

The gateway passes through the provider response. A simplified illustrative
response is:

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

The runtime validates the entire argument string against the schema, checks
authorization, then executes its tool. It must handle every returned tool call
and preserve IDs. Provider-generated arguments are not proof of permission.

## 3. Customer runtime sends the result back

Send another request to the same API with the original context, the exact
assistant tool-call message, and the matching tool result. Include the same
`tools`, `tool_choice`, `stream`, and `store` fields from step 1; the following
fragment shows only the model and complete message sequence:

```json
{
  "model": "explicit-standard",
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
  ]
}
```

The next assistant response should explain that five units can ship, or request
another authorized tool when appropriate. The expected number is an acceptance
criterion, not a promised exact sentence. Repeated tool requests remain subject
to the runtime's turn limit and side-effect protections.

For automatic routing, a different eligible model may serve step 3. Do not
replay provider-specific encrypted reasoning or opaque state across models
without documented compatibility and qualification. If the required SDK
continuation depends on such state, mark that scenario unsupported rather than
silently discarding it.

## 4. Errors, streams, and usage

A proposed gateway policy error body is:

```json
{
  "error": {
    "code": "unknown_model_alias",
    "type": "invalid_request_error",
    "message": "The requested model alias is not configured."
  }
}
```

Return this with HTTP 400, not 200. Authorization failures and provider failures
follow the distinctions in [POC 08](README.md#6-errors-safety-and-ownership).
Do not forward credential-bearing provider error details.

For `stream: true`, return native Chat Completions SSE deltas, preserving tool
call indexes, IDs, argument fragments, and terminal status. Only complete,
validated tool calls are executable. A disconnect before protocol completion
produces an incomplete result; no transparent replay after partial output.
Streaming usage is provider-dependent and may require a supported request
option. Record missing usage as unknown, not zero.
