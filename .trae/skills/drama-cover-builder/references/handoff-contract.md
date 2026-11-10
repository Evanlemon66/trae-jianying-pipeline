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
    ├── dispatch.json                   # 可选：豆包侧调度意图声明（mode/cut_s/derive/priority），ingest 归并进 job.json.workflow，见 §7
    ├── export_config.json              # 可选（旧版兼容）：导出/剪辑偏好（cut_s / derive / start_ep），见 §7
    ├── .export_state.json              # 运行态：导出状态机单一真源（真实导出开始时 cover_watch 自动写，无需手工建），见 §7.1
    ├── .export_retry.json              # 运行态（旧版，仅迁移来源）：首次读真源时迁入 .export_state.json 并备份 .bak
    ├── .edit_state.json                # 运行态：剪辑阶段失败退避/暂停状态机（首次 edit 闸门判定时自动写），见 §7.3
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
        └── receipts\                  # 占位目录：由 create_job 预建，内容由 Stage 3 写入；缺失不阻断前序阶段（见 §8）
```

规则：

- `out_dir` 可覆盖默认目录；未传时：有 `series_dir` → `<剧集目录>/cover_job`，否则 `./jobs/<剧名>`。
- 目录由 Trae 侧 `lib/job_manifest.py::create_job()` 一次建全（含占位目录 `06_publish/receipts`；Python API，无 CLI 子命令），后续阶段**只写自己的子目录**。占位目录允许为空；`06_publish/receipts` 缺失**不阻断前序阶段**，只在 `publish_ready` / `published` 校验时要求存在（见 §8）。
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
| `titles.count` | number | 12 或其整数倍：`12`（旧规范一套）/ `24`（旧规范 12 + 平台新规范 12）；其它数量非法 |
| `titles.spec_version` | string \| null | 元信息：本批候选按哪套规范生成（如 `title-spec-v4.2`），供以后统计；**不参与校验**，缺失按 `legacy` 理解，不迁移、不回写 |
| `titles.gate` | string | 枚举只有 `pending` / `done` 两值；禁止自造（如 `selected`），违规用 `validate --fix` 修复 |
| `titles.prompt_file` | string \| null | `02_titles/prompt.txt`，实际发出的提示词 |
| `titles.prompt_sha256` | string \| null | 提示词快照校验 |
| `titles.prompt_version` | string | 默认 `title-spec-v4.1` |
| `titles.selected` | array | 门禁后三条：`{slot, title_id, title}` 对象数组；禁止字符串/数字数组，违规用 `validate --fix` 修复。三条 `title_id` 必须各自存在于 `titles.json` 候选集合（`T1–T12` / `N1–N12` 等，不限定前缀）且互不重复 |
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
| `video.export_state` | string \| null | 成品生命周期：`null`=从未导出 / `exported`=已导出且成品在 / `cleaned`=导出过但成品已被清理。写入者：`record_video` 置 `exported`，`stream_manifest.py clean` 真正删除成品后置 `cleaned`（`--dry-run` / `--intermediates-only` 不写）。只记录状态，不改变 `stage` 与 `video.status` |
| `workflow.mode` | string | `full`（剪辑+导出，默认）/ `edit_only`（只剪辑停在草稿） |
| `workflow.cut_s` | number \| null | 开头裁剪秒数（正数；`0`/null=不裁）；优先级高于 `video.cut_s` |
| `workflow.derive` | array | 派生平台列表（`[]`=不派生）；支持 `all`/别名 `ig`、`facebook` |
| `workflow.priority` | number | 调度优先级（大者优先，默认 0） |
| `workflow.intent_revision` | number | **仅人工 +1**，程序只读不写；变大即视为人工最终意图 |
| `workflow.intent_source` | string | `default` / `dispatch` / `manual` |
| `workflow.merged_at` | string \| null | 最近一次归并时间 |
| `derived.subtitle_missing` | array | 缺字幕集号清单：升序去重的正整数，缺省 `[]`。仅当该集三档 ASR 全失败、或裁剪后最终字幕条数为 0 时写入；该集**重新剪辑且字幕非空**后才移除（只重跑不消除）。非空时所有导出入口硬拦，见 §7.5 |
| `publish.status` | string | `not_ready` / `ready` / `uploading` / `published` / `failed` |
| `publish.platforms` | array | 每平台 `{name,status,video_id,url,cover_file,attempts,error,uploaded_at}` |
| `publish.idempotency_key` | string \| null | 防重复上传 |
| `publish.receipts` | array | 上传回执 |
| `cleanup` | object | 见 §6 |
| `history` | array | `{at,event,detail,revision}` 追加写 |

**单一写入口**：`job.json` 是**收口产物**，不是 agent 的交付物。第三方 agent 只交内容产物（`titles.json`、`selection.json`、`prompts.json`、4 张图）与调度意图声明（`dispatch.json`，可选），`job.json` 由 `create_job`（Trae 侧 Python API，原 `scripts/drama_cover.py prepare` 已移除）/ `record-titles` / `select` / `ingest` / `set-cut` / `record-video` 生成与更新。`ingest` 同时负责宽容规范化（`selected` 对象/`"T9"`/`9`/纯文本、`slot` 数字/`"封面1"`、`stage` 别名如 `covers_done`、图片任意命名）并把 `dispatch.json` 归并进 `workflow` 段，全部动作记入 `history`。`workflow.intent_revision` 是人工意图标记：仅人工 +1，程序路径只读；变大即视为人工最终意图（`intent_source=manual`），此后 `dispatch.json` 只补缺失、不覆盖。

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
| `prepared` | 目录与输入就绪 | `create_job`（历史事件仍记 `prepare`） |
| `titles_done` | 12（或 12 的整数倍，如 24）条标题与初始提示词已落盘 | Stage 1 |
| `titles_selected` | 用户点满 3 个编号，`gate=done` | 门禁 |
| `prompts_ready` | 4 条出图提示词已写入 | Stage 1.5 |
| `covers_ready` | 4 张图齐且过四项质检 | Stage 2 |
| `video_ready` | mp4 已落 `05_video/` | pipeline |
| `publish_ready` | 元数据与物料齐 | 发布 skill |
| `published` | 至少一个平台回执成功 | 发布 skill |
| `archived` | 已归档，不再参与调度（剧集目录已移入归档池） | `pipeline/archive_job.py` |

只允许向前推进，不回退；失败时只追加 `history`，`stage` 保持不变。每次写 `job.json` 都是「读-改-写 + 原子替换」，`revision` 递增。第三方 agent 写的非契约 stage 别名（`covers_done` / `covers_complete` / `videos_done`）由 `ingest` 与 watcher 统一映射为契约值后再判定。

`archived` 是终结态：watcher 的触发判定对它**硬拦**（`--phase` / `--once` / `--job` 都绕不过去），只由 `pipeline/archive_job.py` 写入，见 §7.4。`publish.status` 当前只有手工 `python lib/job_manifest.py publish-record ...` 会置 `published`，自动发布链没有写入路径（暂未实现），不要依据它判断「已发布」。

## 4. 标题与封面映射

| slot | 构图 | 默认标题 | 文件 |
| --- | --- | --- | --- |
| 1 | 爆点仰拍 | `T1` | `cover_01_hit.png` |
| 2 | 冷暖对比 | `T2` | `cover_02_contrast.png` |
| 3 | 守护挡身 | `T3` | `cover_03_guard.png` |
| vertical | 胸像逆光 | 不参与默认映射 | `cover_04_vertical.png` |

- `create_job` 按 **1-1 / 2-2 / 3-3** 预填 `title_id`，`title` 为 `null`。
- 门禁完成后写 `titles.selected`（3 项 `{slot,title_id}`），并回填 `cover_map[].title_id` / `title`，`gate=done`。
- **硬约束**：`titles.gate` 只能是 `pending` / `done`，禁止自造枚举（把「已选定」写成 `gate="selected"` 是常见错误）；`titles.selected` 必须是 3 个对象 `{slot,title_id,title}`，禁止字符串/数字数组。已落盘的违规形态用 `validate --job <cover_job> --fix` 修复（规范化 gate/selected、记一条 `validate_fix` history），不要手改文件。
- 三条横版必须与标题一一对应；竖版标 `role=video_cover`。
- 竖版绑定：门禁（`select`）或收口（`ingest`）时，默认绑定 `selected` 三条中**总分最高**的一条；**总分相同则按 `selected` 中出现的先后取靠前的一条**；无评分数据则保持 `title_id=null`。人工已显式指定 `vertical_cover.title_id`（经 `update` 写入或手改 job.json）时一律保留，不被默认绑定覆盖。
- 门禁后 `selected` 冻结；改选必须显式操作，`revision` 递增，Stage 2 发现 revision 不一致要把旧图标 `stale`。
- 候选集合：`titles.json` 可只放旧规范 12 条（`T1–T12`），也可叠加平台新规范 12 条（`N1–N12`）共 24 条；`id` 不限前缀，只要求同一批内唯一。校验只看「引用是否存在 + 三条是否互不重复」，不再按 `T1–T12` 白名单卡；编号不连续只告警（写一行日志），不阻断。
- **候选条目字段表（硬约束）**：每条候选是一个对象，只有 `id`、`text` 必填，其余字段可选、仅审计、不参与校验。

| 字段 | 类型 | 必填 | 语义 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | string | **是** | 唯一编号 | 形态 `字母前缀+序号`（如 `T1` / `N12`），大写、同批唯一、不限前缀。**唯一规范编号字段**；`title_id` 仅旧数据读取兼容，写入方禁止产出 |
| `text` | string | **是** | 标题全文 | **唯一规范正文字段**；`title` 仅旧数据读取兼容，写入方禁止产出；两字段并存时以 `text` 为准 |
| `chars` | number | 否 | 字数（版1/2 60—80，版3 55—70） | 仅审计，不校验 |
| `plan` | string \| number | 否 | 方案号（1/2/3） | 仅审计 |
| `version` | string \| number | 否 | 版本号（1—4，4=机制版） | 仅审计 |
| `hook` | string | 否 | 钩子手法 / 机制族（M1—M8） | 仅审计 |
| `face_slap` | string | 否 | 打脸式 | 仅审计 |
| `source` | string | 否 | 简介出处原句（或摘要） | 仅审计 |
| `scores` | array[7] | 是（24 条全给） | 七维分项，每项 0—5，顺序同 `scorecard.md`（第一眼/与我有关/信息缺口/情绪峰值/具体画面/新鲜程度/内容兑现） | **规范形态只有 7 元素数组**；12 条与 N1—N12 共 24 条必须逐条给出，缺一条按缺评分处理。`scores` 为 dict、或旧别名 `score`（dict）都只是旧数据读取兼容，写入方禁止产出 |
| `total` | number | 否 | 七维总分（缺省按 `scores` 求和） | 仅审计 / 排序 |
| `rec` | boolean | 否 | 是否推荐（人工标注） | 仅审计；推荐池以顶层 `summary.recommended` 为准 |
| `std` | string | 否 | 平台规范标识（N 组条目常用） | 仅审计 |
| `slot` | number | 否 | 门禁槽位 | **工具写入字段**：`select` / `ingest` 会在 `titles.json` 原位重写（先清空再按选中项回填）；写入方不必产出，也不可依赖它做业务判断 |

- **评分用途（竖版自动绑定）**：`scores` 是竖版封面自动选标题的打分依据——从 `titles.selected` 的**三条**里取总分最高的一条绑定 `vertical_cover.title_id`，同分取 `selected` 中靠前的一条。**比较范围严格限定在这三条选中项内，绝不遍历全部 12/24 条候选**（未选中项分数再高也不参与）；`vertical_cover.title_id` 已人工指定时保留原值。`auto` 兜底选分与竖版绑定使用同一套总分口径（条目 `total` 优先，缺省按分项合计），保证同一数据在两处算出的分数一致。
- **顶层元信息**：`series`、`spec_version`（规范名；`spec` 仅旧数据兼容）、`generated_at`、`gate`、`summary{count_t, count_n, recommended}`、`selected_at`、`selected_by`。其中 `summary.recommended` 是编号字符串数组，仅作 `auto` 兜底推荐池；`summary.count_t` / `summary.count_n` 仅审计。
- **候选容器唯一（硬约束）**：候选只允许放在三处之一——`{"items":[...]}`（现行）、`{"titles":[...]}`（旧版）、裸数组；同文件同时存在时以 `items` 为准、`titles` 次之。**禁止新增任何其它顶层候选容器**：需要 24 条就把 24 条放进**同一个数组**，不允许另起 `placement`、`extra_titles` 之类第二个数组各装 12 条（`placement` 属规范外写法，历史上由写入方自造，契约不承认）。
- 读取侧兼容（`job_manifest._load_titles_payload`，全仓唯一解析入口）：对规范外顶层数组只做保守兜底——数组内每条都是 `{id: <合法编号>}`、长度为 12 的整数倍、且编号不与主集合重叠时才并入候选并打印 `[warn]`；长度不合规跳过并告警，与主集合重叠按重复副本忽略并告警。兜底只为消化存量数据，**写入侧不得再产出规范外容器**。
- 文件名容错：`04_covers/` 内可用任意文件名（`cover_01_hit` / `封面图1` / `T2 封面` / `竖版` …），`ingest` 按「竖版宽高比/名称提示优先，横版按文件名编号 1/2/3，否则自然顺序」识别并**改名到契约名**，改名记录写入 `history` 与 `report.renamed`。

## 5. 跨阶段兜底链

Stage 2 单独执行、且调用方没传参数时，按下列顺序回查：

| 输入 | 顺序 |
| --- | --- |
| 标题 | `titles.gate=done` 用 `selected` → 未过门禁但有 `titles.json`（含评分）时按兜底模式自动选 3 条：**推荐模式**（默认，`summary.recommended` 推荐池按总分取前 3，不足 3 条从全量补足，推荐池缺失退化为全量评分）/ **评分模式**（全量取前 3）；总分相同推荐条目优先，再平按 内容兑现→信息缺口→情绪峰值；写回 `selected_by="auto"` + `auto_mode` 并追加 history → 无评分数据则报错停下 |
| 参考图 | 显式参数 → `input.reference_image` → `input.reference_original` → 剧集目录探测 → 空并告警。剧集目录探测以 `job_manifest.detect_reference_image` 为准：文件名精确命中（`封面.png/.jpg/.jpeg/.webp` 等）→ 名字含「封面/cover」（排除海报/横版/logo 等 token 后按自然序取第一）→ 自然序第一张；skill 脚本另带自包含兜底 `drama_cover.detect_cover_from_dir`（仅「`封面.*` 精确名 → 目录首图」两级，无排除规则），供无 `lib/` 环境使用，**不参与 job.json 登记** |
| 提示词 | 显式参数 → `03_prompts/prompts.json` → `job.json.prompts.items` → `cover_map[slot].prompt` → 由选中标题 + 构图反推 |

自动选分是可审计的兜底，不是静默代选：`selected_by="auto"` 与 `auto_mode`（recommended/score）会留在 `job.json` 里，用户事后可显式改选（revision 递增，旧封面标 `stale`）。需要严格门禁时用 `resolve --no-auto`，门禁未过直接报错。

Stage 2 的标准取数入口是 `resolve`（一条命令返回标题 + 参考图 + 提示词 + warnings），不要各自拼兜底逻辑。

## 6. 发布与清理

`publish.platforms` 每项独立记录状态、`video_id`、`url`、`cover_file`、`attempts`、`error`、`uploaded_at`。上传前生成幂等键（剧名 + 集数 + 封面 hash），重试不产生重复视频。

平台封面不是同一张：YouTube 缩略图要求 16:9（1280×720，小于 2MB），用横版派生；TikTok 用竖版。派生文件放 `04_covers/derived/`，用 `derive --platform ...` 从契约原图生成（易变产物，不写 job.json、不参与 hash 校验）。

### 手工发布回执 `publish-record`

发布流程尚未自动化，手工上传后用一条命令把结果写进契约文件（不手写 job.json）：

```powershell
python lib/job_manifest.py publish-record --job "<cover_job>" `
  --platform youtube --video-id "dQw4w9WgXcQ" --url "https://youtu.be/dQw4w9WgXcQ" `
  [--uploaded-at "2026-11-04T12:00:00+08:00"] [--cover-file "04_covers/derived/youtube.jpg"] [--note "手工上传"]
```

- `--platform` 只接受 `youtube` / `tiktok`；`--video-id` 与 `--url` **至少给一个**，全缺直接报错。
- 产物：`06_publish/<platform>.json` 与 `06_publish/receipts/<platform>-<时间戳>.json`，字段为 `name` / `status=published` / `video_id` / `url` / `cover_file` / `attempts` / `uploaded_at` / `idempotency_key`（外加可选 `note` 与 `recorded_at`）。
- `job.json`：平台条目追加进 `publish.platforms`（**status 用校验枚举 `succeeded`**，文件侧才是 `published`）、回执条目追加进 `publish.receipts`，`publish.status=published`；stage **不强推**，只有当前已是 `publish_ready`/`published` 时才置 `published`。
- 幂等：同平台同 `video_id`（无 id 时同 `url`）重复登记直接跳过，不追加、不涨 `revision`；不同 id 才追加一条并 `revision+1`。

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
 "command": "python pipeline/stream_manifest.py clean --job \"<draft_name>\" [--dry-run] [--require-receipts] [--include-drafts] [--intermediates-only]",
 "scope": ["jycache_dir", "workdir", "export_bundle(mp4+封面+progress)", "draft_dir(仅 --include-drafts)"],
 "deletes_final_mp4": true,
 "status": "pending"}
```

⚠️ `stream_manifest.py clean` 默认会**连同最终 mp4 一起删除**（`deletes_final_mp4: true`）。所以：cover_job 的 `cleanup.eligible` 只管任务目录内部残留；外部 pipeline 残留是否清理由用户单独决定，执行后把 `external[].status` 改为 `done`，永不自动执行。

### `stream_manifest.py clean` 开关（默认信任"已发布"）

发布流程尚未自动化，`clean` 默认**信任"用户让清理＝已发布"**：

| 开关 | 默认 | 语义 |
| --- | --- | --- |
| （无参） | 开 | 依次清理 `__jycache__` → `_work_*` → 导出成品目录（mp4 + `-封面.jpg` + `.progress.json` 整体）；**不删 C 盘剪映草稿** |
| `--job "<草稿名>"` / `--all` | 单剧 | 只清一部 / 遍历所有记录 |
| `--dry-run` | 关 | 只打印「将删路径 + 各自大小 + 合计」，**零删除、不改记录** |
| `--require-receipts` | 关 | 严格模式：必须有成功回执（`06_publish/receipts/` 内文件，或 `06_publish/<platform>.json` 标 `published` 且 `url`/`video_id` 非空），否则整剧跳过并列入"跳过清单" |
| `--include-drafts` | 关 | 额外删除 C 盘剪映草稿目录（唯一可再导出入口，删后只能重跑 pipeline） |
| `--intermediates-only` | 关 | **只清** `__jycache__` + `_work_*`，导出成品目录与草稿一律不动；与 `--include-drafts` 同时给出时以本开关为准 |

- 删除顺序：中间产物（jycache、workdir）→ 成品 →（可选）草稿；缺失项跳过并记录。
- 保护区断言（命中即跳过并告警）：等于/包含 `<series_dir>` 源目录、位于 `_broken`、位于 `cover_job`、`s2.mp4`/`s3.mp4`/`ss.mp4`、`pipeline/_work` 旧状态目录；`.stream_manifests` 记录本身永不删除。
- 成品真正删除后回写该剧 `job.json`：`video.export_state=cleaned`（`revision+1`，history 记 `export_cleaned`，detail 含路径与释放大小）；无 job.json 时跳过并记录。`--dry-run` 与 `--intermediates-only` 不回写。
- ⚠️ **风险**：默认行为会删除成品 mp4 与整个导出目录，**不可恢复**；只想清中间产物时用 `--intermediates-only`（成品与草稿都保留），或先 `--dry-run` 核对清单。

## 7. 写入与校验

```powershell
# 建目录（Trae 侧 Python API；原 scripts/drama_cover.py prepare 已移除）
python -c "import sys; sys.path.insert(0,'lib'); import job_manifest as jm; jm.create_job(series_dir=r'D:\短剧\重生八零，打猎卖货养妻女')"

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

# 写剪映草稿封面（本 skill 内置；draft_cover.jpg + 原生 composition，缺 composition 会被首帧覆盖）
python scripts/drama_cover.py write-draft --cover "<封面图>" --draft-dir "<草稿目录>"
python scripts/drama_cover.py full --video "<mp4>" --title "<标题>" --out "<输出目录>" [--draft-dir "<草稿目录>"]

# 落盘两阶段调度（监听 cover_job，产物齐自动跑；串行、每轮只推进一部）
#   edit 阶段：validate --fix → ingest（含 dispatch 归并）→ batch_runner --no-export（生成串流草稿+帧对齐）
#   export 阶段：导出 mp4 → verify 质检 → record-video → derive → Toast
python lib/cover_watch.py --root "D:/短剧"                 # 扫盘自动发现 <剧集>/cover_job（推荐）
python lib/cover_watch.py plan --root "D:/短剧"            # 只查看：各剧阶段/意图来源/rev/下一步/被挡原因
python lib/cover_watch.py --hold --once                    # 只列计划不执行（先核对 job.json 再放行）
python lib/cover_watch.py --phase export --once            # 单轮强制只推进导出阶段（配合 export_policy=manual）
python lib/cover_watch.py status --job "<cover_job>"
python lib/cover_watch.py inspect --job "<cover_job>"      # 只读聚合：intent / edit / export（不落盘、不触发调度）
python lib/cover_watch.py          # 无参：读 <repo>/pipeline/presets.json 的 watch 段（每轮热加载）

# 归档：置 stage=archived 并搬入独立归档池（硬拦 watcher；先 --dry-run 核对计划）
python pipeline/archive_job.py --job "<cover_job>" --archive-root "D:/短剧_存档" --dry-run
python pipeline/archive_job.py --job "<cover_job>" --archive-root "D:/短剧_存档"
# watch 段：{"roots":["D:/短剧"],"jobs":[],"exclude":[],"settle":30,"interval":5,
#            "derive":[],"export_policy":"immediate","hold":false,
#            "cold_start":true,"export_stall_timeout":1800,"retry_pause_after":5,
#            "archive_root":"D:/短剧_存档"}
#   export_policy: immediate（剪完即导，默认）/ after_edit_queue_drained（全部剪完才导出）/ manual（等 --phase export）
#   archive_root: 归档池根目录，仅 pipeline/archive_job.py 使用；必须位于 roots 之外（见 §7.4）
#   derive: 全局派生平台列表，默认 []（不派生）；剧级声明优先走 dispatch.json
#   cold_start: 每部剧导出前冷启动剪映（杀进程→拉起→等首页），默认 true；手动同时用剪映时设 false
#   export_stall_timeout: 导出「无进展」判定卡死的秒数，默认 1800（进度 json 每 30s 刷新，仍在写盘不误杀）
#   retry_pause_after: 连续导出失败多少次后暂停该剧，默认 5；退避 1/2/4/8/16/30 分钟，见 §7.1

# 调度意图（豆包侧声明）：写 <cover_job>/dispatch.json，ingest 时归并进 job.json.workflow
# {"version":1,"mode":"full","cut_s":15,"derive":["youtube","tiktok"],"priority":0}
#   mode: full（剪辑+导出，默认）/ edit_only（只剪辑停在草稿）
#   cut_s: 正数秒裁剪；0/省略 = 不裁。derive: []/省略 = 不派生；支持 ig/facebook/all
#   归并规则：intent_revision==0 时 dispatch 全量归并（幂等）；人工把 intent_revision +1 后
#   job.json 永远优先，dispatch 只补缺失不覆盖（冲突记 watcher.log + 桌面通知）
#   人工改意图 = 直接编辑 job.json.workflow 并把 workflow.intent_revision +1（程序永不写该字段）

# 旧版兼容：<cover_job>/export_config.json（{"cut_s": 7200, "derive": [...], "start_ep": 1 }）仍被读取：
#   cut_s / derive 由导出阶段消费；start_ep 由 watcher 剪辑阶段消费（整数，缺省 1）。
#   start_ep > 1 时 watcher 拼编辑命令追加 --only <start_ep>..（从该集起重剪到最后一集）；
#   缺省 / <=1 / 非整数 → 一律按 1（记一行日志，不中断），行为与旧版一致。
#   注意：从中间集起跑会让串流草稿名变成「<剧名>_第NN-MM集串流」，不再是「第01-MM集」。
# 但新任务一律用 dispatch.json；cut 解析优先级 workflow.cut_s > video.cut_s（set-cut 写的旧字段）

# 任意阶段更新
python lib/job_manifest.py update --job "<cover_job>" --patch-file patch.json `
  --event stage2_covers --detail "4 张封面落盘"

# 校验
python lib/job_manifest.py validate --job "<cover_job>"
python lib/job_manifest.py validate --job "<cover_job>" --fix   # 先修复 gate/selected 非法形态并补算 null hash 再校验
```

并发保护：同一任务同一时间只允许一个阶段写；如需并行，先加 `cover_job/.lock` 并在写完删除。
导出互斥：repo 级全局锁 `.export.lock`（`lib/export_lock.py`，pid 判活）保证同一时刻只有一个 batch_runner 导出；watcher 导出步自动持锁，手工跑遇活锁报错退出，`--force` 可强跑。

### 7.1 导出状态真源 `.export_state.json`

导出阶段的「是否已导 / 失败几次 / 是否暂停 / 进度到哪」只读这一份文件，不再分散判断。

- **位置**：`<cover_job>/.export_state.json`（与 `.watcher.lock` 同级）。运行态文件，**仅当该剧真实导出开始时（`lib/export_state.py` 的 `begin()`）创建**；只做计划/闸门评估（如 `cover_watch plan`）不会创建或迁移该文件。未进闸门的剧（已 `video_ready`、缺图/缺 prompts、gate 未 done 等）不会生成该文件。无需手工建。
- **唯一写者**：`lib/export_state.py`，且只由 `cover_watch` 的导出链路调用。剪映导出子进程、`job_manifest`、`series_runner` 都不写它。`job.json` 的 `video.status` 是展示字段，由本真源派生（真源 `done` → `video.status="done"`），任何地方不得绕过真源直接改。
- **原子与幂等**：`.tmp` + `os.replace` 写入；`succeed` 对已 `done` 原样返回不重写。

状态取值与转移：

| 状态 | 谁写 / 何时 | 下一步允许 |
| --- | --- | --- |
| `pending` | 迁移/初始化（无失败记录） | → `exporting` |
| `exporting` | 导出启动前 | → `done` / `retrying` / `paused`；心跳超时被回收为 `retrying` |
| `retrying` | 失败未超阈值；或 `exporting` 心跳超时回收 | 退避结束 → `exporting`；再失败超阈值 → `paused` |
| `paused` | 连续失败 ≥ `retry_pause_after` | 仅 `--phase export` 人工放行 → `exporting`（**不清零计数**） |
| `done` | 导出成功（record-video 后） | 终态；重导需人工删本文件或 `--phase export` |
| `failed` | 不可重试硬失败（素材缺失/磁盘不足） | 终态，需人工处理 |

字段含义：

| 字段 | 说明 |
| --- | --- |
| `status` | 上表六态之一 |
| `attempts` | 本轮成功前的尝试次数（成功清零） |
| `consecutive_failures` | 连续失败次数（驱动退避/暂停；成功清零；硬失败不递增） |
| `last_error` | 最近一次失败原因 |
| `next_retry_at` | 下次可重试的 epoch 秒（0=立即可重试） |
| `progress` | 0–100 进度快照（实时值仍在导出子进程的 `.progress.json`） |
| `heartbeat_ts` | epoch 心跳；`exporting` 时与 `.progress.json` mtime 取大者判活 |
| `updated_at` / `writer` | 最近写入时间 / 写入者 pid |
| `phase` | 当前子阶段（`cold_start`/`export`/`verify`），供追溯 |
| `failure_class` | 失败分类（见下），默认 `unknown` |
| `last_recovery_action` | 最近一次自愈动作（`soft_home`/`kill_restart`/`dismiss`/`dismiss_then_kill`），只留最近一次 |
| `last_recovery_at` | 最近一次自愈动作时间（ISO） |

**崩溃恢复**：`exporting` 且心跳超过 180s 无更新 → 判定上次导出崩溃，回收为 `retrying`。心跳 = `max(heartbeat_ts, .progress.json mtime)`，子进程仍在写盘则不误杀。

**失败分类 `failure_class`**（`lib/failure_class.py`，写入本真源）：

| 类别 | 可重试 | 动作 | 计数 |
| --- | --- | --- | --- |
| `ui_hang` | 是 | 软关回首页（关闭按钮失败才重启）重试 | 是 |
| `asset_missing` | **否** | 直接终态 `failed` + 通知 | 否 |
| `disk_full` | **否** | 直接终态 `failed` + 通知 | 否 |
| `export_timeout` | 是 | **强制杀进程重启**（经管理器，不走软关）重试 | 是 |
| `version_dialog` | 是 | 先关弹窗做就绪判断，就绪即软回；**未就绪即升级强杀重启** | 是 |
| `unknown` | 是 | 软关回首页（失败才重启）重试 | 是 |

实际执行的自愈动作（`soft_home` / `kill_restart` / `dismiss` / `dismiss_then_kill`）由导出子进程写入进度侧写 `.progress.json` 的 `recovery_action` 与流程日志；`cover_watch` 在导出收尾时从 sidecar **读回**，经 `export_state.py` 写入真源 `last_recovery_action`/`last_recovery_at`（**唯一写者仍是 `export_state.py`**，sidecar 无该字段则不写）。可追溯「这次是软回还是真杀」；杀进程只经 `jy_instance.kill()`。

**迁移优先级**（真源缺失时按旧数据一次性推断，并把 `.export_retry.json` 备份为 `.bak`）：
1. `job.json` `video.status=="done"` 或 `stage=="video_ready"` → `done`（已导出事实不可回退）
2. `.export_retry.json` `paused` → `paused`
3. `.export_retry.json` `fails>0` → `retrying`（继承计数与退避点）
4. `.progress.json` `status==running` 且 mtime 新鲜 → `exporting`（交崩溃恢复判定）
5. 其余 → `pending`

**暂停放行**：`paused`/`failed` 只能 `python lib/cover_watch.py --phase export --once` 人工强制放行，或删除本文件重建；放行**不清零** `consecutive_failures`，若再失败会继续计数。

### 7.2 裁剪分歧通知（`workflow.cut_s` vs `export_config.cut_s`）

- **触发条件**：`workflow.cut_s` 与 `export_config.cut_s` 均非空且不等（`_effective_cut`）。
- **三档语义**（`_round_flags`）：预览/只读（`plan`、`--dry-run`、`--hold`）**只打印**，不弹通知、不落日志；`--once` 真实执行**只落日志、不弹通知**；持续 watcher 真实轮次**落日志 + 弹通知**。
- **日志落点**：真实执行轮次（持续 watcher 与 `--once`）触发时写该剧 `cover_job/watcher.log`，内容含剧名、两来源值、最终采用值、优先级分支（rev=0 / rev>0）；同一对值不重复刷日志。
- **去重持久化**：落盘在 `cover_job/.cut_conflict.json`（原子写），分 `logged` 与 `notified` 两个独立通道，各记 `wf_cut`/`ec_cut`/`at`。语义为「**值相比上次做过该动作发生变化才再做一次**」：同一对值重复出现（含进程重启）不重复写日志、不重复弹通知；任一值变了才各自再触发。分通道是为了让 `--once` 记日志不吞掉连续 watcher 的首轮通知。
- 该文件仅是通知去重态，不是导出状态真源（真源仍为 `.export_state.json`，见 §7.1）。

### 7.3 剪辑阶段状态机 `.edit_state.json`

edit（剪辑）阶段的「失败几次 / 是否退避 / 是否暂停」只读这一份文件，与导出状态**互相独立**（剪辑失败不影响导出计数，反之亦然）。

- **位置**：`<cover_job>/.edit_state.json`。运行态文件，首次 edit 闸门判定时自动创建，无需手工建。
- **唯一写者**：`lib/edit_state.py`，且只由 `cover_watch` 的剪辑链路调用。`batch_runner`、`job_manifest` 都不写它；`stage` 等契约字段仍由 `job_manifest` 写，本文件不碰 `job.json`。
- **状态**：`pending`（可剪辑）/ `editing`（batch_runner 执行中）/ `retrying`（退避窗口内）/ `paused`（连续失败暂停）/ `done`（草稿建成）/ `failed`（硬失败终态）。
- **退避与暂停**：可重试失败按 1/2/4/8/16/30 分钟退避（与导出侧同一序列）；**连续 3 次**置 `paused` 并 toast（比导出侧的 5 次更早——剪辑失败多为数据问题，重试救不回来）。
- **失败分类**：复用 `lib/failure_class.py`。`asset_missing` / `disk_full` 等不可重试类 → 直接 `failed` 终态、**不计数**、toast；其余按可重试走退避。
- **可自愈失败（不占额度）**：「缺少目录: …」这类环境缺陷由 watcher 一次自动补齐标准目录（`job_manifest.JOB_DIRS` 模板），写 `watcher.log` 一行「已自动补齐缺失目录…不计入重试」，状态回 `pending` 下一轮立即重试；不递增 `consecutive_failures`、不进退避。同一目录只自愈一次（记录在 `healed_dirs`），补过仍缺按普通失败计数。
- **闸门**：`build_plan` 的 edit 分支先过本闸门——`paused`/`failed`/退避窗口内 → 计划被挡并给出原因（格式与导出侧一致）；`--phase edit` 人工强制放行，**不清零**计数。`editing` 不拦截（watcher 串行 + `.watcher.lock` 已防并发，崩溃残留允许下一轮直接重试）。
- **收尾**：剪辑成功 → `done` 并清零；之后无草稿才会再进 edit，届时重新计数。
- **与 `.export_state.json` 的分工**：草稿建成前归本文件管，导出闸门之后归 §7.1 真源管；两文件同级并存、互不读写。

### 7.4 归档（`stage=archived` + 归档池）

`archived` 是合法 stage，含义是「这部剧本该结束了：已发布或成品已清理，不再参与调度」。watcher 的 `check_trigger` 对它**硬拦**，且硬拦发生在阶段/单剧过滤之前——`--phase` / `--once` / `--job` 都绕不过去。

- **唯一写者**：`pipeline/archive_job.py`。非 dry-run 时依次：
  1. `job_manifest.update_job` 置 `stage=archived`（`revision+1`，`history` 记 `archived`，detail 含目标路径）；
  2. 把整剧目录 `<root>/<剧名>` 移到 `<archive_root>/<剧名>`（同盘即改名）；
  3. 改写 `pipeline/_stream_manifests/*.json` 中 `paths.source_dir` 指向旧目录的记录（`.tmp` + `os.replace` 原子写）。
  `--dry-run` 只打印计划与待改 manifest 条数，不写盘、不移动。
- **准入**（默认满足其一；`--force` 跳过）：`publish.status=="published"` 或 `video.export_state=="cleaned"`。
- **拒绝/跳过**（首次归档）：`.watcher.lock` 的 pid 仍存活（活锁）；`.export_state.json` `status=exporting`（导出进行中）；归档目标已存在（不覆盖，需人工处理）；`archive_root` 等于或位于任一 `watch.roots` 之下（按规范化真实路径比较）；`watch.roots` 未配置时 **fail-closed 直接拒绝**（无法校验归档池位置）。续做（已 archived）只重复位置与目标校验，不再查活锁/导出中。
- **断点可重入**：`stage` 已 `archived` 且源目录仍在、目标不存在 → 继续移动；源已不在且目标存在 → 跳过（视为完成）；两者都不存在 → 报错。因为 `stage` 先写、目录后搬，移动失败也不会再被 scheduler 选中，修复占用/空间后重跑同一命令即可续做。
- **归档池位置**：`--archive-root` 优先，未传时读 `watch.archive_root`；建议与 `watch.roots` 同盘、独立根（如 `D:/短剧_存档`），不要放在 roots 内。
- **必须同步 manifest**：`_stream_manifests` 里的 `paths.source_dir` 是后续 `stream_manifest.py clean` / 回执定位的入口；不改写会出现「剧已搬走、清理仍按旧路径找源」的空转。脚本已自动改，手工搬目录时必须照做。
- **回迁**：目录移回 roots 后不会自动恢复调度，仍需人工把 `job.json.stage` 改回 `video_ready`（或其他待办阶段）。
- **`publish.status` 现状**：只有手工 `python lib/job_manifest.py publish-record ...` 会写 `published`；自动发布链**没有写入路径（暂未实现）**。因此不要依据 `publish.status` 判断「已发布」，归档准入实际以 `video.export_state=="cleaned"` 为准，或显式 `--force`。

### 7.5 缺字幕导出硬拦（`derived.subtitle_missing`）

- **记录**：pipeline 逐集统计，ASR 三档全失败、或裁剪后最终字幕条数为 0 → 记集号；整轮结束经 `job_manifest.sync_subtitle_missing` → `update_job(event="subtitle_missing")` 一次落盘（升序去重、`revision+1`、`history` 追加）。不中断整轮，后续集继续跑。
- **清除**：只有该集重新剪辑并且**本集最终字幕条数 > 0** 才从清单移除；仅重跑仍为空时继续保留。清单清空后由 `cover_watch.note_edit_result(ok=True)` 调 `export_state.clear_block`，把 `failed` 复位为 `pending`。
- **硬拦（所有导出入口）**：`job_manifest.require_export_allowed(job_dir)` 非空即抛 `ExportBlocked`，入口共五处：
  1. `pipeline.run_pipeline` 的 `do_export` 分支（含本轮内存失败集兜底，job.json 缺失也能拦）；
  2. `lib/cover_watch.py::run_export`（watcher 导出主链，先于状态机与子进程）；
  3. `pipeline/batch_runner.py --auto-export`；
  4. `pipeline/series_runner.py::_run_export`（多剧编排）；
  5. `.trae/skills/jianying-editor/examples/robust_auto_export.py` 直调（打印集号后 `sys.exit(2)`）。
  `--phase export` 人工放行**也不能绕过**该闸门。
- **失败态**：watcher 导出入口被拦时写 `.export_state.json`：`status=failed`、`failure_class="subtitle_missing"`、`phase="export"`、`retryable=False`（不计数、不退避），等人工修复；重复检查只追加一行日志，不重复打全量集号。
- **收尾通知**：整轮结束若清单非空，`lib/subtitle_guard.py::notify(job_dir)` 只弹**一条**「⚠️ 缺字幕需人工处理」，正文列全部集号；进程内按 `job_dir` 去重，绝不按集逐条弹。
- **校验**：`validate_job` 要求该字段为非 bool 正整数数组且严格升序去重；违规形态由 `update_job` 拒绝落盘。

## 8. 各阶段产物与意义

| 阶段 | 写入 | 产物 | 意义 |
| --- | --- | --- | --- |
| Stage 0 | `01_input/`、`job.json` | `synopsis.txt`、`reference.png` | 输入固化进任务区，后续不依赖原始素材位置 |
| Stage 1 | `02_titles/` | `prompt.txt`（含 sha256）、`titles.json`、`titles.md` | 提示词快照可复现审计；json 是机器交接源；md 是可选人读评审稿，删了不影响流程 |
| 门禁 | 只写 `job.json` + 标注 `titles.json` | `titles.selected`、`selected_by`、`cover_map` 回填；`titles.json` 选中条加 `selected/slot`、顶层记 `gate/selected_at/selected_by` | 最终选定结果只此两处（job.json 为事实源，titles.json 原位标注不复制文本） |
| Stage 1.5 | `03_prompts/` | `prompts.json` / `prompts.md` | 4 条出图提示词，豆包/火山/Trae 三渠道共用 |
| Stage 2 | `04_covers/` | 3 横版 + 1 竖版 + `derived/` | 平台用图；derived 放 YouTube 16:9 等派生尺寸 |
| pipeline | 只写 `job.json` | `video.export_dir/export_file/draft_name/companions/export_state`；`derived.subtitle_missing` | 只记录导出位置（允许绝对路径），不复制 mp4；`05_video/` 留作需要副本时的可选目录。`record_video` 置 `export_state=exported`；`stream_manifest.py clean` 真正删除成品后置 `cleaned`（history 记 `export_cleaned`）。pipeline 每集统计漏字幕，整轮结束经 `update_job(event="subtitle_missing")` 一次落盘；该清单非空时不导出 |
| Stage 3 | `06_publish/` | `<platform>.json` + `receipts/` | 每平台状态与回执，驱动 `cleanup` 判定；`receipts/` 由 `create_job` 预建占位，内容（回执）只在 Stage 3 首次写入时落盘，缺失不阻断前序阶段 |
