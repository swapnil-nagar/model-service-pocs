# POC 01: dedicated Azure OpenAI resource per customer

**Service vendor:** Microsoft Azure. **Model publisher:** OpenAI.

```powershell
# From the poc directory
python -B .\run_poc.py --approach azure-openai
```

## Local implementation

The Azure OpenAI adapter in [worker.py](../model_service/worker.py) posts to
`/openai/deployments/{trusted-deployment}/chat/completions?api-version=2024-10-21`.
Each customer has a separate HTTP server, deployment name, and mock `api-key`.
The fixed date is this experiment's versioned Chat Completions contract, not a
claim that every model or future Azure feature supports it. A cloud pilot must
select a currently supported API/model combination.

```text
Authenticated customer -> tenant-bound dispatcher -> fresh inference worker
    -> customer's Azure OpenAI resource + allowed deployment
```

## Azure setup, not executed

1. Select an available OpenAI chat model/version and approved processing region
   and deployment type. `gpt-4o-mini` is an illustrative baseline, not a
   provisioned or guaranteed-available model.
2. Create a dedicated Azure OpenAI resource and deployment per customer. Use
   separate subscriptions when quota/administrative boundaries require it.
3. Give only the customer's worker identity the required inference role on that
   resource. Prefer managed identity rather than copying this fixture's keys.
4. Configure private endpoints, private DNS, public-access restrictions, and
   worker egress rules. Validate those rules from another tenant's worker.
5. Keep customer history, tool credentials, storage, and telemetry segregated.
6. Deploy a tenant-specific Function App/worker so runtime provider environment
   values are fixed before startup rather than mutated per request.

## Fit and limits

Best first pilot for OpenAI-only customers: smallest runtime integration change.
Deployment/resource isolation does not establish physical GPU exclusivity.
PTU is a throughput/capacity choice, not proof of dedicated physical hardware.
See the [comparison and acceptance gates](../REPORT.md).
