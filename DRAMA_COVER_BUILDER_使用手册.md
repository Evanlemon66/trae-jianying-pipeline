# Drama Cover Builder 短剧封面生成 Skill 使用手册

> 版本：v1.0 · 2026-10-27  
> 路径：`<项目根>/.trae/skills/drama-cover-builder/`  
> 适用：Trae CN / Trae International（项目内 Skill，可被 pipeline / batch_runner / 任意脚本独立消费）

---

## 目录

- [1. 架构概览](#1-架构概览)
- [2. 系统要求](#2-系统要求)
- [3. 文件结构](#3-文件结构)
- [4. 快速上手](#4-快速上手)
- [5. 子命令详解](#5-子命令详解)
- [6. 阶段 1：标题生成](#6-阶段-1标题生成)
- [7. 阶段 2：Pillow 封面合成](#7-阶段-2pillow-封面合成)
- [8. 阶段 3：剪映草稿写入](#8-阶段-3剪映草稿写入)
- [9. 与 pipeline 集成](#9-与-pipeline-集成)
- [10. 常见问题](#10-常见问题)

---

## 1. 架构概览

Skill 是**三阶段独立模块**：

```
                drama-cover-builder skill
           ┌────────────────────────────────┐
           │                                │
  ┌────────▼────────┐  ┌────────────────────▼──────────────┐  ┌──────────────────▼────────┐
  │ 阶段 1 标题生成   │  │ 阶段 2 Pillow 合成                   │  │ 阶段 3 剪映草稿写入          │
  │ (Agent 调 LLM)   │  │ (compose_cover / compose_full_set) │  │ (set_draft_cover + ...)     │
  │                  │  │                                    │  │                            │
  │ 核心能力：       │  │ 核心能力：                           │  │ 辅助能力（主要 pipeline 消费）│
  │ • 读 title-spec.md│ │ • 底图裁切 + 蒙版 + 标题排版          │  │ • 写 draft_cover.jpg        │
  │ • Agent 在线生成 │  │ • 2 尺寸族：竖 1080×1920 / 横 2848×1600│  │ • 写原生 composition 结构    │
  │ • Ollama 兜底    │  │ • 4 样式 × 可选标题                  │  │ • ffmpeg select+thumbnail   │
  │                  │  │ • 一键出 3 横 + 1 竖                 │  │ • 自动探测 / 冲突点抽帧      │
  └──────────────────┘  └────────────────────────────────────┘  └───────────────────────────┘
```

**消费方**：谁都能独立调——Trae 对话直接用、任意脚本 `import drama_cover`、CLI 单独跑。pipeline.py 和 batch_runner.py 是阶段 3 的主要消费者，但阶段 1 和阶段 2 完全不依赖 pipeline。

---

## 2. 系统要求

### 必需

| 组件 | 版本 | 位置 |
|------|------|------|
| Python | 3.12+ | 任意 |
| Pillow | 10+ | `pip install pillow` |
| ffmpeg | 任意 | 三重查找：PATH → `D:\AI_Deps\tools\ffmpeg\bin` → WinGet 路径 |
| 中文字体 | msyhbd | `C:\Windows\Fonts\msyhbd.ttc`（Windows 自带） |

### 可选

| 组件 | 用途 |
|------|------|
| Ollama qwen2.5:7b | 离线标题生成兜底（Agent 在线生成优先） |

> **纯封面场景不需要 torch、demucs、faster-whisper、funasr-onnx、videocaptioner 等大型 AI 包。**

---

## 3. 文件结构

```
.trae/skills/drama-cover-builder/
├── SKILL.md                              ← Skill 入口（能力边界 / 路由 / 契约 / 门禁）
├── scripts/
│   └── drama_cover.py                    ← 主脚本（argparse CLI + Python API）
├── assets/
│   └── compose_styles/
│       └── pillow_styles.json            ← 阶段 2b：4 样式 + 画布参数
└── references/
    ├── title-spec.md                     ← 阶段 1：标题生成唯一执行标准（v4）
    ├── cover-spec.md                     ← 阶段 2：出图硬约束与构图规范
    ├── scorecard.md                      ← 七维评分与矩阵推荐规则
    ├── failure-cases.md                  ← 翻车案例库 + 已验证标题台账
    ├── platform-rules.md                 ← 审核红线 / 平台规格 / 烧字字数档位
    ├── ab-testing.md                     ← 发布侧 A/B 设计（预留，未启用）
    └── archive/v3-spec.md                ← v3 历史规范（仅追溯）
```

### CLI 调用

```powershell
$skillScripts = "<项目根>\.trae\skills\drama-cover-builder\scripts\drama_cover.py"
python $skillScripts --help
```

### Python API 调用

```python
import sys, os
sys.path.insert(0, os.path.join(
    "<项目根>", ".trae", "skills", "drama-cover-builder", "scripts"))
import drama_cover as dc

dc.compose_cover("base.jpg", "out.jpg", title="谁料野猪王竟被他一箭放倒",
                 style="classic_red_stroke", size_family="horizontal")
```

---

## 4. 快速上手

### 场景 A：Trae 对话中直接用（阶段 1+2，最核心）

```
你：帮我给这段短剧简介生成标题
简介：知青为了活下去，带着粮肉和一群敢打敢拼的兄弟...
底图：D:\素材\ganshan_ep01.jpg
```

流程（**阶段 1 → 用户选择 → 阶段 2，中间有强制选择门禁**）：

```
1. Trae 读 `references/title-spec.md` → 按规范生成 12 条标题（3 方案 × 4 版本）
2. 以 T1—T12 平铺编号呈现，停下等用户选择 ← Agent 不代选、不自动往下走
3. 用户每次回一个编号，依次为封面1/2/3 各选 1 条（也可一次给三个），或自主提供 3 条自定义标题
4. 3 条标题按**封面位**映射到 3 张横版封面（与标题所属版本无关）：
   封面1 → classic_red_stroke（爆点构图）
   封面2 → shadow_outline（冷暖对比构图）
   封面3 → neon_glow（守护挡身构图）
5. 调 compose-pillow 合成 3 横 + 1 竖
```

### 场景 B：用户自主给标题（跳过阶段 1）

```
你：直接用这三条标题做封面：
1. 他拿義兄當親哥，義兄卻反手背叛——...
2. 當年遭義兄背叛，他連口氣都沒處發——...
3. 一個趕山的也配號令萬獸？...
底图：D:\素材\ganshan_ep01.jpg
```

Trae 跳过阶段 1，直接进阶段 2 合成。

### 场景 C：CLI 单步组合

```powershell
$skill = ".trae\skills\drama-cover-builder\scripts\drama_cover.py"

# Step 1: 从视频抽最佳帧
python $skill art-from-video --video "D:\短剧\第1集.mp4" --out "best_frame.jpg"

# Step 2: 一键出 4 张封面（3 横 + 1 竖，都不带标题）
python $skill compose-pillow --base "best_frame.jpg" --style all --out "./cover_out"

# Step 3: 给横版加标题（单独指定）
python $skill compose-pillow --base "best_frame.jpg" --title "知青带粮肉闯山野！" --out "./cover_out/hit.jpg" --size horizontal --style classic_red_stroke

# Step 4: 只把竖版写进剪映草稿
python $skill write-draft --cover "./cover_out/cover_vertical.jpg" --draft-dir "C:\Users\<你>\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\80ganshan_ep01"
```

### 场景 C：pipeline 自动消费（阶段 3）

pipeline.py 启动时自动注入 skill 路径，封面来源有三档优先级：

```
1) pipeline 内部调用 skill 的 ffmpeg 抽帧 → set_draft_cover(extract_from_video=...)
2) 用户传了 --cover-image → set_draft_cover(cover_image=...)
3) batch_runner.py 自动探测 → detect_cover_from_dir(series_dir)
```

---

## 5. 子命令详解

### 5.1 title

```powershell
python drama_cover.py title --synopsis "<简介文字或.txt路径>" --out ./titles/
```

**推荐方式**：Agent 读 `references/title-spec.md` 后直接在对话中调在线模型生成。CLI 里的 title 子命令主要输出规范路径指引，离线 Ollama 兜底可后续加。

输出（Agent 在线生成）：3 个 JSON 对象，每个含 `version`（hit/revenge/legend）、`title`、`style_notes`。

### 5.2 art-from-video

```powershell
python drama_cover.py art-from-video --video <mp4> --out best_frame.jpg
  [--scene-threshold 0.35]      # select 场景变化阈值
  [--thumbnail-batch 500]       # thumbnail 候选帧数上限
  [--fallback-time 3.0]         # 失败后回退固定秒数
```

逻辑：
1. `select=gt(scene,0.35)` 过滤静止帧、片头黑场、logo
2. `thumbnail=n=500` 从剩余帧选颜色直方图最接近全片平均的
3. 失败回退固定 3s 抽帧

### 5.3 compose-pillow

```powershell
python drama_cover.py compose-pillow --base <图片>
  [--title "<标题>"]             # 不传则无标题模式
  [--out cover.jpg]              # --style all 时为输出目录
  [--size vertical|horizontal]   # 画布尺寸族，默认 vertical
  [--style classic_red_stroke|shadow_outline|neon_glow|bottom_tape|all]
```

| --size | 画布 | 典型用途 |
|--------|------|----------|
| `vertical` | 1080×1920 | 剪映草稿封面（唯一写进 draft） |
| `horizontal` | 2848×1600 | 平台发布缩略图 |
| `all`（--style all） | 1 竖 + 3 横 | 批量出全套 |

### 5.4 write-draft

```powershell
python drama_cover.py write-draft --cover <封面jpg> --draft-dir <草稿目录>
```

**剪映 5.9 正确做法——两阶段写入**：

```
Step 1: 写 draft_cover.jpg（<草稿目录>\draft_cover.jpg）
Step 2: 写原生 composition 结构（pipeline.py 已验证的正确做法）
  ├── 复制封面图 → Resources/cover/<uuid>.<ext>
  ├── materials.drafts 里建 type=composition 材质，内嵌 photo 视频段
  └── 顶层 draft["cover"] 设为 {cover_draft_id, type:"image", sub_type:"local"}
```

> **只写 draft_cover.jpg 不够**——剪映 5.9 打开草稿时若顶层 cover 字段为 None，会用第一帧覆盖它。

### 5.5 full（一键全链路）

```powershell
python drama_cover.py full
  [--video <mp4>]           # 视频抽帧（--base-image 优先级更高）
  [--base-image <jpg>]      # 本地图片
  [--title "<标题>"]         # 可选，不传则无标题
  [--out ./cover_out]
  [--draft-dir "<草稿目录>"] # 可选，传入后自动执行 write-draft
```

---

## 6. 阶段 1：标题生成

**核心规范文件**：`references/title-spec.md`（项目实战版 v4.1，Agent 读了之后自己在会话中调 LLM）
**配套文件**：`references/scorecard.md`（七维评分）、`references/failure-cases.md`（翻车案例与台账）

**产出结构**：3 方案（S1 taiyo / S2 coppie / S3 viral）× 4 版本 = **12 条标题**

### → 阶段 1 → 阶段 2 衔接（强制用户选择门禁）

阶段 1 产出 12 条，阶段 2 只消费 3 条：

1. 生成 12 条后以 T1—T12 平铺编号呈现，**停下等用户选择**
2. 用户每次回一个编号，依次为封面1/2/3 各选 1 条；也可一次给三个编号，或自主提供 3 条自定义标题
3. Agent **不得代选、不得自动进入阶段 2**
4. 3 条标题按**封面位**映射 3 张横版封面（封面1 → 爆点构图 / 封面2 → 冷暖对比 / 封面3 → 守护挡身），与标题所属版本无关；版4 被选中时沿用对应封面位构图

### 三方案差异化

| 方案 | 策略 | 系列特征 |
|------|------|---------|
| **S1 taiyo** | 身份反差 · 数字冲击 | 重「数字与资产」 |
| **S2 coppie** | 情感驱动 · 冲突对比 | 重「恩怨情感对撞」 |
| **S3 viral** | 好奇缺口 · 社会认同 | 重「群体反应与身份反转」 |

### 四版本定义

#### 版1｜爽文①·先抑後揚·抓馬現場打臉線
開頭踩慘劇/絕境鉤 → 重生/覺醒當場反擊 → 暴富/異能誇張展現 → 現場打臉反派 → 誇張幸福/成就結尾。走即時狗血、當場羞辱路線。

#### 版2｜爽文②·先抑後揚·前世回溯雪恥線
前世/前史慘狀深度回溯 → 開掛翻盤 → 複仇/奪業打臉 → 「連本帶利討回一切/不讓悲劇重演」收尾。與①拉開差異：①重現場即時打臉，②重積累回溯雪恥。

#### 版3｜傳奇版·專屬狠反問·兩套默認範式
**誅心戳背叛款**：緊扣背叛/構陷細節，戳破對方「兄弟/情義」偽裝，直擊道德痛點。
**霸氣守山款**：以主角地位/地盤/實力為核心，踩對方「不配入門/分一杯羹」，凸顯地位碾壓。

#### 版4｜機制版·不绑构图
从八族技法池（数字资产反差 / 关键物件聚焦 / 结局倒叙 / 身份体面反差 / 价值情绪反转 / 群体社会反应 / 悬念信息缺口 / 处境痛点点名）中取 1 族 + 至多 1 个次机制，结尾仍走打脸四式。三格族不重复，字数同 60-80。

### 硬规则（详见 references/title-spec.md）
- **素材边界（最高纪律）**：只用简介原文元素；夸张只允许 5 个白名单维度（异能画面/财富堆砌/贵人态度/社会反应/地位碾压）；禁止编造剧情事件（背叛方式/牢狱/死因/反派下场）
- 繁體字、第三人稱「他」；禁用主角本名，不擅自增改劇情
- 每条 60-80 字（版3 反问句 55-70）；爆点 ≥5 个呈递进链
- 开头用钩子手法（5 种之一），结尾用打脸四式（之一）
- 每条出稿前过 12 项质检清单

---

## 7. 阶段 2：Pillow 封面合成

### 4 样式（改 `assets/compose_styles/pillow_styles.json` 调参数）

| 样式 | 视觉 | 适用 |
|------|------|------|
| **classic_red_stroke** | 8px 红描边 + 白字 + 底部渐变黑蒙版 | 版 1 爽文①·抓马现场打脸 |
| **shadow_outline** | 多层偏移阴影 + 暖白大字 + 深蒙版 | 版 2 爽文②·前世回溯雪耻 |
| **neon_glow** | 粉色光晕描边 + 白字 + 浅蒙版 | 都市/甜宠（横版可选） |
| **bottom_tape** | 底部红布条 + 黑描边白字 | 版 3 传奇版·狠反问 / 竖版默认 |

### 画布参数

```json
{
  "vertical":   { "width": 1080,  "height": 1920 },
  "horizontal": { "width": 2848, "height": 1600 }
}
```

### 自动换行

标题自动换行不超过 3 行，字号按画布宽度比例估算（竖版 W/14、横版 W/10）。

### 无标题模式

`--title` 不传时，底图直接覆盖裁切，不画蒙版也不画标题——适合后期叠字或纯视觉封面。

---

## 8. 阶段 3：剪映草稿写入

### 正确做法（两阶段）

```
写 draft_cover.jpg          ← 让剪映缩略图立刻显示
写原生 composition 结构      ← 防止剪映 5.9 用第一帧覆盖 draft_cover.jpg
```

两个步骤合并在 `set_draft_cover()` + `set_cover_composition()` 里，`write-draft` 子命令自动调用。

### ffmpeg 抽帧（art-from-video / set_draft_cover）

`select=gt(scene,0.35),thumbnail=n=500` 双层筛选——select 先筛掉静止帧，thumbnail 从候选里挑颜色直方图最接近全片平均的，避开片头黑场/模糊帧/logo 帧。

### 辅助能力（其他脚本可消费）

| 函数 | 作用 | 消费者 |
|------|------|--------|
| `detect_cover_from_dir(dir)` | 从剧集目录自动探测封面（封面.png → 目录下第一张图） | batch_runner.py |
| `extract_at_conflict_point(video, conflict_time_s, peak_json_path)` | 从音频冲突点精确抽帧 | save_covers.py / 任意分析脚本 |
| `_cover_start_for_cut(video_path, cut_us)` | 裁点对应的封面分析起点（避免抽到被裁掉的开头） | pipeline.py 裁片流程 |

---

## 9. 与 pipeline 集成

### pipeline.py

```python
# 自动注入 skill 路径并导入（pipeline.py 顶部）
_dcb_scripts = os.path.join(_REPO_ROOT, ".trae", "skills",
                            "drama-cover-builder", "scripts")
sys.path.insert(0, _dcb_scripts)
from drama_cover import (
    set_draft_cover,
    set_cover_composition,
    _cover_start_for_cut,
)
```

pipeline 保留 `gen_uuid()`（被 28+ 非封面调用），skill 内部自己 `import uuid` 定义独立版本，两者互不依赖。

### batch_runner.py

```python
import drama_cover as _dc

# 封面自动探测（原 17 行内联代码 → 1 行 skill 调用）
found = _dc.detect_cover_from_dir(series_dir)
```

### 三种封面来源（pipeline 内部解耦）

```
1) skill 生成  →  用户手动调 compose-pillow → 产物路径传给 pipeline
2) 本地图片   →  --cover-image 参数
3) ffmpeg 抽帧 →  skill 的 set_draft_cover(extract_from_video=...)
```

pipeline 只管消费，不关心来源。

---

## 10. 常见问题

### Q: 为什么有两个 gen_uuid？
pipeline.py 里的 `gen_uuid()` 被创建草稿、轨道 ID、片段 ID、demucs 路径生成等 28+ 处非封面代码调用，不能删。skill 里自己定义一份独立版本，两者互不依赖。

### Q: write-draft 为什么要写原生 composition？
**只覆盖 draft_cover.jpg 不够**——剪映 5.9 打开草稿时若顶层 `cover` 字段为 None，会用视频第一帧覆盖掉 draft_cover.jpg。必须同时建 composition 材质并挂到 `materials.drafts` + 设置顶层 `cover` 字段。

### Q: SKILL.md 放哪？
项目内 `.trae/skills/drama-cover-builder/SKILL.md`（跟 `.trae/skills/jianying-editor/` 平级）。Phase 1 决定理由：两个 skill 都服务这个短剧 pipeline，项目内 scope 路径清晰。

### Q: 离线 Ollama 有用吗？
7B 模型常见短板：简体混入、本名泄漏、约束遗忘。阶段 1 标题生成建议用 Agent 在线调 Trae 模型（Qwen3.8-Max / GLM-5.3），质量远超本地 7B。Ollama 离线兜底可后续加。

### Q: 中文字体缺字形？
默认 `msyhbd.ttc`（微软雅黑粗体），候选列表：msyhbd → msyh → simhei → simsun，都找不到回退 DejaVuSans（全英文）。

### Q: ffmpeg 路径？
三重查找：PATH `which ffmpeg` → `D:\AI_Deps\tools\ffmpeg\bin` → WinGet 路径。

---

## 修改记录

| 日期 | 版本 | 改动 |
|------|------|------|
| 2026-10-27 | v1.0 | 按最新架构重写：skill 作为三阶段独立模块、标题规范换成 v3（版1 爽文①/版2 爽文②/版3 传奇版狠反问）、Pillow 新增横版 2848×1600 尺寸族、write-draft 改为原生 composition 正确做法、pipeline.py / batch_runner.py 已接入 skill |
| 2026-10-22 | Phase 2 最终版 | 旧版（仅为设计蓝图，脚本本体从未落地） |
