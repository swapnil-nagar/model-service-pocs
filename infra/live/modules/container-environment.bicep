param environmentName string
param location string
param tags object
param workspaceName string
param workspaceCustomerId string

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2026-03-01' existing = {
  name: workspaceName
}

var workspaceSharedKey = logAnalyticsWorkspace.listKeys().primarySharedKey

resource environment 'Microsoft.App/managedEnvironments@2026-07-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspaceCustomerId
        sharedKey: workspaceSharedKey
      }
    }
  }
}

output id string = environment.id
output name string = environment.name
