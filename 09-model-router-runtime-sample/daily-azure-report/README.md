# Native daily Azure report through POC 09

Runs the runtime checkout's existing
`samples\daily-azure-report\src\function_app.py` using `create_function_app()`.
No runtime source changes, replacement agent loop, or new Azure resources.

## Architecture

```text
HTTP / disabled timer -> native Hosted Skills / MAF runtime
                            |-> azure_rest synthetic fixture (local execution)
                            |-> APIM /llm -> Foundry Model Router -> selected model
```

- Every inference, including tool-result continuation, uses `service-default`
  and the existing APIM subscription key. The key stays in the server process.
- The runtime owns tools and conversation history in local Azurite.
  APIM/Model Router remains stateless; no model/session pinning is introduced.
- The original three agent definitions retain their triggers and instructions,
  with an appended dry-run contract. Shared POC configuration sets a 256-token
  output limit, 120-second runtime setting, and no web-request tool.

## Safe scope

- **Synthetic inventory only:** three resources; one created and one updated
  in the last 24 hours. Counts and classifications are calculated in
  [the fixture tool](azure_rest.py), not by the model.
- The sample's **24-hour reporting lookback is preserved**. It is unrelated
  to the removed 24-hour model-session/pinning requirement.
- The POC tool replaces the original unrestricted ARM transport. It accepts
  only GET against one fixed synthetic subscription/path. It cannot access
  Azure, select another customer, perform writes, or use supplied credentials.
- Outlook and Learn MCP client configurations, original management tools/skills,
  and the code sandbox are not loaded. Email is **unavailable, not sent**.
  The original interactive agent's inbound MCP registration is retained but
  is not live-qualified by these tests.
- `AzureWebJobs.daily_azure_report.Disabled=true` prevents automatic runs,
  including overdue schedules. This is not authorization: an administrator
  can still manually invoke a disabled function.
- Missing tools and failed reads must be disclosed, never represented as
  successful inventory or delivery. Fresh profiles reject unexpected files,
  including inherited connector configurations.

This is not a live Azure inventory or email integration. Production needs a
subscription-scoped read-only tool/identity, explicit connector authorization,
server-side grounding/result validation, pagination, and separate qualification.
The native runtime uses automatic tool selection; instructions do not enforce
tool execution. The smoke checker independently rejects missing tool events,
invented counts, missing dry-run disclosures, and incomplete streams.
Large HTML reports are outside the current 256-token gateway contract.

## Run

From the parent POC sample directory, complete the
[installation and key-loading steps](../README.md#1-install-into-an-isolated-environment).
Start local Azurite as described in
[the native basic-chat instructions](../README.md#6-run-the-original-basic-chat-sample),
or reuse the already running emulator. Do not start a second copy on the same ports.

```powershell
.\Start-Agent.ps1 -Sample daily-azure-report -Customer A `
  -GatewayEndpoint "https://<apim-name>.azure-api.net/llm" -Port 7074
```

The launcher executes the original entrypoint, prints its hash, and creates
ignored `.daily-azure-report-profile\A` and `.daily-azure-report-state\A`
directories. History uses a separate `poc09-daily-azure-report-a` Blob container.
It refuses an original `local.settings.json` that could override the gateway.
Keep the host and emulator running while testing.

- UI: `http://localhost:7074/agents/main/`
- JSON chat: `POST /agents/main/chat`
- SSE chat: `POST /agents/main/chatstream`
- Both chat routes accept `{"prompt":"Summarize the synthetic inventory"}`.
- Structured JSON: `POST /resource-summary`, body:

```json
{"subscription_id":"00000000-0000-0000-0000-000000000009"}
```

Expected summary:

```json
{
  "total_resources": 3,
  "by_type": {"Microsoft.Web/sites": 2, "Microsoft.Storage/storageAccounts": 1},
  "by_location": {"eastus2": 2, "westus": 1}
}
```

The original `response_example` requests JSON formatting; it is not a
deterministic inventory validator. The smoke test compares exact fixture counts.
Requests for other subscriptions cannot access live data through this tool.
Endpoints are localhost-only, not production user/session authorization.

## Validation

No paid calls:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests\test_daily_azure_report.py -q
```

After approving paid inference, the live checker makes two runtime requests
(normally four model calls), paced 15 seconds apart. Additional model tool turns
or SDK retries can increase that number; this is not a hard spend cap.
It never invokes the timer or sends email.

```powershell
.\.venv\Scripts\python.exe -B .\smoke_daily_azure_report.py --approve-paid-calls `
  --output .\daily-azure-report-evidence.json
```

**Observed live:** original structured summary returned the exact fixture counts;
interactive SSE executed one matched `azure_rest` tool round trip, delivered
**24 text deltas**, disclosed synthetic data/no email, and ended with `done`.
The disabled timer was verified on the running host. Its native handler/tool
loop is tested locally with synthetic provider responses, not live-triggered.

The initial live summary invented counts without calling the tool and failed
the checker. Strengthened tool-first instructions passed the subsequent checks;
this is not proof that automatic tool selection is universally reliable.
Evidence records that initial failure separately. Streamed history can repeat
the same tool-call record; raw history row counts are not inference counts.
No live Azure inventory, email delivery, automatic schedule, production
authorization, or new gateway-performance claim is made.

See [native profile preparation](../native_samples.py),
[live checker](../smoke_daily_azure_report.py), and
[native-runtime tests](../tests/test_daily_azure_report.py).
