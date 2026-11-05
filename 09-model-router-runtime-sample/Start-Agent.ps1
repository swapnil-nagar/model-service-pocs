param(
    [Parameter(Mandatory)][ValidateSet('A', 'B')][string]$Customer,
    [Parameter(Mandatory)][string]$GatewayEndpoint,
    [int]$Port = 7071,
    [string]$Python = "$PSScriptRoot\.venv\Scripts\python.exe"
)
$ErrorActionPreference = 'Stop'
$key = [Environment]::GetEnvironmentVariable("POC09_APIM_KEY_$Customer")
if ([string]::IsNullOrWhiteSpace($key)) {
    throw "Set POC09_APIM_KEY_$Customer from the approved APIM subscription before starting."
}
$Python = (Get-Item -LiteralPath $Python).FullName
[Environment]::SetEnvironmentVariable('PYTHONPATH', $null, 'Process')
& $Python -c "import sys, azure_functions_agents, agent_framework.openai; from pathlib import Path; expected = Path(sys.argv[1]).parents[1] / 'azure-functions-agents-runtime'; actual = Path(azure_functions_agents.__file__).resolve(); sys.exit(0 if actual.is_relative_to(expected.resolve()) else 'The selected environment imports a different runtime checkout.')" $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'The selected Python environment cannot import the required runtime checkout.' }
$env:PATH = "$(Split-Path -Parent $Python);$env:PATH"
$environmentRoot = Split-Path -Parent (Split-Path -Parent $Python)
if (Test-Path "$environmentRoot\pyvenv.cfg") { $env:VIRTUAL_ENV = $environmentRoot }
$env:languageWorkers__python__defaultExecutablePath = $Python
$env:FUNCTIONS_WORKER_RUNTIME = 'python'
$env:AZURE_FUNCTIONS_AGENTS_PROVIDER = 'azure_openai'
$env:AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT = 'false'
$env:AZURE_FUNCTIONS_AGENTS_MODEL = 'service-default'
$env:AZURE_OPENAI_DEPLOYMENT = 'service-default'
$env:AZURE_OPENAI_ENDPOINT = $GatewayEndpoint.TrimEnd('/')
$env:AZURE_OPENAI_API_VERSION = '2024-10-21'
$env:AZURE_OPENAI_API_KEY = $key
$env:POC09_CUSTOMER = $Customer
$env:ENABLE_SENSITIVE_DATA = 'false'
$env:AzureWebJobsStorage = ''
$env:FUNCTIONS_WORKER_PROCESS_COUNT = '1'
foreach ($name in @('AZURE_OPENAI_BASE_URL', 'OPENAI_BASE_URL', 'APPLICATIONINSIGHTS_CONNECTION_STRING', 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', 'AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER')) {
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}
Push-Location "$PSScriptRoot\agent-app"
try {
    & func start --address 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Functions host failed.' }
} finally {
    Pop-Location
}
