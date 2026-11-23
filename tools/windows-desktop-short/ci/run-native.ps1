param(
    [Parameter(Mandatory)][string]$CandidateZip,
    [Parameter(Mandatory)][string]$DesktopTools,
    [Parameter(Mandatory)][ValidateSet("worker","early")][string]$Case,
    [Parameter(Mandatory)][string]$Trial
)
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $Trial) { throw 'A fresh trial is required' }
$root = (New-Item -Type Directory -Path $Trial).FullName
$CandidateZip = (Resolve-Path -LiteralPath $CandidateZip).Path
$DesktopTools = (Resolve-Path -LiteralPath $DesktopTools).Path
& python -X utf8 "$PSScriptRoot/check_launcher.py" --candidate $CandidateZip --desktop-tools $DesktopTools
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& python -X utf8 "$PSScriptRoot/test_startup.py" --candidate $CandidateZip --desktop-tools $DesktopTools
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& python -X utf8 "$PSScriptRoot/native_collector_check.py" --case $Case --candidate $CandidateZip --desktop-tools $DesktopTools --scratch "$root/private" --evidence "$root/evidence"
exit $LASTEXITCODE
