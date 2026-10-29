# POC 02: dedicated Microsoft Foundry model endpoint per customer

**Service vendor:** Microsoft Azure. **Model publisher:** selected from supported
Foundry models; availability, terms, region, and protocol support vary.

```powershell
# From the poc directory
python -B .\run_poc.py --approach foundry
```

## Local implementation

The Foundry adapter in [worker.py](../model_service/worker.py) posts to an
OpenAI v1-compatible `/openai/v1/chat/completions` endpoint with a bearer mock
credential and trusted `model` deployment name. Each customer has a separate
endpoint and independently enforced allowed model.

This tests a **model inference endpoint**, not Foundry Agent Service or a
Foundry project API. A project URL ending in `/api/projects/{project}` is not
interchangeable with a model endpoint. It also does not test the runtime's
`FoundryChatClient`, which uses project configuration and Azure credentials.

```text
Authenticated customer -> tenant-bound dispatcher -> fresh inference worker
    -> customer's Foundry resource + model deployment
```

## Azure setup, not executed

1. Select a model with a documented Chat Completions interface, approved
   publisher terms, region, and deployment type. Use the same OpenAI model as
   POC 01 for a transport comparison before separately comparing publishers.
2. Create a dedicated Foundry resource per customer; add a project and
   deployment as required. Use separate subscriptions for stricter boundaries.
   A project alone within a shared resource is not equivalent to this design.
3. Grant the customer's identity only the required scoped inference access.
   Replace the opaque mock bearer credential with supported Entra authentication.
4. Configure resource-specific private connectivity, DNS, storage, and egress.
5. Record the deployment identifier in the trusted registry. Deny arbitrary
   endpoint/model fields from prompts or request bodies.
6. Validate the selected model's exact auth, tool-calling, streaming, and schema
   behavior. Do not assume every catalog model supports this fixture's route.

## Fit and limits

This dedicated-endpoint fixture explores publisher/model flexibility; it does
not validate the current shared-deployment/cache objective or require a
deployment per customer. See the [concern audit](../REPORT.md#meeting-concern-coverage).
Foundry and Azure
OpenAI are overlapping Azure offerings, not unrelated vendors. Neither a
resource boundary nor a project boundary proves dedicated physical inference
hardware. See the [report](../REPORT.md) for runtime integration options.
