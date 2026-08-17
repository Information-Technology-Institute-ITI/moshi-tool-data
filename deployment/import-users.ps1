[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Xlsx,
    [string]$Workspace = "",
    [string]$Group = "Alexandria Persona",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
if (-not $Workspace) {
    $Workspace = Join-Path $repoRoot "studio_workspace"
}

function Import-EnvironmentFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) {
            throw "An environment file contains an invalid assignment."
        }
        $name = $line.Substring(0, $separator).Trim()
        if ($name -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
            throw "An environment file contains an invalid variable name."
        }
        $value = $line.Substring($separator + 1).Trim()
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

Import-EnvironmentFile (Join-Path $repoRoot ".env")
Import-EnvironmentFile (Join-Path $repoRoot ".runtime/web-runtime.env")
Import-EnvironmentFile (Join-Path $repoRoot ".runtime/web-secrets.env")

$python = Join-Path $repoRoot ".venv/Scripts/python.exe"
$arguments = @(
    (Join-Path $PSScriptRoot "import-users.py"),
    "--workspace",
    $Workspace,
    "--xlsx",
    (Resolve-Path -LiteralPath $Xlsx).Path,
    "--group",
    $Group
)
if ($DryRun) {
    $arguments += "--dry-run"
}
& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "User import failed."
}
