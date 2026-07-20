[CmdletBinding()]
param(
  [string]$Version = "0.1.0",
  [string]$Iscc = "",
  [switch]$SkipInstaller,
  [switch]$SkipBrowserDownload
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$BuildDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LeadRoot = Split-Path -Parent $BuildDir
$RepoRoot = (Resolve-Path (Join-Path $LeadRoot "..")).Path
$PipelineRoot = Join-Path $RepoRoot "pipeline"
$Staging = Join-Path $RepoRoot "staging\LeadShrimp"
$Release = Join-Path $RepoRoot "release"
$Scanner = Join-Path $RepoRoot "packaging\build\check_release.py"
$Iss = Join-Path $BuildDir "lead_shrimp_public.iss"

function Require-Path([string]$Path) {
  if (-not (Test-Path $Path)) { throw "缺少构建输入: $Path" }
}

function Find-Iscc {
  if ($Iscc) { Require-Path $Iscc; return $Iscc }
  $candidates = @("C:\Program Files (x86)\Inno Setup 6\ISCC.exe", "C:\Program Files\Inno Setup 6\ISCC.exe")
  $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($found) { return $found }
  $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  throw "未找到 ISCC.exe，请安装 Inno Setup 6 或使用 -Iscc 指定路径"
}

foreach ($path in @($LeadRoot, $PipelineRoot, $Scanner, $Iss)) { Require-Path $path }

if (Test-Path $Staging) {
  Remove-Item -Recurse -Force $Staging
}
New-Item -ItemType Directory -Force -Path $Staging | Out-Null

$AppStage = Join-Path $Staging "lead_shrimp"
$PipelineStage = Join-Path $Staging "pipeline"
$RuntimeStage = Join-Path $Staging "python"
New-Item -ItemType Directory -Force -Path $AppStage, $PipelineStage, $RuntimeStage | Out-Null

$PythonExe = (& python -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path $PythonExe)) { throw "无法定位当前 Python 运行时: $PythonExe" }
$PythonRoot = Split-Path -Parent $PythonExe
Copy-Item $PythonExe (Join-Path $RuntimeStage "python.exe") -Force
Copy-Item (Join-Path $PythonRoot "pythonw.exe") (Join-Path $RuntimeStage "pythonw.exe") -Force
Get-ChildItem $PythonRoot -File -Filter "*.dll" | Copy-Item -Destination $RuntimeStage -Force
Copy-Item (Join-Path $PythonRoot "DLLs") (Join-Path $RuntimeStage "DLLs") -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeStage "Lib") | Out-Null
Get-ChildItem (Join-Path $PythonRoot "Lib") | Where-Object { $_.Name -ne "site-packages" } | Copy-Item -Destination (Join-Path $RuntimeStage "Lib") -Recurse -Force
New-Item -ItemType Directory -Force -Path (Join-Path $RuntimeStage "Lib\site-packages") | Out-Null

Get-ChildItem $LeadRoot -File -Filter "*.py" | Copy-Item -Destination $AppStage -Force
Get-ChildItem $PipelineRoot -File -Filter "*.py" | Copy-Item -Destination $PipelineStage -Force
New-Item -ItemType Directory -Force -Path (Join-Path $AppStage "assets") | Out-Null
Copy-Item (Join-Path $LeadRoot "frontend.html") (Join-Path $AppStage "assets\frontend.html") -Force

& $PythonExe -m pip install --disable-pip-version-check --no-compile --no-warn-script-location `
  --target (Join-Path $RuntimeStage "Lib\site-packages") -r (Join-Path $RepoRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败 exit=$LASTEXITCODE" }

# Edge is preferred at runtime, but the public installer must still work on
# machines without Edge. Keep Playwright's Chromium in the installed runtime.
$BrowserStage = Join-Path $RuntimeStage "ms-playwright"
if (-not $SkipBrowserDownload) {
  $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserStage
  & $PythonExe -m playwright install chromium
  if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium 安装失败 exit=$LASTEXITCODE" }
} elseif (Test-Path $BrowserStage) {
  Remove-Item -Recurse -Force $BrowserStage
}

Get-ChildItem $RuntimeStage -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
Get-ChildItem $RuntimeStage -File -Recurse -Filter "*.pyc" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem $RuntimeStage -Directory -Recurse -Filter "tests" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force

if (-not (Test-Path (Join-Path $AppStage "assets"))) {
  New-Item -ItemType Directory -Force -Path (Join-Path $AppStage "assets") | Out-Null
  Copy-Item (Join-Path $LeadRoot "frontend.html") (Join-Path $AppStage "assets\frontend.html") -Force
}

& python $Scanner $Staging
if ($LASTEXITCODE -ne 0) { throw "构建产物安全扫描未通过" }

if ($SkipInstaller) {
  Write-Host "公开版 staging 已生成: $Staging"
  return
}

$Compiler = Find-Iscc
New-Item -ItemType Directory -Force -Path $Release | Out-Null
& $Compiler "/DAppVersion=$Version" "/DStagingDir=$Staging" "/DOutputDir=$Release" $Iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 安装包编译失败 exit=$LASTEXITCODE" }
Write-Host "构建完成: $(Join-Path $Release ("LeadShrimpSetup_" + $Version + ".exe"))"

<#
  The public build intentionally stops at a bundled Python runtime plus Inno Setup.
  No source compiler, account service, or product licensing is included.
#>
<#
foreach ($path in @($Staging, $TempSource, $TempOut)) {
  if (Test-Path $path) { Remove-Item -Recurse -Force $path }
}
New-Item -ItemType Directory -Force -Path $Staging, $TempSource, $TempOut | Out-Null
Copy-Item -Recurse -Force $LeadRoot (Join-Path $TempSource "lead_shrimp")
Copy-Item -Recurse -Force $PipelineRoot (Join-Path $TempSource "pipeline")

$Runtime = Join-Path $TempSource "pipeline\license_runtime.py"
$RuntimeCode = @"
"""Generated unlocked public settings for LeadShrimp."""
LICENSE_ENFORCE = False
LICENSE_SERVER_URL = ""
LICENSE_PUBLIC_KEY = ""
LICENSE_APP_VERSION = "$Version"
LICENSE_PRODUCT_CODE = "lead_shrimp"
ACCOUNT_API_URL = ""
ACCOUNT_PUBLIC_KEY = ""
ACCOUNT_PRODUCT_CODE = ""
UPDATE_PUBLIC_KEY = ""
"@
[System.IO.File]::WriteAllText($Runtime, $RuntimeCode, [System.Text.UTF8Encoding]::new($false))

Push-Location $TempSource
try {
  & python -m nuitka --mode=standalone --assume-yes-for-downloads --zig --low-memory --jobs=1 --lto=no `
    --include-package=pipeline --include-package=lead_shrimp --output-filename=LeadShrimpLauncher.exe `
    --output-dir=$TempOut lead_shrimp\launcher.py
  if ($LASTEXITCODE -ne 0) { throw "Nuitka 编译失败 exit=$LASTEXITCODE" }
} finally { Pop-Location }

$Dist = Get-ChildItem -Path $TempOut -Directory -Filter "*.dist" | Select-Object -First 1
if (-not $Dist) { throw "Nuitka 未生成 standalone 目录" }
Copy-Item -Recurse -Force (Join-Path $Dist.FullName "*") $Staging
New-Item -ItemType Directory -Force -Path (Join-Path $Staging "assets") | Out-Null
Copy-Item (Join-Path $LeadRoot "frontend.html") (Join-Path $Staging "assets\frontend.html") -Force

& python $Scanner $Staging
if ($LASTEXITCODE -ne 0) { throw "构建产物安全扫描未通过" }

if ($SkipInstaller) {
  Write-Host "公开版 staging 已生成: $Staging"
  return
}

$Compiler = Find-Iscc
New-Item -ItemType Directory -Force -Path $Release | Out-Null
& $Compiler "/DAppVersion=$Version" "/DStagingDir=$Staging" "/DOutputDir=$Release" $Iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 安装包编译失败 exit=$LASTEXITCODE" }
Write-Host "构建完成: $(Join-Path $Release ("LeadShrimpSetup_" + $Version + ".exe"))"
#>
