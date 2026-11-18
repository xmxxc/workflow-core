param([Parameter(Mandatory=$true)][string]$ProbeKernel, [Parameter(Mandatory=$true)][string]$ProbeOut)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$env:PYTHONUTF8 = '1'
$env:WFC_BUNDLED_TEST_KERNEL = (Resolve-Path $ProbeKernel).Path
New-Item -ItemType Directory -Force $ProbeOut | Out-Null
python tools/probe_bundled_close.py --attempts 12 --out "$ProbeOut/close-source.json" 2>&1 | Tee-Object "$ProbeOut/close-source.log"
$ProbeSourceExit = $LASTEXITCODE
python build.py --windows-dir "$ProbeOut/relocated payload with spaces"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
Copy-Item $env:WFC_BUNDLED_TEST_KERNEL "$ProbeOut/relocated payload with spaces/wfc-kernel.exe"
$env:WFC_TEST_PROGRAM = (Resolve-Path "$ProbeOut/relocated payload with spaces/wfc.pyz").Path
$env:WFC_BUNDLED_TEST_DISCOVERY = '1'
python tools/probe_bundled_close.py --attempts 12 --out "$ProbeOut/close-packaged.json" 2>&1 | Tee-Object "$ProbeOut/close-packaged.log"
$ProbePackageExit = $LASTEXITCODE
@{source_exit=$ProbeSourceExit; packaged_exit=$ProbePackageExit; kernel_sha256=(Get-FileHash $ProbeKernel -Algorithm SHA256).Hash} | ConvertTo-Json | Set-Content "$ProbeOut/result.json"
if ($ProbeSourceExit -or $ProbePackageExit) { exit 1 }
