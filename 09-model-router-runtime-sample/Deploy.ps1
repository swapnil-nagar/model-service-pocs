param(
    [Parameter(Mandatory)][string]$Subscription,
    [Parameter(Mandatory)][string]$PublisherEmail,
    [string]$ResourceGroup = 'rg-poc09-router-runtime',
    [switch]$ApproveProvisioning
)
$ErrorActionPreference = 'Stop'
if (-not $ApproveProvisioning) {
    throw 'Review the cost and resource plan in README.md and pass -ApproveProvisioning explicitly.'
}
function Invoke-AzJson([string[]]$Arguments) {
    $text = & az @Arguments --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI failed: $($Arguments[0]) $($Arguments[1])" }
    return (($text -join "`n") | ConvertFrom-Json)
}
$account = Invoke-AzJson @('account', 'show')
if ($account.id -ne $Subscription) { throw 'Select the explicitly approved subscription with az account set first.' }
$usage = Invoke-AzJson @('cognitiveservices', 'usage', 'list', '--location', 'eastus2', '--subscription', $Subscription, '--query', "[?name.value=='OpenAI.GlobalStandard.ModelRouter'] | [0]")
if ($null -eq $usage -or ($usage.limit - $usage.currentValue) -lt 10) {
    throw 'Model Router quota cannot be verified or has less than 10 units available.'
}
$exists = & az group exists --name $ResourceGroup --subscription $Subscription --output tsv
if ($LASTEXITCODE -ne 0) { throw 'Resource group existence check failed.' }
if ($exists -eq 'true') {
    $group = Invoke-AzJson @('group', 'show', '--name', $ResourceGroup, '--subscription', $Subscription)
    if ($group.tags.poc -ne '09-model-router-runtime') { throw 'Refusing to deploy into an unrelated resource group.' }
} else {
    $null = Invoke-AzJson @('group', 'create', '--name', $ResourceGroup, '--location', 'eastus2', '--subscription', $Subscription, '--tags', 'poc=09-model-router-runtime')
}
$deploymentArgs = @('--resource-group', $ResourceGroup, '--subscription', $Subscription, '--template-file', "$PSScriptRoot\infra\main.bicep", '--parameters', "publisherEmail=$PublisherEmail", '--mode', 'Incremental')
$preview = Invoke-AzJson (@('deployment', 'group', 'what-if', '--no-pretty-print') + $deploymentArgs)
if ($preview.changes | Where-Object { $_.changeType -eq 'Delete' }) {
    throw 'ARM preview includes deletion; refusing deployment.'
}
$deployment = Invoke-AzJson (@('deployment', 'group', 'create', '--name', 'poc09-runtime') + $deploymentArgs)
$deployment.properties.outputs | ConvertTo-Json -Depth 10
Write-Output 'No keys are included in these outputs. Load subscription keys using Load-Keys.ps1.'
