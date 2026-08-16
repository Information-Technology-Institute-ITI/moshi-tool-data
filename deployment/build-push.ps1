[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Za-z0-9_.-]{7,128}$")]
    [string]$ImageTag,
    [Parameter(Mandatory = $true)]
    [string]$Region,
    [string]$TerraformDirectory = "$PSScriptRoot/terraform"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

foreach ($command in @("aws", "docker", "terraform")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command is required and was not found in PATH."
    }
}
if ($ImageTag -eq "latest") {
    throw "Use an immutable Git SHA or build ID, not latest."
}

$webRepository = terraform "-chdir=$TerraformDirectory" output -raw web_repository_url
if ($LASTEXITCODE -ne 0) { throw "Could not read the web ECR output." }
$processingRepository = terraform "-chdir=$TerraformDirectory" output -raw processing_repository_url
if ($LASTEXITCODE -ne 0) { throw "Could not read the processing ECR output." }
$registry = ($webRepository -split "/", 2)[0]
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { throw "ECR login failed." }

$repoRoot = (Resolve-Path "$PSScriptRoot/..").Path
$webImage = "${webRepository}:$ImageTag"
$processingImage = "${processingRepository}:$ImageTag"

docker build --pull --tag $webImage "$repoRoot/web_service"
if ($LASTEXITCODE -ne 0) { throw "Web image build failed." }
docker push $webImage
if ($LASTEXITCODE -ne 0) { throw "Web image push failed." }

docker build --pull --tag $processingImage "$repoRoot/processing_service"
if ($LASTEXITCODE -ne 0) { throw "Processing image build failed." }
docker push $processingImage
if ($LASTEXITCODE -ne 0) { throw "Processing image push failed." }

Write-Host "Published $webImage"
Write-Host "Published $processingImage"
