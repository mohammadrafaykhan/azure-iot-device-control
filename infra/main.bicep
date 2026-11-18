// Azure IoT device control: IoT Hub (Entra ID for the service plane, X.509 only for devices),
// a Key Vault holding the built-in endpoint's read key, and least-privilege RBAC for the operator.

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('IoT Hub name (globally unique).')
param hubName string = 'iot-devctl-${uniqueString(resourceGroup().id)}'

@description('Key Vault name (globally unique, 3-24 chars).')
@maxLength(24)
param keyVaultName string = 'kv-devctl-${take(uniqueString(resourceGroup().id), 10)}'

@description('IoT Hub SKU. F1 is free (one per subscription, 8,000 messages/day).')
@allowed(['F1', 'B1', 'S1'])
param hubSku string = 'F1'

@description('Object ID of the operator (user) who runs iotctl.')
param operatorPrincipalId string

@description('Principal type of the operator.')
@allowed(['User', 'Group', 'ServicePrincipal'])
param operatorPrincipalType string = 'User'

@description('Event Hubs consumer group used by iotctl monitor.')
param consumerGroupName string = 'iotctl'

param tags object = {
  project: 'azure-iot-device-control'
  environment: 'dev'
}

var telemetrySecretName = 'iothub-events-connection'
var roles = {
  iotHubDataContributor: '4fc6c259-987e-4a07-842e-c321cc9d413f'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
}

resource hub 'Microsoft.Devices/IotHubs@2023-06-30' = {
  name: hubName
  location: location
  tags: tags
  sku: {
    name: hubSku
    capacity: 1
  }
  properties: {
    disableDeviceSAS: true
    disableModuleSAS: true
    eventHubEndpoints: {
      events: {
        retentionTimeInDays: 1
        partitionCount: 2
      }
    }
  }
}

resource consumerGroup 'Microsoft.Devices/IotHubs/eventHubEndpoints/ConsumerGroups@2023-06-30' = {
  name: '${hub.name}/events/${consumerGroupName}'
  properties: {
    name: consumerGroupName
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
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

var servicePolicyKey = filter(hub.listKeys().value, policy => policy.keyName == 'service')[0].primaryKey

resource telemetrySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: telemetrySecretName
  properties: {
    contentType: 'IoT Hub built-in endpoint connection string (ServiceConnect)'
    value: 'Endpoint=${hub.properties.eventHubEndpoints.events.endpoint};SharedAccessKeyName=service;SharedAccessKey=${servicePolicyKey};EntityPath=${hub.properties.eventHubEndpoints.events.path}'
  }
}

resource hubDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(hub.id, operatorPrincipalId, roles.iotHubDataContributor)
  scope: hub
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.iotHubDataContributor)
    principalId: operatorPrincipalId
    principalType: operatorPrincipalType
  }
}

resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, operatorPrincipalId, roles.keyVaultSecretsUser)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: operatorPrincipalId
    principalType: operatorPrincipalType
  }
}

output iotHubHostName string = hub.properties.hostName
output keyVaultUri string = vault.properties.vaultUri
output keyVaultName string = vault.name
output telemetrySecretName string = telemetrySecretName
output consumerGroup string = consumerGroupName
