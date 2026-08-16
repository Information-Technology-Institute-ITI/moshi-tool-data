[CmdletBinding()]
param(
    [ValidateSet("Restart", "Start", "Stop", "Status")]
    [string]$Action = "Restart",
    [string]$EnvFile = "",
    [string]$RuntimeFile = "",
    [string]$SecretsFile = "",
    [switch]$AllowNetworkExposure
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
if (-not $EnvFile) {
    $EnvFile = Join-Path $repoRoot ".env"
}
$runtimeDirectory = Join-Path $repoRoot ".runtime"
if (-not $RuntimeFile) {
    $RuntimeFile = Join-Path $runtimeDirectory "web-runtime.env"
}
if (-not $SecretsFile) {
    $SecretsFile = Join-Path $runtimeDirectory "web-secrets.env"
}
$pidPath = Join-Path $runtimeDirectory "web-service.pid"
$backupMetadataPath = Join-Path $runtimeDirectory "last-web-backup.json"
$portProxyMarker = Join-Path $runtimeDirectory "web-port80-test.json"
$pythonPath = Join-Path $repoRoot ".venv/Scripts/python.exe"
$backupHelperPath = Join-Path $PSScriptRoot "backup-sqlite.py"
$originalEnvironment = @{}

function Import-EnvironmentFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Required
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        if ($Required) {
            throw "Protected environment file was not found: $Path"
        }
        return
    }

    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        if ($line.StartsWith("export ")) {
            $line = $line.Substring(7).TrimStart()
        }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) {
            throw "The environment file contains an invalid assignment."
        }
        $name = $line.Substring(0, $separator).Trim()
        if ($name -notmatch "^[A-Za-z_][A-Za-z0-9_]*$") {
            throw "The environment file contains an invalid variable name."
        }
        $value = $line.Substring($separator + 1).Trim()
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if (-not $originalEnvironment.ContainsKey($name)) {
            $originalEnvironment[$name] = [pscustomobject]@{
                Present = $null -ne [Environment]::GetEnvironmentVariable($name, "Process")
                Value = [Environment]::GetEnvironmentVariable($name, "Process")
            }
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Restore-Environment {
    foreach ($entry in $originalEnvironment.GetEnumerator()) {
        $value = if ($entry.Value.Present) { $entry.Value.Value } else { $null }
        [Environment]::SetEnvironmentVariable($entry.Key, $value, "Process")
    }
}

function Get-WebPort {
    $value = [Environment]::GetEnvironmentVariable("MOSHI_WEB_PORT", "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        $value = [Environment]::GetEnvironmentVariable("web_port", "Process")
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            [Environment]::SetEnvironmentVariable(
                "MOSHI_WEB_PORT",
                $value,
                "Process"
            )
        }
    }
    $port = 0
    if ([string]::IsNullOrWhiteSpace($value) -or -not [int]::TryParse($value, [ref]$port)) {
        throw "MOSHI_WEB_PORT must be set to an integer in the protected environment file."
    }
    if ($port -lt 1 -or $port -gt 65535) {
        throw "MOSHI_WEB_PORT must be between 1 and 65535."
    }
    return $port
}

function Get-WebBindAddress {
    $value = [Environment]::GetEnvironmentVariable("MOSHI_WEB_BIND_ADDRESS", "Process")
    if ([string]::IsNullOrWhiteSpace($value)) {
        return "127.0.0.1"
    }
    return $value.Trim()
}

function Assert-WebConfiguration {
    [void](Get-WebPort)
    if (
        [string]::IsNullOrWhiteSpace(
            [Environment]::GetEnvironmentVariable("AWS_REGION", "Process")
        )
    ) {
        $defaultRegion = [Environment]::GetEnvironmentVariable(
            "AWS_DEFAULT_REGION",
            "Process"
        )
        if (-not [string]::IsNullOrWhiteSpace($defaultRegion)) {
            [Environment]::SetEnvironmentVariable(
                "AWS_REGION",
                $defaultRegion,
                "Process"
            )
        }
    }
    $bindAddress = Get-WebBindAddress
    if (
        $bindAddress -notin @("127.0.0.1", "localhost", "::1") -and
        -not $AllowNetworkExposure
    ) {
        throw "Non-loopback binding requires -AllowNetworkExposure and a reviewed authentication boundary."
    }
    $missingNames = @()
    foreach ($requiredName in @(
        "MOSHI_GPU_INTERNAL_URL",
        "MOSHI_GPU_REQUIRED_BUILD_ID",
        "MOSHI_GPU_INSTANCE_ID",
        "AWS_REGION",
        "MOSHI_DEPLOYMENT_GENERATION",
        "MOSHI_DISPATCH_TOKEN",
        "MOSHI_WORKER_TOKEN"
    )) {
        if ([string]::IsNullOrWhiteSpace(
            [Environment]::GetEnvironmentVariable($requiredName, "Process")
        )) {
            $missingNames += $requiredName
        }
    }
    if ($missingNames.Count -gt 0) {
        throw "Required environment variables are missing: $($missingNames -join ', ')"
    }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "The repository virtual-environment Python executable was not found."
    }
    if (-not (Test-Path -LiteralPath $backupHelperPath -PathType Leaf)) {
        throw "The SQLite backup helper was not found."
    }
}

function Get-MoshiHealth {
    param([Parameter(Mandatory = $true)][int]$Port)

    try {
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "http://127.0.0.1:${Port}/api/health" `
            -TimeoutSec 2
        if ($response.StatusCode -ne 200) {
            return $null
        }
        $value = $response.Content | ConvertFrom-Json
        if ($value.status -ne "ok" -or [string]::IsNullOrWhiteSpace($value.workspace)) {
            return $null
        }
        return $value
    }
    catch {
        return $null
    }
}

function Test-MoshiHealth {
    param([Parameter(Mandatory = $true)][int]$Port)

    return $null -ne (Get-MoshiHealth -Port $Port)
}

function Remove-OwnedPortProxy {
    if (-not (Test-Path -LiteralPath $portProxyMarker -PathType Leaf)) {
        return
    }
    $marker = Get-Content -Raw -LiteralPath $portProxyMarker | ConvertFrom-Json
    & netsh interface portproxy delete v4tov4 `
        "listenport=$([int]$marker.listen_port)" `
        "listenaddress=$($marker.listen_address)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to remove the previously created test port mapping."
    }
    Remove-Item -LiteralPath $portProxyMarker -Force
}

function Get-MoshiWebProcessIds {
    $identifiers = @()
    foreach ($process in Get-Process -Name "python", "pythonw" -ErrorAction SilentlyContinue) {
        $connections = @(
            Get-NetTCPConnection -State Listen -OwningProcess $process.Id -ErrorAction SilentlyContinue
        )
        foreach ($connection in $connections) {
            if (Test-MoshiHealth -Port ([int]$connection.LocalPort)) {
                $identifiers += $process.Id
                break
            }
        }
    }
    return @($identifiers | Sort-Object -Unique)
}

function Get-MoshiWorkspacePaths {
    $paths = @()
    foreach ($process in Get-Process -Name "python", "pythonw" -ErrorAction SilentlyContinue) {
        $connections = @(
            Get-NetTCPConnection -State Listen -OwningProcess $process.Id -ErrorAction SilentlyContinue
        )
        foreach ($connection in $connections) {
            $health = Get-MoshiHealth -Port ([int]$connection.LocalPort)
            if ($null -ne $health) {
                $paths += [string]$health.workspace
            }
        }
    }
    return @($paths | Where-Object { $_ } | Sort-Object -Unique)
}

function Get-ConfiguredWorkspacePath {
    $workspace = [Environment]::GetEnvironmentVariable("MOSHI_WORKSPACE", "Process")
    if ([string]::IsNullOrWhiteSpace($workspace)) {
        $workspace = "/data/studio_workspace"
    }
    if (-not [System.IO.Path]::IsPathRooted($workspace)) {
        $workspace = Join-Path $repoRoot $workspace
    }
    return [System.IO.Path]::GetFullPath($workspace)
}

function Backup-MoshiCatalogs {
    param([string[]]$WorkspacePaths)

    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "The repository virtual-environment Python executable was not found."
    }
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
    $records = @()
    foreach ($workspace in @($WorkspacePaths | Where-Object { $_ } | Sort-Object -Unique)) {
        $catalogPath = Join-Path $workspace "catalog.sqlite3"
        if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf)) {
            continue
        }
        $backupDirectory = Join-Path $workspace "backups"
        New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
        $timestamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
        $backupPath = Join-Path $backupDirectory "catalog-before-web-restart-${timestamp}.sqlite3"
        & $pythonPath $backupHelperPath $catalogPath $backupPath
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
            Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
            throw "The SQLite backup or integrity verification failed."
        }
        $records += [pscustomobject]@{
            CreatedAt = [DateTimeOffset]::UtcNow.ToString("o")
            Path = $backupPath
            Sha256 = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash
        }
        Start-Sleep -Milliseconds 2
    }
    if ($records.Count -gt 0) {
        $records | ConvertTo-Json -Depth 3 |
            Set-Content -LiteralPath $backupMetadataPath -Encoding ascii
        Write-Host "Verified SQLite backup count: $($records.Count)"
    }
}

function Stop-MoshiWeb {
    Remove-OwnedPortProxy
    $identifiers = @(Get-MoshiWebProcessIds)
    if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
        $recordedPid = 0
        if ([int]::TryParse((Get-Content -Raw -LiteralPath $pidPath).Trim(), [ref]$recordedPid)) {
            $recorded = Get-Process -Id $recordedPid -ErrorAction SilentlyContinue
            if ($null -ne $recorded -and $recorded.ProcessName -in @("python", "pythonw")) {
                $identifiers += $recordedPid
            }
        }
    }
    foreach ($identifier in @($identifiers | Sort-Object -Unique)) {
        Stop-Process -Id $identifier -Force -ErrorAction Stop
        Wait-Process -Id $identifier -Timeout 15 -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

function Start-MoshiWeb {
    $port = Get-WebPort
    $bindAddress = Get-WebBindAddress
    Assert-WebConfiguration
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($null -ne $listener) {
        throw "The configured web port already has a listener: $port"
    }

    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
    $timestamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $stdoutPath = Join-Path $runtimeDirectory "web-service-${timestamp}.stdout.log"
    $stderrPath = Join-Path $runtimeDirectory "web-service-${timestamp}.stderr.log"
    $process = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "moshi_data_pipeline.studio.web_main") `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru
    Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ascii

    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($process.HasExited) {
            Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
            throw "The web service exited during startup. Review $stderrPath without sharing secrets."
        }
        if (Test-MoshiHealth -Port $port) {
            [pscustomobject]@{
                PID = $process.Id
                BindAddress = $bindAddress
                Port = $port
                URL = "http://127.0.0.1:${port}"
                StdoutLog = $stdoutPath
                StderrLog = $stderrPath
            } | Format-List
            return
        }
        Start-Sleep -Milliseconds 500
        $process.Refresh()
    }
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    throw "The web service did not become healthy. Review $stderrPath without sharing secrets."
}

function Write-WebStatus {
    $port = Get-WebPort
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    [pscustomobject]@{
        BindAddress = Get-WebBindAddress
        Port = $port
        Healthy = Test-MoshiHealth -Port $port
        PID = if ($null -ne $listener) { $listener.OwningProcess } else { $null }
    } | Format-List
}

try {
    Import-EnvironmentFile -Path $EnvFile -Required
    Import-EnvironmentFile -Path $RuntimeFile
    Import-EnvironmentFile -Path $SecretsFile
    switch ($Action) {
        "Stop" {
            Stop-MoshiWeb
        }
        "Start" {
            Assert-WebConfiguration
            Backup-MoshiCatalogs -WorkspacePaths @((Get-ConfiguredWorkspacePath))
            Start-MoshiWeb
        }
        "Restart" {
            Assert-WebConfiguration
            $workspacePaths = @(Get-MoshiWorkspacePaths)
            $workspacePaths += Get-ConfiguredWorkspacePath
            Stop-MoshiWeb
            Backup-MoshiCatalogs -WorkspacePaths $workspacePaths
            Start-MoshiWeb
        }
        "Status" {
            Write-WebStatus
        }
    }
}
finally {
    Restore-Environment
}
