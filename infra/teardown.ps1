<#
.SYNOPSIS
    Delete every resource for azure-iot-device-control and purge the soft-deleted Key Vault.
.DESCRIPTION
    Purging frees the Key Vault name so the next deploy.ps1 run can reuse it.
.EXAMPLE
    ./infra/teardown.ps1 -ResourceGroup rg-iot-devctl-dev
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'rg-iot-devctl-dev',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$exists = az group exists --name $ResourceGroup
if ($exists -ne 'true') {
    Write-Host "Resource group $ResourceGroup does not exist; nothing to delete."
    return
}

$vaults = @(az keyvault list --resource-group $ResourceGroup --query '[].name' -o tsv)

if (-not $Force) {
    $answer = Read-Host "Delete resource group '$ResourceGroup' and everything in it? Type the name to confirm"
    if ($answer -ne $ResourceGroup) {
        Write-Host 'Cancelled.'
        return
    }
}

az group delete --name $ResourceGroup --yes
if ($LASTEXITCODE -ne 0) { throw "Failed to delete $ResourceGroup" }
Write-Host "Deleted $ResourceGroup"

foreach ($vault in $vaults | Where-Object { $_ }) {
    az keyvault purge --name $vault
    if ($LASTEXITCODE -ne 0) { throw "Failed to purge Key Vault $vault" }
    Write-Host "Purged Key Vault $vault"
}
