targetScope = 'subscription'

@description('Logical environment name used in the AppOnboard tags.')
@minLength(1)
@maxLength(64)
param environmentName string

@description('Azure region for the isolated POC resource group.')
@minLength(1)
param location string

@description('AppOnboard session identifier.')
param sessionId string

@description('Display name or deployment principal label used in the tags.')
param deployedBy string

@description('ISO 8601 creation timestamp used in the tags.')
param createdAt string

@description('Object ID of the principal that seeds Key Vault secrets.')
param deployerObjectId string

@description('Resource group name from the approved naming plan.')
param resourceGroupName string

@description('Microsoft Foundry account name from the approved naming plan.')
param foundryAccountName string

@description('Foundry model deployment name from the approved naming plan.')
param modelDeploymentName string

@description('API Management service name from the approved naming plan.')
param apimName string

@description('Container Apps managed environment name from the approved naming plan.')
param containerEnvironmentName string

@description('Foundry runner Container App name from the approved naming plan.')
param foundryRunnerName string

@description('Azure Container Registry name from the approved naming plan.')
param acrName string

@description('Key Vault name from the approved naming plan.')
param keyVaultName string

@description('Log Analytics workspace name from the approved naming plan.')
param logAnalyticsName string

@description('Application Insights component name from the approved naming plan.')
param appInsightsName string

@description('Foundry runner user-assigned identity name from the approved naming plan.')
param foundryIdentityName string

@description('Stable APIM subscription ID for synthetic customer A.')
param tenantASubscriptionId string

@description('Stable APIM subscription ID for synthetic customer B.')
param tenantBSubscriptionId string

@description('APIM publisher email supplied for the isolated POC.')
param publisherEmail string

@description('APIM publisher display name.')
param publisherName string

@description('Image reference. The placeholder is used for Phase 1.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('When true, defer ACR/Key Vault references and gateway configuration until bootstrap role grants exist.')
param isPlaceholder bool = true

@description('Pinned Foundry model name from the approved plan.')
param modelName string = 'gpt-4.1-mini'

@description('Pinned Foundry model version from the approved plan.')
param modelVersion string = '2025-04-14'

@description('Global Standard deployment capacity from the approved plan.')
param modelCapacity int = 3

@secure()
@description('Backend authentication key for the Foundry runner.')
param foundryRunnerBackendKey string

@secure()
@description('Primary APIM subscription key for synthetic customer A.')
param tenantAPrimaryKey string

@secure()
@description('Secondary APIM subscription key for synthetic customer A.')
param tenantASecondaryKey string

@secure()
@description('Primary APIM subscription key for synthetic customer B.')
param tenantBPrimaryKey string

@secure()
@description('Secondary APIM subscription key for synthetic customer B.')
param tenantBSecondaryKey string

var tags = {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': sessionId
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
}

var gatewayUrl = 'https://${apimName}.azure-api.net'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module logAnalytics './modules/log-analytics.bicep' = {
  name: 'logAnalytics'
  scope: resourceGroup
  params: {
    workspaceName: logAnalyticsName
    location: location
    tags: tags
  }
}

module appInsights './modules/app-insights.bicep' = {
  name: 'appInsights'
  scope: resourceGroup
  params: {
    appInsightsName: appInsightsName
    location: location
    workspaceResourceId: logAnalytics.outputs.id
    tags: tags
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'keyVault'
  scope: resourceGroup
  params: {
    keyVaultName: keyVaultName
    location: location
    tags: tags
    foundryRunnerBackendKey: foundryRunnerBackendKey
    tenantAPrimaryKey: tenantAPrimaryKey
    tenantBPrimaryKey: tenantBPrimaryKey
  }
}

module foundryIdentity './modules/foundry-identity.bicep' = {
  name: 'foundryIdentity'
  scope: resourceGroup
  params: {
    identityName: foundryIdentityName
    location: location
    tags: tags
  }
}

module acr './modules/container-registry.bicep' = {
  name: 'containerRegistry'
  scope: resourceGroup
  params: {
    registryName: acrName
    location: location
    tags: tags
  }
}

module foundry './modules/foundry.bicep' = {
  name: 'foundry'
  scope: resourceGroup
  params: {
    accountName: foundryAccountName
    deploymentName: modelDeploymentName
    location: location
    tags: tags
    modelName: modelName
    modelVersion: modelVersion
    modelCapacity: modelCapacity
  }
}

module containerEnvironment './modules/container-environment.bicep' = {
  name: 'containerEnvironment'
  scope: resourceGroup
  params: {
    environmentName: containerEnvironmentName
    location: location
    tags: tags
    workspaceName: logAnalytics.outputs.name
    workspaceCustomerId: logAnalytics.outputs.customerId
  }
}

module foundryRunner './modules/container-app.bicep' = {
  name: 'foundryRunner'
  scope: resourceGroup
  params: {
    appName: foundryRunnerName
    location: location
    tags: tags
    environmentId: containerEnvironment.outputs.id
    containerImage: containerImage
    isPlaceholder: isPlaceholder
    acrLoginServer: acr.outputs.loginServer
    appIdentityResourceId: foundryIdentity.outputs.id
    keyVaultName: keyVault.outputs.name
    appInsightsConnectionString: appInsights.outputs.connectionString
    gatewayUrl: gatewayUrl
    modelDeploymentName: modelDeploymentName
  }
}

module apiManagement './modules/api-management.bicep' = {
  name: 'apiManagement'
  scope: resourceGroup
  params: {
    apimName: apimName
    location: location
    tags: tags
    publisherEmail: publisherEmail
    publisherName: publisherName
    foundryEndpoint: foundry.outputs.endpoint
    modelDeploymentName: modelDeploymentName
    foundryRunnerFqdn: foundryRunner.outputs.fqdn
    keyVaultName: keyVault.outputs.name
    appInsightsId: appInsights.outputs.id
    appInsightsInstrumentationKey: appInsights.outputs.instrumentationKey
    tenantASubscriptionId: tenantASubscriptionId
    tenantBSubscriptionId: tenantBSubscriptionId
    isPlaceholder: isPlaceholder
    tenantAPrimaryKey: tenantAPrimaryKey
    tenantASecondaryKey: tenantASecondaryKey
    tenantBPrimaryKey: tenantBPrimaryKey
    tenantBSecondaryKey: tenantBSecondaryKey
  }
}

module roleAssignments './modules/role-assignments.bicep' = {
  name: 'roleAssignments'
  scope: resourceGroup
  params: {
    keyVaultName: keyVault.outputs.name
    acrName: acr.outputs.name
    foundryAccountName: foundry.outputs.name
    deployerObjectId: deployerObjectId
    foundryRunnerPrincipalId: foundryIdentity.outputs.principalId
    apimPrincipalId: apiManagement.outputs.principalId
  }
}

output resourceGroupName string = resourceGroup.name
output keyVaultName string = keyVault.outputs.name
output containerRegistryLoginServer string = acr.outputs.loginServer
output apimGatewayUrl string = gatewayUrl
output foundryEndpoint string = foundry.outputs.endpoint
output foundryRunnerFqdn string = foundryRunner.outputs.fqdn
output modelDeploymentName string = modelDeploymentName
output foundryRunnerBackendSecretName string = 'runner-foundry-auth'
