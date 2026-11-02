# POC 06: live shared Foundry behind APIM

This opt-in extension uses **one model deployment shared by two synthetic
tenants**, a real API Management gateway, and one Azure-hosted Python runner.
The original five mock experiments and their default commands are unchanged.

**Current checkpoint: bounded live acceptance passed on 2026-10-30.** The real
image, workload and managed-identity roles are deployed. Four successful turns
across two synthetic tenants passed all ten acceptance checks. See
[live-results.json](../live-results.json) and the [report](../REPORT.md) for
measurements, failures encountered and remaining limits. This is a functional
pilot, not production HostedSkill or provider-cache isolation certification.

## Architecture

```text
Synthetic tenant credential
  -> APIM authenticates its subscription and derives the tenant
  -> protected Foundry runner submits a short asynchronous run
  -> runner loads only that tenant/agent/session history
  -> APIM model API fixes the allowed deployment and uses managed identity
  -> one shared Foundry model deployment
  -> owning run result, retrieved through authenticated status polling
```

Consumption APIM has a 30-second total request limit. The runner therefore
returns a `202` run identifier rather than holding a gateway request open for
agent execution. Submission, status and cancellation retain tenant/session/run
ownership. This prototype does not implement a production durable queue.

## Boundaries

- Each tenant has a different APIM credential. The gateway rejects conflicting
  tenant headers and overrides the trusted backend headers.
- Direct runner calls without its backend secret fail before inference.
- Caller JSON cannot choose a tenant, provider URL, credential or arbitrary model.
- `chat` is a logical alias. Both tenants use the same fixed deployment.
- The model backend uses gateway managed identity, not a model API key.
- The model response carries the gateway's server-derived tenant header; the
  transport rejects a missing or mismatched header before accepting content.
- Histories are separate by tenant, agent, session and alias.
- A new conversation requires `new_session: true`; unknown resumed sessions
  fail instead of silently starting over.
- Failed, timed-out or cancelled sessions are quarantined. No automatic retry,
  model fallback, input correction, or production edits are performed.
- Gateway request limits are **not LLM token quotas**: Consumption does not
  support `llm-token-limit`, `rate-limit-by-key`, or `quota-by-key`.
- Basic per-subscription quotas are attached to the pilot product: 720 model
  calls and 7,200 runner requests per hour. API rate limits remain 12 and 120
  calls per minute, respectively. These are request limits, not spend caps.
- Application/semantic response caching is disabled. Provider prompt/KV cache
  partitioning is not established by this prototype.

The runner has one replica maximum and temporary in-memory state. Restart or
scale-to-zero loses sessions and pending runs. These are synthetic API-key
tenants, not a completed Entra multi-tenant identity integration. This is a
standalone model-service POC, not an already-wired HostedSkill runtime feature.

## Local validation

The original mocks need only Python 3.13+. The opt-in hosted code additionally
uses the versions/hashes in [requirements-live.lock](../requirements-live.lock).

```powershell
# From the repository root; use your approved package-index configuration.
uv venv .venv --python 3.13
uv pip sync --python .\.venv\Scripts\python.exe --require-hashes .\requirements-live.lock
.\.venv\Scripts\python.exe -B -m unittest discover -s live_tests -v
```

No live credentials or external model calls are needed for these tests.
The portable lock contains no internal index URLs. Do not disable TLS
verification to work around a package-network problem.

The latest combined local run passed **86 tests**: 59 hosted/adapter/admin tests
and 27 original mock tests. All 20 locked packages have matching public PyPI
artifact hashes, including the CPython 3.13 Linux x64 `pydantic-core` wheel.
The lock's 157 unsupported MD5 options were removed without changing any pin or
SHA-256 hash. Stock pip and the actual Linux image build now accept it. Public
hash matches alone had not proved installer compatibility or paid inference.

## Controlled deployment

Infrastructure is fixed and parameterized under [infra/live](../infra/live).
The public parameter file omits personal identities, contact information and
secure values. An ignored deployment session holds the approved subscription,
names, region and cost assumptions.

After the separate provisioning approval, the Windows administration helper:

1. Verifies the exact subscription, tenant, operator and resource-group boundary.
2. Registers only required providers and rejects deletion in both ARM previews.
3. Creates credentials once and retains an encrypted Windows-DPAPI checkpoint.
4. Supplies plaintext parameters only through a short-lived directory outside
   the repository and image build context.
5. Deploys the foundation, builds through ACR, then replaces the placeholder
   with the real image and managed secret references.
6. Requires the real runner's `/healthz` JSON and actual built image.

Both administration commands retain redacted CLI start/outcome records in the
ignored session's audit log. Neither command records secret lookup responses.

```powershell
.\.venv\Scripts\python.exe -B -m scripts.deploy_live_pocs `
  --plan "<ignored-session>\prepare-plan.json" `
  --subscription "<approved-subscription-id>" `
  --approve-resource-provisioning
```

Use `--resume` only for this owned, incomplete deployment. The checkpoint must
be readable by the same Windows user. It is not a public artifact and must
never be committed. A completed deployment is not automatically re-bootstrapped.

For an explicitly approved gateway/IaC repair using the **same running image**:

```powershell
.\.venv\Scripts\python.exe -B -m scripts.deploy_live_pocs `
  --plan "<ignored-session>\prepare-plan.json" `
  --subscription "<approved-subscription-id>" `
  --approve-resource-provisioning --resume --reuse-image
```

This verifies the owned scope, current image and prior successful workload
parameters, then previews/reapplies the workload without another build or
placeholder bootstrap. It does not publish Python source changes; a new code
image still requires a reviewed image-update plan.

## Bounded paid acceptance

After a real deployment, the driver verifies that gateway and vault belong to
the recorded scope **before retrieving or sending keys**. Four positive model
turns exercise two tenants using the same public session name. Negative checks
cover missing/invalid keys, spoofed tenant headers, an unapproved alias and
cross-tenant run reads.

The driver first verifies the owned runner image/FQDN and warms its
configuration-only health endpoint. Scale-to-zero cold starts exceeded the
gateway/client deadline during testing. Warmup avoids conflating that pilot
startup condition with model acceptance; it does not solve production cold
starts or preserve history across restarts. Safe stage/turn counters are saved
in the ignored session so pre-inference failures are distinguishable from
potentially billed attempts. Model requests are never automatically retried.

```powershell
.\.venv\Scripts\python.exe -B .\run_live_poc.py `
  --deployment-file "<ignored-session>\live-deployment.json" `
  --subscription "<approved-subscription-id>" `
  --allow-paid-model-calls `
  --output .\live-results.json
```

The result excludes credentials, resource IDs, prompts and response content.
An unsuccessful attempt overwrites stale success with an explicit failure.
Canary recall is an observation, not proof of provider cache isolation.
`/healthz` is configuration-only and never invokes the model.

The accepted scenario observed 340 input and 32 output tokens, no cached input
tokens, and runner execution times from 540.81 to 2,095.54 ms. Six provider
requests were reconciled overall, including two earlier unaccepted attempts;
the total observed input/output counts were 492/34. Raw prompts, response text
and credentials are not published.

## Cost and cleanup

The revised small-pilot estimate is about **USD 5.62/month before free grants,
plus Foundry tokens**, under the private plan's usage assumptions. This is not a
spend cap. ACR accounts for about USD 5/month while retained; extra usage,
builds, transfer and telemetry can increase cost.

No Fusion app, identity, API, GitHub-token upload or paid GitHub test is included:
the [Fusion live preflight is blocked](../05-github-fusion/README.md#live-sdk-preflight).

Cleanup is deliberate, not automatic. Review the exact test resource group
before deleting it; never target existing portal groups:

```powershell
az group delete --subscription "<approved-subscription-id>" `
  --name "<this-poc-resource-group>" --yes
```
