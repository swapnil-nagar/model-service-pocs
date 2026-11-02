param appName string
param location string
param tags object
param environmentId string
param acrLoginServer string
param appIdentityResourceId string
param keyVaultName string
param appInsightsConnectionString string
param gatewayUrl string
param modelDeploymentName string

param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
param isPlaceholder bool = true
param appPort int = 8080

var effectivePort = isPlaceholder ? 80 : appPort

var plainEnv = [
  {
    name: 'PORT'
    value: string(effectivePort)
  }
  {
    name: 'POC_MODE'
    value: 'foundry'
  }
  {
    name: 'POC_ALLOWED_TENANTS'
    value: 'customer-a,customer-b'
  }
  {
    name: 'POC_LIVE_ENABLED'
    value: isPlaceholder ? 'false' : 'true'
  }
  {
    name: 'POC_MAX_SESSIONS'
    value: '20'
  }
  {
    name: 'POC_MAX_TURNS'
    value: '10'
  }
  {
    name: 'POC_MAX_PENDING_RUNS'
    value: '20'
  }
  {
    name: 'POC_RUN_TIMEOUT_SECONDS'
    value: '90'
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsightsConnectionString
  }
  {
    name: 'POC_FOUNDRY_GATEWAY_BASE_URL'
    value: '${gatewayUrl}/model'
  }
  {
    name: 'POC_MODEL_DEPLOYMENT'
    value: modelDeploymentName
  }
  {
    name: 'POC_FOUNDRY_MODEL_NAME'
    value: 'gpt-4.1-mini'
  }
]

var secretEnv = [
  {
    name: 'POC_BACKEND_KEY'
    secretRef: 'runner-foundry-auth'
  }
  {
    name: 'POC_FOUNDRY_TENANT_A_KEY'
    secretRef: 'gateway-customer-a'
  }
  {
    name: 'POC_FOUNDRY_TENANT_B_KEY'
    secretRef: 'gateway-customer-b'
  }
]

#disable-next-line no-hardcoded-env-urls
var keyVaultBaseUrl = 'https://${keyVaultName}.vault.azure.net'

var keyVaultSecrets = [
  {
    name: 'runner-foundry-auth'
    keyVaultUrl: '${keyVaultBaseUrl}/secrets/runner-foundry-auth'
    identity: appIdentityResourceId
  }
  {
    name: 'gateway-customer-a'
    keyVaultUrl: '${keyVaultBaseUrl}/secrets/gateway-customer-a'
    identity: appIdentityResourceId
  }
  {
    name: 'gateway-customer-b'
    keyVaultUrl: '${keyVaultBaseUrl}/secrets/gateway-customer-b'
    identity: appIdentityResourceId
  }
]

resource containerApp 'Microsoft.App/containerApps@2026-07-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: effectivePort
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: isPlaceholder
        ? []
        : [
            {
              server: acrLoginServer
              identity: appIdentityResourceId
            }
          ]
      secrets: isPlaceholder ? [] : keyVaultSecrets
    }
    template: {
      containers: [
        {
          name: 'foundry'
          image: containerImage
          env: concat(plainEnv, isPlaceholder ? [] : secretEnv)
          resources: {
            cpu: '1'
            memory: '2Gi'
          }
          probes: isPlaceholder
            ? []
            : [
                {
                  type: 'Liveness'
                  httpGet: {
                    path: '/healthz'
                    port: appPort
                    scheme: 'HTTP'
                  }
                  initialDelaySeconds: 5
                  periodSeconds: 10
                  failureThreshold: 3
                }
                {
                  type: 'Readiness'
                  httpGet: {
                    path: '/healthz'
                    port: appPort
                    scheme: 'HTTP'
                  }
                  initialDelaySeconds: 5
                  periodSeconds: 10
                  failureThreshold: 3
                }
              ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

output id string = containerApp.id
output name string = containerApp.name
output fqdn string = containerApp.properties.configuration.ingress.fqdn
