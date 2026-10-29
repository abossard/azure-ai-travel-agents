@description('Existing azd environment name, used as the model name prefix.')
param environmentName string

@description('Supported health-model region. May differ from the monitored resources.')
param location string

@description('Actual AI Services account name from the deployed workload inventory.')
param aiAccountName string

@description('Actual Log Analytics workspace name from the deployed workload inventory.')
param logAnalyticsWorkspaceName string

@description('Actual Application Insights resource name from the deployed workload inventory.')
param applicationInsightsName string

var profiles = [
  'platform'
  'stability'
  'capacity'
  'experience'
]

module comparison './healthmodel.bicep' = [
  for profile in profiles: {
    name: 'healthmodel-${profile}'
    params: {
      healthModelName: '${environmentName}-${profile}'
      location: location
      profile: profile
      aiAccountName: aiAccountName
      logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
      applicationInsightsName: applicationInsightsName
    }
  }
]

output healthModelIds array = [for (profile, index) in profiles: comparison[index].outputs.healthModelId]
