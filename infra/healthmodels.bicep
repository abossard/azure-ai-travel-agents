@description('Existing azd environment name, used as the health model name prefix.')
param environmentName string

@description('Supported health-model region. May differ from the monitored resources.')
param location string

@description('Actual AI Services account name from the deployed workload inventory.')
param aiAccountName string

@description('Actual Log Analytics workspace name from the deployed workload inventory.')
param logAnalyticsWorkspaceName string

@description('Actual Application Insights resource name from the deployed workload inventory.')
param applicationInsightsName string

module healthModel './healthmodel.bicep' = {
  name: 'healthmodel'
  params: {
    healthModelName: '${environmentName}-health'
    location: location
    aiAccountName: aiAccountName
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    applicationInsightsName: applicationInsightsName
  }
}

output healthModelIds array = [healthModel.outputs.healthModelId]
