[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "MOSHI_SMTP_PASSWORD",
        "MOSHI_DISPATCH_TOKEN",
        "MOSHI_WORKER_TOKEN"
    )]
    [string]$Name,
    [string]$SecretsFile = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
if (-not $SecretsFile) {
    $SecretsFile = Join-Path $repoRoot ".runtime/web-secrets.env"
}
$directory = Split-Path -Parent $SecretsFile
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$temporaryPath = Join-Path $directory (".web-secrets-" + [guid]::NewGuid() + ".tmp")
$secureValue = Read-Host $Name -AsSecureString
$pointer = [IntPtr]::Zero

try {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if (
        [string]::IsNullOrEmpty($value) -or
        $value.Contains("`r") -or
        $value.Contains("`n")
    ) {
        throw "$Name must be a nonempty single-line value."
    }

    $lines = [Collections.Generic.List[string]]::new()
    $replaced = $false
    if (Test-Path -LiteralPath $SecretsFile -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($SecretsFile)) {
            if ($line -match "^$([Regex]::Escape($Name))=") {
                $lines.Add("$Name=$value")
                $replaced = $true
            }
            else {
                $lines.Add($line)
            }
        }
    }
    if (-not $replaced) {
        $lines.Add("$Name=$value")
    }
    [IO.File]::WriteAllLines(
        $temporaryPath,
        $lines,
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
    Set-Acl -LiteralPath $temporaryPath -AclObject $acl

    if (Test-Path -LiteralPath $SecretsFile -PathType Leaf) {
        [IO.File]::Replace($temporaryPath, $SecretsFile, $null)
    }
    else {
        Move-Item -LiteralPath $temporaryPath -Destination $SecretsFile
    }
    Write-Host "$Name was updated in the protected secret file."
}
finally {
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    Remove-Variable value -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
}
