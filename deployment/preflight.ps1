[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Region,
    [Parameter(Mandatory = $true)]
    [string]$ProcessingInstanceType,
    [string]$WorkerTokenParameter = "/moshi/worker-token",
    [string]$DispatchTokenParameter = "/moshi/dispatch-token",
    [string]$HfTokenParameter = "",
    [string]$ProcessingAmiParameter = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "AWS CLI v2 is required and was not found in PATH."
}

function Invoke-AwsJson {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $value = & aws @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ($value -join [Environment]::NewLine)
    }
    return ($value -join [Environment]::NewLine) | ConvertFrom-Json
}

$identity = Invoke-AwsJson sts get-caller-identity --region $Region --output json
$offerings = Invoke-AwsJson ec2 describe-instance-type-offerings `
    --region $Region `
    --location-type availability-zone `
    --filters "Name=instance-type,Values=$ProcessingInstanceType" `
    --output json
if ($offerings.InstanceTypeOfferings.Count -eq 0) {
    throw "$ProcessingInstanceType has no advertised Availability Zone in $Region."
}

$instanceType = Invoke-AwsJson ec2 describe-instance-types `
    --region $Region `
    --instance-types $ProcessingInstanceType `
    --output json
$requiredVcpus = [int]$instanceType.InstanceTypes[0].VCpuInfo.DefaultVCpus

$quotaList = Invoke-AwsJson service-quotas list-service-quotas `
    --region $Region `
    --service-code ec2 `
    --output json
$quota = @($quotaList.Quotas | Where-Object {
    $_.QuotaName -like "Running On-Demand G and VT instances*"
}) | Select-Object -First 1
if ($null -eq $quota) {
    throw "The EC2 On-Demand G/VT vCPU quota was not returned in $Region."
}
if ([double]$quota.Value -lt $requiredVcpus) {
    throw "G/VT quota is $($quota.Value) vCPUs; $ProcessingInstanceType needs $requiredVcpus. Request a quota increase first."
}

$ami = Invoke-AwsJson ssm get-parameter `
    --region $Region `
    --name $ProcessingAmiParameter `
    --output json

$workerParameter = Invoke-AwsJson ssm get-parameter `
    --region $Region `
    --name $WorkerTokenParameter `
    --output json
if ($workerParameter.Parameter.Type -ne "SecureString") {
    throw "$WorkerTokenParameter must be an SSM SecureString."
}

$dispatchParameter = Invoke-AwsJson ssm get-parameter `
    --region $Region `
    --name $DispatchTokenParameter `
    --output json
if ($dispatchParameter.Parameter.Type -ne "SecureString") {
    throw "$DispatchTokenParameter must be an SSM SecureString."
}

if ($HfTokenParameter) {
    $hfParameter = Invoke-AwsJson ssm get-parameter `
        --region $Region `
        --name $HfTokenParameter `
        --output json
    if ($hfParameter.Parameter.Type -ne "SecureString") {
        throw "$HfTokenParameter must be an SSM SecureString."
    }
}

[pscustomobject]@{
    AccountId = $identity.Account
    Region = $Region
    InstanceType = $ProcessingInstanceType
    RequiredVcpus = $requiredVcpus
    QuotaVcpus = $quota.Value
    AvailabilityZones = ($offerings.InstanceTypeOfferings.Location -join ", ")
    ProcessingAmi = $ami.Parameter.Value
    WorkerToken = "SecureString present"
    DispatchToken = "SecureString present"
    HfToken = if ($HfTokenParameter) { "SecureString present" } else { "not configured" }
} | Format-List

