# POC 03: Azure API Management AI gateway plus dedicated tenant backends

**Service vendor:** Microsoft Azure. **Model publisher:** determined by backend;
the runnable mock uses an Azure OpenAI-shaped backend.

```powershell
# From the poc directory
python -B .\run_poc.py --approach apim
```

## Local implementation

```text
Authenticated customer -> tenant-bound dispatcher -> fresh inference worker
    -> customer's gateway mock -> customer's separately credentialed model mock
```

The gateway receives `/models/chat/completions` requests. Its route/model and
credential are server-controlled. It strips the caller's gateway credential
by constructing a new backend request with a separate backend key. Two tenant
gateways and two tenant backends are started. This deliberately avoids claiming
that a shared gateway process is a dedicated customer compute boundary.

This is a Python routing/credential experiment, **not an APIM emulator**. It does
not execute APIM policy XML, validate Entra JWTs, enforce real token quotas, or
measure APIM latency. Backend HTTP errors are propagated, not retried on another
customer's backend. There is no response or semantic cache.

## Azure setup, not executed

1. Choose an APIM tier/region with required private networking and AI policies.
   Capability support varies by tier; consult the linked Microsoft guidance.
2. Validate Entra issuer, audience, signature, expiration, and application roles.
   Resolve the internal SaaS customer from authorized identity claims plus an
   onboarding mapping; a raw `tid` or caller-supplied tenant header is insufficient.
3. Map verified customer + allowed model alias to a fixed backend. Never select
   arbitrary backend URLs or identity IDs from caller-controlled fields.
4. Use least-privilege managed identity to the tenant's model resource. Prevent
   workers from reaching the backend except by the approved route.
5. Apply per-customer token/request quotas and redacted telemetry; leave caching
   disabled unless the design proves all cache keys and data boundaries.
6. If customers require dedicated gateway compute/network boundaries, provision
   dedicated gateway infrastructure too. A common APIM gateway with logical
   tenant policies is a lower-isolation alternative, not equivalent.

## Fit and limits

Best governance layer for many backends: quotas, policy, usage attribution, and
central authorization. **APIM does not replace model hosting or worker isolation.**
A shared managed identity with access to every backend increases blast radius.
See the [report](../REPORT.md) before choosing shared versus dedicated gateways.
