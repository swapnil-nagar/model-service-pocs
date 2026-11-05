param(
    [Parameter(Mandatory)][ValidateSet('A', 'B')][string]$Customer,
    [Parameter(Mandatory)][string]$GatewayEndpoint,
    [int]$Port = 7071,
    [string]$Python = "$PSScriptRoot\.venv\Scripts\python.exe",
    [ValidateSet('inventory', 'basic-chat', 'daily-azure-report')][string]$Sample = 'inventory'
)
$ErrorActionPreference = 'Stop'
$endpoint = [uri]$GatewayEndpoint
if (-not $endpoint.IsAbsoluteUri -or $endpoint.Scheme -ne 'https' -or
    $endpoint.AbsolutePath.TrimEnd('/') -ne '/llm' -or
    $endpoint.Query -or $endpoint.Fragment -or $endpoint.UserInfo) {
    throw 'GatewayEndpoint must be an HTTPS APIM URL ending in /llm, without credentials, query, or fragment.'
}
$key = [Environment]::GetEnvironmentVariable("POC09_APIM_KEY_$Customer")
if ([string]::IsNullOrWhiteSpace($key)) {
    throw "Set POC09_APIM_KEY_$Customer from the approved APIM subscription before starting."
}
$Python = (Get-Item -LiteralPath $Python).FullName
if (Test-Path Env:\PYTHONPATH) { Remove-Item Env:\PYTHONPATH }
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
foreach ($name in @('AZURE_OPENAI_BASE_URL', 'OPENAI_BASE_URL', 'APPLICATIONINSIGHTS_CONNECTION_STRING', 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', 'AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER', 'AZURE_FUNCTIONS_AGENTS_APP_ROOT', 'AZURE_FUNCTIONS_AGENTS_SESSION_DIR')) {
    if (Test-Path "Env:\$name") { Remove-Item -LiteralPath "Env:\$name" }
}
$appDirectory = "$PSScriptRoot\agent-app"
if ($Sample -in @('basic-chat', 'daily-azure-report')) {
    & $Python -c "import socket; connections = [socket.create_connection(('127.0.0.1', port), timeout=1) for port in (10000, 10001, 10002)]; [connection.close() for connection in connections]"
    if ($LASTEXITCODE -ne 0) { throw "Start local Azurite on ports 10000-10002 before launching native $Sample." }
    $runtimeRepo = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'azure-functions-agents-runtime'
    $raw = & $Python "$PSScriptRoot\native_samples.py" --sample $Sample --runtime-repo $runtimeRepo --profile-root "$PSScriptRoot\.$Sample-profile\$Customer"
    if ($LASTEXITCODE -ne 0) { throw "Native $Sample profile preparation failed." }
    $profile = ($raw -join "`n") | ConvertFrom-Json
    $appDirectory = $profile.script_root
    $env:AZURE_FUNCTIONS_AGENTS_APP_ROOT = $profile.app_root
    $env:AZURE_FUNCTIONS_AGENTS_SESSION_DIR = "$PSScriptRoot\.$Sample-state\$Customer"
    $env:AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER = "poc09-$Sample-$($Customer.ToLowerInvariant())"
    $env:AzureWebJobsStorage = 'UseDevelopmentStorage=true'
    if ($Sample -eq 'daily-azure-report') {
        foreach ($name in @('ACA_SESSION_POOL_ENDPOINT', 'O365_MCP_SERVER_URL', 'O365_MCP_CLIENT_ID', 'TO_EMAIL')) {
            if (Test-Path "Env:\$name") { Remove-Item -LiteralPath "Env:\$name" }
        }
        $env:SUBSCRIPTION_ID = '00000000-0000-0000-0000-000000000009'
        [Environment]::SetEnvironmentVariable('AzureWebJobs.daily_azure_report.Disabled', 'true', 'Process')
        Write-Output 'SYNTHETIC inventory only. Email unavailable; automatic daily_azure_report timer disabled.'
    }
    Write-Output "Running original $Sample entrypoint: $appDirectory\function_app.py"
    Write-Output "Entrypoint SHA256: $($profile.entrypoint_sha256)"
    Write-Output "Agent SHA256: $($profile.agent_sha256)"
}
Push-Location $appDirectory
try {
    & func start --address 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Functions host failed.' }
} finally {
    Pop-Location
}
