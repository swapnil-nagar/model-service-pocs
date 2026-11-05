# Hosted Skills model-service POCs

The original five **local-only architecture experiments** cover customer-isolated
LLM calls: four HTTP/process experiments and one conditional GitHub Copilot
Fusion SDK-boundary experiment. Their default commands never provision cloud
resources or send prompts to real models. This repository is standalone; the
[Azure Functions Agents runtime](https://github.com/Azure/azure-functions-agents-runtime)
is not bundled. The original experiments use it as an external reference; the
optional POC 09 native sample requires a separate editable runtime checkout.

An **explicit opt-in hosted extension** now lives separately from those mocks:
[POC 06: shared Foundry/APIM](06-shared-foundry-apim/README.md). Its source,
deterministic tests and fixed IaC are prepared. The Azure foundation is now
provisioned, the real image is healthy, and **bounded live acceptance passed**:
four successful turns across two synthetic tenants and all ten checks passed.
See the [live results](live-results.json) and [report](REPORT.md) for measurements,
earlier failed attempts and the production/isolation limits. These are not mock
results or a production-readiness certification.
The [real Fusion SDK preflight is blocked](05-github-fusion/README.md#live-sdk-preflight);
the user kept that workstream pending enablement rather than deploying an
unused Fusion runner or substituting another model.

## Integrated architecture design

### New stateless inference designs (2026-11-04)

POC 08 is **design-only**. POC 09 now has a separately prepared
[runnable MAF runtime sample](09-model-router-runtime-sample/README.md).
Its isolated gateway, router, and approved model-only Foundry project are
deployed. Real A/B JSON and SSE tool round trips pass through the project-based
Responses route; POC 08 remains design-only:

| POC | Model selection | Context and tool execution |
| --- | --- | --- |
| [08: stateless AI Gateway](08-stateless-ai-gateway/README.md) | The service selects an approved named deployment through APIM | Customer / Hosted Skills runtime |
| [09: stateless Model Router](09-stateless-model-router/README.md) | The service selects a router deployment; Foundry chooses an approved underlying model | Customer / Hosted Skills runtime |

Both accept customer-provided context and function-tool schemas. The model
returns tool-call requests; the customer runtime validates/executes the tools and
submits their results on the next inference call. Neither APIM nor Model Router
replaces that agent loop. No separate orchestration service is proposed. The
customer does not select a model or routing profile; both POCs use the same
fixed protocol placeholder and operator-controlled customer routing.

**Incremental SSE streaming per inference call is a mandatory acceptance
requirement for both POCs**, with non-streaming JSON also supported. The runtime
owns any customer-facing stream spanning tools and multiple model calls.
POC 08 is not live-qualified. POC 09's sample has 27 local tests and four passing
live checks (A/B JSON and SSE), including tool outputs and incremental deltas.
The small router has a shared 10-model-requests/minute limit; the smoke driver
paces calls. These results do not prove the separate 250 ms APIM forwarding gate,
served-model attribution, cancellation/billing, or production reliability.
See the [shared API examples](08-stateless-ai-gateway/contract-examples.md).

**There is no 24-hour pinning requirement for POCs 08 and 09.** The session APIs,
binding store, and expiry rules in POC 07 and older design documents are a
separate experiment, not prerequisites for these new stateless designs.
The existing default runner and test commands do not execute POCs 08 or 09.

### Earlier integrated/session-affinity design

Start with [the minimal design](MINIMAL-DESIGN.md) for the problem, initial
requirements, architecture, authentication/authorization, and future extensions.
It uses **Agent Runtime (MAF / Copilot SDK)**: MAF is the Functions runtime
default, while Copilot SDK is an explicit preview option.
The [runtime-to-model-service contract](CONTRACT.md) specifies the session and
inference APIs, runtime responsibilities, authentication, expiry, and errors.
The [design FAQ](MINIMAL-DESIGN.md#8-faq-storage-cost-cleanup-and-streaming)
compares Function Apps/Container Apps and Table Storage/Redis, and explains
low-traffic cost goals, backend replacement through APIM, and expiry/cleanup. The
[streaming contract within the minimal design](MINIMAL-DESIGN.md#streaming-contract-sse) defines
SSE events, gateway requirements, timeouts, and partial failures.

[DESIGN.md](DESIGN.md) explains the Functions Host, HostedSkill/MAF agent runtime,
and shared model service in detail. It includes current and proposed architecture
diagrams, tool/model request sequences, tenant identity and state ownership,
durability, caching, API contracts, deployment boundaries, and acceptance gates.
The proposed Functions-to-model-service adapter is not yet implemented; the
standalone live POC evidence remains separate.

## Durable 24-hour session routing

[POC 07: session affinity](07-session-affinity/README.md) implements a separate
opt-in customer model service. It selects among approved model endpoints,
atomically persists a tenant/agent/session binding for **24 fixed hours**, and
forwards Copilot SDK context through a session-scoped Chat Completions URL.
Azure Table Storage is the hosted registry; SQLite supports local verification.
There is no second transcript, sliding renewal, silent model change or fallback.
JSON, function-tool messages and SSE are supported.

The planned host is a separate Azure Function App, with on-demand Flex
Consumption proposed for low initial traffic. The routing core and
local/native-SDK tests are implemented; the Functions HTTP hosting adapter is
not. The new service/table/APIM routes have **not** been deployed or tested
against live Azure. The existing POC 06 image, routes and recorded paid results
are unchanged. See the guide for configuration, expiry behavior, SDK setup and
the limits of the availability guarantee.

## Investigation scope

The current question is **tenant/session isolation while sharing model
deployments and caches**, not a requirement to create a deployment per customer.
The [concern audit](REPORT.md#meeting-concern-coverage) maps all follow-ups to
evidence and remaining closure requirements.

POCs 01-04 still use distinct endpoints/credentials and no prompt/KV or response
cache. POC 05 mocks a GitHub SDK boundary. Their passing tests do not establish
shared-deployment/cache isolation, portal ACA isolation, model quality, or GA
readiness. The report now contains source-backed shared-serving/cache research,
runtime storage/routing findings, optimization/evaluation assessment, and an
explicit list of blocked approval/portal/coordination items. No new shared-service
implementation was introduced for that investigation.

The original offline audit records **27 passing POC test methods** and **95 passing
runtime-source test cases**. The latter were run separately from the external
runtime checkout with its locked dependencies; they are not bundled tests or
an additional dependency for running these POCs. Approvals, portal validation,
ownership handoff, and GA decisions remain explicitly unclosed where evidence
is unavailable.

## Run the original mocks on Windows

Python 3.13+ and its standard library are sufficient. No package installation,
Azure/GitHub login, Docker daemon, API keys, Copilot SDK installation, or network
access beyond loopback is needed.

Clone and run in PowerShell:

```powershell
git clone https://github.com/swapnil-nagar/model-service-pocs.git
Set-Location .\model-service-pocs
python -B .\run_poc.py --approach all --output .\local-results.json
python -B -m unittest discover -s tests -v
```

For an existing checkout, run the Python commands from the repository root.

Run one POC with `--approach azure-openai`, `foundry`, `apim`, `azure-ml`, or
`github-fusion`. POCs 01-04 start real HTTP mock endpoints, invoke fresh Python
subprocesses, check tenant-specific responses, then shut down their servers and
remove per-call temporary directories. POC 05 uses in-memory SDK-boundary mocks
and does **not** create an isolated worker or simulate Fusion's internal router.
The command exits unsuccessfully on failure.

| POC | Service vendor | Model publisher options, not live-tested | Local experiment |
| --- | --- | --- | --- |
| [01: Azure OpenAI](01-azure-openai/README.md) | Microsoft Azure | OpenAI | Deployment-scoped URL and resource-specific credential |
| [02: Foundry model endpoint](02-foundry/README.md) | Microsoft Azure | OpenAI; other supported catalog publishers such as Microsoft or Meta, subject to API/model availability | OpenAI v1-compatible model selection and bearer credential |
| [03: APIM AI gateway](03-apim/README.md) | Microsoft Azure | Inherited from approved backend; OpenAI-shaped mock here | Credential-authenticated gateway forwarding to a separate tenant backend |
| [04: Azure ML serving](04-azure-ml/README.md) | Microsoft Azure | Customer-selected model publisher, subject to license and serving support | Custom scoring request/response contract and dedicated endpoint |
| [05: Copilot Fusion (conditional)](05-github-fusion/README.md) | GitHub | GitHub-selected pool within plan and administrator policies | Customer-specific SDK clients, model discovery, explicit session credentials, and session separation; no live SDK/runtime |

These are five architectures, **not five independent cloud vendors**. Model
publisher and hosting/service vendor are different decisions. Fusion is a
managed router/orchestrator, not a dedicated customer model resource.

## Contract and implementation

- [contracts.py](model_service/contracts.py): immutable customer, target, request,
  and result records; secret/prompt fields excluded from their generated repr.
- [authorization.py](model_service/authorization.py): shared customer
  authentication and request authorization for both kinds of experiment.
- [service.py](model_service/service.py): authenticates a local opaque caller
  token, checks the claimed tenant, authorizes agent and model alias, binds
  credentials and endpoint from a trusted registry, and dispatches a worker.
- [worker.py](model_service/worker.py): four distinct wire adapters, explicit
  upstream errors, no proxy inheritance, and no redirect following.
- [mocks.py](model_service/mocks.py): separately credentialed per-tenant resources;
  APIM adds actual HTTP forwarding to separately credentialed backend mocks.
- [fusion.py](model_service/fusion.py): conditional mock-only SDK boundary; no
  Fusion REST endpoint, real SDK import, model calls, or router reimplementation.
- [test_isolation.py](tests/test_isolation.py): positive and negative contracts.
- [test_fusion.py](tests/test_fusion.py): GitHub identity, discovery-cache,
  session, concurrency, error/quarantine, and mock tool-control contracts.
- [REPORT.md](REPORT.md): architecture comparison, measured results, Azure setup,
  runtime integration implications, and limitations.
- [local-results.json](local-results.json): generated, prompt-free smoke results.
  Schema version 2 distinguishes HTTP/process results (`local_mock_only`) from
  SDK-boundary results (`local_sdk_mock_only`); Fusion does not report worker PIDs.

The public request contains tenant, agent, session, logical model alias, and
prompt. It cannot supply an arbitrary URL, provider key, deployment name, or
backend. Customer model information belongs in an authenticated onboarding
control plane that populates the trusted registry after authorization.

Both `assistant` and `reviewer` agents and the `chat` alias are allowed in this
fixture. History is keyed by `(tenant, agent, session, model alias)` and updated
only after successful inference. Same-session requests serialize; separate
sessions can run concurrently. Sessions allow ten successful turns, prompts
allow 8,192 characters, and POCs 01-04 request at most 128 output tokens. POC 05
does not validate a real Fusion output-token control or effort-tier setting.
Mocks check routing/authentication, not real tokenization or model token limits.
History and scope locks remain in memory until the experiment ends; production
requires tenant-owned durable storage, bounded retention, and distributed leases.
For Fusion, a failed, timed-out, or cancelled send quarantines that session:
subsequent use fails explicitly instead of silently resetting conversation
history or selecting another model. A new session ID is required in this fixture.

## Isolation meaning

Verified locally:

- Customer A's token cannot select customer B's tenant.
- Identical session IDs do not merge customer or agent histories.
- POCs 01-04 give every call a fresh process, fresh temporary working directory,
  and only its selected target credential. The child does not inherit parent secrets.
- Wrong resource credentials/deployments fail; failures are not saved as turns.
- APIM-style gateway credentials cannot directly authenticate to its backend.
- Fusion uses separate mock clients/model-list caches per customer and explicit
  session credentials; missing Fusion availability fails without model fallback.
- The Fusion mock rejects ambient-login configuration and tool access. These
  assertions test our mock boundary, not the real SDK's permission enforcement.

**Not provided or proven:** Entra JWT validation, Azure RBAC, private endpoints,
network egress restrictions, containers, VM isolation, GPU dedication, actual
model quality, Azure costs, or a full HostedSkill/tool/delegation integration.

A subprocess with a temporary working directory **is not a sandbox**: it runs
under the parent's OS account and can access that account's files and network.
This fixture runs fixed trusted inference code, never arbitrary customer code.
The dispatchers are trusted multitenant processes holding mock customer
credentials and histories. Strict customer compute isolation must move that
dispatcher/state into tenant-specific trust boundaries too. All Fusion fixture
clients and sessions run in the same process, so POC 05 proves no OS-level
isolation. GitHub's published Fusion description does not establish a dedicated
customer model deployment, private inference endpoint, or exclusive hardware.

The original `run_poc.py` has no live mode. Its HTTP workers still require
loopback endpoints and its Fusion experiment still uses in-memory SDK mocks.
Real-provider code is separate under `live_pocs`; deployment and paid validation
require explicit configuration, guarded commands, and approval. See
[POC 06](06-shared-foundry-apim/README.md) for that opt-in path and its limits.
