# Drama Cover Builder 短剧封面生成 Skill 使用手册

> 版本：Phase 2 最终版 · 2026-10-22  
> 路径：`c:\Users\唐唐\.trae-cn\skills\drama-cover-builder\`  
> 适用：Trae CN（全局 Skill，跨所有项目可用）

---

## 目录

- [1. 功能概览](#1-功能概览)
- [2. 系统要求](#2-系统要求)
- [3. 文件结构](#3-文件结构)
- [4. 核心概念](#4-核心概念)
- [5. 快速上手](#5-快速上手)
- [6. 子命令详解](#6-子命令详解)
- [7. 底图三档方案](#7-底图三档方案)
- [8. 标题生成三方案](#8-标题生成三方案)
- [9. Pillow 四样式](#9-pillow-四样式)
- [10. 批量处理](#10-批量处理)
- [11. 常见问题](#11-常见问题)

---

## 1. 功能概览

一条命令生成短剧封面，覆盖从标题到剪映草稿的完整链路：

```
简介文字 ──→ 三套标题（各 5 条）
                    ↓
         底图（抽帧 / 本地图 / AI 生图）
                    ↓
         排版合成（4 种 Pillow 样式 + HTML 可选）
                    ↓
         写入剪映草稿 draft_cover.jpg
```

---

## 2. 系统要求

### 必需（已安装）

| 组件 | 版本 | 位置 |
|------|------|------|
| Python | 3.12 | `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` |
| Pillow | 13.0 | pip 包 |
| ffmpeg | 9.0.2 | WinGet `D:\AI_Deps\tools\ffmpeg\bin` |
| Ollama qwen2.5:7b-instruct | Q4_K_M 4.7GB | `D:\AI_Deps\Ollama\models`，服务在 11434 端口 |
| 中文字体 | msyhbd | `C:\Windows\Fonts\msyhbd.ttc` |
| torch (CUDA) | 2.14.1+cu126 | pip 包，demucs GPU 加速专用；安装：`pip install --upgrade torch torchaudio --index-url https://download.pytorch.org/whl/cu126` |
| audio-separator | 0.47.1 | pip 包，RoFormer SOTA 人声分离备用方案，模型权重放 `D:\AI_Deps\models\` |

### 可选

| 组件 | 用途 | 安装 |
|------|------|------|
| Playwright + Chromium | HTML+CSS 排版方案 | `pip install playwright && playwright install chromium`（~245MB）|
| volcengine-python-sdk | 付费 AI 生图（豆包 Seedream）| `pip install volcengine-python-sdk` + ARK_API_KEY |

### Windows 上不可用

| 组件 | 原因 |
|------|------|
| Ollama stable-diffusion | Ollama 图像生成仅 macOS MLX，Windows Coming Soon |
| Ollama x/z-image-turbo | 同上，报 `MLX runtime is not available` |

---

## 3. 文件结构

```
drama-cover-builder/
├── SKILL.md                              ← Skill 触发描述（Trae 路由用）
├── scripts/
│   └── drama_cover.py                    ← 主脚本（~580 行，argparse CLI）
└── assets/
    ├── title_prompts/
    │   ├── s1_taiyo.md                   ← 标题方案 1：12 公式（5 类各 1 条）
    │   ├── s2_coppie.md                  ← 标题方案 2：CoppieGPT 风格
    │   └── s3_viral.md                   ← 标题方案 3：22 心理学
    └── compose_styles/
        └── pillow_styles.json            ← 4 样式参数（改这里调视觉，无需改代码）
```

Python 调用入口：
```
%LOCALAPPDATA%\Programs\Python\Python312\python.exe "c:\Users\唐唐\.trae-cn\skills\drama-cover-builder\scripts\drama_cover.py" <command> [args]
```

可设置别名简化（PowerShell profile）：
```powershell
Set-Alias dc "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe c:\Users\唐唐\.trae-cn\skills\drama-cover-builder\scripts\drama_cover.py"
```

---

## 4. 核心概念

### 画布尺寸
所有输出固定 1080×1920 竖屏（剪映短剧标准）。

### 底图优先级（--base-image > --video）
```
full 子命令:
  1) 传了 --base-image → 直接用这张图，跳过抽帧
  2) 传了 --video → ffmpeg select+thumbnail 抽最佳帧
  3) 两个都没传 → exit 1 报错
```

### 全链路 vs 单步
- **full 子命令**：一键跑 title → art → compose-pillow(all) → compose-html(可选)
- **单步子命令**：title / art-from-video / compose-pillow / compose-html / write-draft，按需自由组合

---

## 5. 快速上手

### 场景 A：最快（视频抽帧，零成本）

```bash
python drama_cover.py full ^
  --synopsis "知青为了活下去，带着粮肉和一群敢打敢拼的兄弟，在深山老林里扎了根，却意外惹上了当地地头蛇。" ^
  --video "D:\短剧\80赶山，我带绝美女知青粮肉满仓\第1集.mp4" ^
  --title "知青带粮肉闯山野！" ^
  --out "C:\Users\唐唐\AppData\Local\Temp\cover_test"
```

产出：3 套标题 JSON + best_frame.jpg + 4 样式 jpg + html_cover.png

### 场景 B：用本地设计稿

```bash
python drama_cover.py full ^
  --synopsis "同上" ^
  --base-image "D:\我的设计稿\ep01_cover_v3.jpg" ^
  --title "知青带粮肉闯山野！" ^
  --out "./cover_out"
```

### 场景 C：单步组合（最灵活）

```bash
# Step 1: 只生成标题，挑一个喜欢的
python drama_cover.py title --synopsis "..." --out ./titles/

# Step 2: 抽帧或自己找底图
python drama_cover.py art-from-video --video ep1.mp4 --out ./art/

# Step 3: 只合成 1 个样式
python drama_cover.py compose-pillow --base ./art/best_frame.jpg --title "挑好的标题！" --out ./final.jpg --style classic_red_stroke

# Step 4: 写剪映草稿
python drama_cover.py write-draft --cover ./final.jpg --draft-dir "C:\Users\唐唐\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\80ganshan_ep01"
```

---

## 6. 子命令详解

### 6.1 title（标题生成）

**推荐方式（v2 架构）：Agent 直接生成**——把简介发给 Trae 在线模型（如 Qwen3.8-Max / GLM-5.3），按 `assets/title_prompts/s*.md` 的规范出 3 方案 × 3 版本，质量远超本地 7B，然后直接把选定标题传给 compose-pillow。

```bash
# 离线兜底方式（本地 Ollama qwen2.5:7b，质量有限）
python drama_cover.py title --synopsis "<文字或.txt路径>" --out <输出目录>
```

输出文件：
- `s1_taiyo.json` — 方案 1，3 条（爽文/敘事/傳奇）
- `s2_coppie.json` — 方案 2，3 条
- `s3_viral.json` — 方案 3，3 条
- `summary.json` — 三份汇总

> 7B 模型常见短板：简体混入（脚本 opencc 兜底转换）、本名泄漏、约束遗忘。正式投放建议用 Agent 在线生成。

### 6.2 art-from-video（视频抽帧）

```bash
python drama_cover.py art-from-video --video <mp4> --out <输出目录>
  [--scene-threshold 0.35]    # select 滤镜阈值，越低越严格
  [--thumbnail-batch 500]    # thumbnail 候选帧数
  [--fallback-time 3.0]      # 抽帧失败后回退固定秒数
```

逻辑：
1. `select=gt(scene,0.35)` 过滤静止帧、片头黑场、logo
2. `thumbnail=n=500` 从剩余帧选颜色直方图最接近全片平均的
3. 失败回退固定 3s 抽帧

输出：`best_frame.jpg`

### 6.3 compose-pillow（Pillow 排版）

```bash
python drama_cover.py compose-pillow --base <图片> --title "<标题>" --out <输出>
  [--style classic_red_stroke|shadow_outline|neon_glow|bottom_tape|all]
```

输出：
- style=单个 → 指定 jpg
- style=all → 输出目录下 4 个 jpg

### 6.4 compose-html（HTML+Playwright 排版，可选）

```bash
python drama_cover.py compose-html --base <图片> --title "<标题>" --out <输出>
  [--font-size 96]
```

需要 `pip install playwright && playwright install chromium`。不装也不影响 Pillow 结果。

### 6.5 write-draft（写剪映草稿）

```bash
python drama_cover.py write-draft --cover <封面jpg> --draft-dir <草稿目录>
```

直接覆盖 `<草稿目录>\draft_cover.jpg`。剪映运行时自动刷新缩略图，不用重启剪映。

**草稿目录在哪找**：
```
C:\Users\唐唐\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\<草稿名>\
```

### 6.6 full（一键全链路）

```bash
python drama_cover.py full --synopsis "<简介>"
  [--video <mp4>]       # 视频抽帧
  [--base-image <jpg>]  # 本地图片（优先级高于 --video）
  [--title "<标题>"]     # 不传则合成时用占位
  [--out <目录>]
  [--pillow-style all]
```

必须提供 `--video` 或 `--base-image` 至少一个。

---

## 7. 底图三档方案

### ⭐ 方案 1：本地图片（0 成本，最推荐）
```bash
compose-pillow --base design.jpg
full --base-image design.jpg
```
直接用你自己 P 的、淘宝买的、或之前积累的素材图。

### ⭐ 方案 2：ffmpeg 视频抽帧（0 成本，默认）
```bash
art-from-video --video ep1.mp4
full --video ep1.mp4
```
从视频里自动选最有代表性的帧。适合批量，零积分消耗。

### 方案 3：Trae 内置 GenerateImage（消耗 Trae 月积分）
**Agent 侧调用，脚本不直接调**：

```
run_mcp:
  server: integrated_default
  tool: GenerateImage
  args:
    prompt: "Drama cover base image: 1980s Chinese village, sunset, film grain"
    path: "C:\Users\唐唐\AppData\Local\Temp\cover_test\ai_base"
    image_size: "portrait_16_9"   # 1440x2560
```

生成后文件在 `<path>.jpg`，再走方案 1 合成。

**积分政策**：Trae 每月免费 500 积分，新用户注册一次性 4000。GenerateImage 每次约 5-10 积分。97 集全 AI 生图可能不够，建议挑前 10 集精品化，其余用方案 2。

---

## 8. 标题生成三方案（v2 新規範）

每个方案输出 **3 条**，对应三版本。三方案差异化定位 + 共享同一套三版本定义。

### 三版本定义（v2 定稿：张力 + 爆点 + 80 字上限）

| 版本 | 定义 | 要点 |
|------|------|------|
| **爽文版** | 長句爆點打臉鉤子 | 第一人称「我」（「老子」可选变体，≤2次），**≤80 字**连环爆点，**打脸对象必须锁定样本核心冲突的当事方**（如「逼我加錢的那家人」），禁止泛化成「看不起我的人」，報應式收尾 |
| **敘事版** | 寫實熱血・加爆點鉤子 | **第三人称**（与爽文版区分），处境→觉醒→行动→成果写实铺陈，**必须埋社会压力钩**（全村看笑话）做首尾呼应，**≤80 字** |
| **傳奇版** | 人倫衝突・結局對比・戲劇張力收尾 | 人伦冲突排比（踩我的、笑我的、悔婚的）+ 结局对比（当初 vs 日后），反诘式收尾（「日後又有何臉低頭、悔不當初？」），**≤80 字** |

### 張力手法（強制）

| 手法 | 要求 | 禁用 |
|------|------|------|
| 動詞烈度 | 撕得粉碎/殺進/狂攬/殺成傳奇/一箭放倒 | 撕了/賺了/成了 |
| 數字誇飾 | 三百斤野豬**王**（标杆示例同款） | —— |
| 懸念鉤 | 「沒人知道我覺醒了什麼——XXX只是開胃菜」 | 平铺系统功能 |
| 打臉烈度 | 悔到抬不起頭/連背影都追不上/還敢開口嗎 | 三方案同一收尾 |
| 反差對撞 | 絕境vs翻盤、笑柄vs仰望、窮酸vs傳奇 | 单线无对比 |

> v1 的「傳奇版玄奇懸念」保留为可选变体，适合 A/B 测试。

### 三方案差异化

| 方案 | 侧重 | 灵感来源 |
|------|------|----------|
| **S1 taiyo** | 身份反差 / 数字冲击（30岁保姆竟是首富亲生女、三百斤野猪） | taiyo 12 公式 |
| **S2 coppie** | 情感驱动 + 冲突对比，主角行动果决 | CoppieGPT 232 模板 |
| **S3 viral** | 好奇心缺口 + 社会认同（读者代入被羞辱→逆袭情绪曲线） | Viral 22 心理学 |

### 硬规则（所有方案）
- **≤80 字**（含标点，50-80 区间）
- **全繁體**，一个简体字都不能有（團夥 not 團伙）
- **无本名**：所有人名替换为身份称谓（他/女方/那家人/女知青/穷猎户）
- **不擅自增改剧情**：样本没有的元素不能加，有的要强化（「野豬王」式夸饰允许）
- 纯文字，无 emoji/hashtag/markdown
- 禁用词：你知道吗/来看看/震惊/天哪/最/第一/顶级/唯一/必看/赶紧/你猜到了吗
- Step 0 预处理：先圈人名和核心冲突，再动笔

---

## 9. Pillow 四样式

改 `assets/compose_styles/pillow_styles.json` 调参数，不用改 Python 代码：

| 样式 | 视觉 | 适用 |
|------|------|------|
| **classic_red_stroke** | 8px 红描边 + 白字 + 底部黑蒙版 | 抖音主流，万能 |
| **shadow_outline** | 多层偏移阴影 + 暖白大字 + 深蒙版 | 年代剧/虐恋 |
| **neon_glow** | 粉色光晕描边 + 白字 + 浅蒙版 | 都市/甜宠 |
| **bottom_tape** | 底部红布条 + 黑描边白字 | 爽文/逆袭 |

画布固定 1080×1920，标题宽度上限 0.82（886px），自动换行不超过 3 行。

---

## 10. 批量处理

### 97 集全视频抽帧（零成本）

```powershell
$outDir = "C:\Users\唐唐\AppData\Local\Temp\ganshan_covers"
$videoDir = "D:\短剧\80赶山，我带绝美女知青粮肉满仓"
$draftRoot = "C:\Users\唐唐\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft"

$videos = Get-ChildItem $videoDir -Filter "*.mp4" | Sort-Object Name
foreach ($v in $videos) {
    $epNum = [regex]::Match($v.Name, '(\d+)').Groups[1].Value
    $draftDir = Join-Path $draftRoot "80ganshan_ep$($epNum.PadLeft(2,'0'))"
    $epOut = Join-Path $outDir "ep$($epNum.PadLeft(2,'0'))"

    python drama_cover.py full `
      --synopsis "知青为了活下去，带着粮肉和一群敢打敢拼的兄弟，在深山老林里扎了根，却意外惹上了当地地头蛇。" `
      --video $v.FullName --title "知青带粮肉闯山野！" --out $epOut

    # 自动写草稿
    $cover = Join-Path $epOut "composed\classic_red_stroke.jpg"
    if (Test-Path $cover -and (Test-Path $draftDir)) {
        python drama_cover.py write-draft --cover $cover --draft-dir $draftDir
        Write-Output "ep$epNum done ✓"
    }
}
```

### 推荐策略（成本控制）

| 范围 | 底图方案 | 积分消耗 |
|------|----------|----------|
| 前 10 集（精品） | GenerateImage | ~50-100 积分 |
| 11-97 集（量产） | ffmpeg 抽帧 | 0 |

---

## 11. 常见问题

### Q: Ollama 检测不到怎么办？
脚本会三重检测：`which ollama` + `OLLAMA_HOST` 环境变量 + 11434 端口。只要 Ollama 服务在跑（D:\AI_Deps\Ollama\ollama serve 或已启动 Ollama App）就能识别。可手动验证：
```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/tags | ConvertTo-Json
```

### Q: 中文字体缺字形？
脚本默认 `msyhbd.ttc`（微软雅黑粗体），找不到回退 DejaVuSans（全英文）。候选列表在 `_guess_font()` 里，可扩展。

### Q: ffmpeg 路径问题？
脚本自动注入：
1. `D:\AI_Deps\tools\ffmpeg\bin`（AI_Deps 根目录）
2. WinGet 默认路径
3. PATH 里

### Q: 剪映草稿目录在哪找？
```
C:\Users\唐唐\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\<草稿名>\
```
草稿目录下必须有 `draft_cover.jpg`，write-draft 直接覆盖它。剪映运行时自动刷新，**不用重启剪映**（2026-10-22 实测确认）。

### Q: 为什么不直接用 volcengine SDK AI 生图？
火山方舟 Seedream 5.0 lite 按量付费，Trae 内置 GenerateImage 每月免费 500 积分足够日常用。volcengine SDK 代码保留在 `cmd_art_from_ai` 里，有付费 Key 就能启用。

### Q: 为什么不 pull Ollama stable-diffusion？
Ollama 图像生成是 macOS MLX 独占功能，Windows 和 Linux Coming Soon。`ollama pull stable-diffusion` 报 `file does not exist`，`pull x/z-image-turbo` 报 `MLX runtime is not available`。

### Q: compose-html 报错 BrowserType.launch 失败？
Playwright 浏览器未安装。修：
```powershell
pip install playwright
playwright install chromium
```

### Q: title 子命令输出全是"待 Trae 在线生成"？
Ollama 服务没启动。启动 D:\AI_Deps\Ollama\ollama.exe 或 Ollama App 后重试。

### Q: SKILL.md frontmatter 里 description 有啥规则？
- 不能有 `: `（冒号空格）
- 不能有 `<` `>`
- 不能有 `TODO`
- 要包含做什么 + 什么时候用 + 不什么时候用
- 不超过 200 字符

---

## 修改记录

| 日期 | 版本 | 改动 |
|------|------|------|
| 2026-10-23 | 环境更新 | Ollama 迁移 D:\AI_Deps\Ollama；torch 换 CUDA 版（demucs GPU 加速 42x）；新增 audio-separator 0.47.1 备用；numpy 定版 2.2.4 / pillow 定版 13.0；funasr-onnx、moviepy METADATA 过时 pin 补丁 |
| 2026-10-22 | Phase 2 最终版 | 默认 A1 抽帧 + 本地图支持；GenerateImage 走 Agent MCP；Ollama SD 废弃；volcengine 降可选 |
| 2026-10-22 | Phase 1 修复版 | Ollama 端口三重检测；Seedream model ID 修正（doubao-seedream-5-0-260128）；Ollama SD base64 解码；Playwright 优雅降级 |
| 2026-10-22 | Phase 1 初版 | 全链路跑通，title/art-from-video/compose-pillow/write-draft/full |
