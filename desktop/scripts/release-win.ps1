# Build a signed Windows installer and verify every signature.
#
# Signing settings come from desktop/signing.local.ps1 (git-ignored; copy
# signing.example.ps1) or from COMMONROOM_* environment variables already set.
# Azure Artifact Signing needs `az login` with the "Artifact Signing
# Certificate Profile Signer" role on the signing account, and a .NET SDK on
# PATH (set COMMONROOM_SIGNING_DOTNET to a private SDK folder if needed).
$ErrorActionPreference = 'Stop'
$desktop = Split-Path $PSScriptRoot -Parent
$local = Join-Path $desktop 'signing.local.ps1'
if (Test-Path -LiteralPath $local) { . $local }

if (-not $env:COMMONROOM_SIGNING -or $env:COMMONROOM_SIGNING -eq 'none') {
    throw 'Signing is not configured. Copy desktop/signing.example.ps1 to desktop/signing.local.ps1 and fill it in.'
}
if ($env:COMMONROOM_SIGNING_DOTNET) {
    if (-not (Test-Path -LiteralPath (Join-Path $env:COMMONROOM_SIGNING_DOTNET 'dotnet.exe'))) {
        throw "No dotnet.exe in COMMONROOM_SIGNING_DOTNET ($env:COMMONROOM_SIGNING_DOTNET)."
    }
    $env:DOTNET_ROOT = $env:COMMONROOM_SIGNING_DOTNET
    $env:PATH = "$env:COMMONROOM_SIGNING_DOTNET;$env:PATH"
}
if ($env:COMMONROOM_SIGNING -eq 'azure') {
    & az account show --only-show-errors 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Sign in to Azure first: az login' }
}

Push-Location $desktop
try {
    & npm.cmd run package:win
    if ($LASTEXITCODE -ne 0) { throw 'Signed packaging failed.' }
} finally {
    Pop-Location
}

$version = (Get-Content (Join-Path $desktop 'package.json') -Raw | ConvertFrom-Json).version
$build = Join-Path $desktop 'build'
$files = @(
    (Join-Path $build "Commonroom-Setup-$version.exe"),
    (Join-Path $build 'win-unpacked\Commonroom.exe')
)
$failed = $false
foreach ($file in $files) {
    $signature = Get-AuthenticodeSignature -LiteralPath $file
    $subject = $signature.SignerCertificate.Subject
    $stamped = [bool]$signature.TimeStamperCertificate
    $ok = $signature.Status -eq 'Valid' -and $subject -like "CN=*$($env:COMMONROOM_SIGNING_PUBLISHER)*" -and $stamped
    if (-not $ok) { $failed = $true }
    '{0}  {1}  timestamped={2}  {3}' -f $(if ($ok) { 'OK  ' } else { 'FAIL' }), $signature.Status, $stamped, (Split-Path $file -Leaf)
    "      $subject"
}
if ($failed) { throw 'One or more files are not validly signed by the expected publisher.' }
$installer = Get-Item -LiteralPath $files[0]
'Signed installer: {0} ({1:N0} MB)' -f $installer.FullName, ($installer.Length / 1MB)
