[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("MOSHI_PUBLIC_ORIGIN")]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [string]$Value,
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
if ($Value -match "[`r`n]") {
    throw "$Name must be a single-line value."
}

$temporary = Join-Path `
    (Split-Path -Parent $EnvFile) `
    (".env-value-" + [guid]::NewGuid() + ".tmp")
$backup = Join-Path `
    (Split-Path -Parent $EnvFile) `
    (".env-value-" + [guid]::NewGuid() + ".bak")
try {
    $lines = [Collections.Generic.List[string]]::new()
    $replaced = $false
    foreach ($line in [IO.File]::ReadAllLines($EnvFile)) {
        if ($line -match "^$([Regex]::Escape($Name))=") {
            $lines.Add("$Name=$Value")
            $replaced = $true
        }
        else {
            $lines.Add($line)
        }
    }
    if (-not $replaced) {
        $lines.Add("$Name=$Value")
    }
    [IO.File]::WriteAllLines(
        $temporary,
        $lines,
        [Text.UTF8Encoding]::new($false)
    )
    Set-Acl -LiteralPath $temporary -AclObject (Get-Acl -LiteralPath $EnvFile)
    [IO.File]::Replace($temporary, $EnvFile, $backup)
    Remove-Item -LiteralPath $backup -Force
    Write-Host "$Name was updated."
}
finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
}
