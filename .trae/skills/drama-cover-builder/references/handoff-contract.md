# 交接契约（Handoff Contract v1.1）

阶段之间只通过**目录 + `job.json`** 交接，不依赖对话上下文。同一部剧可以在豆包做 Stage 1/2、在 Trae 跑 pipeline、由第三方只调用发布，三方读同一份文件即可对齐。

本文件是目录、字段、状态机的唯一标准；`title-spec.md`、`cover-spec.md`、`platform-rules.md` 只负责各自阶段的内容规则。

## 1. 目录结构（固定）

给定一个剧集目录后，默认在**其下**建一个任务目录 `cover_job/`，不污染原始素材，重跑或清理只动这一个目录。

```
D:\短剧\<剧名>\
├── 封面.png / 简介.txt / 第N集.mp4     # 原始素材，只读
└── cover_job\                          # 唯一写入区
    ├── job.json
    ├── export_config.json              # 可选：导出偏好（cut_s / derive），见 §7
    ├── 01_input\
    │   ├── synopsis.txt
    │   └── reference.png
    ├── 02_titles\
    │   ├── prompt.txt
    │   ├── titles.md
    │   ├── titles.json
    │   └── selection.json          # 可选：agent 只交编号清单 {"picks":["T9","T10","T7"]}
    ├── 03_prompts\
    │   ├── prompts.md
    │   └── prompts.json
    ├── 04_covers\
    │   ├── cover_01_hit.png
    │   ├── cover_02_contrast.png
    │   ├── cover_03_guard.png
    │   ├── cover_04_vertical.png
    │   └── derived\
    ├── 05_video\
    └── 06_publish\
        ├── youtube.json
        ├── tiktok.json
        └── receipts\
```

规则：

- `--out` 可覆盖默认目录；未传时：有 `--series-dir` → `<剧集目录>/cover_job`，否则 `./jobs/<剧名>`。
- 目录由 `prepare` 一次建全，后续阶段**只写自己的子目录**。
- 所有路径写入 `job.json` 时使用相对任务目录的正斜杠路径，例如 `../封面.png`。
- 文件名一律 ASCII；剧名、标题只存在于 `job.json` 和 md 文件里。

## 2. job.json 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | string | 固定 `1.1` |
| `job_id` | string | 任务唯一标识 |
| `series` | string | 剧名 |
| `created_at` / `updated_at` | string | ISO 8601 本地时间 |
| `revision` | number | 每次写入 +1，用于检测跨阶段脏数据 |
| `host` | string | `trae` / `doubao` / `other` |
| `stage` | string | 见 §3 |
| `input.synopsis` | string | `01_input/synopsis.txt` |
| `input.synopsis_source` | string | 自动探测到的原文件，或 `cli` |
| `input.synopsis_candidates` | array | 目录内所有简介候选 |
| `input.reference_image` | string \| null | 复制后的参考图 |
| `input.reference_original` | string \| null | 原始参考图相对路径，Stage 2 兜底用 |
| `input.reference_source` / `reference_reason` | string \| null | 来源文件名与选中原因 |
| `input.reference_sha256` | string \| null | 防替换校验 |
| `sizes` | object | `horizontal` / `vertical` 精确像素 |
| `titles.file` | string | `02_titles/titles.json` |
| `titles.count` | number | 固定 12 |
| `titles.gate` | string | `pending` / `done` |
| `titles.prompt_file` | string \| null | `02_titles/prompt.txt`，实际发出的提示词 |
| `titles.prompt_sha256` | string \| null | 提示词快照校验 |
| `titles.prompt_version` | string | 默认 `title-spec-v4.1` |
| `titles.selected` | array | 门禁后三条：`{slot, title_id, title}` |
| `titles.selected_at` | string \| null | 门禁时间 |
| `titles.selected_by` | string \| null | `user`（人工门禁）/ `auto`（兜底自动选分，见 §5） |
| `titles.auto_mode` | string \| null | 兜底模式：`recommended`（推荐池优先，默认）/ `score`（全量评分）；仅 `selected_by=auto` 时有值，人工选置 null |
| `cover_map` | array | 三个横版槽位，见 §4 |
| `vertical_cover` | object | 竖版，`role=video_cover` |
| `prompts.file` | string | `03_prompts/prompts.json` |
| `prompts.channel` | string \| null | `doubao-jimeng` / `volcano-ark` / `trae-host` |
| `prompts.count` | number | 固定 4 |
| `prompts.items` | array | 4 条提示词，Stage 2 兜底用 |
| `video.path` | string \| null | 唯一 mp4 时写入（任务目录内相对路径） |
| `video.source` | string \| null | 原文件名 |
| `video.candidates_count` | number | 发现多个 mp4 时的数量 |
| `video.name_pattern` | string \| null | 例如 `第N集.mp4` |
| `video.duration_s` | number \| null | 秒 |
| `video.cut_s` | number \| null | 导出裁剪秒数（正数，≤ duration_s）；watcher/pipeline 非 null 时传 `--cut` |
| `video.cover` | string \| null | 指向竖版封面 |
| `video.export_dir` | string \| null | pipeline 最终导出目录（**允许绝对路径**，只记录不复制） |
| `video.export_file` | string \| null | 最终成品 mp4 完整路径 |
| `video.draft_name` | string \| null | 串流草稿名，关联 `pipeline/_stream_manifests/<名>.json` |
| `video.companions` | array | 成品旁的脚本产物（`.progress.json`、`-封面.jpg`） |
| `publish.status` | string | `not_ready` / `ready` / `uploading` / `published` / `failed` |
| `publish.platforms` | array | 每平台 `{name,status,video_id,url,cover_file,attempts,error,uploaded_at}` |
| `publish.idempotency_key` | string \| null | 防重复上传 |
| `publish.receipts` | array | 上传回执 |
| `cleanup` | object | 见 §6 |
| `history` | array | `{at,event,detail,revision}` 追加写 |

**单一写入口**：`job.json` 是**收口产物**，不是 agent 的交付物。第三方 agent 只交内容产物（`titles.json`、`selection.json`、`prompts.json`、4 张图），`job.json` 由 `prepare` / `record-titles` / `select` / `ingest` / `set-cut` / `record-video` 生成与更新。`ingest` 同时负责宽容规范化（`selected` 对象/`"T9"`/`9`/纯文本、`slot` 数字/`"封面1"`、`stage` 别名如 `covers_done`、图片任意命名），全部动作记入 `history`。

示例（准备阶段）：

```json
{
  "schema_version": "1.1",
  "job_id": "重生八零，打猎卖货养妻女-20261101-2245",
  "series": "重生八零，打猎卖货养妻女",
  "revision": 2,
  "host": "trae",
  "stage": "titles_done",
  "input": {
    "synopsis": "01_input/synopsis.txt",
    "synopsis_source": "../简介.txt",
    "synopsis_candidates": ["../简介.txt", "../详细简介.txt"],
    "reference_image": "01_input/reference.png",
    "reference_original": "../封面.png",
    "reference_source": "封面.png",
    "reference_reason": "文件名精确命中封面",
    "reference_sha256": "..."
  },
  "titles": {
    "file": "02_titles/titles.json",
    "count": 12,
    "gate": "pending",
    "prompt_file": "02_titles/prompt.txt",
    "prompt_sha256": "...",
    "prompt_version": "title-spec-v4.1",
    "selected": [],
    "selected_at": null
  },
  "cover_map": [
    {"slot": 1, "kind": "horizontal", "composition": "爆点仰拍",
     "title_id": "T1", "title": null, "file": "04_covers/cover_01_hit.png",
     "prompt": null, "channel": null, "image_sha256": null,
     "status": "pending", "generated_at": null}
  ],
  "vertical_cover": {
    "slot": "vertical", "kind": "vertical", "role": "video_cover",
    "composition": "胸像逆光", "title_id": null, "title": null,
    "file": "04_covers/cover_04_vertical.png", "prompt": null,
    "channel": null, "image_sha256": null, "status": "pending",
    "generated_at": null
  },
  "prompts": {"file": "03_prompts/prompts.json", "channel": null,
              "count": 4, "items": []},
  "video": {"path": null, "source": null, "candidates_count": 79,
            "name_pattern": "第N集.mp4", "duration_s": null, "cut_s": null, "cover": null},
  "publish": {"status": "not_ready", "idempotency_key": null,
              "platforms": [], "receipts": []},
  "cleanup": {"status": "blocked", "eligible": false,
              "reason": "尚未完成发布", "requires_confirm": true,
              "targets": ["01_input/reference.*"], "keep": ["..."],
              "executed_at": null, "deleted": []},
  "history": [{"at": "...", "event": "prepare", "detail": "...", "revision": 1}]
}
```

## 3. 状态机

| stage | 含义 | 谁写入 |
| --- | --- | --- |
| `prepared` | 目录与输入就绪 | `prepare` |
| `titles_done` | 12 条标题与初始提示词已落盘 | Stage 1 |
| `titles_selected` | 用户点满 3 个编号，`gate=done` | 门禁 |
| `prompts_ready` | 4 条出图提示词已写入 | Stage 1.5 |
| `covers_ready` | 4 张图齐且过四项质检 | Stage 2 |
| `video_ready` | mp4 已落 `05_video/` | pipeline |
| `publish_ready` | 元数据与物料齐 | 发布 skill |
| `published` | 至少一个平台回执成功 | 发布 skill |

只允许向前推进，不回退；失败时只追加 `history`，`stage` 保持不变。每次写 `job.json` 都是「读-改-写 + 原子替换」，`revision` 递增。第三方 agent 写的非契约 stage 别名（`covers_done` / `covers_complete` / `videos_done`）由 `ingest` 与 watcher 统一映射为契约值后再判定。

## 4. 标题与封面映射

| slot | 构图 | 默认标题 | 文件 |
| --- | --- | --- | --- |
| 1 | 爆点仰拍 | `T1` | `cover_01_hit.png` |
| 2 | 冷暖对比 | `T2` | `cover_02_contrast.png` |
| 3 | 守护挡身 | `T3` | `cover_03_guard.png` |
| vertical | 胸像逆光 | 不参与默认映射 | `cover_04_vertical.png` |

- `prepare` 按 **1-1 / 2-2 / 3-3** 预填 `title_id`，`title` 为 `null`。
- 门禁完成后写 `titles.selected`（3 项 `{slot,title_id}`），并回填 `cover_map[].title_id` / `title`，`gate=done`。
- 三条横版必须与标题一一对应；竖版标 `role=video_cover`。
- 竖版绑定：门禁（`select`）或收口（`ingest`）时，自动绑定 `selected` 三条中评分最高的一条（平分按总分→推荐优先→内容兑现→信息缺口→情绪峰值，与兜底同序）；无评分数据则保持 `title_id=null`。
- 门禁后 `selected` 冻结；改选必须显式操作，`revision` 递增，Stage 2 发现 revision 不一致要把旧图标 `stale`。
- 文件名容错：`04_covers/` 内可用任意文件名（`cover_01_hit` / `封面图1` / `T2 封面` / `竖版` …），`ingest` 按「竖版宽高比/名称提示优先，横版按文件名编号 1/2/3，否则自然顺序」识别并**改名到契约名**，改名记录写入 `history` 与 `report.renamed`。

## 5. 跨阶段兜底链

Stage 2 单独执行、且调用方没传参数时，按下列顺序回查：

| 输入 | 顺序 |
| --- | --- |
| 标题 | `titles.gate=done` 用 `selected` → 未过门禁但有 `titles.json`（含评分）时按兜底模式自动选 3 条：**推荐模式**（默认，`summary.recommended` 推荐池按总分取前 3，不足 3 条从全量补足，推荐池缺失退化为全量评分）/ **评分模式**（全量取前 3）；总分相同推荐条目优先，再平按 内容兑现→信息缺口→情绪峰值；写回 `selected_by="auto"` + `auto_mode` 并追加 history → 无评分数据则报错停下 |
| 参考图 | 显式参数 → `input.reference_image` → `input.reference_original` → 剧集目录探测 → 空并告警 |
| 提示词 | 显式参数 → `03_prompts/prompts.json` → `job.json.prompts.items` → `cover_map[slot].prompt` → 由选中标题 + 构图反推 |

自动选分是可审计的兜底，不是静默代选：`selected_by="auto"` 与 `auto_mode`（recommended/score）会留在 `job.json` 里，用户事后可显式改选（revision 递增，旧封面标 `stale`）。需要严格门禁时用 `resolve --no-auto`，门禁未过直接报错。

Stage 2 的标准取数入口是 `resolve`（一条命令返回标题 + 参考图 + 提示词 + warnings），不要各自拼兜底逻辑。

## 6. 发布与清理

`publish.platforms` 每项独立记录状态、`video_id`、`url`、`cover_file`、`attempts`、`error`、`uploaded_at`。上传前生成幂等键（剧名 + 集数 + 封面 hash），重试不产生重复视频。

平台封面不是同一张：YouTube 缩略图要求 16:9（1280×720，小于 2MB），用横版派生；TikTok 用竖版。派生文件放 `04_covers/derived/`，用 `derive --platform ...` 从契约原图生成（易变产物，不写 job.json、不参与 hash 校验）。

`cleanup` 规则：

- `status`：`blocked` / `eligible` / `done` / `skipped`。
- `eligible=true` 必须同时满足：全部目标平台成功、无待重试、封面与 mp4 的 hash 校验通过。
- 只允许清理 `targets` 内的任务副本（默认 `01_input/reference.*`）；`keep` 内的 `job.json`、简介、标题、提示词、封面、视频永不自动删除。
- `requires_confirm` 固定 `true`，用户确认后才执行，结果写入 `deleted` 与 `executed_at`。

`cleanup.external`（**记录不接管**）：pipeline 串流产物（草稿目录、`__jycache__`、workdir、导出 bundle）由 `pipeline/stream_manifest.py` 独立负责清理，cover_job 不重复删除，只登记联动条目：

```json
{"tool": "pipeline/stream_manifest.py",
 "draft_name": "重生八零，打猎卖货养妻女_第01-80集串流",
 "manifest": "pipeline/_stream_manifests/<名>.json",
 "command": "python pipeline/stream_manifest.py clean \"<draft_name>\"",
 "scope": ["export_bundle(mp4+封面+progress)", "draft_dir", "jycache_dir", "workdir"],
 "deletes_final_mp4": true,
 "status": "pending"}
```

⚠️ `stream_manifest.py clean` 会**连同最终 mp4 一起删除**（`deletes_final_mp4: true`）。所以：cover_job 的 `cleanup.eligible` 只管任务目录内部残留；外部 pipeline 残留是否清理由用户单独决定，执行后把 `external[].status` 改为 `done`，永不自动执行。

## 7. 写入与校验

```powershell
# 建目录（只给剧集目录即可，自动找 简介.txt / 封面 / mp4）
python scripts/drama_cover.py prepare --series-dir "D:\短剧\重生八零，打猎卖货养妻女"

# 登记 Stage 1 结果（标题、初始提示词）
python lib/job_manifest.py record-titles --job "D:\短剧\重生八零，打猎卖货养妻女\cover_job" `
  --titles "02_titles\titles.json" --prompt "02_titles\prompt.txt"

# 人工门禁：依次为封面1/2/3 给 3 个编号
python lib/job_manifest.py select --job "<cover_job>" --picks "T6,T11,T1"

# Stage 2 取上下文（标题+参考图+提示词，含兜底链；门禁未过自动选分）
python lib/job_manifest.py resolve --job "<cover_job>"              # 自动兜底并写回（默认推荐模式）
python lib/job_manifest.py resolve --job "<cover_job>" --mode score # 评分模式（全量取前 3）
python lib/job_manifest.py resolve --job "<cover_job>" --dry-run    # 只演算不落盘
python lib/job_manifest.py resolve --job "<cover_job>" --no-auto    # 严格门禁

# 登记 pipeline 导出目录（自动探测 stream manifest，只记录不复制 mp4）
python lib/job_manifest.py record-video --job "<cover_job>"
python lib/job_manifest.py record-video --job "<cover_job>" --draft-name "<串流草稿名>"
python lib/job_manifest.py record-video --job "<cover_job>" --export-dir "D:\油管打猎\L\<目录>"  # 手工指定优先于自动探测
python lib/job_manifest.py record-video --job "<cover_job>" --dry-run

# 指定/清除导出裁剪秒数（写 video.cut_s，默认 null 不裁剪；watcher 读到非 null 会传 --cut）
python lib/job_manifest.py set-cut --job "<cover_job>" --cut 7200   # 裁掉前 7200 秒
python lib/job_manifest.py set-cut --job "<cover_job>" --cut 0      # 清除，恢复不裁剪

# 派生平台用图到 04_covers/derived/（youtube/tiktok/instagram-reels/facebook-reels）
python lib/job_manifest.py derive --job "<cover_job>" --platform youtube,tiktok
python lib/job_manifest.py derive --job "<cover_job>" --platform all --slot 2

# Stage 2 收口：从落盘产物（prompts.json + 4 张契约图）重建 job.json
# 收登记、规范化（selected.slot 中文→数字）、补 hash、推进 stage（只进不退），幂等
python lib/job_manifest.py ingest --job "<cover_job>" [--dry-run]

# 落盘自动触发链（监听 cover_job，产物齐自动跑：validate --fix → ingest → export_config → 串流导出 mp4（batch_runner 默认自动质检，--no-verify 关）→ record-video → derive → Toast）
python lib/cover_watch.py --root "D:/短剧"                 # 扫盘自动发现 <剧集>/cover_job（推荐）
python lib/cover_watch.py --job "<cover_job>" [--job "<另一个>"] [--settle 30] [--once] [--dry-run] [--no-verify] [--no-derive]
python lib/cover_watch.py status --job "<cover_job>"
python lib/cover_watch.py          # 无参：读 <repo>/pipeline/presets.json 的 watch 段（每轮热加载）{"watch":{"roots":["D:/短剧"],"jobs":[],"exclude":[],"settle":30,"interval":5,"derive":true}}

# 导出偏好（可选）：豆包/用户写 <cover_job>/export_config.json，watcher 触发时应用并记 history
# {"cut_s": 7200, "derive": ["youtube","tiktok"]}
#   cut_s 省略 = 不裁剪（正数秒，≤ duration_s，validate 把关）
#   derive 省略 = 全平台；[] = 不派生；平台名支持短名 ig/facebook，"all" = 全量

# 任意阶段更新
python lib/job_manifest.py update --job "<cover_job>" --patch-file patch.json `
  --event stage2_covers --detail "4 张封面落盘"

# 校验
python lib/job_manifest.py validate --job "<cover_job>"
python lib/job_manifest.py validate --job "<cover_job>" --fix   # 先补算豆包侧写为 null 的 hash 再校验
```

并发保护：同一任务同一时间只允许一个阶段写；如需并行，先加 `cover_job/.lock` 并在写完删除。
导出互斥：repo 级全局锁 `.export.lock`（`lib/export_lock.py`，pid 判活）保证同一时刻只有一个 batch_runner 导出；watcher 导出步自动持锁，手工跑遇活锁报错退出，`--force` 可强跑。

## 8. 各阶段产物与意义

| 阶段 | 写入 | 产物 | 意义 |
| --- | --- | --- | --- |
| Stage 0 | `01_input/`、`job.json` | `synopsis.txt`、`reference.png` | 输入固化进任务区，后续不依赖原始素材位置 |
| Stage 1 | `02_titles/` | `prompt.txt`（含 sha256）、`titles.json`、`titles.md` | 提示词快照可复现审计；json 是机器交接源；md 是可选人读评审稿，删了不影响流程 |
| 门禁 | 只写 `job.json` + 标注 `titles.json` | `titles.selected`、`selected_by`、`cover_map` 回填；`titles.json` 选中条加 `selected/slot`、顶层记 `gate/selected_at/selected_by` | 最终选定结果只此两处（job.json 为事实源，titles.json 原位标注不复制文本） |
| Stage 1.5 | `03_prompts/` | `prompts.json` / `prompts.md` | 4 条出图提示词，豆包/火山/Trae 三渠道共用 |
| Stage 2 | `04_covers/` | 3 横版 + 1 竖版 + `derived/` | 平台用图；derived 放 YouTube 16:9 等派生尺寸 |
| pipeline | 只写 `job.json` | `video.export_dir/export_file/draft_name/companions` | 只记录导出位置（允许绝对路径），不复制 mp4；`05_video/` 留作需要副本时的可选目录 |
| Stage 3 | `06_publish/` | `<platform>.json` + `receipts/` | 每平台状态与回执，驱动 `cleanup` 判定 |
