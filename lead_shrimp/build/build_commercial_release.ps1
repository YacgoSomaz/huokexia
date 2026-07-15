<#
  获客虾商业构建：业务源码由 Nuitka 编译，安装包只携带授权公钥。
  私钥、管理员令牌、卡密数据库均只存在授权服务器，绝不可传入本脚本。
#>
[CmdletBinding()]
param(
  [string]$Version = "0.1.0",
  [string]$LicenseServerUrl = "https://license.runmo.art",
  [string]$LicensePublicKey = "YYHkNVmcsiWjoYweNOa7CEBP3WGRyBbB6Cf3_qvQchc",
  [string]$Iscc = "",
  [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$BuildDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LeadRoot = Split-Path -Parent $BuildDir
$RepoRoot = (Resolve-Path (Join-Path $LeadRoot "..")).Path
$PipelineRoot = Join-Path $RepoRoot "pipeline"
$Staging = Join-Path $RepoRoot "staging\LeadShrimp"
$TempSource = Join-Path $RepoRoot "staging\_leadshrimp_source"
$TempOut = Join-Path $RepoRoot "staging\_leadshrimp_nuitka"
$Release = Join-Path $RepoRoot "release"
$Scanner = Join-Path $RepoRoot "packaging\build\check_release.py"
$Iss = Join-Path $BuildDir "lead_shrimp.iss"

function Require-Path([string]$Path) { if (-not (Test-Path $Path)) { throw "缺少构建输入: $Path" } }
function Find-Iscc {
  if ($Iscc) { Require-Path $Iscc; return $Iscc }
  $candidates = @("C:\Program Files (x86)\Inno Setup 6\ISCC.exe", "C:\Program Files\Inno Setup 6\ISCC.exe")
  $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($found) { return $found }
  $command = Get-Command iscc.exe -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  throw "未找到 ISCC.exe；请安装 Inno Setup 6 或使用 -Iscc 指定路径"
}

if ($LicenseServerUrl -notmatch '^https://[^/\s]+(?:/[^\s]*)?$') { throw "授权服务必须是 HTTPS 地址" }
if ($LicensePublicKey -notmatch '^[A-Za-z0-9_-]{40,128}$') { throw "授权公钥必须是 Ed25519 base64url 公钥" }
foreach ($path in @($LeadRoot, $PipelineRoot, $Scanner, $Iss)) { Require-Path $path }
& python -m nuitka --version
if ($LASTEXITCODE -ne 0) { throw "未安装 Nuitka。请先运行：python -m pip install Nuitka" }

foreach ($path in @($Staging, $TempSource, $TempOut)) {
  if (Test-Path $path) { Remove-Item -Recurse -Force $path }
}
New-Item -ItemType Directory -Force -Path $Staging, $TempSource, $TempOut | Out-Null
Copy-Item -Recurse -Force $LeadRoot (Join-Path $TempSource "lead_shrimp")
Copy-Item -Recurse -Force $PipelineRoot (Join-Path $TempSource "pipeline")

# Generated only in the temporary compiler input.  The key is public; the
# matching private key is never read by this build process.
$Runtime = Join-Path $TempSource "pipeline\license_runtime.py"
$RuntimeCode = @"
"""Generated commercial card-key settings for LeadShrimp."""
LICENSE_ENFORCE = True
LICENSE_SERVER_URL = "$LicenseServerUrl"
LICENSE_PUBLIC_KEY = "$LicensePublicKey"
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

& python $Scanner $Staging --commercial
if ($LASTEXITCODE -ne 0) { throw "构建产物安全扫描未通过" }

if ($SkipInstaller) {
  Write-Host "商业 staging 已生成: $Staging"
  return
}

$Compiler = Find-Iscc
New-Item -ItemType Directory -Force -Path $Release | Out-Null
& $Compiler "/DAppVersion=$Version" "/DStagingDir=$Staging" "/DOutputDir=$Release" $Iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 安装包编译失败 exit=$LASTEXITCODE" }
Write-Host "构建完成: $(Join-Path $Release ("LeadShrimpSetup_" + $Version + ".exe"))"
