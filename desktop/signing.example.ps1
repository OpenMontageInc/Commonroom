# Copy to signing.local.ps1 (git-ignored) and fill in your own certificate.
# Used by scripts/release-win.ps1. Contains no secrets: Azure access comes from `az login`.
$env:COMMONROOM_SIGNING = 'azure'                       # azure | certificate
$env:COMMONROOM_SIGNING_PUBLISHER = 'Your Company, Inc.' # certificate common name, exactly
$env:COMMONROOM_AZURE_SIGNING_ENDPOINT = 'https://eus.codesigning.azure.net/'
$env:COMMONROOM_AZURE_SIGNING_ACCOUNT = 'your-signing-account'
$env:COMMONROOM_AZURE_SIGNING_PROFILE = 'your-certificate-profile'
# Optional: a private .NET SDK folder for the TrustedSigning PowerShell module.
# $env:COMMONROOM_SIGNING_DOTNET = 'C:\path\to\dotnet'
# For a .pfx instead: COMMONROOM_SIGNING='certificate', CSC_LINK, CSC_KEY_PASSWORD.
