# 分阶段重跑（Stage Rerun）规格

> 状态：**只写规格，不改代码**。落地等 A 线退出后再议；本轮不动 `pipeline.py`、ingest 判定、`batch_runner`。
> 边界：不改 `pipeline/presets.json`、不动 `<剧集根>` 数据、不启停剪映和 watcher。

## 0. 要解决的问题

实测代价：A 版只需要补一张封面，走 `python lib/cover_watch.py --phase edit --job <cover_job>`，
在入口被 ingest 的 stage 校验拦下：

```
ingest 要求 stage ∈ titles_selected/prompts_ready/covers_ready，当前 'video_ready'
```

唯一官方退回口是 `reopen --to covers_ready`，它把 `video.status` / `video.export_state`
清空并删除 `.export_state.json`（`lib/cover_watch.py:2551-2563`），于是下一轮 edit 把
整条链重跑：抽音轨 → demucs → ASR → 翻译 → 字幕合并 → 重建草稿 → 导出。

本规格给「只重跑某个阶段」的入口定契约：**保留已导出的下游成果，只重做指定单元**。

## 1. 现有 stage 枚举与真实输入/输出

stage 定义：`lib/job_manifest.py:50-60`。stage 只由各写入方推进（只进不退）；
`reopen` 是唯一允许回退的例外（`lib/cover_watch.py:2493`）。

| stage | 谁推进 | 真正输入 | 真正输出 |
|---|---|---|---|
| `prepared` | `prepare`（drama-cover-builder） | `01_input/synopsis.txt`、`05_video/*.mp4` | 目录骨架 + `job.json` 最小骨架 |
| `titles_done` | 标题生成 | `01_input` | `02_titles/titles.json`（候选） |
| `titles_selected` | 选标题 | `titles.json` + 人工选择 | `titles.selected[]`、`02_titles/prompt.txt`、`titles.gate=done` |
| `prompts_ready` | 提示词生成 | 已选标题 | `03_prompts/prompts.json`、`prompts.md` |
| `covers_ready` | 封面生成 / `ingest` | prompts + 封面图 | `04_covers/*.png`（4 张基础 + 账号图）、`cover_map[]` |
| `video_ready` | 导出成功（`record_video`） | 草稿 + 媒体 + 预设 + 封面 | 各版本草稿、mp4、`video.outputs[<版本>]`、`export_state=exported`、`video.status=done` |
| `publish_ready` | 发布打包 | mp4 | `06_publish/receipts/` |
| `published` | 发布回执 | 打包产物 | `publish.status=published` |
| `archived` | `archive_job` | 发布/清理完成 | `stage=archived` + 整剧移入归档池 |

关键事实：**`covers_ready` 阶段本身没有草稿**。草稿是在 edit 链路里现建的，
导出成功后才把 stage 推到 `video_ready`。所以 `video_ready` 时草稿、成片都在，
这正是「只想补封面」被挡住的原因。

## 2. 执行单元（execution unit）

stage 是对外的粗粒度契约；重跑需要更细的单元。定义 8 个单元：

| 单元 | 对应实现 | 产出 |
|---|---|---|
| `titles` | 标题生成 | `02_titles/titles.json` |
| `prompts` | 提示词生成 | `03_prompts/prompts.json/md` |
| `covers` | 封面图生成 | `04_covers/*.png`、`cover_map[]` |
| `edit` | 现 `pipeline.py` 全链（Step 1–10.9） | 各版本 `draft_content.json` |
| `cover_bind` | 仅 Step 10.9（`set_cover_composition` + `set_draft_cover`） | 现有草稿的 `cover` 字段 + `Resources/cover/*` |
| `verify` | Step 10.5 / 10.8（`verify_draft` + `repair_translation`） | 修好的草稿（按需） |
| `export` | Step 11 / `series_runner` | mp4 + `video.outputs[<版本>]` |
| `publish` | 打包/回执 | `06_publish/receipts/`、`publish.status` |

`edit` 内部再拆可复用子步（为后续断点续跑准备，见 §5）：

```
demucs → audio → asr → translate → subtitles → preset_build → verify → cover_bind
```

## 3. 哪些单元允许单独重跑

| 单元 | 允许的 stage | 前置条件 | 触碰草稿 | 触碰成片 |
|---|---|---|---|---|
| `titles` | `prepared` … `published`（非 archived） | `01_input` 可读 | 否 | 否 |
| `prompts` | `titles_selected` … `published` | `titles.gate=done` | 否 | 否 |
| `covers` | `titles_selected` … `video_ready`/`published` | prompts 存在、封面槽位齐全 | 否 | 否 |
| `cover_bind` | 草稿存在（`covers_ready` … `published`） | 目标版本草稿可读、封面图存在 | 是（仅 cover） | **否** |
| `edit` | `covers_ready`（或显式 `--force` 在 `video_ready`） | 输入齐全、无活锁 | 是（重建） | 是（标 stale） |
| `verify` | 草稿存在 | 草稿可读 | 是（仅修复时） | 是（修复时标 stale） |
| `export` | 草稿存在；`export_state != cleaned`；非 archived | 无活锁、`.export_state.json != exporting` | 否 | 是 |
| `publish` | mp4 存在 | 无活锁 | 否 | 否 |

统一拒绝条件（所有单元）：

- `stage=archived` → 拒绝（需先人工移回归档区）。
- `.watcher.lock` 的 pid 存活 → 拒绝（等本轮结束）。
- `export` 额外拒绝 `.export_state.json status=exporting`。
- `export` 拒绝 `video.export_state=cleaned`（与 `check_trigger` L192-195 一致；
  清理后重导属于「重新产出」，应走 `reopen`，不是 rerun）。

### 3.1 触发条件

- 人工显式调用（本入口是人工命令，不挂 watcher 自动决策）。
- `--dry-run` 必须只读：打印将执行的单元、目标版本、会写的文件、freshness 影响，不落盘。
- 非交互环境必须 `--yes`（对齐 `cmd_reopen` 的约定，`cover_watch.py:2540-2546`）。

## 4. 入口设计

新增子命令（名字可调，语义固定）：

```
python lib/cover_watch.py rerun --job <cover_job> --unit <unit>
       [--versions a,b] [--dry-run] [--yes]
       [--force]            # 允许 edit 在 video_ready 上执行
       [--resume <journal>] # 中断后继续
       [--rollback <journal>]
```

约束：

1. **不调用 `ingest_job` 的 stage 闸门**（`lib/job_manifest.py:2756-2758`）。
   rerun 用只读的 `ensure_inputs(unit)` 校验输入，不推进 stage、不改 stage。
   `ingest` 保持原契约，仅服务「首次 edit」路径。
2. **不修改 `video.outputs` / `video.export_state` / `video.status`**（`export`/`publish`
   成功时按正常写入方更新台账，其余单元一律不碰）。
3. 所有写盘走统一原语 `pipeline/draft_io.py`（另一会话正在收口；本规格只依赖其
   `write_draft_content(draft, path, job_dir=..., writer=...)` 契约）。
4. 每个单元在执行前后各写一条 `job.history`：`rerun.<unit>.start` / `.done` / `.failed`。
5. 同一时刻只允许一个 rerun（`.watcher.lock` phase=`rerun:<unit>`）。

## 5. `edit` 的断点续跑（阶段 2，本规格只定契约）

现状：`pipeline.py` 每次从预设克隆/重置草稿，demucs、ASR、翻译都无条件重跑；
`vc_asr.srt` 在每集循环里被覆盖（`pipeline/pipeline.py:2391-2440`），没有可复用的
逐集产物。因此「只重跑字幕」目前无法实现。

为支持部分重跑，规定中间产物契约（落在 `pipeline/_work_<draft>_<tag>/`）：

| 子步 | 产物 | 复用键 |
|---|---|---|
| demucs | `demucs_epNN/<model>/<stem>/{vocals,no_vocals}.wav` | 源片 `(basename,size,duration)` |
| audio | `audio_epNN.wav` | 同上 |
| asr | `asr_epNN.json`（**新增，逐集**） | audio 指纹 |
| translate | `translation_epNN.json`（**新增**） | asr 指纹 + 目标语言 |
| subtitles | `subtitles_epNN.json`（**新增**） | translation 指纹 + 拆分规则版本 |

规则：子步命中缓存且指纹一致 → 跳过；指纹不一致 → 只重跑该子步及其下游。
`edit --force` 才允许无条件全跑。**在缓存契约落地前，`edit` 只允许整段重跑**，
不允许承诺「只重跑 ASR」。

## 6. 保留下游已有成果

### 6.1 默认规则

- rerun 不删任何已导出 mp4，不删 `video.outputs` 历史。
- `covers` / `cover_bind` / `publish` 不触碰 `_export_stale.json`，不触发重导。
- `edit` / `verify`（发生修复）会写 `_export_stale.json`，把旧成片标为过期；
  旧 mp4 文件保留，等新导出覆盖或人工清理。
- `export --versions a` 只更新 a 的台账；b 的 `outputs[b]` 原样保留。

### 6.2 已导出产物在 freshness 里的身份

- `edit` 成功 → E1 `valid`，E2 `stale(draft_changed)`，旧成片仍在但不再被当作有效。
- `cover_bind` 成功 → E1 更新为 cover-only 写入，**E2 保持 `valid`**（见 §7.2）。
- `export` 成功 → 该版本 E2 `valid`，记录新 size/ffprobe 身份。
- `publish` → 不改变任何 edge。

## 7. 与产物新鲜度机制（`lib/freshness.py`）对接

唯一判定入口仍是 `freshness_of(job_dir, version, consumer)`；写时确权仍是
`record_draft_write` / `record_cover` / `record_export`。rerun 只是新的**写入者**，
不新增第二套判定。

### 7.1 单元 → edge 影响

| 单元 | E1 `prompts_to_draft` | E2 `draft_to_video` | E3 `cover_to_draft` |
|---|---|---|---|
| `titles` / `prompts` | 输入变了 → `stale(intent)`（不自动重建） | 不动 | `stale(covers_pending_regeneration)` |
| `covers` | 不动 | 不动 | `stale`（等 bind） |
| `edit` | `valid`（新指纹） | `stale(draft_changed)` + `_export_stale.json` | `valid`（10.9 已挂） |
| `cover_bind` | 更新（`writer=cover_only`） | **保持 `valid`**（见 7.2） | `valid` |
| `verify` | `valid`（仅在发生写回时） | `stale`（仅在发生写回时） | 不动 |
| `export` | 不动 | `valid`（仅目标版本） | 不动 |
| `publish` | 不动 | 不动 | 不动 |

结论：**重跑不必然让下游 stale**。只有真正改时间线的写入才让 E2 stale；
纯封面绑定不改时间线，E2 保持 valid。

### 7.2 cover-only 写入契约（新增，落地时与 `draft_io` 对齐）

现状 `record_draft_write` 无条件把 E2 置 `stale`（`lib/freshness.py:653-685`）。
封面绑定会改 `draft.cover` 与封面 composition 素材，草稿 hash 会变，但**时间线没变，
渲染帧不变**。因此新增：

```
record_cover_write(job_dir, draft_path, writer="cover_only")
  - 追加 E1 写入日志：{writer:"cover_only", output_hash:新值, timeline_unchanged:true}
  - E1 → valid（output_hash/weak_hash 用新值；输入 hash 不变）
  - E2 不动（state/input_hash 保留），记 e2.cover_only_since_export = 时间
```

`freshness_of(E2)` 处理：

1. 草稿 hash == E2 记录 → `valid`；
2. 草稿 hash != 记录，但写入日志里同 hash 的那条 `timeline_unchanged=true`
   → `valid(reason="cover_only")`（自愈，不重导）；
3. 其余情况维持现有逻辑（`_export_stale.json` 优先 → `stale(draft_changed)`）。

同时规定：

- `cover_bind` **不得**写 `_export_stale.json`；
- `cover_bind` 不得改 `video.outputs` / `video.export_state`；
- 若封面未来被放进时间线（当前不是），必须退回普通 `record_draft_write`，E2 正常 stale。

### 7.3 `evidence_missing` / `legacy(baseline)`

rerun 不改变 §12 的反向检测：`export` 成功仍必须调用 `record_export`；漏写仍报
`evidence_missing`。`cover_bind` 不产生新成片，不涉及 sidecar。
上线前导出的历史产物继续由 `data/freshness_legacy_baseline.json` 兜底。

## 8. 与 ingest / reopen / archive / clean 的关系

### 8.1 `ingest`

- `ingest_job` 现在是「首次 edit」的输入归一 + stage 闸门，只接受
  `titles_selected|prompts_ready|covers_ready`（`job_manifest.py:2756`）。
- rerun **不复用这条闸门**；新增只读 `ensure_inputs(unit)`：
  - 校验单元所需文件存在且可解析；
  - 不推进 stage、不写 job.json；
  - `archived` 直接拒绝。
- 长期可把 `ingest` 的 stage 条件放宽为「≥ titles_selected 且非 archived」，
  但那是另一件事，落地前先保持 `ingest` 原样。

### 8.2 `reopen`

- `reopen` 是「整段回退重做」的大锤：清 `video.status` / `video.export_state`、
  删 `.export_state.json`、stage 回退。
- `rerun` 是「保留下游、只重做单元」的手术刀：**绝不**清这两个字段、不回退 stage。
- 出现下列情况仍应使用 `reopen`：成品已 `cleaned` 且要重新产出；需要重跑整条
  edit 链且不在 `covers_ready`；stage 已损坏需要人工回退。

### 8.3 `archive` / `clean`

- `stage=archived` → rerun 一律拒绝（与 `check_trigger` L187-190、`archive_job` 一致）。
- `video.export_state=cleaned` → `export` 拒绝；`covers` / `cover_bind` / `publish`
  不受影响（不产出新成品）。
- `clean` 之后草稿可能已被删：`cover_bind` 前必须校验草稿存在，不存在则明确报错，
  不自动重建（重建属于 `edit`，成本高，需人工显式选择）。
- rerun 不写墓碑、不改 `06_publish/freshness_tombstones/`。

## 9. 失败与中断回滚

目标：**不留半新半旧的草稿**。

1. **执行日志（write-ahead）**
   `<job>/06_publish/rerun/<ts>_<unit>/plan.json`：
   `{unit, versions, stage_before, job_revision_before, will_write[], status:"running"}`。
   结束写 `done.json` 或 `failed.json`（含错误摘要与已完成的原子步骤）。
2. **job.json 快照**
   第一次改 job.json 前复制到同目录 `job.before.json`；失败时恢复并
   `revision+1`，history 记 `rerun.<unit>.rolled_back`。
3. **草稿写盘**
   统一走 `draft_io.write_draft_content` 的临时文件 + `os.replace`；写前把原
   `draft_content.json` 复制为 `.rerun_backup_<ts>.json`。失败 → 恢复备份，
   不留下截断/半写 JSON。
4. **封面文件**
   新封面先写 `.tmp` 再 `os.replace`；旧图保留为 `.bak` 直到 `cover_bind` 成功；
   失败 → 恢复旧图，E3 不更新。
5. **导出**
   台账只在 `verify_export` 通过、`record_video` 调用成功后才更新；
   导出失败保持 `_export_stale.json` 与原 `outputs` 条目不变，旧 mp4 不删。
6. **中断恢复**
   - 发现 `plan.json` 仍为 `running` 且锁 pid 已死 → 默认拒绝新 rerun，提示二选一：
     `--resume <journal>`（幂等重做未完成步骤）或 `--rollback <journal>`（恢复 job/草稿/封面）。
   - 幂等：`covers` 覆盖同图、`cover_bind` 重复绑定、`export` 覆盖同版本，均安全。

## 10. 「只补封面」的最短路径（本规格的核心场景）

前提：A 版已 `video_ready`，草稿与 mp4 都在，只想换封面。

```
# 1) 重画目标封面（只写 04_covers，不碰草稿）
python lib/cover_watch.py rerun --job <cover_job> --unit covers --versions a --yes

# 2) 把封面绑到现有草稿（不重建、不重导）
python lib/cover_watch.py rerun --job <cover_job> --unit cover_bind --versions a --yes
```

预期：

- 只执行 `set_cover_composition` + `set_draft_cover` + `save_draft` + `record_cover`
  + `record_cover_write`；
- **不跑 demucs / ASR / 翻译 / 字幕 / 重建草稿 / 导出**；
- E2 保持 `valid`，`_export_stale.json` 不出现，`video.outputs[a]` 不变；
- 若平台需要更新缩略图/包装，只重新上传包装，不重渲染视频。

若封面确实被当作视频首帧内容（当前实现不是），则退化为：

```
python lib/cover_watch.py rerun --job <cover_job> --unit edit --versions a --yes
```

此时 E2 stale、需要重新导出，这是正确代价。

## 11. 验收（落地时逐条测）

1. `cover_bind` 在 `video_ready` 上成功，且 `_export_stale.json` 不存在、
   `video.outputs` / `export_state` 不变。
2. `cover_bind` 后 `freshness_of(a, scheduler).state == "valid"`，`reason` 为
   `cover_only`；`scheduler` 不重导。
3. `covers` + `cover_bind` 全程不产生新的 ASR/翻译调用（可用日志断言）。
4. `edit` 在 `video_ready` 不带 `--force` 时拒绝，并提示用 `reopen` 或 `--force`。
5. `export --versions a` 只改 a 台账，b 的 `outputs[b]` 逐字节不变。
6. `stage=archived` / 活锁 / `exporting` 三种情况全部拒绝且不写任何文件。
7. 人为让 `cover_bind` 在写盘后抛错：草稿恢复原字节，job.json 恢复，
   history 有 `rolled_back`，无 `.tmp` 残留。
8. 中断后 `--resume` 幂等完成；`--rollback` 恢复 job/草稿/封面。

## 12. 非目标

- 本轮不改任何代码；不改 `pipeline/pipeline.py`、`ingest_job`、`batch_runner`。
- 不改变 stage 契约与现有 watcher 自动调度规则。
- 不引入新的自动重试/自动重导；rerun 始终是人工命令。
- 不解决 `edit` 子步缓存落地（§5 只定契约，落地另排）。
