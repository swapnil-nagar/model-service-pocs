# POC 09 Proposal: Stateless Model Service

**Date:** 2026-11-05

**Status:** Proposed contract and runtime changes; the MAF pilot is functional, not production-approved.

Stateless alternative to the [original requirements](../customer-model-service.md),
not an implementation of their 24-hour affinity guarantee.
The [session-affinity contract](../CONTRACT.md) is unchanged.

## 1. Problem

Customers running agents with MAF or Copilot SDK need governed model access
without managing provider endpoints or credentials. The agent-runtime must
retain conversation context and tool execution while the service selects a
compatible model for each inference.

## 2. Requirements

1. Route **every model call through APIM / AI Gateway**, including delegated,
   workflow and tool-originated calls; prohibit direct Foundry access or fallback.
2. Accept only `service-default`; route to an operator-approved Model Router
   subset qualified for the common text, function-tool and context profile.
3. Keep history, authorization, tools and the agent loop in the runtime.
   Resend complete context on every inference; do not require provider-side state.
4. Support JSON and genuine incremental SSE on the same inference operation,
   preserving each protocol's tool, completion and failure semantics.
5. Authenticate every request, derive customer identity from verified credentials,
   separate customer/provider secrets, and enforce limits, deadlines and cancellation.
6. Make required runtime changes explicit across all roles and entrypoints;
   qualify MAF first and Copilot SDK separately. Report partial failures and
   unknown usage explicitly; never replay partial output transparently.

## 3. Out of scope

- Customer-selected models, 24-hour model/version pinning, session bindings or expiry.
- Model-service conversation storage, agent hosting/orchestration or tool execution.
- Provider-managed conversations/retrieval, background inference, stream resume or replay.
- Arbitrary provider endpoints, customer-specific model entitlements, dedicated capacity or spending budgets.
- Production availability, cost or residency guarantees based only on pilot results.

## 4. Design and ownership

**All model calls must go through APIM configured as the AI Gateway.**
This includes main/delegated agents, workflows, and tool-originated model calls.
There is **no direct access from the agent-runtime or browser to Foundry models**;
gateway failure must not trigger a bypass/fallback. Only the gateway identity invokes Foundry.

```mermaid
flowchart TB
    U["User / application"] --> R["Agent-runtime / MAF"]
    R <-->|"Authorized execution"| T["Customer tools and data"]
    subgraph MS["Model Service: stateless inference boundary"]
        G["APIM / AI Gateway"] --> P["Foundry project Responses endpoint"]
        P --> MR["Foundry Model Router"]
        MR --> M["Approved compatible models"]
    end
    R -->|"Complete context + schemas + service-default"| G
    G -->|"JSON or incremental SSE"| R
```

| Owner | Responsibility |
| --- | --- |
| Agent-runtime | User/conversation authorization, history and summarization, tool execution, agent loop, customer-facing JSON/SSE |
| APIM / AI Gateway | Authenticate each request, derive trusted customer identity, validate/limit requests, map the fixed alias, separate credentials, forward incrementally |
| Foundry Model Router | Select an eligible model per inference from the operator-approved subset; no same-model continuation guarantee |
| Operator | Versioned router/API configuration, model qualification, access/network controls, capacity and monitoring |

The Model Service is **not another Function App** and owns no conversation, binding, or tool execution.
Every inference resends context; runtime sessions remain runtime-owned.

## 5. Agent-runtime -> Model Service contracts

### 5.1 Endpoints and authentication

Public base: `https://<approved-apim-host>/llm`. All operations support JSON and SSE.

| POST path | Protocol / qualification |
| --- | --- |
| `/llm/openai/v1/responses` | Primary native Responses API; qualified with the pinned MAF runtime |
| `/llm/v1/chat/completions` | Separate Chat Completions compatibility route; live SDK qualification pending |
| `/llm/openai/deployments/service-default/chat/completions` | Azure OpenAI SDK alias for Chat Completions; qualification pending |

Required headers: `Content-Type: application/json` and
`api-key: <customer-apim-subscription-key>`.
Keep this **APIM key**, not a provider key, in backend secret storage.
Identity comes from the verified subscription, not caller metadata.
Entra authentication is future work. No session/assignment/discovery API is required.

### 5.2 Request and result

| Field / rule | Contract |
| --- | --- |
| `model` | Required string, exactly `service-default`; APIM replaces it with the approved router deployment |
| `stream` | Optional boolean, default `false`; `true` requests native SSE on the same operation |
| `store` | Omitted or `false`; APIM forces `false` upstream |
| Responses | Required inline text `input` or input-item array; optional string `instructions`; output limit is `max_output_tokens` |
| Chat Completions | Required nonempty `messages`; one of `max_tokens` / `max_completion_tokens`, never both; `n` omitted or `1` |
| Stateless profile | Responses `background` omitted/`false`, `truncation` omitted/`disabled`; APIM forces these values |
| Rejected | Unknown top-level fields, routing/deployment overrides, `previous_response_id`, provider conversation/thread/file references, and hosted tools |

Full allowlists: [pilot policy](../09-model-router-runtime-sample/infra/policy.xml).
Inline text/function calls are the minimum profile; other options need qualification.
Nested/context-budget validation remains hardening work.

Minimal Responses request; change only `stream` to `true` for SSE:

```json
{
  "model": "service-default",
  "input": [{"role": "user", "content": "Hello."}],
  "max_output_tokens": 128,
  "stream": false,
  "store": false,
  "background": false,
  "truncation": "disabled"
}
```

HTTP 200 JSON preserves the native response, not a custom flattened answer:

| Protocol | Result and successful termination |
| --- | --- |
| Responses | `id`, `object: "response"`, `status`, `output[]`, reported `model`/`usage`; require `status: "completed"` and inspect `error` / `incomplete_details` |
| Chat Completions | `id`, `object: "chat.completion"`, `choices[].message`, `finish_reason`, reported `model`/`usage`; `stop` completes text, `tool_calls` hands control to the runtime |

HTTP 200 alone is not success; incomplete/blocked/failed results are not complete answers.
Function-call completion is not user-turn completion.
Preserve metadata; router names do not prove underlying-model attribution.
Missing usage/attribution means unknown, not zero.

### 5.3 Tools and continuation

| Protocol | Tool schema / returned call / next request |
| --- | --- |
| Responses | `tools[]` uses `type: "function"`, `name`, `parameters`; returned `function_call` has `call_id`, `name`, JSON-string `arguments`; resend the call plus `function_call_output` with matching `call_id` and string `output` |
| Chat Completions | `tools[].function` contains `name`/`parameters`; assistant `tool_calls` has IDs and arguments; resend it plus `role: "tool"` messages with matching `tool_call_id` |

Validate complete arguments, authorize using trusted identity, and execute only registered tools;
never partial streamed arguments. Continuations resend instructions, schemas, history, calls and results;
each may select another model. Call IDs are correlation, not authorization/bindings.
Return explicit tool failures; side effects require idempotency/reconciliation.
See [Chat Completions examples](../08-stateless-ai-gateway/contract-examples.md), using this proposal's `api-key` header.

### 5.4 Streaming and errors

Successful SSE starts HTTP 200 with `Content-Type: text/event-stream`.
The target also requires `Cache-Control: no-store` (**not yet set by the pilot policy**).

| Protocol | Required completion semantics |
| --- | --- |
| Responses | Preserve lifecycle, text/tool-argument deltas, and `response.completed` with successful status; no Chat Completions `[DONE]` |
| Chat Completions | Preserve choice/tool indexes, IDs, argument fragments and finish reason, followed by `[DONE]`; `length` / `content_filter` are partial/blocked |

- Forward incrementally with `buffer-response="false"`; no response/semantic cache, body diagnostics at API or inherited scopes, or outbound body policies that buffer SSE.
- Parse UTF-8/SSE framing across transport chunks; preserve event order and final usage, including usage-only Chat chunks.
- Native errors, `response.failed` / `response.incomplete`, cancellation, or EOF without successful termination mean failure. Never replay partial output or manufacture a success marker.
- Assemble tool calls by IDs and execute only after successful inference completion. The runtime can hold one customer-facing stream across multiple inference/tool cycles.

Target pre-stream error envelope:

```json
{"error": {"code": "invalid_model", "type": "invalid_request_error", "message": "Use service-default."}}
```

| HTTP status | Meaning |
| --- | --- |
| 400 | Invalid JSON, fields, alias, profile options, or conflicting limits |
| 401 / 403 | Missing/invalid credentials / unauthorized customer or identity conflict |
| 413 / 429 | Body too large / gateway or provider rate/capacity limit |
| 502 / 503 / 504 | Upstream protocol failure / unavailable service / timeout |

Preserve safe provider errors/retry guidance; uniform envelopes remain hardening work.
After SSE headers, status cannot change: native errors or truncated EOF mean failure.
No binding-conflict/expiry, resume, or exactly-once guarantees.

## 6. Changes needed in the agent-runtime

**Pilot:** existing MAF works through APIM; no new provider, orchestrator, routes, or history store.
**Adoption:** promote sample-local checks into an opt-in app-wide gateway-only profile.
Other deployments retain existing behavior.

Use the existing environment settings:

```text
AZURE_FUNCTIONS_AGENTS_PROVIDER=azure_openai
AZURE_OPENAI_ENDPOINT=https://<approved-apim-host>/llm
AZURE_OPENAI_API_KEY=<customer-apim-subscription-key>
AZURE_OPENAI_DEPLOYMENT=service-default
AZURE_FUNCTIONS_AGENTS_MODEL=service-default
AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT=false
```

| Runtime surface | Required change / what stays |
| --- | --- |
| Configuration: [env.py](../../azure-functions-agents-runtime/src/azure_functions_agents/config/env.py), [_provider_config.py](../../azure-functions-agents-runtime/src/azure_functions_agents/harness/_provider_config.py) | Validate approved HTTPS host + `/llm`, APIM key and fixed alias. Reject direct providers, base-URL overrides and credential/default-provider fallback; autodetection alone is insufficient. |
| Resolved agents: [_maf_runner.py](../../azure-functions-agents-runtime/src/azure_functions_agents/harness/agent_framework/_maf_runner.py), [schema.py](../../azure-functions-agents-runtime/src/azure_functions_agents/config/schema.py) | Qualify every catalog entry after merging. Replace concrete models with `service-default`; validate configured `agent_configuration.max_output_tokens` within 1-256 and the common text/function/context profile. |
| Client/context: [_maf_client.py](../../azure-functions-agents-runtime/src/azure_functions_agents/harness/agent_framework/_maf_client.py), [_maf_execution.py](../../azure-functions-agents-runtime/src/azure_functions_agents/harness/agent_framework/_maf_execution.py) | Keep `OpenAIChatClient`'s native Responses, runtime history/tools and `store: false`. Qualify wire options and budget SDK retries; no protocol translation or provider-state continuation. |
| Delegation/workflows: [runner.py](../../azure-functions-agents-runtime/src/azure_functions_agents/runner.py), [engine.py](../../azure-functions-agents-runtime/src/azure_functions_agents/workflows/engine.py) | Specialists build separate clients from their resolved models. Enforce the gateway target across all roles/entrypoints and tool-originated model calls; parent settings alone are insufficient. |
| Endpoints/history/tools: [endpoints.py](../../azure-functions-agents-runtime/src/azure_functions_agents/registration/endpoints.py) | Preserve UI/JSON/SSE and runtime sessions. Enforce user/conversation/tool isolation and coordinate overlapping turns. No browser keys; anonymous debug settings are not production authorization. |
| Streaming/telemetry: [_maf_execution.py](../../azure-functions-agents-runtime/src/azure_functions_agents/harness/agent_framework/_maf_execution.py), [_maf_observability.py](../../azure-functions-agents-runtime/src/azure_functions_agents/harness/agent_framework/_maf_observability.py) | Qualify native termination/errors, deadlines, idle detection and cancellation cleanup. Record metadata-only attempts/usage; distinguish alias from reported serving model. Disable sensitive-content capture. |
| Copilot SDK: [_copilot_providers.py](../../azure-functions-agents-runtime/src/azure_functions_agents/harness/copilot_sdk/_copilot_providers.py) | Deferred: host-only Azure endpoint validation rejects `/llm`. Qualify gateway Responses path/header/alias before enabling preview; no direct/built-in fallback. |

**Customer-facing contract stays separate:** `GET /agents/inventory/` serves the UI;
`POST /agents/inventory/chat` and `/chatstream` accept `{"prompt":"Hello."}`.
JSON: `response`, `model`, `tool_calls`, optional usable `session_id`.
Runtime SSE uses `session`, `tool_start`, `tool_end`, `delta`, `done` / `error`;
correlate by `tool_call_id`. `done` ends the loop, not proof of tool success.
Session IDs / optional `x-ms-session-id` never authorize access or pin models.

## 7. Gateway controls and acceptance

| Control | Pilot / target |
| --- | --- |
| Body / output | At most 1 MiB; requested output 1-256 tokens, default 256; no silent context truncation |
| Capacity | Per-key pilot: 12 calls/minute, 200/hour; shared router: 10 requests and 10,000 tokens/minute at capacity 10; no fairness or dollar-cap promise |
| Time / retries | Target: 120-second whole-turn deadline including tools/delegation; each inference uses remaining budget; 30-second upstream-event idle limit; explicitly bounded attempts, no replay after partial output |
| Gateway forwarding | Pilot `forward-request timeout="120" buffer-response="false"`; its timeout is for backend **headers**, not total stream lifetime; absolute/idle enforcement requires qualification |

APIM alone uses managed identity: project Responses uses the `https://ai.azure.com`
audience / scoped Foundry User role; account Chat Completions uses
`https://cognitiveservices.azure.com` / Cognitive Services OpenAI User.
APIM strips caller credentials and owns backend paths/API versions.
Enforce RBAC/network/egress: no runtime provider keys, Foundry inference role, or direct-model reachability.
**Required enforcement, not demonstrated by the public-network pilot.**

Version and requalify router configuration before changes; no 24-hour binding drain.
Pilot router settings: [IaC](../09-model-router-runtime-sample/infra/main.bicep).
`NoAutoUpgrade` does not freeze eligibility; GlobalStandard is not regional-only;
`store: false` is not zero retention. Approve data policy and full-turn cost/latency;
router-internal attempts can add cost. No added gateway replay.
Developer APIM has no production SLA.

Recorded [sample evidence](../09-model-router-runtime-sample/README.md): 116 local tests,
live inventory/weather JSON/SSE tool round trips, and native basic-chat continuation.
Recorded POC results, not a new test run or proof of these gates:

| Gate | Required evidence |
| --- | --- |
| Gateway-only access | Capture every role/entrypoint's model traffic through APIM; reject direct endpoints/overrides; prove runtime credentials and networking cannot bypass the gateway |
| Statelessness / portability | Fresh worker/connection continues from resent history; no binding lookup; qualify every candidate and observe a real model change before claiming cross-model continuation |
| JSON / tools / protocols | Each exposed API and exact SDK passes native JSON/SSE, complete authorized tools, matching IDs, explicit failures and correct terminal markers |
| Incremental delivery / isolation | Through real APIM, at least five controlled events 500 ms apart, each arriving within 250 ms of calibrated emission; interleave customers without cross-customer events |
| Failure / hardening | Test late errors, truncated EOF, disconnects, absolute/idle deadlines, cleanup and retry budgets; complete nested/context validation, `no-store`, consistent safe errors and production user isolation |

Detailed qualification remains in the [design](README.md).
Documentation only: runtime changes/deployment are not implemented or authorized.
See the [APIM timeout reference](https://learn.microsoft.com/en-us/azure/api-management/forward-request-policy)
and [Responses routing reference](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses-model-routing).
