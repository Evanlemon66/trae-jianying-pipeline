---
name: jianying-editor
description: 剪映 (JianYing) AI自动化剪辑的高级封装 API (JyWrapper)。提供开箱即用的 Python 接口，支持录屏、素材导入、字幕生成、Web 动效合成及项目导出。
---

# JianYing Editor Skill

Use this skill when the user wants to automate video editing, generate drafts, or manipulate media assets in JianYing Pro.

Agent execution playbook: [docs/agent-playbook.md](docs/agent-playbook.md)
Minimal command SOP: [docs/minimal-command-sop.md](docs/minimal-command-sop.md)
Draft inspector CLI:
`python <SKILL_ROOT>/scripts/draft_inspector.py list --limit 20`
`python <SKILL_ROOT>/scripts/draft_inspector.py summary --name "DraftName"`
`python <SKILL_ROOT>/scripts/draft_inspector.py show --name "DraftName" --kind content --json`
For generic editing requests, always follow the "Quick Edit Runtime Template" and "Acceptance Checklist" in that playbook.

## 🚨 重要开发原则 (CRITICAL DEVELOPER RULES)
1.  **脚本位置**：**禁止在 Skill 内部目录创建剪辑脚本**。所有的剪辑逻辑实现代码（`.py` 脚本）必须存放在用户当前项目的**根目录**（或子目录，如 `scripts/`），以保持 Skill 库的纯净和可移植性。
2.  **配乐选择**：
    - **简单演示使用默认音乐**。实际项目，应优先检索并推荐 `data/cloud_music_library.csv` 中的相关曲目，或根据视频主题（如“科技”、“温暖”）进行关键词过滤。
    - 询问用户：“我发现了几首符合主题的云端音乐，要不要试试？（如：`Illuminate` - 科技感）”。

##  规则指南 (Rules)

Read the individual rule files for specific tasks and constraints:

- [rules/setup.md](rules/setup.md) - **Mandatory** initialization code for all scripts.
- [rules/core.md](rules/core.md) - Core operations: Saving, Exporting, and Draft management.
- [rules/cli.md](rules/cli.md) - CLI contracts and machine-readable output conventions.
- [rules/media.md](rules/media.md) - Importing assets & **AI Video Analysis Optimization (30m/360p)**.
- [rules/text.md](rules/text.md) - Adding Subtitles, Text, and Captions.
- [rules/keyframes.md](rules/keyframes.md) - **Advanced**: Adding Keyframe animations.
- [rules/effects.md](rules/effects.md) - Searching for and applying Filters, Effects, and Transitions.
- [rules/recording.md](rules/recording.md) - **New**: Screen Recording & Smart Zoom automation.
- [rules/web-vfx.md](rules/web-vfx.md) - Advanced: Web-to-Video generation.
- [rules/generative.md](rules/generative.md) - Chain of Thought for generative editing.
- [rules/audio-voice.md](rules/audio-voice.md) - **New**: TTS Voiceover & BGM sourcing.

## 🎯 Agent Quick Routing

- 云端视频 + 云端音乐：`rules/media.md` + `rules/audio-voice.md` -> `examples/cloud_video_music_tts_demo.py`
- 旁白与字幕对齐：`rules/text.md` + `rules/audio-voice.md` -> `examples/cloud_video_music_tts_demo.py`
- 滤镜 / 特效 / 转场：`rules/effects.md` -> 优先调用 `add_filter_simple()` / `add_effect_simple()` / `add_transition_simple()`
- 录屏与智能变焦：`rules/recording.md` -> `tools/recording/recorder.py`
- 批量导出/无头导出：`rules/core.md` + `rules/cli.md` -> `examples/robust_auto_export.py`
- 影视解说生成：`rules/generative.md` -> `scripts/movie_commentary_builder.py`

## ✅ VFX 调用约定

- 给视频片段加滤镜：优先使用 `project.add_filter_simple("哈苏蓝", video_segment=seg)`。
- 给时间轴加特效：优先使用 `project.add_effect_simple("复古DV", start_time="0s", duration="3s")`。
- 给两个视频片段之间加转场：优先保存前一个片段引用，再调用 `project.add_transition_simple("叠化", video_segment=seg1, duration="0.5s")`。
- 只有在明确知道轨道上已有至少两个视频片段时，才允许用 `track_name="V1"` 让包装层自动推断转场锚点。
- 生成完成后，必须检查草稿是否真的写入了 `filters`、`video_effects`、`transitions`，不能只依据 AI 自己的口头说明判断成功。

## 🔄 导出实例生命周期与失败自愈（robust_auto_export）

`examples/robust_auto_export.py` 的拉起/回首页统一走 `examples/jy_instance.py` 的实例管理器，关键行为：

- **实例状态机**：`stopped → launching → home → editing → exporting → (home | stopped)`。所有转移用墙钟 deadline，每次 UIA 探测带独立线程超时，探测失败不会无限等待。
- **冷启动默认开**：`--cold-start`（watcher 侧 `watch.cold_start`，默认 true）在每部剧导出前杀进程→拉起→等首页，规避「上一部刚导完、窗口存在但未就绪」的首试假死；手动同时用剪映时设 false。
- **杀进程唯一入口**：只有管理器的 `kill()` 调 `taskkill`，`connect`/`switch_to_home`/重启/冷启动均为薄封装转调，不直接杀进程。
- **就绪兜底**：`ensure` 到 deadline 未就绪只 `restart()` 一次，再失败抛含「未就绪/冷启动失败」关键词的异常，交由上层失败分类处理。

失败按 `lib/failure_class.py` 分五类（+`unknown` 兜底），决定重试与自愈动作：

| 类别 | 可重试 | 动作 |
| --- | --- | --- |
| `ui_hang`（窗口无响应/打开草稿超时） | 是 | 重启剪映重试 |
| `asset_missing`（草稿/素材缺失损坏） | **否** | 直接失败不重试 |
| `disk_full`（目标盘不足） | **否** | 拒绝启动不重试 |
| `export_timeout`（导出无进展卡死） | 是 | 杀进程重试 |
| `version_dialog`（更新/公告弹窗挡路） | 是 | 关弹窗后重试 |

`asset_missing`/`disk_full` 不重试、不计入连续失败，直接终态并通知，避免长时间空转。

## 📦 导出完成后的归档（pipeline 侧）

导出成功不等于归档：剧集默认留在 `watch.roots` 下，只是 `video.status=done` 不再触发导出。需要「下线」时用 `pipeline/archive_job.py` 显式归档：

- `job.json.stage=archived` 是合法 stage，含义是「该剧已归档、不再参与调度」；由归档脚本写入，watcher 的触发判定对它**硬拦**，`--phase` / `--once` / `--job` 都绕不过去（硬拦发生在阶段/单剧过滤之前）。
- 准入：`publish.status=published` 或 `video.export_state=cleaned`（`--force` 可跳过）；首次归档时 `.watcher.lock` 的 pid 存活（活锁）或 `.export_state.json` `status=exporting` 会拒绝（续做只校验位置与目标）。
- 归档池位置：`archive_root` 不能等于或位于 `watch.roots` 之下；`watch.roots` 为空时脚本直接拒绝执行（fail-closed）。建议与 roots 同盘、独立根（如 `D:/短剧_存档`）。
- 断点可重入：`stage` 已 archived、源目录仍在而目标不存在时，重跑同一命令即续做移动；先写 stage 再搬目录，因此移动失败也不会再被调度。
- 归档会同步改写 `pipeline/_stream_manifests/*.json` 的 `paths.source_dir`；不改写会导致后续 `stream_manifest.py clean` 按旧路径找不到源。
- `publish.status` 当前只有手工 `python lib/job_manifest.py publish-record ...` 会写 `published`，自动发布链没有写入路径（暂未实现）；不要依据它判断「已发布」。

```powershell
python pipeline/archive_job.py --job "<cover_job>" --archive-root "D:/短剧_存档" --dry-run
python pipeline/archive_job.py --job "<cover_job>" --archive-root "D:/短剧_存档"
```

完整契约见 `drama-cover-builder/references/handoff-contract.md` §7.4。

## 📖 经典示例 (Examples)

Refer to these for complete workflows:
- [examples/my_first_vlog.py](examples/my_first_vlog.py) - A complete vlog creation demo with background music and animated text.
- [examples/simple_clip_demo.py](examples/simple_clip_demo.py) - Quick-start tutorial for basic cutting and track management.
- [examples/compound_clip_demo.py](examples/compound_clip_demo.py) - **New**: Professional nested project (Compound Clip) automation.
- [examples/cloud_video_music_tts_demo.py](examples/cloud_video_music_tts_demo.py) - Cloud video + cloud BGM + TTS/subtitle alignment.
- [examples/web_to_video_intro_demo.py](examples/web_to_video_intro_demo.py) - Web-to-Video intro demo (HTML animation -> timeline clip).
- [examples/robust_auto_export.py](examples/robust_auto_export.py) - Stable export workflow and failure handling.
- [examples/auto_exposure_align_demo.py](examples/auto_exposure_align_demo.py) - CV-assisted exposure alignment workflow.
- [examples/video_transcribe_and_match.py](examples/video_transcribe_and_match.py) - **Advanced**: AI-driven workflow (Transcribe Video -> Match B-Roll via AI semantics -> Assemble Draft).

## 🧠 提示词与集成工具 (Prompts & Integrated Tools)

Use these templates and scripts for complex tasks:
- **Asset Search**: Find filters, transitions, and animations by Chinese/English name:
  ```bash
  python <SKILL_ROOT>/scripts/asset_search.py "复古" -c filters
  ```
- **Movie Commentary Builder**: Generate 60s commentary videos from a storyboard JSON:
  ```bash
  python <SKILL_ROOT>/scripts/movie_commentary_builder.py --video "video.mp4" --json "storyboard.json"
  ```
- **Sync Native Assets**: Import your favorited/played BGM from JianYing App to the Skill:
  ```bash
  python <SKILL_ROOT>/scripts/sync_jy_assets.py
  ```
- **README to Tutorial**: Convert a project's README.md into a full installation tutorial video script:
  - Read prompt: `prompts/readme_to_tutorial.md`
  - Inject content into `{{README_CONTENT}}` variable
- **Screen Recorder & Smart Zoom**: Record your screen and auto-apply zoom keyframes:
  ```bash
  python <SKILL_ROOT>/tools/recording/recorder.py
  # Or apply zoom to existing video:
  python <SKILL_ROOT>/scripts/jy_wrapper.py apply-zoom --name "Project" --video "v.mp4" --json "e.json"
  ```
- **Auto Exporter**: Headless export of a draft to MP4/SRT:
  ```bash
  python <SKILL_ROOT>/scripts/auto_exporter.py "DraftName" "output.mp4" --res 1080 --fps 60
  # For SRT only:
  python <SKILL_ROOT>/scripts/jy_wrapper.py export-srt --name "DraftName"
  ```
- **Template Clone & Replacer**: 安全克隆模板并批量替换物料 (防止损坏原模板):
  ```bash
  # 克隆模板生成新项目
  python <SKILL_ROOT>/scripts/jy_wrapper.py clone --template "酒店模板" --name "客户A_副本"
  ```
- **API Validator**: Run a quick diagnostic of your environment:
  ```bash
  python <SKILL_ROOT>/scripts/api_validator.py
  ```

## 🚀 快速开始示例

```python
import os
import sys

# 1. 环境初始化 (必须同步到脚本开头)
current_dir = os.path.dirname(os.path.abspath(__file__))
env_root = os.getenv("JY_SKILL_ROOT", "").strip()
# 探测 Skill 路径 (支持 Antigravity, Trae, Claude 等)
skill_root = next((p for p in [
    env_root,
    os.path.join(current_dir, ".agent", "skills", "jianying-editor"),
    os.path.join(current_dir, ".trae", "skills", "jianying-editor"),
    os.path.join(current_dir, ".claude", "skills", "jianying-editor"),
    os.path.join(current_dir, "skills", "jianying-editor"),
    os.path.abspath(".agent/skills/jianying-editor"),
    os.path.abspath(".trae/skills/jianying-editor"),
    os.path.abspath(".claude/skills/jianying-editor"),
    os.path.dirname(current_dir) # 如果在 examples/ 目录下
] if p and os.path.exists(os.path.join(p, "scripts", "jy_wrapper.py"))), None)

if not skill_root: raise ImportError("Could not find jianying-editor skill root.")
sys.path.insert(0, os.path.join(skill_root, "scripts"))
from jy_wrapper import JyProject

if __name__ == "__main__":
    project = JyProject("My Video Project")
    assets_dir = os.path.join(skill_root, "assets")

    # 2. 导入视频与配乐
    project.add_media_safe(os.path.join(assets_dir, "video.mp4"), "0s")
    project.add_media_safe(os.path.join(assets_dir, "audio.mp3"), "0s", track_name="Audio")

    # 3. 添加带动画的标题
    project.add_text_simple("剪映自动化开启", start_time="1s", duration="3s", anim_in="复古打字机")

    project.save()
```

## 🛠️ 初始化与项目规范 (Initialization & Project Rules)

在初始化 `JyProject` 时，请务必根据主视频素材的比例设置分辨率。**默认值为横屏 (1920x1080)**。

### 🚨 脚本存放位置规范
**禁止在 Skill 安装目录下创建你的业务剪辑脚本**。
- **正确做法**：将你的剪辑 Python 脚本放在项目的根目录。
- **原因**：Skill 目录应该只包含工具集源码，便于后续 `git pull` 升级。业务代码混入会导致版本管理混乱。
