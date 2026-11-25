# Developer launcher for running Commonroom from source on Windows.
# Opens the installed app if there is one, else the development desktop app,
# else runs the service with Python and opens it in the browser.
$ErrorActionPreference = 'Stop'
$url = 'http://127.0.0.1:8731'

try {
    $installedApp = Join-Path $env:LOCALAPPDATA 'Programs\Commonroom\Commonroom.exe'
    if (Test-Path -LiteralPath $installedApp -PathType Leaf) {
        Start-Process -FilePath $installedApp
        exit 0
    }

    $desktop = Join-Path $PSScriptRoot 'desktop'
    $electron = Join-Path $desktop 'node_modules\electron\dist\electron.exe'
    if (Test-Path -LiteralPath $electron -PathType Leaf) {
        Start-Process -FilePath $electron -ArgumentList '.' -WorkingDirectory $desktop
        exit 0
    }

    try {
        if ((Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 1).service -ceq 'commonroom') {
            Start-Process $url
            exit 0
        }
    } catch {}

    if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) {
        throw 'Commonroom needs Python 3.10 or newer. Install it with: winget install -e --id Python.Python.3.12'
    }
    Write-Host "Starting Commonroom at $url (close this window to stop it)."
    Start-Job -ScriptBlock {
        param($address)
        Start-Sleep -Seconds 2
        Start-Process $address
    } -ArgumentList $url | Out-Null
    Set-Location -LiteralPath $PSScriptRoot
    & py.exe -3 -m commonroom --port 8731
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
