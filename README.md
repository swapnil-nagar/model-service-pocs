# Hosted Skills model-service POCs

Five runnable, **local-only architecture experiments** for customer-isolated LLM
calls: four HTTP/process experiments and one conditional GitHub Copilot Fusion
SDK-boundary experiment. Nothing provisions cloud resources or sends prompts to
a real model. This repository is standalone; the
[Azure Functions Agents runtime](https://github.com/Azure/azure-functions-agents-runtime)
is an external reference, not a dependency or bundled checkout.

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

The final report records **27 passing POC test methods** and **95 passing
runtime-source test cases**. The latter were run separately from the external
runtime checkout with its locked dependencies; they are not bundled tests or
an additional dependency for running these POCs. Approvals, portal validation,
ownership handoff, and GA decisions remain explicitly unclosed where evidence
is unavailable.

## Run on Windows

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

No live mode is implemented. HTTP dispatchers/workers require generated loopback
endpoints; the Fusion experiment has no networking or live SDK implementation.
