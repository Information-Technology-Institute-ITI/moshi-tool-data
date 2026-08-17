[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Za-z0-9_.-]{7,128}$")]
    [string]$WebGeneration,
    [Parameter(Mandatory = $true)]
    [string]$Region,
    [Parameter(Mandatory = $true)]
    [string]$WebRepository,
    [string]$Platform = "linux/amd64"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

foreach ($command in @("aws", "docker")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command is required and was not found in PATH."
    }
}
if ($WebGeneration -eq "latest") {
    throw "Use immutable deployment identifiers, not latest."
}

$WebRepository = $WebRepository.TrimEnd("/")
$registry = ($WebRepository -split "/", 2)[0]
aws ecr get-login-password --region $Region |
    docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { throw "ECR login failed for $registry." }

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
$webImage = "${WebRepository}:$WebGeneration"

docker build --pull --platform $Platform --tag $webImage $repoRoot
if ($LASTEXITCODE -ne 0) { throw "Web image build failed." }
docker push $webImage
if ($LASTEXITCODE -ne 0) { throw "Web image push failed." }

Write-Host "Published $webImage"
Write-Host "Configure MOSHI_DEPLOYMENT_GENERATION=$WebGeneration on m8i."
