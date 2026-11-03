# Minimal Design: Customer Model Service

**Date:** 2026-11-03

**Status:** initial scope aligned with the locally tested session-routing
implementation. The new Azure Table/service deployment is not yet live-qualified.

## 1. Problem

Customers run agents through Copilot SDK and need access to several model
endpoints.

**Once a session selects a model, every request in that session must use the
same model/version for 24 hours, even if the model-service worker restarts.**

Copilot SDK manages conversation context, compaction, and the agent flow.
The model service must not maintain a second conversation history.

## 2. Requirements and Initial Scope

The first version will:

1. Support multiple configured model endpoints.
2. Authenticate customers and restrict which agents and models they can use.
3. Persist one model binding per customer, agent, and session within the application.
4. Keep that binding fixed for **24 hours from creation**, without sliding renewal.
5. Forward SDK-packaged requests without adding history.
6. Reject unauthorized, expired, or conflicting requests.
7. Never silently substitute another model.

**Boundary:** this provides routing consistency, not a guarantee of uninterrupted
provider availability. Active model deployments must not be retired or upgraded
during their binding window.

For this version, a session belongs to **one agent**. Conversation-wide affinity
across multiple agents is a future extension.

## 3. Design

```text
Customer backend / Functions-hosted agent
                 |
             Copilot SDK
        Context and compaction owner
                 |
                 v
           Model Service
     Authentication + authorization
          Session routing lookup
            /             \
           v               v
   Azure Table Storage   Selected model endpoint
   24-hour binding       Model A / Model B / Model C
```

| Component | Responsibility |
| --- | --- |
| Functions Host / customer backend | Runs application and agent code |
| Copilot SDK | Manages context, compaction, tools, and SDK session state |
| Model service | Checks access and routes each request |
| Azure Table Storage | Remembers the session's assigned model and expiry |
| Model endpoints | Perform inference |

The registry stores routing metadata only:

```text
Customer + Agent + Session
    -> Endpoint + Model version + Created time + Expiry
```

The application namespace separates applications. No prompts, conversation
history, tool outputs, or credential values are stored in the routing registry.
SDK-state persistence and resume remain the calling runtime's responsibility.

### Request flow

**Session creation:**

1. Authenticate the customer calling `POST /sessions`.
2. Check permission for the requested agent and model alias.
3. Resolve the alias to a configured endpoint.
4. Atomically save the 24-hour binding so concurrent creators agree.
5. Return the session-specific provider URL for Copilot SDK.

**Later inference requests:**

1. Authenticate and authorize again.
2. Load the existing binding.
3. Forward the SDK's context to the pinned endpoint.

Repeated creation with the same model returns the original binding and expiry.
At expiry, return **410: session expired**; a new session ID is required.
If the assigned endpoint is unavailable or changed, return an explicit error,
not another model.

## 4. Authentication: Who Is Calling?

For the minimal version, use **one distinct API key per customer backend**.

- The backend sends `Authorization: Bearer <customer-api-key>` over HTTPS.
- The model service validates the key and derives the customer identity.
- Customer identity is **not trusted from request JSON or an arbitrary tenant
  header**.
- Keys stay in backend secret storage, not browser code, source, or prompts.

Missing or invalid credentials return **401**.

The model service uses **separate server-side provider credentials** when
calling model endpoints. The customer key is never forwarded to the provider.
Azure Table access uses the service's managed identity.

## 5. Authorization: What Can This Caller Access?

Maintain a small customer policy:

| Customer | Allowed agents | Allowed models |
| --- | --- | --- |
| Customer A | Assistant | Economy, Quality |
| Customer B | Assistant | Economy |

For every operation, verify:

- The customer may use the requested agent and model.
- The binding belongs to that customer.
- The binding has not expired.
- The requested model matches the binding.

| Condition | Response |
| --- | --- |
| Model or agent not permitted | **403** |
| Session absent under that customer | **404** |
| Attempt to change the session's model | **409** |
| Session expired | **410** |
| Pinned endpoint unavailable or changed | **503** |

Knowing a session ID or URL does **not** grant access. This initial authorization
boundary is **customer-level**, not individual-user-level.

## 6. Future Extensions

These are not required for the initial routing contract:

- **Entra ID/workload authentication**, per-user permissions, and richer
  credential lifecycle management.
- **Cost-aware admission routing:** choose the cheapest qualified model when
  creating a session, then preserve its 24-hour binding.
- Same-model failover, capacity management, and availability objectives.
- Shared token budgets, billing attribution, and detailed usage reconciliation.
- Additional provider protocols and cross-agent affinity.

## Implementation References

- [Session-affinity setup and SDK guide](07-session-affinity/README.md)
- [Example endpoint/customer catalog](07-session-affinity/config.example.json)
- [Detailed architecture and qualification boundaries](DESIGN.md)

The routing implementation is locally tested; the new Azure Table, service,
and gateway routes have not been deployed or live-validated. This document does
not authorize a deployment or new paid model calls.

**In one sentence:** Copilot SDK owns the conversation; our service owns customer
access and the durable 24-hour model assignment.
