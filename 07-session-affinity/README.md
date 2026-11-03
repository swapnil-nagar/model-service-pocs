# POC 07: durable 24-hour session-to-model affinity

**Implemented locally; not deployed to Azure.** This extension adds a customer
model service with multiple approved model endpoints, fixed 24-hour session
bindings, and an OpenAI Chat Completions interface usable by the pinned
Copilot SDK. It does not change or redeploy the existing POC 06 runner.

The Azure Table Storage adapter is implemented and tested against SDK-shaped
fakes. Real SDK client construction/cleanup was checked without Azure requests.
SQLite verifies persistence, cross-process reads, and concurrent binding
creation locally. A separate opt-in smoke test uses the real Copilot SDK/native
runtime, a localhost service, and a synthetic provider; it makes no paid calls.

## Contract

```text
Authenticated customer + agent + public session
  -> persistent registry lookup
     -> existing binding: use the same endpoint/model/version
     -> new binding: choose the alias's approved endpoint and insert atomically
  -> forward the SDK-packaged context to that pinned endpoint
```

- Scope is `(application_id, tenant_id, agent_id, session_id)`.
- Expiry is exactly **24 hours from creation**, not sliding idle expiry.
- A repeated create with the same model returns the existing binding and expiry.
- A different model on an existing session returns **409**.
- At expiry, create/read/inference return **410**. Use a new public session ID.
- Missing/foreign sessions return **404**; inference never creates a session.
- Endpoint removal/configuration drift returns **503**, not another model.
- Endpoint/model/version and the SDK session ID remain stable across restarts.
- Customer API keys authenticate a tenant; they are not user-level Entra auth.
- Provider keys are separate server-side credentials, resolved from environment
  references on each inference call. Their values are never stored in bindings.
- Provider response `model` must match the configured concrete reported model.
- Neither registry stores prompts, messages, responses, tool outputs or keys.
- No model retry, load-balancing reassignment, fallback, or semantic cache is
  introduced. Copilot SDK may independently retry; budget those attempts.

This guarantees **routing affinity**, not uninterrupted provider availability,
a reserved GPU, provider KV-cache retention, or automatic SDK-state durability.
Keep concrete deployments available and do not auto-upgrade them while active
bindings exist. Synchronize service clocks to UTC.

## API

All protected requests use `Authorization: Bearer <customer-api-key>`.
Credentials map to configured tenants; callers cannot select a tenant by header
or JSON. Authentication remains necessary even when the session URL is known.

| Route | Behavior |
| --- | --- |
| `GET /healthz` | Configuration only; does not probe a model or the table |
| `POST /sessions` | Idempotent create/resume of a model binding; 201 new / 200 existing |
| `GET /sessions/{agent_id}/{session_id}` | Owning, unexpired routing metadata |
| `GET /sessions/{agent_id}/{session_id}/v1/models` | The session's one allowed model alias |
| `POST /sessions/{agent_id}/{session_id}/v1/chat/completions` | JSON or SSE inference using the binding |

Example create body:

```json
{
  "session_id": "conversation-001",
  "agent_id": "assistant",
  "model_alias": "economy"
}
```

The response contains the same public identifiers, a server-generated
`sdk_session_id`, model provenance, `created_at`, `expires_at`, `provider_limits`,
and a session-specific `provider_base_url`. It contains no backend URL or key.

The provider URL carries the session routing reference in its path. This avoids
assuming that Copilot SDK automatically forwards its internal session ID as an
HTTP header. The API key still establishes which customer owns that reference.

## Configuration and startup

Start with [config.example.json](config.example.json). All URLs, resource names,
and environment names are placeholders, not provisioned resources.

The catalog is operator-controlled:

- `endpoints` defines concrete deployment names, versions, expected response
  model names, credential references, and token limits.
- `models` maps public aliases to endpoint IDs.
- `customers` grants aliases/agents and references distinct customer API keys.
- `public_base_url` is the trusted externally reachable root of this service,
  including any reverse-proxy prefix. It is not taken from a request Host header.
- Endpoint backends require HTTPS. The public service root permits HTTP only
  for localhost development.

Use the restored optional dependencies from
[requirements-live.lock](../requirements-live.lock). No installation is needed
for the original standard-library mocks.

```powershell
# Configure a private copy; never add real keys to the example/source.
$env:MODEL_ROUTING_CONFIG = "<absolute-path-to-private-config.json>"
$env:MODEL_ROUTING_LIVE_ENABLED = "false"

# Supply the catalog's customer keys from your approved secret mechanism.
# Set provider keys only when explicitly enabling real inference.

.\.venv\Scripts\python.exe -B -m uvicorn live_pocs.routing_app:app `
  --host 127.0.0.1 --port 8080 --no-access-log
```

Registration can write binding metadata even with inference disabled. It cannot
send a provider request until `MODEL_ROUTING_LIVE_ENABLED=true`.
Never treat a successful health/registration response as paid inference evidence.

For **local metadata-only use**, replace the example's `registry` section:

```json
{
  "backend": "sqlite",
  "sqlite_path": ".model-routing/bindings.sqlite3"
}
```

Also set `public_base_url` to the actual local root, for example
`http://127.0.0.1:8080`. The `.model-routing` directory is ignored by
[.gitignore](../.gitignore) and must be on persistent local disk if
used across restarts. SQLite is not a shared-disk/multi-replica cloud guarantee;
use Azure Table Storage for that scenario.

## Azure Table prerequisites

No Azure provisioning or role assignment is performed by this change.
Before hosting:

1. Provision an Azure Storage account and the configured table through the
   approved infrastructure workflow.
2. Grant the service identity **Storage Table Data Contributor**, scoped as
   narrowly as the deployment permits, and allow role propagation.
3. Use managed identity: `development_credential=false` (default). For a
   user-assigned identity, configure `managed_identity_client_id`.
4. Only for explicit local Azure development, set `development_credential=true`
   to use `DefaultAzureCredential`. There is no production fallback to it.
5. Supply customer and provider credentials through the hosted secret mechanism.
6. Deploy this routing entry point, configure its HTTPS ingress, and verify
   backend protocol, reported model names, limits, and access controls.

Table reads/writes use deterministic tenant/session keys. Creation uses
`create_entity`, never upsert: a conflicting creator reads the winning row.
A missing table, denied credential, uncertain write, corrupt record or
unresolved conflict fails explicitly. Nothing falls back to local memory.

Expired rows are retained as tombstones; there is no automatic deletion or
renewal. Deleting them externally permits an old public ID to be created again,
so any future cleanup policy must preserve the application's expired-ID contract.

The existing [POC 06 infrastructure](../infra/live/main.bicep) does not provision
this table, deploy this entry point, or expose these routes. The existing
[Dockerfile](../Dockerfile.live) includes the new modules/dependencies, but its
default command intentionally still starts the legacy runner. A separately
reviewed deployment must select `live_pocs.routing_app:app`.

## Copilot SDK configuration

Use BYOK/custom-provider mode with `wire_api="completions"`. This is independent
of GitHub-hosted Fusion entitlement and does not enable Fusion.

After authenticating to `POST /sessions`, configure the SDK from its response:

```python
from copilot import CopilotClient
from copilot.generated.rpc import PermissionDecisionReject
from copilot.session import ProviderConfig

provider = ProviderConfig(
    type="openai",
    base_url=binding["provider_base_url"],
    wire_api="completions",
    api_key=customer_api_key,
    max_prompt_tokens=binding["provider_limits"]["max_prompt_tokens"],
    max_output_tokens=binding["provider_limits"]["max_output_tokens"],
)

client = CopilotClient(
    mode="empty",
    use_logged_in_user=False,
    working_directory=approved_workspace,
    base_directory=tenant_private_persistent_sdk_directory,
)
await client.start()
session = await client.create_session(
    session_id=binding["sdk_session_id"],
    model=binding["model_alias"],
    provider=provider,
    on_permission_request=lambda request, invocation: PermissionDecisionReject(
        feedback="This example does not grant tool permissions."
    ),
    available_tools=[],
    enable_config_discovery=False,
    enable_skills=False,
    streaming=True,
)
```

The application still owns client/session cleanup, scoped SDK persistence,
resumption, tool grants and retry budgets. On resume, fetch the same unexpired
binding and supply its provider configuration again. Never create a fresh SDK
conversation silently when required persisted SDK state is missing.

The service forwards the SDK's complete messages, compaction summaries, tool
definitions/call IDs/results, stream options and usage. It substitutes only
the bound deployment, enforces `store=false`, and applies output limits. It
adds no history or agent instructions and executes no tools.

The configured prompt/output limits must be validated against each real model
so SDK compaction uses appropriate budgets. A separately configured SDK
compaction/utility model cannot bypass affinity: a different request model is
rejected. Actual compaction-trigger behavior remains a runtime qualification
item; the native smoke covers two ordinary context-packed turns, while proxy
tests explicitly cover compacted-summary/tool messages.

`max_prompt_tokens` is an SDK budget hint, not a server-side tokenizer quota.
The service enforces byte limits and supported output-token settings; a shared
input-token/financial budget remains future governance work.

## Supported scope and failures

- Initial wire protocol: **OpenAI-compatible Chat Completions**, text/function
  tool messages, JSON and SSE. Responses API and native Anthropic protocols are
  not implemented.
- Stored provider response/conversation references and hosted provider tools
  are rejected; they need separate tenant ownership/authorization contracts.
- Non-streaming provider errors are sanitized and retain appropriate HTTP
  status. No raw provider error payload or credential is returned.
- Streaming validates reported model before returning the first data event.
  Later errors produce an explicit SSE error without a successful `[DONE]`.
- Provider redirects are not followed. Each service call makes one attempt.
- Requests/responses are bounded; the configured timeout is also an absolute
  inference/stream deadline, not just a per-chunk socket timeout.
- A failed inference leaves the immutable route intact. Whether/how to retry
  or quarantine conversation context belongs to the calling SDK/application.
- There is no durable agent queue or transcript in this service.

APIM Consumption's existing 30-second ceiling and policy limitations still
apply to that pilot. Microsoft's customer SSE guidance requires a supported
classic/v2 tier, not Consumption; selecting/deploying that tier is separate
work. Disable response buffering on both APIM API surfaces and align deadlines.
Streaming does not remove gateway/application limits. Do not copy the existing
model-rewrite policy onto this service and overwrite its selected deployment.
See the [streaming contract](../CONTRACT.md#41-streaming-request-and-response-sse).

## Verification

The combined regression run passed **125 tests**, including the explicitly
enabled native SDK smoke (39 new routing tests plus the previous 86).
Focused type checking of all new source/test modules passed with zero errors.
The portable lock's 41 package hashes were checked against public artifacts,
and stock pip accepted a hash-verified Linux Python 3.13 binary-only dry run.
These are local results, not a 24-hour live Azure soak or provider SLA evidence.

```powershell
.\.venv\Scripts\python.exe -B -m unittest `
  live_tests.test_routing live_tests.test_routing_storage `
  live_tests.test_routing_app -v

# Optional: installed native Copilot runtime, localhost and synthetic provider.
$env:MODEL_ROUTING_NATIVE_SDK_TEST = "true"
.\.venv\Scripts\python.exe -B -m unittest live_tests.test_routing_sdk -v
```

Tests cover exact expiry, no sliding renewal/rebinding, concurrent creators,
different-process reads, Azure insert-conflict behavior, permission/storage
failures, endpoint drift, tenant ownership, context/tool preservation, model
response validation, SSE fragmentation/errors/deadlines, and explicit live guards.
The native smoke is opt-in so ordinary tests do not acquire a CLI runtime.
The pinned SDK currently emits native-stdio `ResourceWarning`s on Windows
shutdown; its client and the local HTTP server are stopped by the test.

## Future cost-aware router

The `models` mapping is currently explicit alias policy, not an AI classifier.
Changing an alias affects **new sessions only**. Existing bindings still resolve
their stored endpoint ID/fingerprint.

A future cost/quality selector can replace the admission-time alias lookup and
persist its chosen endpoint/policy provenance once. It must not reselect models
on each request inside the 24-hour window. A managed per-request router or
silent escalation would be a different customer contract.
