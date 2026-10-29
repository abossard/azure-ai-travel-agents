@description('Name of one comparison model. Deploy separately from the application.')
param healthModelName string

@description('A supported Microsoft.CloudHealth region, verified before deployment.')
param location string

@allowed([
  'platform'
  'stability'
  'capacity'
  'experience'
])
param profile string

@description('Actual deployed Microsoft.CognitiveServices/accounts resource name.')
param aiAccountName string

@description('Actual deployed Log Analytics workspace name.')
param logAnalyticsWorkspaceName string

@description('Actual deployed Application Insights resource name.')
param applicationInsightsName string

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource model 'Microsoft.CloudHealth/healthmodels@2026-11-01' = {
  name: healthModelName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, model.id, 'healthmodel-reader')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')
    principalId: model.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource workspaceReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, model.id, 'healthmodel-monitoring-reader')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '43d0d8ad-25c7-4714-9337-8ba259a9fe05')
    principalId: model.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource authentication 'Microsoft.CloudHealth/healthmodels/authenticationsettings@2026-11-01' = {
  parent: model
  name: 'system-assigned'
  properties: {
    authenticationKind: 'ManagedIdentity'
    managedIdentityName: 'SystemAssigned'
  }
  dependsOn: [
    reader
    workspaceReader
  ]
}

var containerNamespace = 'Microsoft.App/containerApps'
var aiNamespace = 'Microsoft.CognitiveServices/accounts'
var applicationInsightsId = resourceId('Microsoft.Insights/components', applicationInsightsName)
var metricDefinitions = concat([
  {
    name: 'replicas'
    displayName: 'Running replicas'
    namespace: containerNamespace
    metric: 'Replicas'
    aggregation: 'Average'
    unit: 'Count'
    operator: 'LessThan'
    degraded: 1
    unhealthy: json('0.5')
  }
  {
    name: 'ai-availability'
    displayName: 'AI request availability'
    namespace: aiNamespace
    metric: 'AzureOpenAIAvailabilityRate'
    aggregation: 'Average'
    unit: 'Percent'
    operator: 'LessThan'
    degraded: 99
    unhealthy: 95
  }
], profile == 'capacity' ? [
  {
    name: 'cpu'
    displayName: 'CPU usage'
    namespace: containerNamespace
    metric: 'CpuPercentage'
    aggregation: 'Average'
    unit: 'Percent'
    operator: 'GreaterThan'
    degraded: 80
    unhealthy: 95
  }
  {
    name: 'memory'
    displayName: 'Memory usage'
    namespace: containerNamespace
    metric: 'MemoryPercentage'
    aggregation: 'Average'
    unit: 'Percent'
    operator: 'GreaterThan'
    degraded: 80
    unhealthy: 95
  }
] : [], profile == 'experience' ? [
  {
    name: 'response-fast'
    displayName: 'UI or tool response time'
    namespace: containerNamespace
    metric: 'ResponseTime'
    aggregation: 'Average'
    unit: 'Milliseconds'
    operator: 'GreaterThan'
    degraded: 1000
    unhealthy: 3000
  }
  {
    name: 'response-chat'
    displayName: 'Assistant response time'
    namespace: containerNamespace
    metric: 'ResponseTime'
    aggregation: 'Average'
    unit: 'Milliseconds'
    operator: 'GreaterThan'
    degraded: 30000
    unhealthy: 60000
  }
  {
    name: 'ai-latency'
    displayName: 'AI time to last byte'
    namespace: aiNamespace
    metric: 'AzureOpenAITTLTInMS'
    aggregation: 'Average'
    unit: 'Milliseconds'
    operator: 'GreaterThan'
    degraded: 30000
    unhealthy: 60000
  }
] : [])

resource definitions 'Microsoft.CloudHealth/healthmodels/signaldefinitions@2026-11-01' = [
  for definition in metricDefinitions: {
    parent: model
    name: definition.name
    properties: {
      signalKind: 'AzureResourceMetric'
      displayName: definition.displayName
      metricNamespace: definition.namespace
      metricName: definition.metric
      aggregationType: definition.aggregation
      dataUnit: definition.unit
      timeGrain: 'PT5M'
      refreshInterval: 'PT1M'
      evaluationRules: {
        degradedRule: {
          operator: definition.operator
          threshold: definition.degraded
        }
        unhealthyRule: {
          operator: definition.operator
          threshold: definition.unhealthy
        }
      }
    }
  }
]

var journeyName = 'plan-trip'
var componentDefinitions = [
  { name: 'interface', displayName: 'Chat interface' }
  { name: 'assistant', displayName: 'Assistant coordination' }
  { name: 'preferences', displayName: 'Customer preferences' }
  { name: 'destinations', displayName: 'Destination recommendations' }
  { name: 'itinerary', displayName: 'Itinerary planning' }
  { name: 'generation', displayName: 'AI generation' }
]

var destinationDisplayClause = ' or Log_s contains \'Destination Recommendation\''
var destinationIdClause = ' or Log_s contains \'destination-recommendation\''

var bindings = [
  { name: 'interface', component: 'interface', resourceName: 'ui-angular', ai: false }
  { name: 'assistant-langchain', component: 'assistant', resourceName: 'api-langchain-js', ai: false }
  { name: 'assistant-llamaindex', component: 'assistant', resourceName: 'api-llamaindex-ts', ai: false }
  { name: 'assistant-python', component: 'assistant', resourceName: 'api-maf-python', ai: false }
  { name: 'preferences', component: 'preferences', resourceName: 'mcp-customer-query', ai: false }
  { name: 'destinations-mcp', component: 'destinations', resourceName: 'mcp-destination-recommendation', ai: false }
  { name: 'destinations-ai', component: 'destinations', resourceName: aiAccountName, ai: true }
  { name: 'itinerary', component: 'itinerary', resourceName: 'mcp-itinerary-planning', ai: false }
  { name: 'generation', component: 'generation', resourceName: aiAccountName, ai: true }
]

resource journey 'Microsoft.CloudHealth/healthmodels/entities@2026-11-01' = {
  parent: model
  name: journeyName
  properties: {
    displayName: 'Plan a trip'
    icon: {
      iconName: 'UserFlow'
    }
    healthObjective: json('99.9')
    alerts: {
      degraded: {
        severity: 'Sev2'
        description: 'Plan a trip has diminished functionality'
      }
      unhealthy: {
        severity: 'Sev1'
        description: 'Plan a trip is unavailable or unacceptably slow'
      }
    }
    signalGroups: {
      dependencies: {
        aggregationType: 'WorstOf'
      }
    }
  }
}

resource components 'Microsoft.CloudHealth/healthmodels/entities@2026-11-01' = [
  for component in componentDefinitions: {
    parent: model
    name: component.name
    properties: {
      displayName: component.displayName
      icon: {
        iconName: 'SystemComponent'
      }
      signalGroups: {
        // MaxNotHealthy may count Degraded alternatives; Unknown behavior is not yet verified.
        dependencies: component.name == 'assistant' || component.name == 'destinations' ? {
          aggregationType: 'MaxNotHealthy'
          unit: 'Absolute'
          degradedThreshold: 1
          unhealthyThreshold: component.name == 'assistant' ? 3 : 2
          ignoreUnknown: true
        } : {
          aggregationType: 'WorstOf'
        }
      }
    }
  }
]

resource resources 'Microsoft.CloudHealth/healthmodels/entities@2026-11-01' = [
  for binding in bindings: {
    parent: model
    name: '${binding.name}-resource'
    properties: {
      displayName: '${binding.ai ? 'AI Services' : 'Container App'}: ${binding.resourceName} (${binding.name})'
      signalGroups: union({
        azureResource: {
          authenticationSetting: authentication.name
          azureResourceId: resourceId(binding.ai ? aiNamespace : containerNamespace, binding.resourceName)
          signals: map(concat(
            [binding.ai ? 'ai-availability' : 'replicas'],
            !binding.ai && profile == 'capacity' ? ['cpu', 'memory'] : [],
            profile == 'experience' ? [binding.ai ? 'ai-latency' : (binding.component == 'assistant' ? 'response-chat' : 'response-fast')] : []
          ), signalName => {
            name: signalName
            signalKind: 'AzureResourceMetric'
            signalDefinitionName: signalName
          })
          resourceHealth: {
            enabled: binding.ai ? 'Enabled' : 'Disabled'
          }
        }
      }, binding.component == 'assistant' || (profile == 'stability' && !binding.ai) ? {
        azureLogAnalytics: {
          authenticationSetting: authentication.name
          logAnalyticsWorkspaceResourceId: workspace.id
          signals: concat(binding.component == 'assistant' ? [
            {
              name: 'logged-assistant-failures'
              signalKind: 'LogAnalyticsQuery'
              displayName: 'Logged assistant or required tool failures'
              queryText: 'ContainerAppConsoleLogs_CL | where TimeGenerated > ago(5m) | where ContainerAppName_s == \'${binding.resourceName}\' | where Stream_s == \'stderr\' | where Log_s contains \'Error during streaming\' or Log_s contains \'Error occurred:\' or Log_s contains \'Error in workflow execution\' or Log_s contains \'Error processing chat request\' or Log_s contains \'Error fetching MCP tools\' or Log_s contains \'Error initializing workflow\' or Log_s contains \'Service error in workflow\' or Log_s contains \'Service error in Magentic workflow\' or Log_s contains \'Error streaming workflow events\' or Log_s contains \'Error in Magentic workflow\' or Log_s contains \'ImportError:\' or Log_s contains \'ModuleNotFoundError:\' or (Log_s contains \'MCP server \' and (Log_s contains \'is not reachable\' or Log_s contains \'connection timed out\') and (Log_s contains \'Customer Query\' or Log_s contains \'Itinerary Planning\'${binding.resourceName == 'api-maf-python' ? '' : destinationDisplayClause})) or (Log_s contains \'Error creating MCP tools for \' and (Log_s contains \'customer-query\' or Log_s contains \'itinerary-planning\'${binding.resourceName == 'api-maf-python' ? '' : destinationIdClause})) | summarize failures=count()'
              valueColumnName: 'failures'
              timeGrain: 'PT5M'
              refreshInterval: 'PT1M'
              dataUnit: 'Count'
              evaluationRules: {
                degradedRule: {
                  operator: 'GreaterThan'
                  threshold: 0
                }
                unhealthyRule: {
                  operator: 'GreaterThan'
                  threshold: profile == 'stability' ? 2 : 3
                }
              }
            }
          ] : [], binding.component == 'assistant' && binding.resourceName == 'api-maf-python' ? [
            {
              name: 'appinsights-required-tool-failures'
              signalKind: 'LogAnalyticsQuery'
              displayName: 'Python required tool discovery errors in Application Insights'
              queryText: 'AppTraces | where TimeGenerated > ago(5m) | where _ResourceId =~ \'${applicationInsightsId}\' | where SeverityLevel >= 2 | where tostring(Properties[\'logger_name\']) == \'src.orchestrator.tools.tool_registry\' | where Message startswith \'MCP server Customer Query is not reachable:\' or Message startswith \'MCP server Itinerary Planning is not reachable:\' or Message == \'MCP server Customer Query connection timed out\' or Message == \'MCP server Itinerary Planning connection timed out\' | summarize failures=count()'
              valueColumnName: 'failures'
              timeGrain: 'PT5M'
              refreshInterval: 'PT1M'
              dataUnit: 'Count'
              evaluationRules: {
                degradedRule: {
                  operator: 'GreaterThan'
                  threshold: 0
                }
                unhealthyRule: {
                  operator: 'GreaterThan'
                  threshold: profile == 'stability' ? 2 : 3
                }
              }
            }
          ] : [], profile == 'stability' && !binding.ai ? [
            {
              name: 'container-terminations'
              signalKind: 'LogAnalyticsQuery'
              displayName: 'Container terminations'
              queryText: 'ContainerAppSystemLogs_CL | where TimeGenerated > ago(5m) | where ContainerAppName_s == \'${binding.resourceName}\' | where Reason_s == \'ContainerTerminated\' | summarize terminations=count()'
              valueColumnName: 'terminations'
              timeGrain: 'PT5M'
              refreshInterval: 'PT1M'
              dataUnit: 'Count'
              evaluationRules: {
                degradedRule: {
                  operator: 'GreaterThan'
                  threshold: 0
                }
                unhealthyRule: {
                  operator: 'GreaterThan'
                  threshold: 3
                }
              }
            }
          ] : [])
        }
      } : {})
    }
    dependsOn: [
      definitions
    ]
  }
]

var edges = concat(
  [{ parent: healthModelName, child: journeyName }],
  map(componentDefinitions, component => { parent: journeyName, child: component.name }),
  map(bindings, binding => { parent: binding.component, child: '${binding.name}-resource' })
)

resource relationships 'Microsoft.CloudHealth/healthmodels/relationships@2026-11-01' = [
  for edge in edges: {
    parent: model
    name: '${edge.parent}-${edge.child}'
    properties: {
      parentEntityName: edge.parent
      childEntityName: edge.child
    }
    dependsOn: [
      journey
      components
      resources
    ]
  }
]

output healthModelId string = model.id
