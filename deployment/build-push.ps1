[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Za-z0-9_.-]{7,128}$")]
    [string]$ImageTag,
    [Parameter(Mandatory = $true)]
    [string]$Region,
    [Parameter(Mandatory = $true)]
    [string]$WebRepository,
    [Parameter(Mandatory = $true)]
    [string]$ProcessingRepository,
    [string]$Platform = "linux/amd64"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

foreach ($command in @("aws", "docker")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command is required and was not found in PATH."
    }
}
if ($ImageTag -eq "latest") {
    throw "Use an immutable Git SHA or build ID, not latest."
}

$WebRepository = $WebRepository.TrimEnd("/")
$ProcessingRepository = $ProcessingRepository.TrimEnd("/")
$registries = @(
    ($WebRepository -split "/", 2)[0]
    ($ProcessingRepository -split "/", 2)[0]
) | Sort-Object -Unique
foreach ($registry in $registries) {
    aws ecr get-login-password --region $Region |
        docker login --username AWS --password-stdin $registry
    if ($LASTEXITCODE -ne 0) { throw "ECR login failed for $registry." }
}

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
$webImage = "${WebRepository}:$ImageTag"
$processingImage = "${ProcessingRepository}:$ImageTag"

docker build --pull --platform $Platform --tag $webImage "$repoRoot/web_service"
if ($LASTEXITCODE -ne 0) { throw "Web image build failed." }
docker push $webImage
if ($LASTEXITCODE -ne 0) { throw "Web image push failed." }

docker build --pull --platform $Platform --tag $processingImage "$repoRoot/processing_service"
if ($LASTEXITCODE -ne 0) { throw "Processing image build failed." }
docker push $processingImage
if ($LASTEXITCODE -ne 0) { throw "Processing image push failed." }

Write-Host "Published $webImage"
Write-Host "Published $processingImage"
