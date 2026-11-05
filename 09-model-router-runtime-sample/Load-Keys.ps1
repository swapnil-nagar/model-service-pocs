param(
    [Parameter(Mandatory)][string]$Subscription,
    [Parameter(Mandatory)][string]$GatewayName,
    [string]$ResourceGroup = 'rg-poc09-router-runtime'
)
$ErrorActionPreference = 'Stop'
$raw = & az apim show --name $GatewayName --resource-group $ResourceGroup --subscription $Subscription --query '{tags:tags,sku:sku.name}' --only-show-errors -o json
if ($LASTEXITCODE -ne 0) { throw 'Gateway scope verification failed.' }
$gateway = ($raw -join "`n") | ConvertFrom-Json
if ($gateway.tags.poc -ne '09-model-router-runtime' -or $gateway.sku -ne 'Developer') {
    throw 'Refusing to retrieve keys for an unrelated gateway or unsupported tier.'
}
foreach ($customer in @('A', 'B')) {
    $id = "poc09-$($customer.ToLowerInvariant())"
    $url = "https://management.azure.com/subscriptions/$Subscription/resourceGroups/$ResourceGroup/providers/Microsoft.ApiManagement/service/$GatewayName/subscriptions/$id/listSecrets?api-version=2024-05-01"
    $key = & az rest --method POST --url $url --query primaryKey --only-show-errors -o tsv
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($key)) { throw "Key retrieval failed for $id." }
    [Environment]::SetEnvironmentVariable("POC09_APIM_KEY_$customer", $key.Trim(), 'Process')
}
Write-Output 'Customer A/B subscription keys loaded into process environment; their values were not printed or saved.'
