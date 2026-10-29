# Hosted Skills model service: five-POC evaluation and concern audit

## Executive recommendation

**Scope correction after the meeting-concern review (2026-10-29): investigate
tenant and session isolation while sharing model deployments and caches.
A separate model deployment for every customer is not the baseline requirement.**
The immediate deliverable is evidence and unresolved questions, not a new
model-service implementation or deployment plan.

Foundry documents a shared-resource/shared-deployment approach, with tenant
isolation enforced by the application and its data layer. Investigate that
approach first. Dedicated resources/deployments remain alternatives for
customers with stronger network, version, throughput, or compliance boundaries;
the original recommendation to start with a dedicated resource per customer
is superseded for the shared-service investigation.

The five existing POCs remain useful protocol and application-contract
experiments. **They do not answer the shared-deployment/cache question.**
POCs 01-04 require distinct tenant endpoints and credentials, deliberately
reject shared targets in [service.py](model_service/service.py), and implement
no prompt/KV or response cache. Do not remove those checks or reinterpret their
passing tests as evidence for shared serving without a separately approved scope.

Add **POC 05: GitHub Copilot Fusion** as a **conditional managed-orchestration
candidate** for automatic model selection and cost/quality tradeoffs. It is
not a customer-dedicated model resource or private inference network. The user
approved local SDK-boundary mocks only; no GitHub model calls were made.

Neither shared-service isolation nor the stronger dedicated-boundary alternatives
are certified by these local POCs. Provider cache guarantees, real tenant/session
authorization, portal isolation, quality monitoring, and operating ownership
remain distinct concerns with distinct closure evidence.

POCs 01-04 use Microsoft Azure services; POC 05 evaluates a GitHub service.
OpenAI, Microsoft, Meta, and other catalog publishers are model publishers,
not five independent cloud vendors. No live model or publisher has been
benchmarked here.

## Final report disposition

Finalized on **2026-10-29** against runtime commit `5174e72`. All ten follow-up
tasks and both additional product themes have a recorded finding, evidence
boundary, and next closure action. The scope/recommendation corrections and
local verification work are complete; **this is not a declaration that every
operational task is complete**.

- **Verified locally:** 95 focused runtime test cases passed in 8.75 seconds,
  and all 27 POC test methods passed in 51.474 seconds. The five-POC smoke
  snapshot was regenerated without external model calls.
- **Research answered:** shared serving is supported as an isolation approach;
  caches, stored history, response routing, model abstraction, optimization and
  evaluation have separate documented responsibilities and limitations.
- **Still requires external evidence:** the selected provider's same-subscription
  cache assurance, live caller/session authorization, portal source and isolation
  validation, approvals, ownership handoff, recovery-prototype agreement, and
  customer/GA evidence. No approval, sync, deployment, or ownership transfer is
  inferred from local tests.
- **Implementation boundary:** no new shared-service design, production retry/
  correction feature, portal infrastructure, or runtime source change was
  introduced. This preserves the requested investigation-first scope.

## Meeting-concern coverage

This audit covers all ten follow-up tasks and the additional model-abstraction
and GA themes. **Documented is not the same as closed.** Research findings,
tested fixture behavior, operational validation, and human approvals are
distinguished below. The portal source and approval/handoff records were
confirmed unavailable for this review. Raw meeting notes and attendee details
are not reproduced here. Accountable roles below identify responsibility areas
to confirm; they are not accepted assignments or completed ownership transfers.

| ID | Concern | Current evidence/status | Accountable role to confirm | What remains to close it |
| --- | --- | --- | --- | --- |
| F01 | Shared model deployment and prompt/KV-cache isolation | **Research conclusion recorded; provider qualification remains open.** Shared serving is documented; provider prompt caching is not conversation memory. Existing POCs use separate endpoints and no cache. | Model-service investigation owner and provider contact | Obtain selected-model/API/SKU cache-boundary assurance, especially for SaaS tenants in one Azure subscription; then authorize appropriate validation. |
| F02 | Session-to-model routing and response ownership | **Source-reviewed, locally tested, and patterns compared.** MAF returns results to the owning invocation; target selection is per call, not a persisted session-to-deployment binding. | Runtime and model-service owners | Decide whether sessions require sticky deployment/version or may use managed routing; validate the chosen live path across streams, retries and continuations. |
| F03 | Runtime/MAF use of AzureWebJobsStorage | **Source and local unit validation complete.** The 95-case runtime selection covers storage/history and MAF execution. Transcript storage is separate from inference caches and uses agent/session paths without an explicit tenant segment. | Runtime/storage owner | Verify authenticated caller-to-session ownership and storage access in the deployed host; local tests do not establish that boundary. |
| F04 | Foundry and runtime model optimization | **Research review complete; not benchmarked.** Cache controls, workload/SKU tradeoffs, model router, and Agent Optimizer examples are covered below. | Model optimization owner | Evaluate representative quality, token cost, throughput, and latency before selecting or enabling optimizations. |
| F05 | Per-invocation/scheduled evaluation and quality score | **Usefulness assessment complete; no quality rollout.** Existing runtime Vally evaluation is relevant prior art; the POCs have no quality results. | Quality/evaluation owner | Agree datasets, evaluators, sampling, budgets, thresholds, and score interpretation; obtain real evaluation evidence. |
| F06 | Reuse a portal-provisioned model resource | **Pending approval.** No approval or portal pipeline evidence was supplied. | Portal owner and provisioning approver | Record approval, resource/pipeline ownership, resource group, allowed deployment/model, access, capacity, and budget. |
| F07 | ACA portal file, execution, and user-data isolation | **Blocked on portal source/environment.** A model-endpoint POC is not evidence about the creation portal. | Portal/execution owner | Trace browser and ACA file lifecycles, storage/mounts, runtime identity, execution scope, concurrency, cleanup, and access controls. |
| F08 | Fixed parameterized deployment artifacts and PR flow | **Prior art reviewed; portal validation blocked.** Parameterized Bicep exists in runtime samples, not proof of portal behavior. | Portal and CI/CD owners | Verify a versioned template/parameter allowlist, validation, PR approval, and deployment workflow; no unrestricted direct production edits. |
| F09 | Telemetry handoff and ownership transfer | **Pending coordination.** Instrumentation documentation is not a completed handoff. | Current and receiving telemetry owners | Hold the sync, record work inventory/access/acceptance criteria, and obtain acknowledgment from the receiving owner. |
| F10 | Lightweight error-analysis/auto-correction prototype | **Assessment boundary recorded; owner and prototype agreement pending.** Existing error propagation is not analysis or correction. | Prototype coordinator, not yet assigned | Assign an owner and agree capture, structured explanation, safe input correction, retry/idempotency boundaries, and measured prototype criteria. |
| C01 | Hide exact model management; optional model family | **Product direction captured, not a completed UX feature.** Local aliases and managed routers are relevant but do not establish the portal contract. | Product and model-policy owners | Decide the supported family/capability policy and enforce quality/version provenance without making customers operate individual deployments. |
| C02 | Differentiation, adoption, and GA readiness | **Not demonstrated.** Durable workflow capabilities are relevant; no adoption or comparative outcome data was supplied. | Product and GA decision makers | Agree GA gates and collect representative customer usage, durable-loop reliability, recovery benefit, quality, latency, cost, and operational evidence. |

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

## Shared-deployment and cache findings

### Separate the meanings of state and cache

| Layer | What it contains | Isolation implication |
| --- | --- | --- |
| Model weights/inference | The base model is not a per-session conversation database. | Sharing weights/deployment does not itself attach another user's history to a request. |
| Provider prompt/KV cache | Reusable computations for matching input prefixes; KV means attention key/value tensors here. | A cache hit is not retrieval of another session's messages or a previously generated answer. The documented cache scope still matters. |
| Runtime conversation history | Prior messages supplied through MAF/session storage. | Wrong session ownership or history lookup can put another tenant's data into a prompt, regardless of provider cache safety. |
| Provider-managed state | Stored responses, conversations, files, or agent state when those features are used. | Service object references must be authorized for the correct tenant; a shared credential is not per-SaaS-tenant authorization. |
| Application/gateway cache | Cached answers or general key-value data, including semantic-cache matches. | The application owns partitioning, authorization, retention, invalidation, and side-effect rules. This is not the provider's KV-tensor cache. |

### What the documentation establishes

The [Foundry multitenancy guidance][shared-foundry] explicitly supports sharing a
Foundry resource and model deployment. It also makes the application responsible
for isolation in that topology and identifies weaker isolation/noisy-neighbor
tradeoffs. Dedicated deployments are an option, not a prerequisite for all SaaS
customers.

The [Azure OpenAI prompt-cache guidance][prompt-caching] describes exact-prefix
computation reuse, a minimum cacheable prefix of 1,024 tokens, and cached-token
usage reporting. It states that prompt caching changes cost/latency rather than
the response content. It documents no sharing of in-memory prompt caches
**between Azure subscriptions**. That is not a promise of separate cache
partitions for two SaaS tenants inside the same subscription/resource/deployment.

`prompt_cache_key` is documented as a cache-matching/routing aid. It is not
documented as an authorization control or proof of tenant separation. Matching
public prefixes across tenants is conceptually different from admitting another
tenant's private history into a request. No assertion that arbitrary different
session IDs create independent provider KV-cache partitions is justified.

Cache controls are model- and deployment-specific. The current documentation
distinguishes earlier-model in-memory/extended retention from GPT-5.6-family
breakpoints and minimum-lifetime controls, and distinguishes Standard from
PTU-M support. For example, the documented cache-bypass option for GPT-5.6+
Standard requests is not a universal disable switch for older models/PTU-M.
Do not assume one TTL, cache-write price, or API parameter works for every
catalog model or for the versioned API used by a POC.

The [Models sold by Azure privacy guidance][model-privacy] separates stateless
base-model inference from optional stored service data and describes protection
of prompts/outputs from other Azure customers. Those commitments do not perform
our SaaS session authorization. They also do not mean there is no provider-side
storage: optional stateful features and applicable abuse-monitoring policies
must be considered separately.

For shared Responses API state, the multitenancy guidance requires the
application to scope response/conversation references. Keeping another copy of
history in Blob Storage does not remove a service-retained copy. Stateful and
stateless API modes must be assessed explicitly; background execution can impose
storage requirements. Do not generalize the Azure OpenAI cache documentation
to every publisher endpoint, self-hosted serving stack, or Foundry Agent Service.

For an application semantic/response cache, the same multitenancy guidance
requires trusted tenant-aware cache partitioning and relevant model/prompt/tool/
safety-version variation. Similarity-based answer reuse is especially unsuitable
for personalized or transactional/tool-side-effect-dependent responses without
strong additional constraints. The present POCs have **no such cache**.

### Open provider questions, before an implementation decision

For the intended model/version, API, deployment type, and geography, obtain
authoritative answers on:

1. The actual cache lookup/sharing scope within one subscription, resource, and
   deployment, including whether any supported control guarantees SaaS-tenant
   cache partitioning rather than just influencing cache hits.
2. Applicable retention, GPU-local persistence, processing location, cache-bypass
   support, and data-handling obligations for that exact combination.
3. Ownership controls for every optional response/conversation/file identifier,
   distinct from the inference cache.
4. Available evidence for internal caching and model calls. Cached-token
   counters or a model declining to reveal another user's data are not proof
   that unauthorized data never entered the request.

**Conclusion:** shared serving is a documented candidate. Full same-subscription
SaaS-tenant KV partition assurance is not established by the reviewed material
or our tests. Record that distinction instead of claiming either that all shared
caches leak data or that separate customer deployments are always necessary.
No shared-serving/cache implementation was added in this review.

## Runtime storage and session routing findings

### AzureWebJobsStorage is transcript storage, not model KV memory

Source findings below are pinned to runtime commit `5174e72`.

| Source | Observed behavior | Relevance to the isolation question |
| --- | --- | --- |
| [Storage settings][runtime-storage] | `AzureWebJobsStorage` takes precedence over `AzureWebJobsStorage__blobServiceUri`; the default session container is `azure-functions-agents`, with a configurable override. | The account/container boundary is separate from the model resource/deployment boundary. |
| [MAF Blob history][runtime-blob] | Default append-blob path is `agent-sessions/{agent_slug}/{session_id}.jsonl`; message deltas are serialized as JSONL. | No explicit authenticated SaaS tenant appears in this path contract. Same agent/session in the same store resolves to the same history object. |
| [MAF construction/execution][runtime-execution] | Configured Blob history is selected; otherwise local agent-scoped JSONL is used under the resolved configuration directory. | This is selection when configuration is absent, not a promise of safe local fallback after a configured storage failure. |
| [MAF inference defaults][runtime-execution] | Harness-agent construction supplies `default_options={"store": False}` and disables file memory. | Local tests verify this wiring; it is not a provider KV-cache disable switch or proof that no service-side data handling occurs. |
| [Session locks][runtime-locks] | An in-memory `asyncio.Lock` is keyed by agent and session. | It does not serialize turns across separate Functions workers or replicas. |
| [Copilot storage identity][runtime-copilot-storage] | Copilot uses an opaque SDK-owned filesystem with a `copilot-native/{agent_id}/{session_id}` prefix. | It is a different persistence format, not a MAF transcript or an inference KV cache. |

The MAF provider's cached Blob service clients are connection/client reuse,
not cached prompts, generated answers, or attention tensors. Shared storage
does not inherently require a dedicated model deployment; separate model
deployments do not repair an incorrect transcript authorization boundary either.
Functions host/Durable storage usage must not be confused with the explicit
message-history provider.

At the inspected runtime layer, session validation and agent-scoped paths are
not proof that an authenticated caller owns a conversation. An upstream host
could enforce that ownership, but no portal source/configuration was available
to verify it. The POC's `(tenant, agent, session, model alias)` in-memory key is
an experiment's behavior, **not an already implemented runtime storage key**.

The initial test attempt used a global environment with
`azure-functions-durable==1.2.10`, which did not expose the imported
`DurableFunctionsClient`. That differed from the repository's pinned
`azure-functions-durable==2.0.0b2`; it was an environment mismatch, not evidence
that these runtime tests fail under their intended dependencies.

For the finalized report, locked development dependencies were restored with
`uv sync --frozen --extra dev --no-install-project` into a session-only isolated
environment. All **95 test cases passed in 8.75 seconds** across:

- `tests\test_session_storage.py`
- `tests\test_maf_blob_history.py`
- `tests\test_maf_file_history.py`
- `tests\test_history_identity.py`
- `tests\test_maf_execution.py`

The selected execution tests include real MAF orchestration with a fake chat
client, fresh-session history reload, configured-Blob selection/failure
behavior, inference-default wiring, and result/stream interpretation. The
environment used Python 3.13.15, `agent-framework-core==1.13.0`,
`agent-framework-openai==1.10.2`, `agent-framework-foundry==1.10.3`, and
`azure-functions-durable==2.0.0b2`.

These are local tests, not live inference or deployed Blob/RBAC validation.
Runtime source, dependency manifests, lockfile, and the global Python environment
were unchanged. Passing a constructor option test is not a wire-level
verification that every provider honors `store=False`, and provider prompt/KV
caching remains a separate feature.

### Deployment choice and return routing are separate from history

In the current MAF path, a fresh agent/session is constructed for a run;
`build_chat_client(model)` resolves the target from configuration on that call.
The reviewed code does not persist an immutable session-to-deployment/version
binding in the history provider. The model field on `AgentResult` reflects the
resolved target/deployment, not proven provenance of every underlying model
chosen by a router.

Responses return through the owning awaited SDK invocation or its stream.
The runtime produces an `AgentResult` with the resolved session ID; the
[built-in HTTP surface][runtime-endpoints] returns session/model metadata and
the streaming path emits session identification. Sharing a deployment does not
require broadcasting responses or using a cache key as a reply address.
Authorization and correlation still need validation on every exposed surface.

| Integration pattern under investigation | Session/state ownership and response association | Unresolved tradeoff |
| --- | --- | --- |
| Direct runtime calls to one shared Foundry/OpenAI deployment | Runtime retains the owning invocation and history context; each SDK response belongs to its request/stream. | Tenant authorization, storage scope, quotas, and whether a deployment/version must stay fixed across turns. |
| Central model-service proxy | Caller and service both need an unambiguous owning request; asynchronous work must preserve tenant/session/invocation correlation. | Extra trust boundary, propagation, streaming/cancellation, retries and metering; not implemented here. |
| APIM in front of shared inference | Request/stream returns through the gateway; history generally remains with its existing owner unless a separate state feature is chosen. | Tenant-aware policy/cache keys and limits; backend load balancing alone is not session/deployment stickiness. |
| Provider-managed Responses/agent state | Provider state identifiers supply continuity; the application must authorize every reference. | Service retention and the exact identity boundary differ from stateless model inference. |
| Foundry model router or GitHub Fusion | Conversation ownership remains with the application/session, while underlying model selection may vary by request. | A stable router name is not an immutable underlying model/version or a quality guarantee. |

The decision to investigate is **sticky deployment/version per conversation
versus policy-controlled model changes with preserved, authorized history**.
Neither strategy has been selected or implemented by this review. All nested
tool/subagent calls, streams, workflow continuations, retries, and history reads
must be included when qualifying the eventual choice.

## Model abstraction, optimization, and quality findings

### Customer contract and regression risk

The product direction is a stable Hosted Skills capability, optionally with a
model-family/capability constraint, rather than asking customers to create or
manage individual models. The POC's `chat` alias is useful prior art, not proof
that the portal implements that experience.

The platform still needs internal deployment/model/version, prompt/tool version,
and evaluator provenance to explain quality changes. A deployment name or API
version is not the same as the model's weight version. [Foundry version
policies][model-versions] vary by model/provider/deployment type; opting out of
updates does not keep a retired model usable indefinitely.

[Foundry model router][foundry-router] is a relevant service-side option that
was missing from the original discussion of model abstraction. It exposes
Balanced/Cost/Quality modes and eligible model subsets behind one deployment;
it is not the same product as GitHub Fusion. Its active router version can
receive new underlying models/features without a new router version identifier.
A configured subset can constrain eligible models, but neither a stable router
version nor its claimed quality band substitutes for workload-specific regression
evaluation. No router was deployed or benchmarked.

### Optimization options reviewed, not enabled

| Option | Potential benefit | Required qualification |
| --- | --- | --- |
| Stable reusable prefixes and supported cache controls | Reduce repeated input processing and input cost. | Cache/tenant boundary, model-specific support, retention, read/write accounting and actual hit rate. |
| Prompt/history/tool-context and output/reasoning budgets | Avoid unnecessary tokens, work and latency. | Preserve task success and tool behavior; measure total agent-loop cost, not one cheap call. |
| Standard versus priority/provisioned throughput | Match variable demand or predictable interactive throughput/latency needs. | [Provisioned capacity][provisioned-throughput] has committed capacity costs and sizing constraints; it is not automatically cheaper at low utilization or a SaaS tenant boundary. |
| Batch processing | Discounted offline processing with separate enqueued-token quota. | [Batch documentation][batch-processing] describes a 24-hour target, not an interactive latency guarantee; unsuitable for a synchronous creation response. |
| Dynamic quota where supported | Opportunistically consume spare throughput. | [Documented as preview/classic][dynamic-quota], not guaranteed additional capacity or a spend ceiling; more requests can increase cost. |
| Foundry model router / Fusion | Delegate some model selection and quality/cost decisions. | Benchmark the actual allowed pool, workload, full-call costs, policy and data boundary; router behavior can change. |
| Agent Optimizer | Compare instruction, skill, tool-description, and model candidates against an evaluation objective. | [Reviewed examples][optimizer-targets] use datasets/evaluators, a baseline, separate evaluation/optimization models and optional `model_search_space`; candidate models must be deployed. |

Agent Optimizer is an evaluation-driven candidate-generation workflow, not an
automatic per-request tenant-isolation control. The reviewed example includes
a held-out validation dataset and explicit model-search configuration. It does
not demonstrate that this Functions runtime is already optimizer-ready or that
an optimizer candidate can safely edit production. No job, quality/cost gain,
candidate application, deployment, or automatic correction was executed.

### Are per-invocation and scheduled evaluations useful?

**Yes, with different roles.** Apply cheap deterministic correctness checks
where appropriate, evaluate representative production samples asynchronously,
and run repeatable regression evaluations before changing prompts/models/tools
and on an agreed recurring cadence. Expensive model-based judging on every
invocation should be justified by risk, latency, privacy, and budget rather than
made the default without evidence.

[Foundry evaluation][foundry-evaluation] supports turn-level and conversation
evaluation, datasets, production traces, CI/CD and recurring production
evaluation. The runtime already has a [Vally evaluation integration][runtime-evaluation]
for authored behavior through the normal chat endpoint; it does not prove
platform reliability or GA readiness.

A Hosted Skills quality indicator should identify its rubric, version, data
population, sample count, time window and limitations. A judge's score or the
model's self-reported "confidence" is **not a calibrated probability of success**.
Do not display a confidence percentage without calibration against labelled
outcomes. Compare task/groundedness/tool correctness and safety alongside
failure/retry rate, latency and full token cost, including judge/router calls.
No quality/confidence measurement or evaluator is supplied by the existing mocks.

## Portal, recovery, telemetry, and GA closure requirements

These are tracked concerns, not completed tasks or an approved implementation.

- **Portal model provisioning (F06):** obtain the portal owner's approval for
  an existing model resource/deployment managed with the portal pipeline and
  resource group; record identity, quotas, lifecycle, operational owner, and
  budget. A resource group is not a tenant authorization boundary, and a
  provisioned deployment's idle costs differ from pay-per-token serving.
  No new deployment per creation flow is justified by these notes.
- **Portal isolation (F07):** obtain the portal source and deployment settings.
  Trace whether generated files exist in browser memory/IndexedDB/downloads,
  ACA working directories or temporary files, mounted volumes, Blob storage,
  Git/PR artifacts, and logs. Verify concurrent users cannot access another
  user's files, credentials, execution/session state, outputs, or retained data;
  include cancellation, restart, cleanup and tool/subagent paths. "Browser
  storage only" and "ACA isolates every user" remain unverified claims.
- **Fixed artifacts and deployment workflow (F08):** runtime samples contain
  [parameterized Bicep][runtime-bicep] and a [model module][runtime-foundry-bicep].
  They are reusable prior art, not evidence of the missing portal workflow.
  Review a fixed versioned template, constrained parameters, validation, and
  the existing PR/deployment approval path. The sampled model module also uses
  automatic default-version upgrades, which reinforces the quality-monitoring
  concern. No generated infrastructure or production editing was introduced.
- **Telemetry handoff (F09):** use the existing [observability contract][runtime-observability]
  as an inventory starting point, not proof of ownership transfer. The sync
  must settle emitters, correlation/schema, dashboards/alerts, permissions,
  sampling, privacy/retention, remaining work, acceptance and receiving-owner
  acknowledgment. Minimize prompt/input content in routine diagnostics. No
  meeting was scheduled or held, and no work was transferred by this audit.
- **Lightweight recovery prototype (F10):** assign an owner and agree a small
  assessment with the relevant feature contributors before implementation.
  Cover sanitized failure capture, a structured explanation with evidence and
  uncertainty, permitted input corrections, original/corrected invocation
  correlation, and how retry outcomes are represented. Decide an explicit
  retry budget, authorization and idempotency policy; timeouts can leave an
  unknown side-effect outcome. Do not equate transport retries with input
  repair, auto-edit production, or bypass authentication/safety failures.
  The current POCs only propagate errors; no such prototype is implemented.
- **Differentiation and GA (C02):** [durable workflows][runtime-workflows] and
  selective agent-assisted recovery are hypotheses to validate with customers,
  not established differentiators. Agree representative customers/tasks,
  adoption and repeat usage, quality/cost/latency targets, durable resume and
  failure-injection criteria, recovery benefit, support and operational
  ownership. No target counts, outcomes, production SLOs, or GA approval were
  supplied. Passing local protocol/isolation mocks cannot close this concern.

## Implemented architecture and trust boundaries

The following describes the existing experiments, not a shared-service design
selected after the meeting.

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

## Existing POC comparison

These dedicated-endpoint scenarios and the Fusion mock remain unchanged.
Shared serving and application-level isolation must be investigated separately;
this table is not a decision to create a model deployment for every customer.

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

For final report publication, the full POC suite completed **27 test methods in
51.474 seconds, all passing**: 15 Fusion-boundary methods and the 12 existing
HTTP/process methods. This includes regression coverage for all four original
approaches, not only the new mock. Separately, the five selected runtime
storage/history/MAF execution files passed **95 test cases in 8.75 seconds**
under locked dependencies, as detailed above.

The report does not count those source-checkout tests as bundled POC tests or
as live integration certification. Both suites and the regenerated five-POC
smoke run used local fixtures rather than external model calls.

The smoke run in [local-results.json](local-results.json) completed:

| Approach | Concurrent customer calls | Distinct worker PIDs | Distinct temporary workspaces | Observed per-call elapsed time |
| --- | --- | --- | --- | --- |
| Azure OpenAI mock | 2 | 2 | 2 | 378.48-380.15 ms |
| Foundry mock | 2 | 2 | 2 | 369.66-369.70 ms |
| APIM + backend mocks | 2 | 2 | 2 | 474.70-475.02 ms |
| Azure ML mock | 2 | 2 | 2 | 323.74-324.62 ms |
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

## Integration decision status

No shared-service implementation has been selected. The routing/state patterns
above are a research comparison, not approval to add a new proxy, per-customer
deployment, cache, or runtime contract. Resolve the provider and ownership
questions first. Any later runtime change must follow its repository review
process and preserve authenticated context across every execution surface.

The Fusion mock's `disconnect()` clears in-memory fixture state. Real SDK
`disconnect()` preserves persisted sessions, so live cleanup must explicitly
address deletion, retention, and tenant-owned storage. Similarly, a cancelled
wait does not prove a live agent stopped. Validate abort behavior and recovery;
Fusion-discarded drafts can leave workspace edits that are not automatically undone.

## Future validation gates

These are evidence gates, not an approved deployment plan. The per-POC READMEs
retain their original optional dedicated-scenario setup. Before a live pilot:

1. Resolve the shared-deployment/cache questions and the required isolation
   level. Dedicated resource, subscription, deployment compute, and physical
   exclusivity are different optional requirements, not universal defaults.
2. Select model/version/publisher, supported tool/streaming interfaces, regional
   deployment type, data processing/residency rules, quota, and licensing.
3. Approve a cost estimate using actual expected token volumes and resource
   SKUs. Include idle GPU replicas, gateway, networking, storage, and telemetry.
4. Confirm least-privilege service identities and resource scopes, and separately
   enforce tenant/session ownership. A resource credential authenticates service
   access; it does not by itself identify or isolate every SaaS customer.
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
11. [Foundry shared-resource/shared-deployment guidance][shared-foundry] covers
    application-owned isolation, stateful references, tenant-aware caching and
    deterministic isolation validation.
12. [Azure OpenAI prompt caching][prompt-caching] describes prefix matching,
    subscription scope, model/SKU-specific controls, retention and token accounting.
13. [Data privacy for Models sold by Azure][model-privacy] distinguishes
    stateless models from stored service features and provider data handling.
14. [Agent Optimizer targets and examples][optimizer-targets] includes dataset,
    evaluator, held-out validation and model-search configuration.
15. [Evaluation in Foundry][foundry-evaluation] describes turn/conversation,
    offline, CI/CD and production evaluation options.
16. [Foundry model router][foundry-router] documents routing modes, subsets and
    the active router version's in-place updates.
17. [Model version policies][model-versions] distinguishes model/API versions,
    auto-update choices, provider differences and retirement constraints.
18. [Provisioned throughput][provisioned-throughput], [batch processing][batch-processing],
    and [dynamic quota][dynamic-quota] describe different capacity/cost/latency
    tradeoffs, not measured benefits for this application.

[shared-foundry]: https://learn.microsoft.com/azure/architecture/guide/multitenant/service/foundry-models
[prompt-caching]: https://learn.microsoft.com/azure/foundry/openai/how-to/prompt-caching
[model-privacy]: https://learn.microsoft.com/azure/foundry/responsible-ai/openai/data-privacy
[optimizer-targets]: https://learn.microsoft.com/azure/foundry/agents/how-to/optimize-agent-targets
[foundry-evaluation]: https://learn.microsoft.com/azure/foundry/concepts/evaluation-overview
[foundry-router]: https://learn.microsoft.com/azure/foundry/openai/concepts/model-router
[model-versions]: https://learn.microsoft.com/azure/foundry/foundry-models/concepts/model-versions
[provisioned-throughput]: https://learn.microsoft.com/azure/foundry/openai/concepts/provisioned-throughput
[batch-processing]: https://learn.microsoft.com/azure/ai-foundry/openai/how-to/batch
[dynamic-quota]: https://learn.microsoft.com/azure/ai-foundry/openai/how-to/dynamic-quota
[runtime-storage]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/harness/_session_storage.py
[runtime-blob]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/harness/agent_framework/_maf_blob_history.py
[runtime-execution]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/harness/agent_framework/_maf_execution.py
[runtime-locks]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/harness/_harness_execution.py
[runtime-copilot-storage]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/harness/copilot_sdk/_copilot_session_identity.py
[runtime-endpoints]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/src/azure_functions_agents/registration/endpoints.py
[runtime-evaluation]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/docs/evaluation.md
[runtime-observability]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/docs/observability.md
[runtime-workflows]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/docs/workflows.md
[runtime-bicep]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/samples/basic-chat/infra/main.bicep
[runtime-foundry-bicep]: https://github.com/Azure/azure-functions-agents-runtime/blob/5174e72d6036cc6248013d3678f715f7c0e14998/samples/basic-chat/infra/app/foundry.bicep
