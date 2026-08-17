[CmdletBinding()]
param(
    [ValidateSet("0", "1")]
    [string]$Value = "1",
    [string]$EnvFile = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
if (-not $EnvFile) {
    $EnvFile = Join-Path $repoRoot ".env"
}
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "The environment file was not found."
}

$temporary = Join-Path `
    (Split-Path -Parent $EnvFile) `
    (".env-auth-" + [guid]::NewGuid() + ".tmp")
$backup = Join-Path `
    (Split-Path -Parent $EnvFile) `
    (".env-auth-" + [guid]::NewGuid() + ".bak")
try {
    $lines = [Collections.Generic.List[string]]::new()
    $replaced = $false
    foreach ($line in [IO.File]::ReadAllLines($EnvFile)) {
        if ($line -match "^MOSHI_REQUIRE_SIGN_IN=") {
            $lines.Add("MOSHI_REQUIRE_SIGN_IN=$Value")
            $replaced = $true
        }
        else {
            $lines.Add($line)
        }
    }
    if (-not $replaced) {
        $lines.Add("MOSHI_REQUIRE_SIGN_IN=$Value")
    }
    [IO.File]::WriteAllLines(
        $temporary,
        $lines,
        [Text.UTF8Encoding]::new($false)
    )
    Set-Acl -LiteralPath $temporary -AclObject (Get-Acl -LiteralPath $EnvFile)
    [IO.File]::Replace($temporary, $EnvFile, $backup)
    Remove-Item -LiteralPath $backup -Force
    Write-Host "MOSHI_REQUIRE_SIGN_IN was updated."
}
finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
}
