# Hosted Skills model service: five-POC evaluation

## Executive recommendation

Start with **POC 01: customer-dedicated Azure OpenAI resources and tenant-specific
agent workers** as the smallest integration pilot for OpenAI-backed Hosted Skills.
Use **POC 02: customer-dedicated Foundry resources** when model publisher choice
is a business requirement. Add **POC 03: APIM** for identity-based routing,
quotas, and governance; it complements either hosting option, not replaces it.
Choose **POC 04: Azure ML managed online endpoints** when dedicated serving
deployments and control of model weights justify higher operational cost.

Add **POC 05: GitHub Copilot Fusion** as a **conditional managed-orchestration
candidate** for automatic model selection and cost/quality tradeoffs. It is
not a customer-dedicated model resource or private inference network. The user
approved local SDK-boundary mocks only; no GitHub model calls were made.

The requested scope includes customer data/credential/history isolation,
per-execution process/container isolation, and dedicated model/network boundaries.
**No candidate is certified to meet that complete requirement by these local
POCs.** Application contracts and process dispatch were tested; cloud resource,
identity, container, network, and hardware guarantees remain acceptance gates.

POCs 01-04 use Microsoft Azure services; POC 05 evaluates a GitHub service.
OpenAI, Microsoft, Meta, and other catalog publishers are model publishers,
not five independent cloud vendors. No live model or publisher has been
benchmarked here.

## Evidence from the existing runtime

The runtime is Python-based and defaults to Microsoft Agent Framework (MAF).
The following source was inspected without modifying the
[public runtime repository at commit 5174e72](https://github.com/Azure/azure-functions-agents-runtime/tree/5174e72d6036cc6248013d3678f715f7c0e14998).
These pinned external references are evidence for the analysis; the runtime
checkout is not included in or required to run this POC repository.

- [`_provider_config.py`](https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/harness/_provider_config.py):
  provider selection and model precedence are based on process/app environment.
- [`_maf_client.py`](https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/harness/agent_framework/_maf_client.py):
  Azure OpenAI constructs `OpenAIChatClient` with the configured endpoint;
  Foundry constructs `FoundryChatClient` with a **project** endpoint and credential.
- [`hosted_skill_binding.py`](https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/hosted_skill_binding.py):
  `HostedSkill.run()`/`stream()` accept prompt and optional session ID; they do not
  accept an authenticated customer-specific endpoint/credential target.
- [`architecture.md`](https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/docs/architecture.md):
  selection is once per app, and history persistence is owned by the harness.
- [`_copilot_runtime.py`](https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/harness/copilot_sdk/_copilot_runtime.py)
  and [`_copilot_providers.py`](https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/harness/copilot_sdk/_copilot_providers.py):
  the optional Copilot preview uses `mode="empty"`, disables logged-in-user
  discovery, and constructs explicit OpenAI/Azure OpenAI/Foundry BYOK providers.
- [`pyproject.toml`](https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/pyproject.toml):
  the Copilot extra pins `github-copilot-sdk==1.0.14`; current public SDK
  documentation alone does not live-qualify that pinned runtime's Fusion support.

Implication: do not temporarily mutate `AZURE_OPENAI_ENDPOINT`, provider keys,
or Foundry environment variables around calls in a shared host. Concurrent agent
runs and nested tool/delegation calls could observe the wrong configuration.
These observations describe an integration constraint, not a claimed exploit.
In particular, switching a BYOK deployment/model string to `fusion` does not
enable GitHub-hosted Fusion. A reviewed GitHub-authenticated harness path is
required; this POC does not change the runtime.

## Implemented architecture and trust boundaries

```text
Control plane (future): verified onboarding -> immutable customer model registry

Local data plane:
opaque authenticated token + claimed customer + agent/session/model alias + prompt
    -> compare token ownership with claimed customer
    -> authorize allowed agent/model alias
    -> bind trusted endpoint and one resource credential
    -> load only (customer, agent, session, model) history
    -> fresh Python process + fresh temporary working directory
    -> selected protocol -> customer's HTTP mock resource
    -> validate response -> commit successful turn -> return

POC 03 adds: customer's gateway mock -> customer's model backend mock

POC 05 uses a different, mock-only SDK boundary:
authorized customer -> customer-specific mock SDK client/model-list cache
    -> explicit customer token + scoped SDK session -> mock final message
    -> successful turn or explicit error and quarantined session
```

The dispatchers hold tenants' state and credentials and are **trusted**.
In POCs 01-04, workers receive only the selected tenant's credential/history via stdin, not
command-line arguments or inherited secret environment variables. There is no
customer-code execution, application-level provider fallback, response cache, or
retry. Redirects and environment HTTP proxies are disabled in the HTTP inference
worker. Real Fusion internally chooses models and may escalate; that service
behavior is separate from application fallback and is not simulated here.

History is in-memory and protected by a per-scope lock. Ten successful turns are
allowed per session, prompt length is capped at 8,192 characters, and the HTTP
adapters request 128 output tokens. The Fusion fixture validates neither a real
output-token control nor an effort-tier setting. Mocks do not enforce real tokens.
State is lost on service restart; histories and lock entries are not evicted
until that instance ends. This is intentionally a bounded experiment, not
production storage or a fleet-wide concurrency implementation.
For Fusion, session history is held by the in-memory SDK session mock. A failed,
cancelled, or timed-out send quarantines that session: reuse fails explicitly,
without pretending that a live SDK conversation could be rolled back safely.

### Critical distinctions

1. Separate session IDs are not customer authentication.
2. A fresh process/cwd is not an OS sandbox, container, restricted filesystem, or
   network boundary. Workers share the host account and its privileges.
3. A dedicated deployment is not necessarily a dedicated Azure resource.
4. A dedicated resource/project/subscription is not necessarily dedicated
   physical model-serving hardware or a restriction on inference geography.
5. Private endpoints secure connectivity only when public access, DNS, identity,
   and outbound paths are configured and tested together.
6. APIM governs model access; it does not isolate an agent's tools or storage.
7. Process-per-model-call here is not a full agent-per-execution isolation test.
8. Fusion's local fixture runs in one process and proves no process/container
   isolation. Model-policy restrictions and scoped sessions do not establish
   a dedicated GitHub model deployment or private inference endpoint.

## Comparison

| Dimension | 01 Azure OpenAI | 02 Foundry model endpoint | 03 APIM + dedicated backend | 04 Azure ML online endpoint | 05 GitHub Copilot Fusion |
| --- | --- | --- | --- | --- | --- |
| Primary purpose | Managed OpenAI inference | Managed inference with model/publisher choice | Governance and routing | Customer-controlled model serving | Managed model routing and orchestration |
| Model choice | Available OpenAI models | Supported catalog models; APIs vary | Whatever approved backend supports | Licensed weights fitting selected serving stack | GitHub selects from the identity's policy/plan-allowed pool |
| Local protocol | Versioned deployment Chat Completions | OpenAI v1 Chat Completions | Gateway OpenAI-shaped request, real mock backend forwarding | Proposed custom `/score` schema | In-memory SDK-boundary mocks; no Fusion REST API assumed |
| Proposed tenant boundary | Dedicated resource, identity, worker, storage, network | Dedicated resource, identity, worker, storage, network; not project alone | Dedicated gateway if required, plus tenant-dedicated backend/worker | Dedicated workspace as required, endpoint, deployment compute, worker, storage | Scoped GitHub identity/session and application worker; no dedicated inference boundary established |
| Current runtime fit | Closest existing environment-configured path | Existing project client path; this model-endpoint adapter is distinct | Endpoint adapter or validated routing through a compatible runtime client | Custom model-client adapter required | Needs a GitHub-authenticated path, not the existing BYOK-only preview |
| Main benefit | Smallest OpenAI-only pilot | Publisher/model flexibility | Authorization, quotas, usage governance | Weights/runtime/deployment control | Automatic model/pattern selection; avoids building a router |
| Main risk | Resource/quota sprawl; no physical exclusivity assumption | Catalog/protocol differences; resource/project confusion | Shared gateway/identity blast radius; cost and extra hop | GPU quota/cost, image/model lifecycle, capacity management | Changing model pool, variable credits/latency, entitlement and isolation constraints |
| Relative operating effort | Low to medium, rises with tenant count | Medium, rises with tenant/model variety | Medium to high, in addition to hosting | Highest | Less serving operations; additional identity, policy, and runtime integration |
| Cloud validity today | Not deployed/tested | Not deployed/tested | No APIM policies deployed/tested | No model/container deployed/tested | Conditional; no real SDK/runtime/model call |

Relative effort is an architectural judgment, not a measured Azure cost.
No dollar estimates are supplied: model, token volume, region, tenancy count,
networking/APIM tiers, replicas, and GPU SKU are not yet selected.

## Fusion feasibility: routing is not dedicated isolation

[POC 05](05-github-fusion/README.md) evaluates the actual GitHub product boundary,
not a home-built "Fusion-like" router.

GitHub describes **Single**, **Cascade**, and **Critique** execution patterns.
Standard selects one model; High and Max can add judging/escalation or parallel
critique when needed. A single customer request may therefore cause multiple
internal model calls, but Fusion returns one final response. It operates
separately from subagents and does not automatically start them.

The CLI documents `--model fusion` plus `--fusion-tier standard|high|max`.
The SDK says CLI models are supported and provides `list_models()`. The local
mock uses the documented CLI selector `fusion`, but the actual identifier and
tier control must be verified with the pinned SDK/runtime under an approved
identity. The public Python `create_session` signature inspected here has no
named `fusion_tier` parameter. No undocumented SDK flag, private endpoint,
generic Azure OpenAI compatibility, or tier/reasoning-effort equivalence is
assumed.

The mock implements per-customer clients because SDK model listings are cached
per client. Sharing discovery state across identities could produce the wrong
capability decision even if session credentials were otherwise distinct.
The fixture checks availability before sending; missing Fusion fails without
selecting a different model. All sessions receive explicit credentials and
customer/agent/session/model-scoped IDs. Its `mode="empty"`, disabled ambient
login, and denied-tool tests exercise **our mock configuration**, not live SDK
security behavior.

### Identity, policy, and billing

- User-attributed use needs authorized GitHub credentials and applicable
  Copilot entitlement; never use an operator's saved login as a fallback.
- GitHub also documents organization-attributed server-to-server access using
  enabled GitHub App installations, the Copilot Requests organization permission,
  and short-lived installation tokens. This path does not require a signed-in
  user; organization enablement and billing rules still apply.
- Bind the authenticated SaaS customer to the correct GitHub user/installation.
  Verify licensing and commercial terms; do not assume one employee seat covers
  a multi-customer service. Rotation and attribution remain live pilot gates.
- Fusion respects available models and organization/enterprise restrictions,
  including applicable data-residency policies. The model pool can change.
  These policies are not dedicated customer compute or private-network controls.
- GitHub bills the models used, without a separate Fusion fee. High/Max may use
  more credits because of additional model calls. No dollar estimate is inferred.

### Decision for this platform

**Conditional go** for a GitHub-managed routing/orchestration pilot where shared
managed inference is acceptable. **Not qualified for the full strict-isolation
requirement**: the reviewed sources do not establish customer-dedicated model
deployments, exclusive hardware, private inference networks, or a customer-
auditable trace of every internal model call.

For customers requiring a fixed exact model or a dedicated serving/network
boundary, retain an appropriate dedicated Azure design rather than assuming
Fusion offers that boundary. Choosing an optional local model in Fusion does
not guarantee every routing step stays local.

## Measured local results

Validation environment: Windows, terminal Python 3.13.15. Docker was not required
or used. No real model credentials were used, and no Azure/GitHub model calls
or cloud resource changes were made.

After adding Fusion and extracting shared request authorization, the full suite
completed **27 test methods in 54.688 seconds, all passing**: 15 Fusion-boundary
methods and the 12 existing HTTP/process methods. This includes regression
coverage for all four original approaches, not only the new mock.
An additional explicit tool-enablement rejection assertion then passed in a
focused rerun (0.029 seconds). The standalone `--approach github-fusion` command
also passed, reporting zero GitHub model calls.

The smoke run in [local-results.json](local-results.json) completed:

| Approach | Concurrent customer calls | Distinct worker PIDs | Distinct temporary workspaces | Observed per-call elapsed time |
| --- | --- | --- | --- | --- |
| Azure OpenAI mock | 2 | 2 | 2 | 396.79-398.52 ms |
| Foundry mock | 2 | 2 | 2 | 334.08-361.16 ms |
| APIM + backend mocks | 2 | 2 | 2 | 514.06-517.94 ms |
| Azure ML mock | 2 | 2 | 2 | 334.38-337.50 ms |
| Fusion SDK-boundary mock | 2 | Not exercised | Not exercised | 0.14-0.24 ms, in-memory only |

APIM recorded two separate backend forwards. The other approaches call their
model mocks directly, except Fusion, which makes no HTTP/model call at all.
Fusion created two customer-specific mock SDK clients and two distinct SDK
session IDs. It did not invoke or simulate GitHub's internal execution patterns.
The HTTP/process timings include Python subprocess startup and local HTTP handling.
Fusion's in-memory timings exclude all real SDK, network, model, and process
startup costs and **must not be used to claim Fusion is faster**.
Two samples per approach are insufficient for rankings or percentiles.
**They are not Azure/GitHub latency, throughput, model-quality, or cost benchmarks.**
Regenerating the results file changes timings and process identifiers.
Results schema version 2 distinguishes HTTP/process from SDK-boundary records.

### Tested acceptance contracts

| Contract | Evidence |
| --- | --- |
| Same session name across customers remains isolated | Concurrent calls return only the correct customer's prompt markers |
| History remains correctly scoped across turns | Second turn contains only its customer's prior user prompt |
| Agent/session namespaces remain separate | Reviewer agent and another session start at turn one |
| Concurrent turns do not overwrite history | Three same-session calls return turns 1, 2, 3 with all user prompts retained |
| Customer identity cannot be spoofed | Invalid tokens and token/customer mismatch fail before provider calls |
| Customer cannot select arbitrary model or agent | Unknown aliases/agents fail before provider calls |
| Resource credentials are distinct | Another tenant's resource key is rejected by every protocol mock |
| Deployment routing is enforced | Wrong deployment rejected by Azure OpenAI, Foundry, and gateway protocols |
| Failure does not become a successful turn | 401, 403, 429, 500, and malformed responses produce explicit errors |
| APIM does not reuse gateway credential at backend | Backend rejects gateway credential; normal forwarding uses its own key |
| No cross-tenant failover | Customer A backend failure does not alter customer B's successful routing |
| Worker environment is reduced | A parent secret sentinel is present during tests but not accepted in child environment |
| Workers are separate from dispatcher | Distinct worker PIDs differ from parent; unique workspaces are removed after calls |
| Worker deadline is enforced | Deliberately tiny timeout reports `worker_timeout` |
| Stated turn threshold is exact | Ten successes, eleventh denied before a provider call |
| Live endpoints are blocked | Registry rejects nonloopback and path/userinfo-bearing endpoints |

The table above describes POCs 01-04. Additional Fusion-only contracts are:

| Contract | Mock-boundary evidence |
| --- | --- |
| Identity/session separation | Identical public session IDs map to distinct customer SDK sessions and clients |
| Discovery stays identity-scoped | Customer A's Fusion availability does not enable Fusion for customer B |
| No ambient credential fallback | Invalid or another customer's GitHub credential is rejected; stored-login configuration is disabled |
| Agent/session history separation | Reviewer and alternate sessions begin at turn one |
| Same-session concurrency | Three overlapping sends serialize to turns 1, 2, 3 without lost prompts |
| No silent model fallback | Missing Fusion prevents session creation and prompt send |
| Explicit failure policy | Failed, timed-out, cancelled, absent, or empty responses quarantine the affected session |
| Mock tool boundary | Nonempty tool configuration and fixture tool access are rejected |
| Threshold/cleanup | Ten successes allowed; eleventh denied; clients/sessions close at context exit |
| Evidence labeling | Smoke result explicitly reports zero GitHub model calls and untested cloud/process properties |

Mock credentials are opaque random strings, not real OAuth tokens. Successful
mock tests do not validate Azure/GitHub authorization, token scopes, real SDK
permissions, Fusion's internal calls, or provider-side isolation.

## How to integrate without mutating the existing runtime

### Lowest-change cloud pilot: dedicated agent host per customer

1. Authenticate at the platform edge and map the authorized principal to an
   internal customer ID. Do not trust a customer ID embedded in English prompts.
2. Route that customer only to its dedicated agent host/worker and storage.
3. Supply the selected provider endpoint, identity, deployment, and history
   storage before that host starts. Do not change them on concurrent requests.
4. Invoke the existing HostedSkill facade within that customer's host.
5. Ensure **every** model call, subagent, tool, MCP connector, sandbox path,
   checkpoint, workflow continuation, and retry inherits the same customer
   boundary. Ban independently authenticated escape routes and unscoped caches.
6. If per-execution containers are required, run each complete agent execution
   in an ephemeral, restricted tenant-bound worker. Test identity/credential
   projection for the chosen isolation primitive before declaring it supported.

This pilot reuses the runtime without changing its provider contract. It
requires deployment/integration work not included in this local-only delivery.

### Later option: shared hosts with a typed model-service boundary

Introduce an immutable authenticated execution context and model-client factory
at the runner/harness boundary. Carry it through all turns, tool-driven LLM
calls, streaming, delegation, workflows, history, and retries. Avoid ambient
environment mutation. This is a separate runtime feature proposal requiring
the repository's design/review process, not an already implemented adapter.

For Foundry, choose deliberately between the runtime's project client path and
a model inference endpoint. For APIM/AML, verify or implement the exact client
adapter; setting an endpoint variable alone does not establish compatibility.
For Fusion, add a separate explicitly authenticated GitHub path only after
reviewing entitlement, discovery, supported effort controls, and session storage.
Do not reuse a shared developer login or the BYOK provider override.

The Fusion mock's `disconnect()` clears in-memory fixture state. Real SDK
`disconnect()` preserves persisted sessions, so live cleanup must explicitly
address deletion, retention, and tenant-owned storage. Similarly, a cancelled
wait does not prove a live agent stopped. Validate abort behavior and recovery;
Fusion-discarded drafts can leave workspace edits that are not automatically undone.

## Azure pilot setup and acceptance gates

The per-POC READMEs contain service-specific setup. Before deploying:

1. Agree what "dedicated" means: resource, subscription, deployment compute, or
   contractual physical hardware. Managed APIs must not be sold as physically
   exclusive without explicit service guarantees.
2. Select model/version/publisher, supported tool/streaming interfaces, regional
   deployment type, data processing/residency rules, quota, and licensing.
3. Approve a cost estimate using actual expected token volumes and resource
   SKUs. Include idle GPU replicas, gateway, networking, storage, and telemetry.
4. Choose tenant-specific identities and least-privilege resource scopes.
   Avoid a single broadly privileged identity shared across all workers.
5. Configure private ingress, DNS, public-access restrictions, egress allowlists,
   image isolation, resource limits, timeouts, and tenant-specific storage.
6. Run adversarial cross-tenant tests from deployed workers: wrong identity,
   wrong endpoint, same session ID, expired token, forged tenant claim,
   nonapproved egress, credential rotation, and backend failures.
7. Exercise a real multi-call HostedSkill execution including subagent/tool/
   streaming/workflow paths, host restart, parallel requests, and cancellation.
8. Verify redacted logs/traces, retention/deletion, tenant billing attribution,
   quotas, real token limits, p50/p95 latency, throughput, and failure recovery.
9. Record the deployed resource IDs, identity assignments, network test
   evidence, and measured model evaluation results. Only then change the status
   from local architecture experiment to cloud-validated POC.

No Azure provisioning or execution was approved for this delivery.
No GitHub model execution was approved either. A Fusion pilot additionally
requires evidence of identity/organization billing and token rotation, live
SDK/runtime Fusion selection, per-tier credit/quality/latency measurements,
actual permission and storage isolation, and a decision on whether the service's
documented inference boundaries meet each customer's requirements.

## Official Microsoft and GitHub sources

Documentation was retrieved during this work; availability and service details
can change. These sources establish architectural guidance, not evidence that
any Azure configuration was deployed here.

1. [Multitenancy and Foundry Models](https://learn.microsoft.com/azure/architecture/guide/multitenant/service/openai)
   compares shared and dedicated resources/projects/subscriptions and explains
   that a dedicated resource/subscription alone does not constrain processing
   geography.
2. [AI gateway in Azure API Management](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities)
   covers model APIs, authentication, token quotas, tier differences, and caching.
3. [Secure managed online endpoints using network isolation](https://learn.microsoft.com/azure/machine-learning/how-to-secure-online-endpoint?view=azureml-api-2)
   describes workspace private endpoints, managed virtual networks, and approved
   outbound connectivity.
4. [Azure OpenAI / Foundry v1 API guidance](https://learn.microsoft.com/azure/ai-foundry/foundry-models/how-to/use-chat-completions?view=foundry-classic)
   describes v1 model APIs and deployment-name usage; a project API is distinct.
5. [About GitHub Copilot Fusion](https://docs.github.com/en/copilot/concepts/models/copilot-fusion)
   describes execution patterns, effort levels, policy restrictions, billing,
   optional local models, and workspace/subagent limitations.
6. [Choosing a model in Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/choose-a-model)
   documents the `fusion` selector and `--fusion-tier` command-line option.
7. [Copilot SDK overview](https://github.com/github/copilot-sdk/blob/main/README.md)
   documents CLI-model support and runtime model discovery.
8. [SDK multi-tenancy and server deployments](https://github.com/github/copilot-sdk/blob/main/docs/setup/multi-tenancy.md)
   covers empty mode, explicit per-session credentials, and storage/runtime boundaries.
9. [SDK authentication](https://github.com/github/copilot-sdk/blob/main/docs/auth/authenticate.md)
   and [server-to-server tokens](https://github.com/github/copilot-sdk/blob/main/docs/auth/server-to-server-tokens.md)
   describe user and organization-attributed authentication paths and token lifecycle.
10. [Python SDK client](https://github.com/github/copilot-sdk/blob/main/python/copilot/client.py)
    and [session implementation](https://github.com/github/copilot-sdk/blob/main/python/copilot/session.py)
    were inspected for session arguments, client-scoped model caching, messaging,
    and disconnect semantics. Public main-branch source is not proof of deployed
    behavior in the runtime's pinned SDK.
