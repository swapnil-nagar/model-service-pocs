targetScope = 'resourceGroup'

param location string = 'eastus2'
param publisherEmail string
param publisherName string = 'Hosted Skills POC09'
@minValue(1)
@maxValue(10)
param routerCapacity int = 10

var suffix = uniqueString(subscription().id, resourceGroup().id)
var routerName = 'poc09-router'
var projectName = 'poc09-runtime'
var tags = { poc: '09-model-router-runtime', purpose: 'approved-inference-test' }

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ai-poc09-${suffix}'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'ai-poc09-${suffix}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: projectName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'POC09 model-only Responses endpoint'
    description: 'Model inference only; no hosted agent or capability host.'
  }
  dependsOn: [router]
}

resource router 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = {
  parent: foundry
  name: routerName
  sku: { name: 'GlobalStandard', capacity: routerCapacity }
  properties: {
    model: { format: 'OpenAI', name: 'model-router', version: '2025-11-18' }
    versionUpgradeOption: 'NoAutoUpgrade'
    routing: {
      mode: 'balanced'
      models: [
        { format: 'OpenAI', name: 'gpt-4.1', version: '2025-04-14' }
        { format: 'OpenAI', name: 'gpt-4.1-mini', version: '2025-04-14' }
      ]
    }
  }
}

resource gateway 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: 'apim-poc09-${suffix}'
  location: location
  tags: tags
  sku: { name: 'Developer', capacity: 1 }
  identity: { type: 'SystemAssigned' }
  properties: { publisherEmail: publisherEmail, publisherName: publisherName }
}

resource inferenceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, gateway.id, 'poc09-inference')
  scope: foundry
  properties: {
    principalId: gateway.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}

resource projectInferenceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, gateway.id, 'poc09-project-inference')
  scope: project
  properties: {
    principalId: gateway.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  }
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: gateway
  name: 'poc09-inference'
  properties: {
    displayName: 'POC09 stateless model inference'
    path: 'llm'
    protocols: ['https']
    subscriptionRequired: true
    subscriptionKeyParameterNames: { header: 'api-key', query: 'subscription-key' }
  }
}

resource sdkOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'sdk-chat'
  properties: {
    displayName: 'Azure OpenAI SDK streaming or JSON'
    method: 'POST'
    urlTemplate: '/openai/deployments/service-default/chat/completions'
  }
}

resource sharedOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'shared-chat'
  properties: {
    displayName: 'Shared Chat Completions contract'
    method: 'POST'
    urlTemplate: '/v1/chat/completions'
  }
}

resource responsesOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: api
  name: 'sdk-responses'
  properties: {
    displayName: 'MAF Responses API streaming or JSON'
    method: 'POST'
    urlTemplate: '/openai/v1/responses'
  }
}

resource product 'Microsoft.ApiManagement/service/products@2024-05-01' = {
  parent: gateway
  name: 'poc09-pilot'
  properties: {
    displayName: 'POC09 isolated pilot'
    subscriptionRequired: true
    approvalRequired: true
    state: 'published'
  }
}

resource productApi 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = {
  parent: product
  name: api.name
}

resource customerSubscriptions 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = [for customer in ['a', 'b']: {
  parent: gateway
  name: 'poc09-${customer}'
  properties: {
    displayName: 'POC09 synthetic customer ${customer}'
    scope: product.id
    state: 'active'
  }
}]

resource policy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: api
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(
      replace(
        replace(
          replace(loadTextContent('policy.xml'), '__FOUNDRY_HOST__', 'https://${foundry.name}.openai.azure.com'),
          '__ROUTER_DEPLOYMENT__', routerName
        ),
        '__PROJECT_HOST__', 'https://${foundry.name}.services.ai.azure.com'
      ),
      '__PROJECT_NAME__', projectName
    )
  }
  dependsOn: [sdkOperation, sharedOperation, responsesOperation, inferenceRole, projectInferenceRole]
}

output gatewayEndpoint string = '${gateway.properties.gatewayUrl}/llm'
output sharedChatEndpoint string = '${gateway.properties.gatewayUrl}/llm/v1/chat/completions'
output gatewayName string = gateway.name
output foundryName string = foundry.name
output routerDeploymentName string = router.name
output projectResourceName string = project.name
output projectEndpoint string = 'https://${foundry.name}.services.ai.azure.com/api/projects/${projectName}'
