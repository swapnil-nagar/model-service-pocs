# POC 05: GitHub Copilot Fusion (conditional)

**Service vendor:** GitHub. **Model publishers:** selected by GitHub from the
pool available to the authenticated identity, plan, and administrator policies.

**Verdict:** worth evaluating for automatic routing and cost/quality tradeoffs,
but **not a substitute for dedicated customer model/compute/network isolation**.
The user approved this as a conditional, local SDK-boundary POC with no GitHub
model calls.

## Run locally

From the `poc` directory:

```powershell
python -B .\run_poc.py --approach github-fusion
python -B -m unittest tests.test_fusion -v
```

The normal `--approach all --output .\local-results.json` command also includes
Fusion. Python 3.13+ and its standard library are enough. No SDK package, Copilot
CLI/runtime, sign-in, model download, or external network access is required.

The result reports `local_sdk_mock_only`, `status: conditional`, zero GitHub
model calls, and unverified effort-tier, internal orchestration, compute, and
network properties. Its in-memory timings are **not comparable** to the four
HTTP/process experiments or to real Fusion latency.

## What the real product provides

[GitHub's Fusion documentation](https://docs.github.com/en/copilot/concepts/models/copilot-fusion)
describes a router/orchestrator rather than a single model:

| Pattern | Product behavior |
| --- | --- |
| Single | One model handles the request |
| Cascade | A draft is judged, with escalation when needed |
| Critique | Independent critiques inform a revised final response |

Fusion Standard selects one model; High and Max can add orchestration when the
task warrants it. A tier is not a request to force one particular pattern.
The model pool and selected pattern can change across prompts. Fusion operates
separately from subagents; it does not automatically start them.

The CLI documents the selector `--model fusion` and
`--fusion-tier standard|high|max`. The SDK documentation says CLI models are
available through the SDK and exposes `list_models()` for discovery. The local
fixture therefore uses `fusion` as its selector and fails if that exact ID is
absent from its mock account catalog.

**Not yet established:** the exact identifier/effort-tier session configuration
supported by the runtime's pinned SDK version in an authenticated live session.
The inspected public Python `create_session` signature has no named
`fusion_tier` argument. This POC does not invent one, equate a Fusion tier with
generic `reasoning_effort`, or assume a CLI flag is an SDK session option.
Live discovery and a supported tier-selection path are pilot acceptance gates.

## Implemented boundary

```text
SaaS caller token -> shared request authorization -> customer/GitHub identity binding
    -> separate mock SDK client and model-list cache for that customer
    -> list_models(): Fusion must be present; no fallback to another model
    -> create_session(model="fusion", explicit customer GitHub token,
                      customer/agent/session/model-scoped ID, no tools)
    -> send_and_wait(customer prompt) -> validate nonempty final message
    -> successful turn, or explicit error and quarantined session
```

[fusion.py](../model_service/fusion.py) contains the experiment and intentionally
small in-memory mocks of `list_models`, `create_session`, `send_and_wait`,
`disconnect`, and `stop`. These are **not a complete SDK implementation or a
drop-in live adapter**. No GitHub REST endpoint is assumed or fabricated.
The fixture echoes scoped user-prompt markers; it does not run a model,
reimplement Fusion's router, or manufacture draft/judge/critic calls.

The fixture uses `mode="empty"`, disables stored-user fallback, supplies explicit
client/session credentials, and rejects tool access. Its tool-denial probe is
fixture-only, not an SDK method or a validated production permission handler.
Real SDK permissions and tool discovery must be independently tested.

The shared [authorization helper](../model_service/authorization.py) is also used
by POCs 01-04. This preserves the same tenant checks, agent/model allowlists,
session-ID validation, and 8,192-character prompt limit. Fusion sessions allow
ten successful turns and serialize same-session sends.

Failed, cancelled, or timed-out sends invalidate the fixture session. Reusing
that session ID returns `fusion_session_failed`; it does not silently reset
history, retry on another model, or switch credentials. Other customers remain
unaffected. Ending the experiment closes sessions and clients.

## Authentication and billing choices for a later pilot

Two documented identity patterns are relevant:

1. **User-attributed:** obtain an authorized GitHub OAuth/App user token and
   bind it to the correct SaaS customer. Validate Copilot entitlement and policy.
2. **Organization-attributed automation:** where enabled, install a GitHub App
   with the documented Copilot Requests organization permission and mint a
   short-lived installation token. GitHub documents direct organization billing
   and one-hour installation-token expiry. This does not require a signed-in
   user, but the organization must be enabled for this authentication path.

Keep application caller authentication separate from GitHub inference identity.
Do not silently use a developer's saved login, share a broadly privileged token
across customers, or assume one employee seat covers a customer-facing service.
Confirm licensing, organizational policy, and commercial terms with GitHub.
Token rotation and billing attribution are not implemented or validated here.

Fusion charges for the models it uses rather than adding a separate router fee.
High/Max can consume more credits because they can use multiple models.
No cost estimate is derived from these mocks.

## Existing Hosted Skills runtime integration

The existing Copilot preview already uses the SDK but constructs explicit
OpenAI/Azure OpenAI/Foundry **BYOK provider** configurations and disables
logged-in-user discovery. That does not make GitHub-hosted Fusion available by
changing the deployment/model environment variable to `fusion`.

A future integration needs an explicitly reviewed GitHub-authenticated Copilot
path, per-customer identity and storage, scoped model discovery, and the same
context on every tool/delegation/workflow continuation. The runtime repository
was not changed for this POC.

## Conditions before calling this cloud-validated

- Verify Fusion selection and effort control with the actual pinned SDK/runtime.
- Confirm each customer identity's plan, policies, allowed model pool, supported
  organization authentication, licensing, token refresh, and billing attribution.
- Validate real `mode="empty"` behavior, deny-by-default permission handling,
  filesystem/config discovery, MCP/skill isolation, and session storage paths.
- Run the **whole agent execution** in an approved restricted worker when OS/
  container isolation is required; this POC stays in one process.
- Establish contractual data processing/residency and internal-call telemetry
  requirements. Model availability policies are not dedicated network isolation.
- Validate cancellation/abort semantics and history recovery. Real SDK
  `disconnect()` preserves persisted session data; it is not deletion. Fusion
  drafts can also make workspace changes that a discarded draft does not undo.
- Reject Fusion for customers requiring a dedicated model deployment/private
  inference network unless GitHub supplies an applicable documented guarantee.

## Official sources

- [About GitHub Copilot Fusion](https://docs.github.com/en/copilot/concepts/models/copilot-fusion)
- [Choosing a model in Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/choose-a-model)
- [SDK overview and supported models](https://github.com/github/copilot-sdk/blob/main/README.md)
- [SDK multi-tenancy and server deployments](https://github.com/github/copilot-sdk/blob/main/docs/setup/multi-tenancy.md)
- [SDK authentication](https://github.com/github/copilot-sdk/blob/main/docs/auth/authenticate.md)
- [SDK server-to-server authentication](https://github.com/github/copilot-sdk/blob/main/docs/auth/server-to-server-tokens.md)
