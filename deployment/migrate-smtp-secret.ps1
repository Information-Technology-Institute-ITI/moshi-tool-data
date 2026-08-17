[CmdletBinding()]
param(
    [string]$EnvFile = "",
    [string]$SecretsFile = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
if (-not $EnvFile) {
    $EnvFile = Join-Path $repoRoot ".env"
}
if (-not $SecretsFile) {
    $SecretsFile = Join-Path $repoRoot ".runtime/web-secrets.env"
}
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "The environment file was not found."
}

$password = $null
$remaining = [Collections.Generic.List[string]]::new()
foreach ($line in [IO.File]::ReadAllLines($EnvFile)) {
    if ($line -match "^(SMTP_PASSWORD|MOSHI_SMTP_PASSWORD)=(.*)$") {
        if ($null -eq $password) {
            $password = $Matches[2].Trim()
        }
        continue
    }
    $remaining.Add($line)
}
if ([string]::IsNullOrWhiteSpace($password)) {
    throw "No SMTP password assignment was found in .env."
}

$secretDirectory = Split-Path -Parent $SecretsFile
New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
$secretLines = [Collections.Generic.List[string]]::new()
$secretReplaced = $false
if (Test-Path -LiteralPath $SecretsFile -PathType Leaf) {
    foreach ($line in [IO.File]::ReadAllLines($SecretsFile)) {
        if ($line -match "^MOSHI_SMTP_PASSWORD=") {
            $secretLines.Add("MOSHI_SMTP_PASSWORD=$password")
            $secretReplaced = $true
        }
        else {
            $secretLines.Add($line)
        }
    }
}
if (-not $secretReplaced) {
    $secretLines.Add("MOSHI_SMTP_PASSWORD=$password")
}

$envTemporary = Join-Path `
    (Split-Path -Parent $EnvFile) `
    (".env-smtp-" + [guid]::NewGuid() + ".tmp")
$envBackup = Join-Path `
    (Split-Path -Parent $EnvFile) `
    (".env-smtp-" + [guid]::NewGuid() + ".bak")
$secretTemporary = Join-Path `
    $secretDirectory `
    (".web-secrets-" + [guid]::NewGuid() + ".tmp")
$secretBackup = Join-Path `
    $secretDirectory `
    (".web-secrets-" + [guid]::NewGuid() + ".bak")

try {
    [IO.File]::WriteAllLines(
        $secretTemporary,
        $secretLines,
        [Text.UTF8Encoding]::new($false)
    )
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemIdentity = [Security.Principal.SecurityIdentifier]::new(
        [Security.Principal.WellKnownSidType]::LocalSystemSid,
        $null
    )
    foreach ($identity in @($currentIdentity, $systemIdentity)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $identity,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $secretTemporary -AclObject $acl

    if (Test-Path -LiteralPath $SecretsFile -PathType Leaf) {
        [IO.File]::Replace($secretTemporary, $SecretsFile, $secretBackup)
        Remove-Item -LiteralPath $secretBackup -Force
    }
    else {
        Move-Item -LiteralPath $secretTemporary -Destination $SecretsFile
    }

    [IO.File]::WriteAllLines(
        $envTemporary,
        $remaining,
        [Text.UTF8Encoding]::new($false)
    )
    Set-Acl -LiteralPath $envTemporary -AclObject (Get-Acl -LiteralPath $EnvFile)
    [IO.File]::Replace($envTemporary, $EnvFile, $envBackup)
    Remove-Item -LiteralPath $envBackup -Force
    Write-Host "SMTP password moved to the protected secret file."
}
finally {
    Remove-Variable password -ErrorAction SilentlyContinue
    foreach ($path in @(
        $envTemporary,
        $envBackup,
        $secretTemporary,
        $secretBackup
    )) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}
