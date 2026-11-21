param(
    [Parameter(Mandatory)][string]$CandidateZip,
    [Parameter(Mandatory)][string]$Trial,
    [ValidateSet('idle','busy')][string]$Mode='idle'
)
$ErrorActionPreference = 'Stop'
$commit = '2409c40b469e0e40bd5c55326df3d5ebb86ca81b'
if ((Get-FileHash -LiteralPath $CandidateZip).Hash -ne 'ff7bf5a0fe76297d4ef6040f01bdd7065474a1591af484aff9e60b2965e059ee') { throw 'Candidate ZIP SHA-256' }
if (Test-Path -LiteralPath $Trial) { throw 'A fresh trial is required' }
$trialDir = (New-Item -ItemType Directory -Path $Trial).FullName
$out = (New-Item -ItemType Directory -Path "$trialDir/evidence").FullName
Expand-Archive -LiteralPath $CandidateZip -DestinationPath "$trialDir/candidate package"
$pkg = "$trialDir/candidate package/wfc"
if ((Get-Content -Raw -LiteralPath "$pkg/COMMIT.txt").Trim() -ne $commit) { throw 'Package identity' }
# Resolve only the fixed native Windows distribution referenced by the official JS package.
$base = 'https://registry.npmjs.org/@openai%2fcodex'
$js = Invoke-RestMethod -Uri "$base/0.164.0" -TimeoutSec 30
$nativeSpec = $js.optionalDependencies.'@openai/codex-win32-x64'
if ($nativeSpec -ne 'npm:@openai/codex@0.164.0-win32-x64') { throw "Unexpected native distribution $nativeSpec" }
$native = Invoke-RestMethod -Uri "$base/0.164.0-win32-x64" -TimeoutSec 30
if ($native.name -ne '@openai/codex' -or $native.version -ne '0.164.0-win32-x64' -or
    ([uri]$native.dist.tarball).Host -ne 'registry.npmjs.org' -or ([uri]$native.dist.tarball).Scheme -ne 'https' -or
    $native.dist.integrity -notmatch '^sha512-') { throw 'Fixed native distribution metadata' }
$tgz = "$trialDir/native.tgz"
Invoke-WebRequest -Uri $native.dist.tarball -OutFile $tgz -TimeoutSec 120
$sri = 'sha512-' + [Convert]::ToBase64String([Convert]::FromHexString((Get-FileHash -Algorithm SHA512 -LiteralPath $tgz).Hash))
if ($sri -cne $native.dist.integrity) { throw 'Native npm integrity' }
& tar.exe -xzf $tgz -C $trialDir
if ($LASTEXITCODE) { throw 'Native extraction failed' }
$bins = @(Get-ChildItem -LiteralPath "$trialDir/package/vendor" -Filter codex.exe -Recurse -File |
    Where-Object { $_.FullName -match 'x86_64-pc-windows-msvc' })
if ($bins.Count -ne 1) { throw 'Unique native x64 codex.exe required' }
$exe = $bins[0].FullName
@{ commit=$commit; zip=(Get-FileHash $CandidateZip).Hash; nativeVersion=$native.version; url=$native.dist.tarball;
    integrity=$sri; exeSha256=(Get-FileHash $exe).Hash; mode=$Mode;
    os=[Environment]::OSVersion.VersionString } | ConvertTo-Json | Set-Content -Encoding utf8 "$out/dependencies.json"
& python "$PSScriptRoot/windows_probe.py" --package $pkg --codex $exe --mode $Mode --out "$out/observation"
exit $LASTEXITCODE
