param keyVaultName string
param acrName string
param foundryAccountName string
param deployerObjectId string
param foundryRunnerPrincipalId string
param apimPrincipalId string

var keyVaultSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var cognitiveServicesOpenAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource keyVault 'Microsoft.KeyVault/vaults@2026-05-15' existing = {
  name: keyVaultName
}

resource runnerFoundryAuth 'Microsoft.KeyVault/vaults/secrets@2026-05-15' existing = {
  parent: keyVault
  name: 'runner-foundry-auth'
}

resource gatewayCustomerA 'Microsoft.KeyVault/vaults/secrets@2026-05-15' existing = {
  parent: keyVault
  name: 'gateway-customer-a'
}

resource gatewayCustomerB 'Microsoft.KeyVault/vaults/secrets@2026-05-15' existing = {
  parent: keyVault
  name: 'gateway-customer-b'
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: acrName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-09-01' existing = {
  name: foundryAccountName
}

resource deployerSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, deployerObjectId, keyVaultSecretsOfficerRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsOfficerRoleId)
    principalId: deployerObjectId
    principalType: 'User'
  }
}

resource foundryRunnerAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, foundryRunnerPrincipalId, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: foundryRunnerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryRunnerBackendSecretUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(runnerFoundryAuth.id, foundryRunnerPrincipalId, keyVaultSecretsUserRoleId)
  scope: runnerFoundryAuth
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: foundryRunnerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryRunnerCustomerASecretUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(gatewayCustomerA.id, foundryRunnerPrincipalId, keyVaultSecretsUserRoleId)
  scope: gatewayCustomerA
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: foundryRunnerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource foundryRunnerCustomerBSecretUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(gatewayCustomerB.id, foundryRunnerPrincipalId, keyVaultSecretsUserRoleId)
  scope: gatewayCustomerB
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: foundryRunnerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource apimFoundryBackendSecretUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(runnerFoundryAuth.id, apimPrincipalId, keyVaultSecretsUserRoleId)
  scope: runnerFoundryAuth
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource apimFoundryOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, apimPrincipalId, cognitiveServicesOpenAiUserRoleId)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAiUserRoleId
    )
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
  }
}
