#Requires -Version 5.1
<#
.SYNOPSIS
  剪映 pipeline 一键依赖安装（Windows / PowerShell 5.1+）

.DESCRIPTION
  以本机已验证环境为参考基线，但安装时按当前机器状态自动选择最优组件：
    - NVIDIA GPU 且驱动 >=528  -> torch/torchaudio CUDA 版 (cu126)，demucs GPU 加速 (~42x)
      驱动 460~527               -> torch cu118；无 NVIDIA / 更旧驱动 -> torch CPU 版
    - Ollama 翻译模型分级: 显存>=20G 或 内存>=64G -> qwen3:32b；显存>=12G 或 内存>=32G -> qwen3:14b
      显存>=7G 或 内存>=24G -> qwen3:8b；内存>=12G -> qwen3:4b；更低 -> qwen3:1.7b
    - whisper 兜底模型分级: 显存>=12G -> large-v3-turbo；>=6G -> medium；>=3G -> small；
      >=1.5G -> base；纯 CPU 内存>=16G -> small(int8)；>=8G -> base；更低 -> tiny
    - ASR 链路固定: 云端(videocaptioner) -> 本地 Paraformer(int8, CPU) -> faster-whisper(分级)
  所有工具/模型/缓存统一放 -DepsRoot（默认 D:\AI_Deps），并写入用户级环境变量：
    HF_HUB_CACHE / TORCH_HOME / OLLAMA_MODELS / PATH(ffmpeg) / pip cache-dir
  每次运行都会在 pipeline\_env_profile.json 写入机器画像，pipeline 运行时据此选择
  whisper 模型/设备；-DetectOnly 只探测+写画像，不安装任何组件。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File pipeline\install_deps.ps1
  powershell -ExecutionPolicy Bypass -File pipeline\install_deps.ps1 -DetectOnly
  powershell -ExecutionPolicy Bypass -File pipeline\install_deps.ps1 -TorchChannel cpu -SkipOllama
#>
param(
    [string]$DepsRoot = "D:\AI_Deps",
    [ValidateSet("auto","cu126","cu118","cpu")] [string]$TorchChannel = "auto",
    [string]$OllamaModel = "auto",            # auto / qwen3:8b / qwen3:14b / ... / none
    [ValidateSet("auto","tiny","base","small","medium","large-v3-turbo")]
    [string]$WhisperModel = "auto",
    [string]$PipIndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",  # 留空则用官方源
    [switch]$DetectOnly,                       # 只做硬件探测+写画像，不安装
    [switch]$SkipOllama,
    [switch]$SkipModels,
    [switch]$SkipPlaywrightBrowser,
    [switch]$SkipWhisperWarmup
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # IWR 大文件下载提速
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Info($m)  { Write-Host $m -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Fail($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red }
function Step($n,$t,$m) { Write-Host ""; Info "[$n/$t] $m" }

$TOTAL = 10
$summary = [System.Collections.Generic.List[string]]::new()

# ============================================================
Step 1 $TOTAL "环境探测（Python / GPU / 内存 / 磁盘）"
# ============================================================

# --- Python：优先 memory 固定路径 -> PATH -> py launcher -> winget 安装 ---
$Py = $null; $PyPre = @()
$pyCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")
)
foreach ($c in $pyCandidates) { if (Test-Path $c) { $Py = $c; break } }
if (-not $Py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source }
}
if (-not $Py) {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source; $PyPre = @("-3.12") }
}
if (-not $Py) {
    Warn "未找到 Python，尝试用 winget 安装 Python 3.12 ..."
    winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    $Py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (-not (Test-Path $Py)) { throw "Python 安装失败，请手动安装 Python 3.12 后重跑本脚本" }
    Warn "Python 已安装，请重新打开终端后再运行本脚本（PATH 需刷新）"
}
$pyVer = & $Py @PyPre --version 2>&1
Ok "Python: $pyVer ($Py)"
$summary += "Python: $pyVer"

# --- GPU 探测 ---
$gpuName = ""; $gpuDriver = 0; $gpuVramGB = 0
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    try {
        $gpuName   = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1).Trim()
        $gpuDriver = [double]((& nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>$null | Select-Object -First 1).Trim())
        $gpuVramGB = [math]::Round((& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1) / 1024, 1)
    } catch {}
}
if ($gpuName) { Ok "GPU: $gpuName (driver $gpuDriver, VRAM ${gpuVramGB}GB)" } else { Warn "未检测到 NVIDIA GPU，torch 将使用 CPU 版" }
$summary += "GPU: $(if ($gpuName) { "$gpuName / driver $gpuDriver / ${gpuVramGB}GB" } else { 'none' })"

# --- 内存 ---
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
Ok "RAM: ${ramGB}GB"
$summary += "RAM: ${ramGB}GB"

# --- 依赖根目录（D 盘不存在时回退 C 盘）---
if ($DepsRoot -like "D:\*" -and -not (Test-Path "D:\")) {
    Warn "本机没有 D 盘，DepsRoot 回退为 C:\AI_Deps"
    $DepsRoot = "C:\AI_Deps"
}
if ($DepsRoot -ne "D:\AI_Deps") {
    Warn "DepsRoot=$DepsRoot 不是 D:\AI_Deps：pipeline.py 中 PARAFORMER_DIR 等常量硬编码 D:\AI_Deps，装完需同步修改或建目录联接"
}
Ok "DepsRoot: $DepsRoot"
$summary += "DepsRoot: $DepsRoot"

# --- torch channel 决策 ---
if ($TorchChannel -eq "auto") {
    if     (-not $gpuName)      { $TorchChannel = "cpu" }
    elseif ($gpuDriver -ge 528) { $TorchChannel = "cu126" }
    elseif ($gpuDriver -ge 460) { $TorchChannel = "cu118" }
    else                        { $TorchChannel = "cpu" }
}
Ok "Torch channel: $TorchChannel"
$summary += "Torch: $TorchChannel"

# --- Ollama 模型决策（翻译兜底，分级） ---
if ($OllamaModel -eq "auto") {
    if     ($gpuVramGB -ge 20 -or $ramGB -ge 64) { $OllamaModel = "qwen3:32b" }
    elseif ($gpuVramGB -ge 12 -or $ramGB -ge 32) { $OllamaModel = "qwen3:14b" }
    elseif ($gpuVramGB -ge 7  -or $ramGB -ge 24) { $OllamaModel = "qwen3:8b" }
    elseif ($ramGB -ge 12)                       { $OllamaModel = "qwen3:4b" }
    else                                         { $OllamaModel = "qwen3:1.7b" }
}
if (-not $SkipOllama -and $OllamaModel -ne "none") { Ok "Ollama model: $OllamaModel" }
$summary += "Ollama model: $(if ($SkipOllama) {'skipped'} else {$OllamaModel})"

# --- whisper 兜底模型决策（分级；pipeline 运行时从 _env_profile.json 读取） ---
$WhisperDevice = "cpu"; $WhisperCompute = "int8"
if ($WhisperModel -eq "auto") {
    if ($gpuName) {
        if     ($gpuVramGB -ge 12)  { $WhisperModel = "large-v3-turbo" }
        elseif ($gpuVramGB -ge 6)   { $WhisperModel = "medium" }
        elseif ($gpuVramGB -ge 3)   { $WhisperModel = "small" }
        elseif ($gpuVramGB -ge 1.5) { $WhisperModel = "base" }
        else                        { $WhisperModel = "tiny" }
    } else {
        if     ($ramGB -ge 16) { $WhisperModel = "small" }
        elseif ($ramGB -ge 8)  { $WhisperModel = "base" }
        else                   { $WhisperModel = "tiny" }
    }
}
if ($gpuName -and $TorchChannel -ne "cpu") { $WhisperDevice = "cuda"; $WhisperCompute = "float16" }
Ok "Whisper fallback: $WhisperModel ($WhisperDevice/$WhisperCompute)"
$summary += "Whisper: $WhisperModel ($WhisperDevice/$WhisperCompute)"

# --- 写机器画像 pipeline\_env_profile.json（pipeline.py 运行时消费；全组件注册表） ---
$profilePath = Join-Path $PSScriptRoot "_env_profile.json"
$profile = [ordered]@{
    generated_at   = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    deps_root      = $DepsRoot
    python         = "$pyVer"
    gpu            = @{ name = $gpuName; driver = $gpuDriver; vram_gb = $gpuVramGB }
    ram_gb         = $ramGB
    torch_channel  = $TorchChannel
    whisper        = @{ model = $WhisperModel; device = $WhisperDevice; compute_type = $WhisperCompute
                        options = @("tiny","base","small","medium","large-v3-turbo") }
    ollama_model   = $(if ($SkipOllama) { "none" } else { $OllamaModel })
    paraformer     = @{ quantize = $true   # int8；fp32(model.onnx) 需实测验证后才开放，本机不做
                        model_dir = Join-Path $DepsRoot "models\paraformer\iic_onnx"
                        vad_dir   = Join-Path $DepsRoot "models\paraformer\vad_onnx" }
    demucs         = @{ model = "htdemucs_ft"; fallback = "htdemucs"
                        options = @("htdemucs_ft","htdemucs","htdemucs_6s") }
    translate      = @{ glm_model = "glm-4-flash"; xfyun_model = "lite"
                        ollama_model = $(if ($SkipOllama) { "none" } else { $OllamaModel })
                        ollama_options = @("qwen3:1.7b","qwen3:4b","qwen3:8b","qwen3:14b","qwen3:32b") }
    tts            = @{ backend = "sami"; fallback = "edge"; speaker = "zh_male_huoli"
                        note = "云端服务，与硬件无关；speaker 见 scripts/universal_tts.py" }
    asr_chain      = @("jianying-cloud", "paraformer", "faster-whisper")
    translate_chain = @("glm-4-flash", "spark-lite", "ollama")
}
($profile | ConvertTo-Json -Depth 5) | Out-File -FilePath $profilePath -Encoding utf8
Ok "机器画像已写入: $profilePath"

if ($DetectOnly) {
    Write-Host ""
    Info "================ 探测摘要（-DetectOnly，未安装任何组件）================"
    $summary | ForEach-Object { Write-Host "  $_" }
    exit 0
}

# ============================================================
Step 2 $TOTAL "创建统一目录 + 用户级环境变量"
# ============================================================
$dirs = @{
    tools    = Join-Path $DepsRoot "tools\ffmpeg\bin"
    hfhub    = Join-Path $DepsRoot "models\huggingface\hub"
    torch    = Join-Path $DepsRoot "models\torch"
    para     = Join-Path $DepsRoot "models\paraformer\iic_onnx"
    vad      = Join-Path $DepsRoot "models\paraformer\vad_onnx"
    ollama   = Join-Path $DepsRoot "Ollama\models"
    pipcache = Join-Path $DepsRoot "pip_cache"
}
foreach ($d in $dirs.Values) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
Ok "目录已创建: $DepsRoot\{tools\ffmpeg\bin, models\{huggingface\hub,torch,paraformer}, Ollama\models, pip_cache}"

# 用户级环境变量（已存在且相同则跳过）
$envMap = @{
    HF_HUB_CACHE  = $dirs.hfhub
    TORCH_HOME    = $dirs.torch
    OLLAMA_MODELS = $dirs.ollama
}
foreach ($k in $envMap.Keys) {
    $cur = [Environment]::GetEnvironmentVariable($k, "User")
    if ($cur -ne $envMap[$k]) {
        [Environment]::SetEnvironmentVariable($k, $envMap[$k], "User")
        Ok "设置用户环境变量 $k = $($envMap[$k])"
    } else { Ok "$k 已是目标值" }
    Set-Item "env:$k" $envMap[$k]   # 当前会话立即生效
}

# pip 全局缓存
& $Py @PyPre -m pip config set global.cache-dir $dirs.pipcache | Out-Null
Ok "pip cache-dir -> $($dirs.pipcache)"

# ============================================================
Step 3 $TOTAL "安装 torch / torchaudio（$TorchChannel）"
# ============================================================
$pipIdx = @()
if ($PipIndexUrl) { $pipIdx = @("-i", $PipIndexUrl) }
if ($TorchChannel -eq "cpu") {
    & $Py @PyPre -m pip install --upgrade torch torchaudio @pipIdx
} else {
    & $Py @PyPre -m pip install --upgrade torch torchaudio --index-url "https://download.pytorch.org/whl/$TorchChannel"
}
if ($LASTEXITCODE -ne 0) { throw "torch 安装失败" }
& $Py @PyPre -c "import torch; print('  torch', torch.__version__, '| cuda:', torch.cuda.is_available())"

# ============================================================
Step 4 $TOTAL "安装 requirements.txt 全部 Python 包"
# ============================================================
$reqFile = Join-Path $PSScriptRoot "..\requirements.txt"
& $Py @PyPre -m pip install -r $reqFile @pipIdx
if ($LASTEXITCODE -ne 0) { throw "requirements.txt 安装失败" }
Ok "requirements.txt 安装完成"

# ============================================================
Step 5 $TOTAL "funasr-onnx 元数据补丁（numpy pin 过时会引发 pip 纠正性降级）"
# ============================================================
# 必须用 Python 以 utf-8 读写，禁止 PowerShell 重写 METADATA（BOM 会被破坏导致 pip show 崩溃）
$patchCode = @'
import importlib.metadata, pathlib, sys
dist = None
for d in importlib.metadata.distributions():
    if (d.metadata.get("Name") or "").lower() == "funasr-onnx":
        dist = d; break
if dist is None:
    print("funasr-onnx 未安装，跳过补丁"); sys.exit(0)
meta = pathlib.Path(dist._path) / "METADATA"
text = meta.read_text(encoding="utf-8")
old_pins = ["Requires-Dist: numpy<=1.26.4", "Requires-Dist: numpy (<=1.26.4)"]
patched = False
for pin in old_pins:
    if pin in text:
        text = text.replace(pin, "Requires-Dist: numpy (>=2.0,<2.5)")
        patched = True
if patched:
    meta.write_text(text, encoding="utf-8")
    print(f"已打补丁: {meta}")
else:
    print("无需补丁（pin 已是新值或格式不同）: " + str(meta))
'@
$patchCode | & $Py @PyPre -

# ============================================================
Step 6 $TOTAL "Playwright Chromium 浏览器（web_recorder 用）"
# ============================================================
if ($SkipPlaywrightBrowser) {
    Warn "跳过（-SkipPlaywrightBrowser）"
} else {
    & $Py @PyPre -m playwright install chromium
    if ($LASTEXITCODE -eq 0) { Ok "chromium 就绪" } else { Warn "chromium 安装失败，可稍后手动: python -m playwright install chromium" }
}

# ============================================================
Step 7 $TOTAL "FFmpeg（优先 $DepsRoot\tools\ffmpeg -> PATH/WinGet -> winget 安装）"
# ============================================================
$ffDir  = $dirs.tools
$ffExe  = Join-Path $ffDir "ffmpeg.exe"
$ffmpegReady = $false
if (Test-Path $ffExe) {
    Ok "已存在: $ffExe"
    $ffmpegReady = $true
} else {
    $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($cmd) {
        Ok "PATH 已有 ffmpeg: $($cmd.Source)（pipeline 可定位，未复制到 $DepsRoot）"
        $ffmpegReady = $true
    } else {
        Warn "未找到 ffmpeg，尝试 winget 安装 Gyan.FFmpeg ..."
        winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
        $cmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
        if ($cmd) { $ffmpegReady = $true; Ok "winget 安装完成: $($cmd.Source)" }
    }
}
if (-not $ffmpegReady) {
    Warn "winget 不可用，回退 BtbN 直链下载到 $ffDir"
    $zip = Join-Path $env:TEMP "ffmpeg-btbn.zip"
    Invoke-WebRequest -Uri "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $zip
    $extractTo = Join-Path $env:TEMP "ffmpeg-btbn"
    if (Test-Path $extractTo) { Remove-Item $extractTo -Recurse -Force }
    tar -xf $zip -C $env:TEMP
    $inner = Get-ChildItem $env:TEMP -Directory -Filter "ffmpeg-master-latest-win64-gpl" | Select-Object -First 1
    Copy-Item (Join-Path $inner.FullName "bin\*") $ffDir -Force
    Remove-Item $zip, $inner.FullName -Recurse -Force -ErrorAction SilentlyContinue
    Ok "ffmpeg 已解压到 $ffDir"
    $ffmpegReady = $true
}
# PATH 追加（用户级，防重复）
if (Test-Path $ffExe) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ";") -notcontains $ffDir) {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$ffDir", "User")
        Ok "用户 PATH 追加: $ffDir"
    }
    $env:Path = "$env:Path;$ffDir"
}
$summary += "ffmpeg: $ffmpegReady"

# ============================================================
Step 8 $TOTAL "Ollama + 本地翻译兜底模型（$OllamaModel）"
# ============================================================
if ($SkipOllama -or $OllamaModel -eq "none") {
    Warn "跳过 Ollama（翻译兜底链将缺少本地一层）"
} else {
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Warn "未找到 ollama，尝试 winget 安装 ..."
        winget install Ollama.Ollama --accept-source-agreements --accept-package-agreements
        $ollama = Get-Command ollama -ErrorAction SilentlyContinue
        if (-not $ollama) {
            $defaultOllama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
            if (Test-Path $defaultOllama) { $ollama = @{ Source = $defaultOllama } }
        }
    }
    if (-not $ollama) {
        Fail "Ollama 安装失败，请手动安装 https://ollama.com/download 后重跑本步骤"
    } else {
        Ok "ollama: $($ollama.Source)"
        # 确保 serve 已启动
        $running = $false
        try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 3 | Out-Null; $running = $true } catch {}
        if (-not $running) {
            Start-Process -FilePath $ollama.Source -ArgumentList "serve" -WindowStyle Hidden
            Start-Sleep -Seconds 5
        }
        $hasModel = (& $ollama.Source list 2>$null | Select-String -SimpleMatch $OllamaModel) -ne $null
        if ($hasModel) { Ok "模型已存在: $OllamaModel" }
        else {
            Info "  拉取模型 $OllamaModel（可能数 GB，请耐心等待）..."
            & $ollama.Source pull $OllamaModel
            if ($LASTEXITCODE -eq 0) { Ok "模型就绪: $OllamaModel" } else { Fail "模型拉取失败: $OllamaModel" }
        }
        # 同步 vc_config.json 的 ollama.model（Python utf-8 读写，保留 api_key 等其它字段）
        $vcCfg = Join-Path $PSScriptRoot "vc_config.json"
        if (Test-Path $vcCfg) {
            @'
import json, sys
cfg_path, model = sys.argv[1], sys.argv[2]
cfg = json.load(open(cfg_path, encoding="utf-8"))
old = cfg.setdefault("ollama", {}).get("model")
cfg["ollama"]["enabled"] = True
cfg["ollama"]["model"] = model
json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"  vc_config.json ollama.model: {old} -> {model}")
'@ | & $Py @PyPre - $vcCfg $OllamaModel
        } else {
            Warn "vc_config.json 不存在（gitignore 文件），请参照仓库示例创建并填 GLM API key，ollama.model 设为 $OllamaModel"
        }
    }
}
$summary += "Ollama: $(if ($SkipOllama -or $OllamaModel -eq 'none') {'skipped'} else {$OllamaModel})"

# ============================================================
Step 9 $TOTAL "模型权重下载（Paraformer + demucs + whisper）"
# ============================================================
function Save-ModelFile($url, $dst, $minKB = 1) {
    if ((Test-Path $dst) -and ((Get-Item $dst).Length -gt ($minKB * 1KB))) { Ok "已存在: $(Split-Path $dst -Leaf)"; return $true }
    for ($i = 1; $i -le 3; $i++) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing
            if ((Get-Item $dst).Length -gt ($minKB * 1KB)) { Ok "下载完成: $(Split-Path $dst -Leaf)"; return $true }
        } catch { Warn "下载失败($i/3): $url -> $($_.Exception.Message)" }
    }
    Fail "下载失败: $url"; return $false
}

if ($SkipModels) {
    Warn "跳过模型下载（-SkipModels）"
} else {
    # --- Paraformer int8 + Fsmn VAD（ModelScope 国内 CDN）---
    $msBase = "https://modelscope.cn/models"
    $paraRepo = "$msBase/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx/resolve/master"
    $vadRepo  = "$msBase/iic/speech_fsmn_vad_zh-cn-16k-common-onnx/resolve/master"

    Info "  Paraformer int8 ASR -> $($dirs.para)"
    Save-ModelFile "$paraRepo/model_quant.onnx" (Join-Path $dirs.para "model_quant.onnx") 100000 | Out-Null
    Save-ModelFile "$paraRepo/am.mvn"           (Join-Path $dirs.para "am.mvn") 1 | Out-Null
    Save-ModelFile "$paraRepo/config.yaml"      (Join-Path $dirs.para "config.yaml") 1 | Out-Null
    # tokens：优先 tokens.txt，失败则下 tokens.json 并用 Python 转成 "token id" 每行
    $tokensTxt = Join-Path $dirs.para "tokens.txt"
    if (-not (Save-ModelFile "$paraRepo/tokens.txt" $tokensTxt 1)) {
        $tokensJson = Join-Path $dirs.para "tokens.json"
        if (Save-ModelFile "$paraRepo/tokens.json" $tokensJson 1) {
            @'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
tokens = json.load(open(src, encoding="utf-8"))
with open(dst, "w", encoding="utf-8") as f:
    for i, t in enumerate(tokens):
        f.write(f"{t} {i}\n")
print(f"tokens.json -> tokens.txt ({len(tokens)} 行)")
'@ | & $Py @PyPre - $tokensJson $tokensTxt
        }
    }

    Info "  Fsmn VAD -> $($dirs.vad)"
    Save-ModelFile "$vadRepo/model_quant.onnx" (Join-Path $dirs.vad "model_quant.onnx") 100 | Out-Null
    Save-ModelFile "$vadRepo/am.mvn"           (Join-Path $dirs.vad "am.mvn") 1 | Out-Null
    Save-ModelFile "$vadRepo/config.yaml"      (Join-Path $dirs.vad "config.yaml") 1 | Out-Null

    # --- demucs 权重预下载（htdemucs_ft 主 + htdemucs 回退，存 TORCH_HOME）---
    Info "  demucs 权重预下载（htdemucs_ft + htdemucs）"
    & $Py @PyPre -c "from demucs.pretrained import get_model; get_model('htdemucs_ft'); get_model('htdemucs'); print('  demucs 权重就绪')"
    if ($LASTEXITCODE -ne 0) { Warn "demucs 预下载失败，首次运行时会自动重试" }

    # --- faster-whisper 分级模型预热（最终兜底，存 HF_HUB_CACHE；型号来自 Step 1 决策）---
    if ($SkipWhisperWarmup) {
        Warn "跳过 whisper 预热（-SkipWhisperWarmup）"
    } else {
        Info "  faster-whisper $WhisperModel 预热 ($WhisperDevice/$WhisperCompute)"
        & $Py @PyPre -c "from faster_whisper import WhisperModel; WhisperModel('$WhisperModel', device='$WhisperDevice', compute_type='$WhisperCompute'); print('  whisper $WhisperModel 就绪')"
        if ($LASTEXITCODE -ne 0) { Warn "whisper 预热失败，首次运行时会自动下载（GPU 加载失败时 pipeline 自动回退 CPU）" }
    }
}
$summary += "Models: $(if ($SkipModels) {'skipped'} else {"paraformer+vad / demucs / whisper $WhisperModel"})"

# ============================================================
Step 10 $TOTAL "验证汇总"
# ============================================================
$checkCode = @'
import importlib.util, shutil, os
mods = ["torch", "demucs", "faster_whisper", "funasr_onnx", "videocaptioner",
        "audio_separator", "numpy", "PIL", "uiautomation", "playwright", "psutil",
        "requests", "cv2", "imageio", "websockets", "edge_tts", "pymediainfo", "pynput"]
bad = 0
for m in mods:
    ok = importlib.util.find_spec(m) is not None
    print(("  [OK] " if ok else "  [MISS] ") + m)
    bad += 0 if ok else 1
for exe in ["ffmpeg", "ffprobe"]:
    p = shutil.which(exe)
    print(("  [OK] " if p else "  [MISS] ") + exe + (f" -> {p}" if p else "（重开终端后生效）"))
    if not p: bad += 1
import torch
print(f"  torch.cuda.is_available() = {torch.cuda.is_available()}")
para = os.environ.get("PARA_HOME", r"D:\AI_Deps\models\paraformer")
for rel in [r"iic_onnx\model_quant.onnx", r"iic_onnx\tokens.txt", r"iic_onnx\am.mvn", r"iic_onnx\config.yaml",
            r"vad_onnx\model_quant.onnx", r"vad_onnx\am.mvn", r"vad_onnx\config.yaml"]:
    p = os.path.join(para, rel)
    print(("  [OK] " if os.path.exists(p) else "  [MISS] ") + p)
# --- 画像声明组件校验 ---
import json, glob, subprocess
prof_path = os.environ.get("PROFILE_PATH", "_env_profile.json")
try:
    prof = json.load(open(prof_path, encoding="utf-8-sig"))
except Exception:
    prof = {}
    print("  [WARN] _env_profile.json 缺失，跳过画像组件校验")
if prof:
    ckpts = glob.glob(os.path.join(torch.hub.get_dir(), "checkpoints", "*.th"))
    ok = len(ckpts) >= 1
    print(("  [OK] " if ok else "  [MISS] ") + f"demucs 权重缓存 ({len(ckpts)} 个文件, {torch.hub.get_dir()})")
    bad += 0 if ok else 1
    wm = (prof.get("whisper") or {}).get("model", "base")
    hf = os.environ.get("HF_HUB_CACHE", r"D:\AI_Deps\models\huggingface\hub")
    wdir = os.path.join(hf, f"models--Systran--faster-whisper-{wm}")
    ok = os.path.isdir(wdir)
    print(("  [OK] " if ok else "  [WARN] ") + f"whisper {wm} 缓存" + ("" if ok else "（未预热，首次兜底时自动下载）"))
    om = (prof.get("translate") or {}).get("ollama_model", "none")
    if om and om != "none":
        try:
            out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10).stdout
            ok = om in out
        except Exception:
            ok = False
        print(("  [OK] " if ok else "  [MISS] ") + f"ollama 模型 {om}")
        bad += 0 if ok else 1
print(f"EXIT_BAD={bad}")
'@
$env:PARA_HOME = Join-Path $DepsRoot "models\paraformer"
$env:PROFILE_PATH = $profilePath
$checkOut = $checkCode | & $Py @PyPre -
$checkOut | ForEach-Object { Write-Host $_ }
$badCount = 0
if ($checkOut -match "EXIT_BAD=(\d+)") { $badCount = [int]$Matches[1] }

Write-Host ""
Info "================ 安装摘要 ================"
$summary | ForEach-Object { Write-Host "  $_" }
if ($badCount -gt 0) {
    Warn "有 $badCount 项缺失/未生效（ffmpeg 类可能重开终端后正常），请检查上方 [MISS] 行"
} else {
    Ok "全部依赖就绪"
}
Write-Host ""
Info "后续手工事项："
Write-Host "  1. pipeline\vc_config.json 填入智谱 GLM-4-Flash API key（翻译主链路，免费）: https://open.bigmodel.cn/"
Write-Host "  2. 确认 vc_config.json 的 ollama.model 与本机已拉取模型一致（当前选择: $OllamaModel）"
Write-Host "  3. 剪映 5.9 需位于 skill 约定路径；导出 UI 自动化使用 5.9 版 JianyingPro.exe"
Write-Host ""
Ok "Done!"
