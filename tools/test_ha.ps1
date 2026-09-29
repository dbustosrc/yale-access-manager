[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9._-]+@[A-Za-z0-9.:-]+$')]
    [string]$Target,

    [Parameter(Mandatory = $true)]
    [string]$IdentityFile,

    [switch]$LiveTemporaryAuthorized,

    [switch]$LiveDisableEnableAuthorized
)

$ErrorActionPreference = 'Stop'
if ($LiveDisableEnableAuthorized -and -not $LiveTemporaryAuthorized) {
    throw 'Disable/enable requires authorization to create and clean up the temporary test access.'
}
$repository = Split-Path -Parent $PSScriptRoot
$component = Join-Path $repository 'custom_components\yale_access_manager'
$sources = @{}
foreach ($file in Get-ChildItem -LiteralPath $component -Filter '*.py') {
    $module = 'yale_access_manager' + $(if ($file.Name -ne '__init__.py') { '.' + $file.BaseName } else { '' })
    $sources[$module] = @{ package = $file.Name -eq '__init__.py'; source = Get-Content -LiteralPath $file.FullName -Raw -Encoding utf8 }
}
$sources['test_integration'] = @{ package = $false; source = Get-Content -LiteralPath (Join-Path $repository 'tests\test_integration.py') -Raw -Encoding utf8 }
if ($LiveTemporaryAuthorized) {
    $sources['live_temporary_check'] = @{ package = $false; source = Get-Content -LiteralPath (Join-Path $repository 'tests\live_temporary_check.py') -Raw -Encoding utf8 }
}
$assets = @{}
foreach ($file in Get-ChildItem -LiteralPath $component -Recurse -File | Where-Object { $_.Extension -in '.json', '.yaml' }) {
    $relative = [IO.Path]::GetRelativePath($repository, $file.FullName).Replace('\', '/')
    $assets[$relative] = Get-Content -LiteralPath $file.FullName -Raw -Encoding utf8
}
$assets['hacs.json'] = Get-Content -LiteralPath (Join-Path $repository 'hacs.json') -Raw -Encoding utf8
$runner = Get-Content -LiteralPath (Join-Path $repository 'tools\run_checks.py') -Raw -Encoding utf8
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($runner))
$remoteCommand = "sudo -n docker exec -e PYTHONDONTWRITEBYTECODE=1 -i homeassistant python3 -B -c `"import base64; exec(base64.b64decode('$encoded'))`""
@{ sources = $sources; assets = $assets; authorized_live_temporary_test = [bool]$LiveTemporaryAuthorized; authorized_disable_enable_test = [bool]$LiveDisableEnableAuthorized } | ConvertTo-Json -Depth 100 -Compress |
    ssh -i $IdentityFile -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 $Target $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Yale native checks failed.' }
