// ---------------------------------------------------------------------------
// Prior Auth MAF — Main Bicep template
// ---------------------------------------------------------------------------

targetScope = 'subscription'

// ── Parameters ──────────────────────────────────────────────────────────────

@minLength(1)
@maxLength(64)
@description('Name of the environment (e.g., dev, staging, prod)')
param environmentName string

@minLength(1)
@description('Primary location for all resources.')
@allowed([
  'eastus2'
  'swedencentral'
])
param location string

@description('Azure OpenAI deployment name')
param azureOpenAIDeploymentName string = 'gpt-5.4-mini'

@description('Deployment SKU')
@allowed(['GlobalStandard', 'DataZoneStandard'])
param deploymentSkuName string = 'GlobalStandard'

@description('Whether container images have been built')
param imagesBuilt string = ''

// ✅ NEW PARAMETER (ONLY ADDITION)
@description('Name of the existing Resource Group to deploy resources into')
param existingResourceGroupName string

// ── MCP URLs ────────────────────────────────────────────────────────────────
param mcpIcd10CodesUrl string = 'https://mcp.deepsense.ai/icd10_codes/mcp'
param mcpPubmedUrl string = 'https://pubmed.mcp.claude.com/mcp'
param mcpClinicalTrialsUrl string = 'https://mcp.deepsense.ai/clinical_trials/mcp'
param mcpNpiRegistryUrl string = 'https://mcp.deepsense.ai/npi_registry/mcp'
param mcpCmsCoverageUrl string = 'https://mcp.deepsense.ai/cms_coverage/mcp'

// ── Variables ───────────────────────────────────────────────────────────────

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
  'solution-accelerator': 'prior-auth-maf'
}

// ❌ REMOVED: Resource Group creation

// ✅ EXISTING RESOURCE GROUP (ONLY CHANGE)
resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' existing = {
  name: existingResourceGroupName
}

// ── Container Registry ──────────────────────────────────────────────────────

module containerRegistry './modules/container-registry.bicep' = {
  name: 'container-registry'
  scope: rg
  params: {
    name: '${abbrs.containerRegistryRegistries}${resourceToken}'
    location: location
    tags: tags
  }
}

// ── Monitoring ──────────────────────────────────────────────────────────────

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    logAnalyticsName: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    appInsightsName: '${abbrs.insightsComponents}${resourceToken}'
    location: location
    tags: tags
  }
}

// ── AI Foundry ──────────────────────────────────────────────────────────────

module aiFoundry './modules/ai-foundry.bicep' = {
  name: 'ai-foundry'
  scope: rg
  params: {
    name: '${abbrs.aiFoundry}${resourceToken}'
    location: location
    tags: tags
    appInsightsInstrumentationKey: monitoring.outputs.appInsightsInstrumentationKey
    appInsightsResourceId: monitoring.outputs.appInsightsResourceId
    deploymentName: azureOpenAIDeploymentName
    deploymentSkuName: deploymentSkuName
  }
}

// ── Container Apps Environment ──────────────────────────────────────────────

module containerAppsEnv './modules/container-apps-env.bicep' = {
  name: 'container-apps-env'
  scope: rg
  params: {
    name: '${abbrs.appManagedEnvironments}${resourceToken}'
    location: location
    tags: tags
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
  }
}

// ── Backend ─────────────────────────────────────────────────────────────────

module backend './modules/container-app.bicep' = {
  name: 'backend'
  scope: rg
  params: {
    name: '${abbrs.appContainerApps}backend-${resourceToken}'
    location: location
    tags: union(tags, { 'azd-service-name': 'backend' })
    containerAppsEnvironmentId: containerAppsEnv.outputs.environmentId
    containerRegistryName: containerRegistry.outputs.name
    containerRegistryLoginServer: containerRegistry.outputs.loginServer
    imageName: 'backend'
    targetPort: 8000
    useAcrImage: imagesBuilt == 'true'
    cpu: '1'
    memory: '2Gi'
    minReplicas: 1
    env: [
      { name: 'AZURE_AI_PROJECT_ENDPOINT', value: aiFoundry.outputs.projectEndpoint }
      { name: 'AZURE_OPENAI_DEPLOYMENT_NAME', value: azureOpenAIDeploymentName }
      { name: 'HOSTED_AGENT_CLINICAL_NAME', value: 'clinical-reviewer-agent' }
      { name: 'HOSTED_AGENT_COVERAGE_NAME', value: 'coverage-assessment-agent' }
      { name: 'HOSTED_AGENT_COMPLIANCE_NAME', value: 'compliance-agent' }
      { name: 'HOSTED_AGENT_SYNTHESIS_NAME', value: 'synthesis-agent' }
      { name: 'HOSTED_AGENT_TIMEOUT_SECONDS', value: '180' }
      { name: 'APPLICATION_INSIGHTS_CONNECTION_STRING', value: monitoring.outputs.appInsightsConnectionString }
      { name: 'FRONTEND_ORIGIN', value: 'https://${abbrs.appContainerApps}frontend-${resourceToken}.${containerAppsEnv.outputs.defaultDomain}' }
    ]
    secrets: []
    healthCheckPath: '/health'
  }
}

// ── Role Assignments ────────────────────────────────────────────────────────

module roleAssignments './modules/role-assignments.bicep' = {
  name: 'role-assignments'
  scope: rg
  params: {
    foundryAccountName: aiFoundry.outputs.accountName
    backendPrincipalId: backend.outputs.principalId
    containerRegistryName: containerRegistry.outputs.name
    foundryProjectPrincipalId: aiFoundry.outputs.projectPrincipalId
  }
}

// ── Frontend ────────────────────────────────────────────────────────────────

module frontend './modules/container-app.bicep' = {
  name: 'frontend'
  scope: rg
  params: {
    name: '${abbrs.appContainerApps}frontend-${resourceToken}'
    location: location
    tags: union(tags, { 'azd-service-name': 'frontend' })
    containerAppsEnvironmentId: containerAppsEnv.outputs.environmentId
    containerRegistryName: containerRegistry.outputs.name
    containerRegistryLoginServer: containerRegistry.outputs.loginServer
    imageName: 'frontend'
    targetPort: 80
    useAcrImage: imagesBuilt == 'true'
    minReplicas: 1
    env: [
      { name: 'BACKEND_URL', value: 'https://${abbrs.appContainerApps}backend-${resourceToken}.${containerAppsEnv.outputs.defaultDomain}' }
    ]
    secrets: []
    healthCheckPath: '/'
  }
}

// ── Outputs ─────────────────────────────────────────────────────────────────

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.outputs.loginServer
output AI_FOUNDRY_ACCOUNT_NAME string = aiFoundry.outputs.accountName
output AI_FOUNDRY_PROJECT_NAME string = aiFoundry.outputs.projectName
output AI_FOUNDRY_ENDPOINT string = aiFoundry.outputs.endpoint
output AI_FOUNDRY_PROJECT_ENDPOINT string = aiFoundry.outputs.projectEndpoint
output AI_FOUNDRY_PORTAL_URL string = aiFoundry.outputs.portalUrl
output BACKEND_CONTAINER_APP_NAME string = backend.outputs.name
output FRONTEND_CONTAINER_APP_NAME string = frontend.outputs.name
output AZURE_OPENAI_DEPLOYMENT_NAME string = azureOpenAIDeploymentName
output APPLICATION_INSIGHTS_CONNECTION_STRING string = monitoring.outputs.appInsightsConnectionString
output frontendUrl string = frontend.outputs.fqdn
output backendUrl string = backend.outputs.fqdn
