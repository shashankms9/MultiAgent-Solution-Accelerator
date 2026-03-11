// ---------------------------------------------------------------------------
// Role Assignments for Prior Auth MAF (Foundry Hosted Agents deployment)
//
// 1. Backend ACA identity → Cognitive Services OpenAI User on Foundry account
//    Allows the FastAPI orchestrator to call the Foundry Responses API with
//    agent_reference routing to invoke Foundry Hosted Agents.
//
// 2. Foundry project managed identity → AcrPull on Container Registry
//    Allows Foundry Agent Service to pull the 4 agent container images from
//    ACR when provisioning Foundry Hosted Agent deployments.
// ---------------------------------------------------------------------------

@description('Name of the existing Foundry (CognitiveServices) account')
param foundryAccountName string

@description('Principal ID of the backend Container App system-assigned managed identity')
param backendPrincipalId string

@description('Name of the Azure Container Registry (for AcrPull grant to Foundry project)')
param containerRegistryName string

@description('Principal ID of the Foundry project system-assigned managed identity')
param foundryProjectPrincipalId string

// Cognitive Services OpenAI User — allows calling Azure OpenAI + Foundry APIs
var cognitiveServicesOpenAIUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

// AcrPull — allows pulling container images from Azure Container Registry
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: containerRegistryName
}

// 1. Backend → CognitiveServicesOpenAIUser on Foundry account
resource backendFoundryRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, backendPrincipalId, cognitiveServicesOpenAIUserRoleId)
  scope: foundryAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUserRoleId)
    principalId: backendPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// 2. Foundry project identity → AcrPull on Container Registry
resource foundryProjectAcrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, foundryProjectPrincipalId, acrPullRoleId)
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: foundryProjectPrincipalId
    principalType: 'ServicePrincipal'
  }
}
