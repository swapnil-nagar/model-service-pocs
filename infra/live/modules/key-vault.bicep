param keyVaultName string
param location string
param tags object

@secure()
param foundryRunnerBackendKey string

@secure()
param tenantAPrimaryKey string

@secure()
param tenantBPrimaryKey string

resource keyVault 'Microsoft.KeyVault/vaults@2026-05-15' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource runnerFoundryAuth 'Microsoft.KeyVault/vaults/secrets@2026-05-15' = {
  parent: keyVault
  name: 'runner-foundry-auth'
  properties: {
    value: foundryRunnerBackendKey
  }
}

resource gatewayCustomerA 'Microsoft.KeyVault/vaults/secrets@2026-05-15' = {
  parent: keyVault
  name: 'gateway-customer-a'
  properties: {
    value: tenantAPrimaryKey
  }
}

resource gatewayCustomerB 'Microsoft.KeyVault/vaults/secrets@2026-05-15' = {
  parent: keyVault
  name: 'gateway-customer-b'
  properties: {
    value: tenantBPrimaryKey
  }
}

output id string = keyVault.id
output name string = keyVault.name
output vaultUri string = keyVault.properties.vaultUri
output runnerFoundryAuthId string = runnerFoundryAuth.id
output gatewayCustomerAId string = gatewayCustomerA.id
output gatewayCustomerBId string = gatewayCustomerB.id
