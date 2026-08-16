[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Za-z0-9_.-]{7,128}$")]
    [string]$WebGeneration,
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$GpuBuildId,
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
if ($WebGeneration -eq "latest" -or $GpuBuildId -eq "latest") {
    throw "Use immutable deployment identifiers, not latest."
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
$webImage = "${WebRepository}:$WebGeneration"
$processingImage = "${ProcessingRepository}:$GpuBuildId"

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
Write-Host "Configure MOSHI_DEPLOYMENT_GENERATION=$WebGeneration on m8i."
Write-Host "Configure MOSHI_GPU_REQUIRED_BUILD_ID=$GpuBuildId on m8i and MOSHI_BUILD_ID=$GpuBuildId on g4dn."
