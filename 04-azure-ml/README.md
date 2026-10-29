# POC 04: customer-dedicated Azure ML managed online serving

**Service vendor:** Microsoft Azure. **Model publisher:** customer-selected
weights, subject to license, serving implementation, and hardware support.

```powershell
# From the poc directory
python -B .\run_poc.py --approach azure-ml
```

## Local implementation

The adapter posts to `/score` with:

```json
{
  "input_data": {
    "messages": [{"role": "user", "content": "customer prompt"}]
  },
  "parameters": {"max_new_tokens": 128}
}
```

The mock returns `{"output": "..."}`. **This is our proposed custom scoring
container contract, not a universal Azure ML/MLflow API.** A real scoring
implementation must implement it, or the adapter must match that deployment's
documented inference schema. No model weights or scoring container are included.

```text
Authenticated customer -> tenant-bound dispatcher -> fresh inference worker
    -> customer's managed online endpoint + dedicated model deployment
```

## Azure setup, not executed

1. Select licensed model weights, serving image, and supported compute SKU; check
   regional GPU capacity and quota before promising an onboarding SLA.
2. Create a tenant-specific workspace/resource boundary as required, a managed
   online endpoint, and dedicated deployment compute. Size replicas and token
   capacity from a real load test, not this mock.
3. Package an approved serving/scoring implementation, pin its model and image
   versions, and validate this POC's scoring contract against it.
4. Use supported Entra authentication and separate endpoint/worker identities.
5. Disable public endpoint access as required, configure workspace private
   endpoints and managed-network approved outbound access, and validate DNS.
6. Segregate storage, registry access, keys, logs, patching, scaling, and rollback.

## Fit and limits

Best candidate when control of model weights and dedicated serving deployments
outweigh operational/GPU cost. Microsoft manages the endpoint platform, but the
platform team still owns model/image lifecycle and capacity planning.
Dedicated deployment compute is not automatically a contractual promise of
exclusive physical host hardware. See the [report](../REPORT.md).
