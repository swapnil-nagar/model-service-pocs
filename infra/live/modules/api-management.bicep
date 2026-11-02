param apimName string
param location string
param tags object
param publisherEmail string
param publisherName string
param foundryEndpoint string
param modelDeploymentName string
param foundryRunnerFqdn string
param keyVaultName string
param appInsightsId string
param appInsightsInstrumentationKey string
param tenantASubscriptionId string
param tenantBSubscriptionId string
param isPlaceholder bool

@secure()
param tenantAPrimaryKey string

@secure()
param tenantASecondaryKey string

@secure()
param tenantBPrimaryKey string

@secure()
param tenantBSecondaryKey string

#disable-next-line no-hardcoded-env-urls
var keyVaultBaseUrl = 'https://${keyVaultName}.vault.azure.net'
var customerATenant = 'customer-a'
var customerBTenant = 'customer-b'

resource apiManagement 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: 'Consumption'
    capacity: 0
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

resource foundryBackendNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = if (!isPlaceholder) {
  parent: apiManagement
  name: 'runner-foundry-auth'
  properties: {
    displayName: 'runner-foundry-auth'
    secret: true
    keyVault: {
      secretIdentifier: '${keyVaultBaseUrl}/secrets/runner-foundry-auth'
    }
  }
}

resource modelApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (!isPlaceholder) {
  parent: apiManagement
  name: 'model'
  properties: {
    displayName: 'Shared Foundry model'
    description: 'Fixed shared model route for the synthetic tenant pilot.'
    path: 'model'
    protocols: [
      'https'
    ]
    subscriptionRequired: true
  }
}

resource modelChatOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = if (!isPlaceholder) {
  parent: modelApi
  name: 'chat-completions'
  properties: {
    displayName: 'Chat completions'
    method: 'POST'
    urlTemplate: '/chat/completions'
    templateParameters: []
  }
}

resource modelPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = if (!isPlaceholder) {
  parent: modelApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(
      replace(
        replace(
          replace(
            replace(
              replace(
                '''
<policies>
  <inbound>
    <base />
    <rate-limit calls="12" renewal-period="60" />
    <choose>
      <when condition="@(context.Subscription == null || (context.Subscription.Id != &quot;__TENANT_A_SUBSCRIPTION_ID__&quot; &amp;&amp; context.Subscription.Id != &quot;__TENANT_B_SUBSCRIPTION_ID__&quot;))">
        <return-response>
          <set-status code="401" reason="Unauthorized" />
          <set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>
          <set-body>{"error":"A synthetic tenant subscription is required."}</set-body>
        </return-response>
      </when>
      <when condition="@(!string.IsNullOrEmpty(context.Request.Headers.GetValueOrDefault(&quot;x-poc-tenant-id&quot;, &quot;&quot;)) &amp;&amp; context.Request.Headers.GetValueOrDefault(&quot;x-poc-tenant-id&quot;, &quot;&quot;) != (context.Subscription.Id == &quot;__TENANT_A_SUBSCRIPTION_ID__&quot; ? &quot;__CUSTOMER_A_TENANT__&quot; : &quot;__CUSTOMER_B_TENANT__&quot;))">
        <return-response>
          <set-status code="403" reason="Forbidden" />
          <set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>
          <set-body>{"error":"The tenant header conflicts with the subscription identity."}</set-body>
        </return-response>
      </when>
    </choose>
    <set-variable name="pocTenant" value="@(context.Subscription.Id == &quot;__TENANT_A_SUBSCRIPTION_ID__&quot; ? &quot;__CUSTOMER_A_TENANT__&quot; : &quot;__CUSTOMER_B_TENANT__&quot;)" />
    <set-variable name="requestBody" value="@(context.Request.Body.As&lt;JObject&gt;(preserveContent: true))" />
    <choose>
      <when condition="@{
          var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
          return body == null
            || (string)body[&quot;model&quot;] != &quot;chat&quot;
            || body[&quot;messages&quot;] == null
            || (body[&quot;store&quot;] != null &amp;&amp; (bool)body[&quot;store&quot;])
            || (body[&quot;stream&quot;] != null &amp;&amp; (bool)body[&quot;stream&quot;])
            || (body[&quot;max_tokens&quot;] != null &amp;&amp; (int)body[&quot;max_tokens&quot;] &gt; 128)
            || (body[&quot;max_output_tokens&quot;] != null &amp;&amp; (int)body[&quot;max_output_tokens&quot;] &gt; 128);
        }">
        <return-response>
          <set-status code="400" reason="Bad Request" />
          <set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>
          <set-body>{"error":"Only model chat, messages, store=false, stream=false and output up to 128 are supported."}</set-body>
        </return-response>
      </when>
    </choose>
    <set-body>@{
        var body = context.Request.Body.As&lt;JObject&gt;(preserveContent: true);
        body[&quot;model&quot;] = &quot;__MODEL_DEPLOYMENT__&quot;;
        body[&quot;store&quot;] = false;
        if (body[&quot;max_tokens&quot;] == null) { body[&quot;max_tokens&quot;] = 128; }
        return body.ToString();
    }</set-body>
    <set-header name="Authorization" exists-action="delete" />
    <set-header name="Ocp-Apim-Subscription-Key" exists-action="delete" />
    <set-header name="x-poc-tenant-id" exists-action="override"><value>@((string)context.Variables["pocTenant"])</value></set-header>
    <set-backend-service base-url="__FOUNDRY_ENDPOINT__/openai/v1" />
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
    <set-header name="x-poc-tenant-id" exists-action="override"><value>@((string)context.Variables["pocTenant"])</value></set-header>
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
''',
                '__TENANT_A_SUBSCRIPTION_ID__',
                tenantASubscriptionId
              ),
              '__TENANT_B_SUBSCRIPTION_ID__',
              tenantBSubscriptionId
            ),
            '__CUSTOMER_A_TENANT__',
            customerATenant
          ),
          '__CUSTOMER_B_TENANT__',
          customerBTenant
        ),
        '__MODEL_DEPLOYMENT__',
        modelDeploymentName
      ),
      '__FOUNDRY_ENDPOINT__',
      foundryEndpoint
    )
  }
}

resource foundryApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (!isPlaceholder) {
  parent: apiManagement
  name: 'foundry'
  properties: {
    displayName: 'Foundry runner'
    description: 'Tenant-scoped asynchronous Foundry runner API.'
    path: 'foundry'
    protocols: [
      'https'
    ]
    subscriptionRequired: true
  }
}

resource foundryRunsOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = if (!isPlaceholder) {
  parent: foundryApi
  name: 'runs'
  properties: {
    displayName: 'Submit run'
    method: 'POST'
    urlTemplate: '/runs'
    templateParameters: []
  }
}

resource foundryRunStatusOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = if (!isPlaceholder) {
  parent: foundryApi
  name: 'run-status'
  properties: {
    displayName: 'Get run'
    method: 'GET'
    urlTemplate: '/runs/{run_id}'
    templateParameters: [
      {
        name: 'run_id'
        type: 'string'
        required: true
      }
    ]
  }
}

resource foundryRunDeleteOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = if (!isPlaceholder) {
  parent: foundryApi
  name: 'run-delete'
  properties: {
    displayName: 'Delete run'
    method: 'DELETE'
    urlTemplate: '/runs/{run_id}'
    templateParameters: [
      {
        name: 'run_id'
        type: 'string'
        required: true
      }
    ]
  }
}

resource foundryPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = if (!isPlaceholder) {
  parent: foundryApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(
      replace(
        replace(
          replace(
            replace(
              '''
<policies>
  <inbound>
    <base />
    <rate-limit calls="120" renewal-period="60" />
    <choose>
      <when condition="@(context.Subscription == null || (context.Subscription.Id != &quot;__TENANT_A_SUBSCRIPTION_ID__&quot; &amp;&amp; context.Subscription.Id != &quot;__TENANT_B_SUBSCRIPTION_ID__&quot;))">
        <return-response>
          <set-status code="401" reason="Unauthorized" />
          <set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>
          <set-body>{"error":"A synthetic tenant subscription is required."}</set-body>
        </return-response>
      </when>
      <when condition="@(!string.IsNullOrEmpty(context.Request.Headers.GetValueOrDefault(&quot;x-poc-tenant-id&quot;, &quot;&quot;)) &amp;&amp; context.Request.Headers.GetValueOrDefault(&quot;x-poc-tenant-id&quot;, &quot;&quot;) != (context.Subscription.Id == &quot;__TENANT_A_SUBSCRIPTION_ID__&quot; ? &quot;__CUSTOMER_A_TENANT__&quot; : &quot;__CUSTOMER_B_TENANT__&quot;))">
        <return-response>
          <set-status code="403" reason="Forbidden" />
          <set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>
          <set-body>{"error":"The tenant header conflicts with the subscription identity."}</set-body>
        </return-response>
      </when>
    </choose>
    <set-variable name="pocTenant" value="@(context.Subscription.Id == &quot;__TENANT_A_SUBSCRIPTION_ID__&quot; ? &quot;__CUSTOMER_A_TENANT__&quot; : &quot;__CUSTOMER_B_TENANT__&quot;)" />
    <set-header name="Authorization" exists-action="delete" />
    <set-header name="Ocp-Apim-Subscription-Key" exists-action="delete" />
    <set-header name="x-poc-tenant-id" exists-action="override"><value>@((string)context.Variables["pocTenant"])</value></set-header>
    <set-header name="x-poc-backend-key" exists-action="override"><value>{{runner-foundry-auth}}</value></set-header>
    <set-backend-service base-url="https://__FOUNDRY_RUNNER_FQDN__" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
''',
              '__TENANT_A_SUBSCRIPTION_ID__',
              tenantASubscriptionId
            ),
            '__TENANT_B_SUBSCRIPTION_ID__',
            tenantBSubscriptionId
          ),
          '__CUSTOMER_A_TENANT__',
          customerATenant
        ),
        '__CUSTOMER_B_TENANT__',
        customerBTenant
      ),
      '__FOUNDRY_RUNNER_FQDN__',
      foundryRunnerFqdn
    )
  }
  dependsOn: [
    foundryBackendNamedValue
  ]
}

resource pilotProduct 'Microsoft.ApiManagement/service/products@2024-05-01' = if (!isPlaceholder) {
  parent: apiManagement
  name: 'pilot'
  properties: {
    displayName: 'Synthetic tenant pilot'
    description: 'Shared model and runner APIs for the bounded synthetic-data POC.'
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}

resource pilotPolicy 'Microsoft.ApiManagement/service/products/policies@2024-05-01' = if (!isPlaceholder) {
  parent: pilotProduct
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: '''
<policies>
  <inbound>
    <base />
    <quota calls="7920" renewal-period="3600">
      <api id="model" calls="720" />
      <api id="foundry" calls="7200" />
    </quota>
  </inbound>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>
'''
  }
  dependsOn: [
    modelApi
    foundryApi
  ]
}

resource pilotModelApi 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = if (!isPlaceholder) {
  parent: pilotProduct
  name: modelApi.name
}

resource pilotFoundryApi 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = if (!isPlaceholder) {
  parent: pilotProduct
  name: foundryApi.name
}

resource customerASubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = if (!isPlaceholder) {
  parent: apiManagement
  name: tenantASubscriptionId
  properties: {
    displayName: 'Synthetic customer A'
    scope: '/products/pilot'
    state: 'active'
    allowTracing: false
    primaryKey: tenantAPrimaryKey
    secondaryKey: tenantASecondaryKey
  }
  dependsOn: [
    pilotProduct
  ]
}

resource customerBSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = if (!isPlaceholder) {
  parent: apiManagement
  name: tenantBSubscriptionId
  properties: {
    displayName: 'Synthetic customer B'
    scope: '/products/pilot'
    state: 'active'
    allowTracing: false
    primaryKey: tenantBPrimaryKey
    secondaryKey: tenantBSecondaryKey
  }
  dependsOn: [
    pilotProduct
  ]
}

resource appInsightsLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = if (!isPlaceholder) {
  parent: apiManagement
  name: 'applicationinsights'
  properties: {
    loggerType: 'applicationInsights'
    description: 'Metadata-only POC gateway telemetry.'
    credentials: {
      instrumentationKey: appInsightsInstrumentationKey
    }
    resourceId: appInsightsId
  }
}

resource appInsightsDiagnostic 'Microsoft.ApiManagement/service/diagnostics@2024-05-01' = if (!isPlaceholder) {
  parent: apiManagement
  name: 'applicationinsights'
  properties: {
    loggerId: appInsightsLogger.id
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    alwaysLog: 'allErrors'
    verbosity: 'error'
    httpCorrelationProtocol: 'W3C'
    frontend: {
      request: {
        headers: [
          'x-poc-correlation-id'
        ]
        body: {
          bytes: 0
        }
      }
      response: {
        headers: []
        body: {
          bytes: 0
        }
      }
    }
    backend: {
      request: {
        headers: []
        body: {
          bytes: 0
        }
      }
      response: {
        headers: []
        body: {
          bytes: 0
        }
      }
    }
  }
}

output id string = apiManagement.id
output name string = apiManagement.name
output principalId string = apiManagement.identity.principalId
output gatewayUrl string = 'https://${apimName}.azure-api.net'
