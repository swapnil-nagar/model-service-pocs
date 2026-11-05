import os
from pathlib import Path
from urllib.parse import urlsplit

from azure_functions_agents import create_function_app

endpoint = urlsplit(os.environ.get("AZURE_OPENAI_ENDPOINT", ""))
if endpoint.scheme != "https" or not endpoint.hostname or endpoint.path.rstrip("/") != "/llm":
    raise RuntimeError("AZURE_OPENAI_ENDPOINT must be the approved HTTPS APIM URL ending in /llm.")
if os.environ.get("AZURE_FUNCTIONS_AGENTS_PROVIDER") != "azure_openai":
    raise RuntimeError("This sample requires the azure_openai MAF provider.")
if os.environ.get("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "").lower() in {"true", "1"}:
    raise RuntimeError("This sample uses MAF, not the Copilot preview harness.")
if not os.environ.get("AZURE_OPENAI_API_KEY"):
    raise RuntimeError("An APIM subscription key is required; never use a Foundry provider key.")
for name in ("AZURE_OPENAI_BASE_URL", "OPENAI_BASE_URL"):
    if os.environ.get(name):
        raise RuntimeError("Remove base-URL overrides; this sample uses only AZURE_OPENAI_ENDPOINT.")
    # MAF considers a present-but-empty base URL to be configured.
    os.environ.pop(name, None)
if os.environ.get("POC09_CUSTOMER") not in {"A", "B"}:
    raise RuntimeError("POC09_CUSTOMER must be A or B, fixed by the operator for this process.")

app = create_function_app(app_root=Path(__file__).parent)
