[CmdletBinding()]
param(
    [string]$SecretsFile = "",
    [switch]$Generate
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
$bytes = $null
$generated = $null
$random = $null
$secureValue = if ($Generate) {
    $bytes = [byte[]]::new(48)
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $random.GetBytes($bytes)
    $generated = [Convert]::ToBase64String($bytes)
    $generated = $generated.TrimEnd("=").Replace("+", "-").Replace("/", "_")
    ConvertTo-SecureString $generated -AsPlainText -Force
}
else {
    Read-Host "MOSHI_DISPATCH_TOKEN" -AsSecureString
}
$pointer = [IntPtr]::Zero

try {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $invalidCharacters = @(
        $value.ToCharArray() |
            Where-Object { [int]$_ -lt 33 -or [int]$_ -eq 127 }
    )
    if ($value.Length -lt 32 -or $invalidCharacters.Count -gt 0) {
        throw "The token must contain at least 32 printable, whitespace-free characters."
    }

    [IO.File]::WriteAllText(
        $temporaryPath,
        "MOSHI_DISPATCH_TOKEN=$value`r`n",
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
    Write-Host "Protected dispatch-token file updated."
}
finally {
    if ($null -ne $random) {
        $random.Dispose()
    }
    if ($null -ne $bytes) {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    Remove-Variable generated -ErrorAction SilentlyContinue
    Remove-Variable invalidCharacters -ErrorAction SilentlyContinue
    Remove-Variable value -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
}
