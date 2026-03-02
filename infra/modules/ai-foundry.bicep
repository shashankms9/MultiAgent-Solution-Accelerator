// ---------------------------------------------------------------------------
// Azure AI Foundry — Hub + Project
// Creates the AI Foundry hub with required dependencies (Storage, Key Vault)
// and a project for deploying Claude models from the model catalog.
// ---------------------------------------------------------------------------

@description('Base name for AI Foundry resources')
param name string

@description('Location for all resources')
param location string

@description('Tags for all resources')
param tags object = {}

@description('Application Insights resource ID (optional)')
param appInsightsId string = ''

// ── Storage Account (required dependency for AI Hub) ────────────────────────

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take(replace('st${name}', '-', ''), 24)
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

// ── Key Vault (required dependency for AI Hub) ──────────────────────────────

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('kv-${name}', 24)
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

// ── AI Foundry Hub ──────────────────────────────────────────────────────────

resource aiHub 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: 'hub-${name}'
  location: location
  tags: tags
  kind: 'Hub'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'Prior Auth AI Foundry Hub'
    description: 'AI Foundry hub for Prior Authorization multi-agent solution'
    storageAccount: storageAccount.id
    keyVault: keyVault.id
    applicationInsights: appInsightsId != '' ? appInsightsId : null
  }
}

// ── AI Foundry Project ──────────────────────────────────────────────────────

resource aiProject 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: 'proj-${name}'
  location: location
  tags: tags
  kind: 'Project'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'Prior Auth Project'
    description: 'AI Foundry project for deploying Claude models'
    hubResourceId: aiHub.id
  }
}

// ── Outputs ─────────────────────────────────────────────────────────────────

output hubName string = aiHub.name
output projectName string = aiProject.name
output hubId string = aiHub.id
output projectId string = aiProject.id
output portalUrl string = 'https://ai.azure.com/manage/project?wsid=${aiProject.id}'
