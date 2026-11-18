<#
.SYNOPSIS
    Deploy the IoT Hub, Key Vault, and operator RBAC for azure-iot-device-control.
.DESCRIPTION
    Uses the signed-in Azure CLI identity as the operator. Writes .env from the
    deployment outputs when .env does not exist yet; otherwise prints the values.
.EXAMPLE
    ./infra/deploy.ps1 -ResourceGroup rg-iot-devctl-dev -Location centralindia
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-iot-devctl-dev',
    [string]$Location = 'centralindia'
)

$ErrorActionPreference = 'Stop'

function Invoke-Az {
    $output = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed (exit $LASTEXITCODE)" }
    return $output
}

$operatorId = Invoke-Az ad signed-in-user show --query id -o tsv
Write-Host "Operator object id: $operatorId"

Invoke-Az group create --name $ResourceGroup --location $Location `
    --tags project=azure-iot-device-control environment=dev -o none
Write-Host "Resource group $ResourceGroup ready in $Location"

$deploymentName = "devctl-$(Get-Date -Format 'yyyyMMddHHmmss')"
$outputs = Invoke-Az deployment group create --resource-group $ResourceGroup --name $deploymentName `
    --template-file (Join-Path $PSScriptRoot 'main.bicep') `
    --parameters operatorPrincipalId=$operatorId `
    --query properties.outputs -o json | ConvertFrom-Json

$values = [ordered]@{
    IOTHUB_HOSTNAME       = $outputs.iotHubHostName.value
    KEY_VAULT_URI         = $outputs.keyVaultUri.value
    TELEMETRY_SECRET_NAME = $outputs.telemetrySecretName.value
    CONSUMER_GROUP        = $outputs.consumerGroup.value
}

$envFile = Join-Path (Split-Path $PSScriptRoot -Parent) '.env'
if (Test-Path $envFile) {
    Write-Host "`n.env already exists; update it with:" -ForegroundColor Yellow
    $values.GetEnumerator() | ForEach-Object { Write-Host "$($_.Key)=$($_.Value)" }
} else {
    $lines = @(
        "IOTHUB_HOSTNAME=$($values.IOTHUB_HOSTNAME)",
        'DEVICE_ID=press-01',
        'X509_CERT_FILE=certs/press-01-cert.pem',
        'X509_KEY_FILE=certs/press-01-key.pem',
        'X509_KEY_PASSPHRASE=',
        "KEY_VAULT_URI=$($values.KEY_VAULT_URI)",
        "TELEMETRY_SECRET_NAME=$($values.TELEMETRY_SECRET_NAME)",
        "CONSUMER_GROUP=$($values.CONSUMER_GROUP)"
    )
    Set-Content -Path $envFile -Value $lines -Encoding ascii
    Write-Host "`nWrote $envFile" -ForegroundColor Green
}

Write-Host "`nDeployed. Role assignments can take 1-5 minutes to apply before iotctl works." -ForegroundColor Green
