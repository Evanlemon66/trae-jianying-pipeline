<#
.SYNOPSIS
  剪映 pipeline 管理员前置项（最小化提权批处理）

.DESCRIPTION
  只做三件需要管理员权限的事，其余安装仍由 install_deps.ps1 以普通权限完成：
    1. 开启系统长路径 LongPathsEnabled（模型/缓存路径很容易超过 260 字符）；
    2. 缺失时安装 VC++ Redistributable x64（onnxruntime / torch 的运行库依赖）；
    3. 仅在 <DepsRoot>\tools\ffmpeg 与 PATH 都没有 ffmpeg 时，走 winget 安装。

  -DetectOnly：只探测并把结果分成「普通项 / 需要管理员项」，不修改任何东西；
  最后一行输出 ADMIN_NEEDED=0|1，供 Console 决定是否弹 UAC。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File pipeline\install_deps_admin.ps1 -DetectOnly
  powershell -ExecutionPolicy Bypass -File pipeline\install_deps_admin.ps1
#>
param(
    [string]$DepsRoot = "",
    [switch]$DetectOnly
)

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host $m -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Need($m) { Write-Host "  [ADMIN] $m" -ForegroundColor Magenta }

$isAdmin = ([Security.Principal.WindowsPrincipal](
    [Security.Principal.WindowsIdentity]::GetCurrent())
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# 依赖根：-DepsRoot -> JY_DEPS_ROOT -> presets.json deps_root（与主脚本同口径，不写死盘符）
$presetFile = Join-Path $PSScriptRoot "presets.json"
$presetDeps = $null
if (Test-Path $presetFile) {
    try {
        $cfg = Get-Content -Path $presetFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.deps_root) { $presetDeps = [string]$cfg.deps_root }
    } catch { }
}
if (-not $DepsRoot) { $DepsRoot = $env:JY_DEPS_ROOT }
if (-not $DepsRoot) { $DepsRoot = $presetDeps }
if ($DepsRoot) { Ok "DepsRoot: $DepsRoot" } else { Warn "未配置 deps_root（跳过 ffmpeg 目录探测）" }

$needCount = 0

Info "[1/3] 系统长路径支持（LongPathsEnabled）"
$longPathOk = $false
try {
    $fsKey = "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem"
    $lp = (Get-ItemProperty -Path $fsKey -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
    $longPathOk = ($lp -eq 1)
} catch { $longPathOk = $false }
if ($longPathOk) {
    Ok "已生效（LongPathsEnabled=1，无需再次提权）"
} else {
    Need "未开启 → 需要管理员写入 HKLM（重启后对已有进程生效）"
    if ($DetectOnly) {
        $needCount++
    } else {
        if (-not $isAdmin) { throw "设置 LongPathsEnabled 需要管理员权限，请以管理员身份运行本脚本" }
        New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
            -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force | Out-Null
        Ok "已开启 LongPathsEnabled=1（重启后对已有进程生效）"
    }
}

Info "[2/3] VC++ Redistributable x64（onnxruntime / torch 运行库）"
$vcOk = $false
try {
    $vc = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" `
        -ErrorAction SilentlyContinue
    $vcOk = ($vc -and [int]$vc.Installed -eq 1)
} catch { $vcOk = $false }
if (-not $vcOk) {
    $sys32 = Join-Path $env:WINDIR "System32"
    $vcOk = (Test-Path (Join-Path $sys32 "vcruntime140.dll")) -and
            (Test-Path (Join-Path $sys32 "vcruntime140_1.dll"))
}
if ($vcOk) {
    Ok "已安装"
} else {
    Need "未检测到 vcruntime140(_1).dll → 需要安装 VC++ Redistributable x64"
    if ($DetectOnly) {
        $needCount++
    } else {
        if (-not $isAdmin) { throw "安装 VC++ Redistributable 需要管理员权限，请以管理员身份运行本脚本" }
        $installed = $false
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            try {
                winget install --id Microsoft.VCRedist.2015+.x64 -e --silent `
                    --accept-source-agreements --accept-package-agreements | Out-Host
                $installed = ($LASTEXITCODE -eq 0)
            } catch { $installed = $false }
        }
        if (-not $installed) {
            $exe = Join-Path $env:TEMP "vc_redist.x64.exe"
            Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $exe
            $p = Start-Process -FilePath $exe -ArgumentList "/install", "/quiet", "/norestart" -Wait -PassThru
            $installed = ($p.ExitCode -eq 0 -or $p.ExitCode -eq 3010)
        }
        if ($installed) { Ok "VC++ Redistributable 安装完成" } else { Warn "VC++ Redistributable 安装失败，请手动安装" }
    }
}

Info "[3/3] ffmpeg（仅当 <DepsRoot>\tools\ffmpeg 与 PATH 都没有时才安装）"
$ffDir = if ($DepsRoot) { Join-Path $DepsRoot "tools\ffmpeg\bin" } else { "" }
$ffLocal = $ffDir -and (Test-Path (Join-Path $ffDir "ffmpeg.exe"))
$ffPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffLocal) {
    Ok "已有: $(Join-Path $ffDir 'ffmpeg.exe')"
} elseif ($ffPath) {
    Ok "PATH 已有: $($ffPath.Source)"
} else {
    Need "未找到 ffmpeg → 需要 winget 安装（Gyan.FFmpeg）"
    if ($DetectOnly) {
        $needCount++
    } else {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            try {
                winget install --id Gyan.FFmpeg -e --silent `
                    --accept-source-agreements --accept-package-agreements | Out-Host
                Ok "winget 安装完成（新终端生效）"
            } catch { Warn "winget 安装 ffmpeg 失败，主脚本会回退到 zip 解压" }
        } else {
            Warn "未找到 winget，主脚本会回退到 GitHub zip 解压"
        }
    }
}

$need = if ($needCount -gt 0) { 1 } else { 0 }
Write-Host ""
Info "================ 管理员前置项$(if ($DetectOnly) { '（-DetectOnly，未做任何修改）' }) ================"
Write-Host "  需要管理员项: $needCount"
Write-Host "ADMIN_NEEDED=$need"
exit 0
