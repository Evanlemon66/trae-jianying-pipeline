# 产物新鲜度（Artifact Freshness）规格

> 状态：设计已批准，本文件只写规格，未改动任何代码。
> 实施边界：只改仓库内代码；不读写 `D:\短剧` 下的 job 数据（除用户显式指定 job 运行 `check`）；不碰正在跑的 A 版 edit→export；不自动重导、不自动归档。

## 0. 模型

三条边，各自维护「输入指纹 → 输出指纹」：

| 边 | 输入 | 输出 | 状态存放 |
|---|---|---|---|
| E1 `prompts_to_draft` | 提示词/标题 + 剪辑指令 + 预设 + 媒体清单 | 草稿 `draft_content.json` | `job.freshness.edges.prompts_to_draft` |
| E2 `draft_to_video` | 草稿内容 | 成片 mp4 | `job.freshness.edges.draft_to_video` |
| E3 `cover_to_draft` | 版本封面图 | 草稿封面 `cover` | `job.freshness.edges.cover_to_draft` |

每类产物三态：

| 状态 | 含义 | 对现有流程的影响 |
|---|---|---|
| `valid` | 输入/输出指纹与记录一致，且无外部修改 | 允许复用产物（不重建、不重导） |
| `stale` | 输入变了、输出缺失/不符，或检测到非本管线写入 | 按 §5 决定自动重建/重导，或转人工 |
| `unknown` | 无记录、记录版本不符、文件不可读、指纹计算失败 | **完全走现有 stage 逻辑**，不做自动判断 |

`externally_modified` 不是第四态，是 `stale.reason` 的一个取值。

内部三态机不变。消费点（§12）额外解释出三个**消费态**：

| 消费态 | 来源 | 含义 |
|---|---|---|
| `manual_confirmed` | 人工 `--reuse-as-is` 覆盖出身 | 合法，所有消费点按有效产物处理 |
| `legacy` | 老产物无 sidecar，且 job 的导出老台账（无 `outputs`）也没有 E2 记录；或命中「sidecar 上线前」一次性冻结基线（快照/墓碑 `reason=baseline`） | 归档/clean 放行并标注，不倒查、不拦截 |
| `evidence_missing` | job 的 `video.outputs[<版本>].state == "exported"`（新台账登记过导出）但成品旁没有 sidecar | **不降级为 `legacy`**：告警 + 标注；归档/clean 仍放行但留痕；scheduler 不按「有效产物」复用（`allowed=None`，回落旧判断） |

### 0.1 与现有失效机制的关系（收口，不另起一套）

仓库里已有两套粗粒度标记，本系统不替换、不并行，只把它们当输入信号：

| 现有机制 | 位置 | 语义 | 在新系统中的位置 |
|---|---|---|---|
| `_export_stale.json` | `pipeline.py` L2544-2577；`publish_staged_draft` L333 写入；`cover_watch` L633/L2372、`batch_runner` L240/L632、`rebase_presets` L112 清除 | 草稿重建/克隆后旧 mp4 不算成品 | **E2 的硬信号**：文件存在 → E2 一律 `stale(draft_changed)`，优先于任何记录 |
| `derived.covers_stale` | `job_manifest.py` L1595-1601 置位、L2915 重建封面后清除 | 重选标题后 `04_covers` 需重做 | **E3 的上游信号**：为 true → E3 `stale(covers_pending_regeneration)` |

因此实施时必须遵守：

- E2 不再在每个导出入口散落判定，只认 `_export_stale.json` + E2 记录；
- `clear_export_stale()` 之后（确实导出成功）才写 E2 `valid` 基线；
- E1 的任何草稿写入都要让 E2 失效（等价于补一条 `_export_stale.json` 的语义），
  否则 `_framefix_draft.py` 这类「不走 publish 的写入」会被漏掉。

## 1. 指纹口径（硬规则）

指纹只吃「规范化后的内容」，不 hash 文件本身。

### 1.1 通用 JSON 规范化

1. 递归剔除键：`id`、`uuid`、`create_time`、`update_time`、`*_time`、`tm_*`、`mtime`、
   `at`、`generated_at`、`updated_at`、`recorded_at`、`path`、`draft_fold_path`、
   `draft_json_file`、`draft_root_path`、`draft_cover`、`built_at`、`extra_info`；
   顶层草稿名 `name` 也不进 hash（素材的 `material_name` 保留）。
2. 数值：整数值浮点转 int；非整数保留 6 位小数。
3. 序列化：`json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`，
   UTF-8 后 sha256。
4. 记录中同时保存 `canon` 版本号；实现变更时版本 +1，旧记录直接变 `unknown`，不迁移、不误判。

### 1.2 草稿语义视图 `canon_draft`

草稿不能直接按上面规则 hash（素材 UUID 每次重建都会变），先转语义视图：

1. 素材引用：先为每类素材生成有序语义签名，segment 用「签名下标」引用素材，不保留 `material_id` 原值。
2. 素材签名：
   - video：`(type, material_name, duration, width, height)`
   - photo：`(type, duration, width, height)`
   - text：`(type, content 规范化, font_size, text_color, alignment)`
   - sticker：`(type, resource_name, width, height)`
   - audio：`(type, duration, music_name, volume)`
   - draft（复合片段）：`(type, duration, canon_draft(inner))`
3. segment 保留：`target_timerange`、`source_timerange`、`speed`、`reverse`、`render_index`、
   `visible`、`clip`（剔 id 后的 alpha/scale/transform/rotation）、`volume`。
4. 轨道按 `(type, render_index)` 排序；轨道内 segment 保持时间顺序，不重排。
5. 另存一份 `weak_hash`：在强视图上再剔除 `render_index`、`visible`、`clip` 的纯展示字段
   与轨道顺序。只用于识别「剪映仅重排 JSON」的序列化噪音，不单独升级/降级状态。

### 1.3 媒体与图片

| 类型 | 指纹 | 理由 |
|---|---|---|
| 主轨媒体 | `(basename, duration_us, size_bytes)`，按 basename 排序 | 路径/mtime 会变；文件字节太大 |
| 预设草稿 | `canon_draft(preset_draft_content)` | 预设内容变了必须失效 |
| 封面图 | 像素归一 hash（PIL 缩放到 64×64 RGB 后 sha256） | 重新保存/加元数据不应失效 |
| 成片 | `size_bytes` + ffprobe `duration / nb_frames / avg_frame_rate / width / height` | 8.6 GB 不 hash 字节 |
| `draft_cover.jpg` | **不参与判定** | 剪映会重写、会缩放，纳入必然假失效 |

## 2. 三条边的输入清单

### 2.1 E1 `prompts_to_draft`

E1 的输入分两组，**都进指纹**，但失效后的处理不同（见 §5）：

#### A. 提示词/文案组（intent）

| # | 文件 | 字段 |
|---|---|---|
| 1 | `03_prompts/prompts.json` | `spec_version`、`channel`、`count`、`items[]` 的 `group / slot / title_id / title / composition / size / prompt` |
| 2 | `03_prompts/prompts.md` | 全文规范化（去掉空行差异） |
| 3 | `02_titles/titles.json` | `gate`、`selected[]` 的 `slot / title_id / title`、`selected_groups[]` 的 `group / picks[]`、`prompt_version` |
| 4 | `02_titles/prompt.txt` | 全文规范化 |
| 5 | `01_input/synopsis.txt` | 全文规范化 |

说明：这些文件当前不直接写入 `draft_content.json`，它们先决定封面与文案意图。仍然纳入 E1，
因为「提示词变了」就是「这一版草稿的意图变了」；但只改这组时**不自动重建**（见 §5），
避免每次改标题都白跑一遍剪辑。

实际传播路径（也是 intent 不自动重建的原因）：

```
02_titles / 03_prompts 变化
  → E1 stale(intent) + derived.covers_stale = true
  → 重生成 04_covers
  → E3 stale(covers_pending_regeneration)
  → 重挂封面（只改 cover 字段）
  → 草稿 hash 变化 → E2 stale
  → 重导
```

#### B. 剪辑指令/素材组（build）

| # | 文件 | 字段 |
|---|---|---|
| 1 | `cover_job/job.json` | `series`、`sizes.horizontal`、`sizes.vertical` |
| 2 | `cover_job/job.json` | `workflow.mode`、`workflow.cut_s`、`workflow.preset_profile`、`workflow.derive[]` |
| 3 | `pipeline/presets.json` | 解析结果：`presets_<组>` 的 `(预设名, alpha)` 有序列表；`default_preset_profile`（仅 job 未显式指定组时生效） |
| 4 | 预设库 `<LOCALAPPDATA>/JianyingPro/User Data/Presets/Combination/Presets/<预设名>/preset_draft/draft_content.json` | `canon_draft()` 摘要（含内层 `drafts[].draft`） |
| 5 | `05_video` 下被主轨引用的媒体文件 | `(basename, duration_us, size_bytes)`，按 basename 排序 |

不参与：`04_covers/**`（归 E3）、`06_publish/**`、任何 `job.json` 的时间戳/history/revision。

E1 输出：草稿的 `canon_draft()` 强摘要 + `weak_hash`，加草稿目录名。
写入时机：本管线每次写 `draft_content.json` 后（写入点见 §4.1）。

### 2.2 E2 `draft_to_video`

输入：

| # | 来源 | 字段 |
|---|---|---|
| 1 | `job.json` `video.outputs[<profile>].draft` 指向的草稿 | `canon_draft()` 强摘要 |

输出身份：

| 字段 | 来源 |
|---|---|
| `exists` | `os.path.isfile` |
| `size_bytes` | `os.path.getsize` |
| `duration_us` | `ffprobe format=duration` × 1e6 取整 |
| `nb_frames` | `ffprobe stream.nb_frames`（头部字段，不解码） |
| `avg_frame_rate` / `width` / `height` | `ffprobe` |

不参与：绝对路径、mtime、文件 sha256、progress 内容、`verify.log` 结论。
写入时机：导出成功、`clear_export_stale()` 清除标记之后
（`cover_watch.py` L2372、`batch_runner.py` L240/L632、`rebase_presets.py` L112）。

### 2.3 E3 `cover_to_draft`

输入：

| # | 来源 | 字段 |
|---|---|---|
| 1 | `jm.resolve_profile_cover(job_dir, profile)` 解析出的图片 | 像素归一 hash |
| 2 | `job.json` `cover_map[]` / `vertical_cover` | `slot`、`kind`、`title_id`、`file`（相对逻辑路径） |
| 3 | 组绑定 | `profile`（`a`/`b`/…）与组目录名 |

输出：

| 字段 | 取法 |
|---|---|
| `cover` 非空 | `draft.cover` 或 `materials.drafts` 中的封面 composition |
| `embedded_hash` | `Resources/cover/<uuid>.*` 的像素归一 hash |
| `bound_title_id` | 封面写入时记录的 `title_id`，防串组 |

写入时机：`pipeline.py` Step 10.9 封面落盘后；`preset_ops.py` L444/L461 版本封面写完后。

## 3. 三态转换

### 3.1 判定顺序（`check` 固定按此执行）

1. 总开关关闭 / 无 `job.json` / 无该边记录 → `unknown`
2. 记录 `canon` 与当前实现不一致 → `unknown`
3. 计算输入 hash 失败（文件缺失、JSON 坏、ffprobe 失败）→ `unknown`，原因写 `detail`
4. 输入 hash == 记录值 → 进入输出校验
5. 输入 hash != 记录值，但写入日志里能查到同 hash → `valid`（自愈，补写 job）
6. 输入 hash != 记录值，查不到 → `stale(input_changed)`，再按 §5 区分 intent/build
7. 现有标记前置：
   - E2：草稿目录存在 `_export_stale.json` → 直接 `stale(draft_changed)`（优先于 E2 记录）；
   - E3：`job.derived.covers_stale == true` → 直接 `stale(covers_pending_regeneration)`。
8. 输出校验：输出缺失 → `stale(output_missing)`；输出 hash 命中写入日志 → `valid`；
   强 hash 不同但弱 hash 相同 → `valid(serialization_only)`；否则 → `stale(externally_modified | output_changed)`

任何异常都必须被吞掉并降级为 `unknown`：新鲜度模块不允许把失败抛回调用方，不允许让一次正常导出因指纹计算失败而中断。

### 3.2 转换表

| 起点 | 触发 | 终点 | 附带动作 |
|---|---|---|---|
| unknown | 本管线首次成功构建/导出/挂封面 | valid | 写第一条记录 |
| valid | 输入变化（可解释） | stale | 标记下一轮重建/重导 |
| valid | 草稿被非本管线改写且无写入记录 | stale(`externally_modified`) | 写原因、时间、前后 hash 前 8 位 |
| valid | 成片被删/被替换 | stale(`output_missing`/`output_changed`) | 允许重导 |
| stale | 重建/重导/重挂完成并写记录 | valid | 记录新指纹 |
| stale | `--mark-stale --reason ...` | stale(`manual_mark`) | 原因入 `job.history` |
| stale/unknown | `--reuse-as-is` | `manual_confirmed`（`accepted_as_is` 快照） | 覆盖出身，不建放行名单 |
| 任意 | 记录 `canon` 版本不符 | unknown | 不自动迁移 |
| 任意 | `JY_FRESHNESS=0` 或 `job.freshness.enabled=false` | unknown | 全链路退回旧行为 |

## 4. 人工编辑的口子

### 4.1 所有 `draft_content.json` 写入点（必须全部挂记录）

| 文件 | 行 | 场景 | `writer` |
|---|---|---|---|
| `pipeline/pipeline.py` | L303 / L456 / L503 / L516 | 建空稿/模板稿（可能无 job） | `pipeline_create` |
| `pipeline/pipeline.py` | L3251 | 正式发布草稿 | `pipeline_publish` |
| `pipeline/pipeline.py` | L3334 | Step 10.9 封面最终落盘 | `pipeline_cover` |
| `pipeline/preset_ops.py` | L451 | 克隆稿换预设 | `preset_clone` |
| `pipeline/verify_draft.py` | L508 | 终检自动修复写回 | `verify_fix` |
| `pipeline/repair_translation.py` | L220 | 残缺集补翻写回 | `repair_translation` |
| `pipeline/split_long_subtitles.py` | L235 | 长字幕拆分写回 | `split_subtitles` |
| `pipeline/_framefix_draft.py` | L342 / L448 | 帧对齐修复（含 `_pipeline_backup.json`） | `framefix` |

`_framefix_draft.py` 必须挂：它是「不是主 pipeline 写的，但确实是我们的程序写的」这一类；
不挂就会被误判成人工修改。

### 4.2 每次写的记录内容

1. 计算 `canon_draft` 强/弱摘要；
2. 追加一行到 `cover_job/06_publish/freshness_writes.jsonl`：
   ```json
   {"at":"2026-11-14T13:01:02+08:00","edge":"prompts_to_draft",
    "writer":"framefix","draft":"..._b","input_hash":"sha256:...",
    "output_hash":"sha256:...","weak_hash":"sha256:...",
    "adopted_external":false}
   ```
3. 更新 `job.freshness.edges.prompts_to_draft` 的最新值；
4. 同一事务把 `edges.draft_to_video` 置 `stale(reason="draft_changed")`——草稿内容一变，
   旧成片必然过期（与 `_export_stale.json` 同义）。这样 `_framefix_draft.py` 这类不走 publish
   的写入也不会漏掉 E2 失效。

### 4.3 读的时候怎么判

| 情况 | 判定 |
|---|---|
| 当前 hash == 记录 `output_hash` | `valid` |
| 当前 hash != 记录，但命中写入日志中任一条 `output_hash` | 我们自己改的（或写到一半崩了）：升 `valid` 并补写 job |
| 当前 hash != 记录，日志里找不到 | `stale(externally_modified)`，原因写「`<last_at>` 之后被非本管线写入，当前 hash 无写入记录」 |
| 当前 hash != 记录，但弱 hash 相同 | `valid(serialization_only)`：只是剪映重排了字段 |
| 命中 `adopted_external=true` 的写入记录 | `valid`，并记 `history: adopted_external`（说明某次写回把人工内容一起带上了） |

### 4.4 识别边界（明确告知）

能看见：剪映保存草稿导致的 JSON 变化；别的脚本/手工改 JSON。
看不见：只改 `draft_cover.jpg`、只改 `Resources/cover/`；改源视频但 `(basename, duration, size)` 不变；
只改提示词而不重新生成封面（这是 §5 的「intent 变化」场景，会标 stale，但不自动重建）。

## 5. 人工确认矩阵

| 场景 | 自动动作 | 必须人工确认？ | 阻断哪一步 |
|---|---|---|---|
| E1 `build` 输入变化，草稿无外部修改 | 允许重建草稿 | 否 | 无 |
| E1 只有 `intent` 输入变化（提示词/标题/简介） | 不自动重建（当前不会改变草稿内容） | **是**：确认是否重生成封面/重跑 | edit 阶段 |
| E1 输入变化 + 草稿有外部修改 | 不自动重建 | **是**：重建=覆盖手工编辑，或 `--reuse-as-is` | edit 阶段 |
| E1 仅外部修改（上游没变） | 不重建、不覆盖 | **是**：`--reuse-as-is` 确认这版可用才清 stale | edit 阶段 |
| E2 草稿变了 / 成片缺失 / 身份不符 | 允许重导 | 否 | 无（export 正常） |
| E3 封面图变了 / 草稿没挂上 | 允许重挂封面（只改 cover 字段） | 否 | 无（导前挂） |
| E3 草稿封面与输入不符且草稿被外部改过 | 允许重挂 | 否，ledger 记 `cover_readopted` | 无 |
| 任一 `unknown` | 不判断 | 否 | 无 |

原则：**重建会丢人工内容，必须确认；重导/重挂不会丢内容，自动做。**

## 6. 两个手工兜底命令

新增 `pipeline/freshness.py`：

```bash
# 我知道我改过（或我知道这版不能用）
python pipeline/freshness.py --job "D:\短剧\<剧>\cover_job" \
  --edge all --mark-stale --reason "手工拖了第 12 集的片段"

# 确认这版能用，把 unknown/stale 直接变 valid
python pipeline/freshness.py --job "D:\短剧\<剧>\cover_job" \
  --edge e1 --reuse-as-is
```

`--edge`：`e1|drafts`、`e2|video`、`e3|covers`、`all`（默认 `all`）。
只写 `job.json` 与 `freshness_writes.jsonl`，不碰草稿、不碰成品、不触发导出。
两者都写 `history`：`freshness_mark_stale` / `freshness_reuse_as_is`，`detail` 带原因。
`--reuse-as-is` 不是「变 valid」，而是把该版本记为 `manual_confirmed`（`by/at/why`），
同时写 job 与产物旁的 sidecar；之后所有消费点看到的就是合法状态。
另有只读命令 `python pipeline/freshness.py --job <dir> --check [--json] [--explain]`，
不写任何文件。

## 7. 假失效：避免与回退

### 7.1 避免

| 风险 | 处理 |
|---|---|
| 素材/预设 id 每次重建都变 | 语义签名 + 下标引用，id 不进 hash |
| 预设 manifest 的 `built_at` 变 | 键剔除 |
| 媒体被规范化到 `__jycache__` | 用 `(basename, duration, size)`，路径不进 |
| 封面被重新保存/加元数据 | 像素归一 hash |
| `draft_cover.jpg` 被剪映重写 | 不参与 E3 判定 |
| 剪映仅重排 JSON | 弱 hash 相同 → `serialization_only` 采纳 |
| 写到一半崩溃、记录没更新 | 写入日志命中 → 自愈 valid |
| 提示词变了但草稿不会变 | 归为 `intent` 变化，标 stale 但不自动重建（§5） |

### 7.2 回退（按代价从低到高）

1. `--reuse-as-is`：把当前版本记为 `manual_confirmed`（覆盖出身），最常用。
2. `job.freshness.ignore`：字符串数组，命中的规范化路径不参与 hash。默认空。
3. `job.freshness.enabled=false` 或 `JY_FRESHNESS=0`：三条边全部 unknown，行为与改动前完全一致。
4. `canon` 版本 +1：旧记录变 unknown，不迁移、不误判。
5. 所有判断只读产物、只写 `job.json` 与 `freshness_writes.jsonl`；回退不删除、不重写任何草稿/成品，**不存在破坏性回滚**。

## 8. 老 job 兼容

1. 没有 `freshness` → 三条边 `unknown`；`stage`、`_draft_exported`、`export_state`、watcher 行为一字不改。
2. 历史产物不回溯建指纹、不扫描、不补记录。
3. `job_manifest.migrate_job` 只补空骨架（`schema:1`、`canon:"c1"`、三条边 `unknown`），
   不写 hash、不改 `revision`、不写 `history`。
4. `SCHEMA_VERSION` 保持 `1.1`：`freshness` 是可选扩展键，`validate_job` 不要求它。
5. 只有被新版代码实际重建/导出/挂封面的 job 才产生记录；产生记录的那一刻起，该边进入三态判定。
6. 老 job 想立刻纳入管理：显式跑一次 `--reuse-as-is`。

## 9. 存储

`job.json` 新增（示例）：

```json
"freshness": {
  "schema": 1,
  "canon": "c1",
  "enabled": true,
  "ignore": [],
  "edges": {
    "prompts_to_draft": {
      "state": "valid",
      "input_hash": "sha256:...",
      "output_hash": "sha256:...",
      "weak_hash": "sha256:...",
      "draft": "..._b",
      "recorded_at": "2026-11-14T13:01:02+08:00",
      "last_writer": "framefix",
      "reason": null,
      "accepted_as_is": null,
      "detail": null
    },
    "draft_to_video": {
      "state": "unknown", "input_hash": null, "output": null,
      "recorded_at": null, "reason": null
    },
    "cover_to_draft": {
      "state": "unknown", "input_hash": null, "output_hash": null,
      "recorded_at": null, "reason": null
    }
  },
  "manual_confirmed": {
    "b": {"by": "唐唐", "at": "2026-11-14T15:30:00+08:00",
          "why": "确认 12 集手工拖过的这版可用",
          "size_bytes": 123456789, "video_sha256": "sha256:..."}
  }
}
```

产物旁 sidecar：`<export_file>.freshness.json`（§12.2），与 mp4 同目录、随归档一起搬走。
墓碑：`cover_job/06_publish/freshness_tombstones/<version>_<ts>.json`（§12.5）。
归档快照：`cover_job/06_publish/freshness_archive_snapshot.json`（§12.4）。

写入日志：`cover_job/06_publish/freshness_writes.jsonl`，append-only，单行 JSON；
超过 2000 行保留最近 2000 行（临时文件 + `os.replace`）。
新鲜度更新**不调用** `update_job`（避免 `validate_job` 在 edit 半成品状态下把一次成功写入变成异常），
走单独的 `record_freshness()`：读 → 深合并 → `revision+1` → `history` → 原子写。

## 10. 实施落点

### 10.1 新模块 `lib/freshness.py`

```python
CANON = "c1"

def canon_draft(draft: dict) -> dict          # 强语义视图
def weak_draft(draft: dict) -> dict           # 弱语义视图
def file_pixel_hash(path: str) -> str         # PIL 64x64；PIL 缺失退回文件 sha256
def compute_input_hash(job_dir, edge) -> str  # 各边输入 hash
def check(job_dir: str, edge: str | None = None) -> dict
def freshness_of(job_dir, version, consumer="scheduler") -> dict
def write_sidecar(export_file, evidence) -> str
def read_sidecar(export_file) -> dict | None
def sha256_file(path) -> str
def write_tombstone(job_dir, version, evidence, status) -> str
def record_draft_write(job_dir, draft_path, writer, adopted_external=False) -> None
def record_export(job_dir, profile, draft_name, export_file, verify_rc=None) -> None
def record_cover(job_dir, profile, draft_dir, cover_image) -> None
def mark_stale(job_dir, edges, reason) -> None
def reuse_as_is(job_dir, edges) -> None
def main(argv=None) -> int                    # CLI
```

### 10.2 接线点

| 文件 | 改动 |
|---|---|
| `lib/job_manifest.py` L274-326 | `build_job()` 增加 `"freshness": default_freshness()` |
| `lib/job_manifest.py` L337-349 | `migrate_job()` 补空骨架 |
| `lib/job_manifest.py` L1595-1601 / L2915 | 不改 `covers_stale` 语义；E3 `check` 读取它作为前置信号 |
| `pipeline/pipeline.py` L378-380 `save_draft` | 新增可选 `job_dir=None, writer=None`；有 job 时写后 `record_draft_write`，无 job 时 no-op（保持旧行为） |
| `pipeline/pipeline.py` L303 | 发布时只改 `name`/`update_time`，两者都不进 `canon_draft`，**不需要**记录 |
| `pipeline/pipeline.py` L3251、L3334 | 走 `save_draft(..., job_dir, writer)`；L3334 后 `record_cover` |
| `pipeline/preset_ops.py` L451、L461-464 | `save_draft(..., job_dir, writer="preset_clone")`；封面后 `record_cover` |
| `pipeline/verify_draft.py` L508 | `record_draft_write(writer="verify_fix")` |
| `pipeline/repair_translation.py` L220 | `record_draft_write(writer="repair_translation")` |
| `pipeline/split_long_subtitles.py` L235 | `record_draft_write(writer="split_subtitles")` |
| `pipeline/_framefix_draft.py` L342/L448 | 原始 `json.dump` 不走 `save_draft`，必须显式 `record_draft_write(writer="framefix")` |
| `lib/cover_watch.py` L627 `_draft_exported` | 判定顺序：先看 `_export_stale.json`（有 → 未导出）→ 再看 E2 `valid`（是 → 已导出）→ 否则走旧逻辑 |
| `lib/cover_watch.py` L1222 `build_plan` | E1 `stale` 且需人工确认 → `blocked`，理由写入 plan |
| `lib/cover_watch.py` L2372 | `clear_export_stale` 后 `record_export(...)` |
| `pipeline/batch_runner.py` L240 / L632 | `clear_export_stale` 后 `record_export(...)` |
| `pipeline/rebase_presets.py` L112 | 导出成功后 `record_export(...)` |
| `pipeline/archive_job.py` move 前 | 逐版本 `freshness_of(..., consumer="archive")` → 写 `freshness_archive_snapshot.json`；不阻断 |
| `pipeline/stream_manifest.py` L612-634 删除循环 | 逐版本 `freshness_of(..., consumer="clean")` → 写墓碑；不阻断 |
| `pipeline/verify_export.py`（可选） | 只读输出一条新鲜度摘要到日志，不参与判定 |

### 10.3 明确不改

- 不改 `mark_export_stale` / `clear_export_stale` 的行为：`_export_stale.json` 继续写、继续读，
  只在其调用链补 `record_export`。
- 不改 `derived.covers_stale` 的置位/清除逻辑，只读它。
- 不改 `stage` 语义、`STAGES`、`export_state` 状态机。
- 不改 `pipeline.py` 的预设合并逻辑（复合片段 duration 是另一案）。
- 不引入新依赖；PIL 仅作可选加速，缺失时退回字节 hash。
- 不动 `D:\短剧` 下任何 job 文件；测试全部用临时目录。

## 11. 验收

1. **稳定性**：同一草稿连续读 10 次，`canon_draft` 不变；把顶层 `id`、`update_time`、素材 `path` 改成随机值，摘要不变。
2. **假失效**：模拟剪映重排 JSON（只改键顺序/缩进/id），弱 hash 不变 → `valid(serialization_only)`。
3. **外部修改**：手改一个 `target_timerange.start`，不写日志 → `stale(externally_modified)`，`reason` 含时间与 hash 前 8 位。
4. **我们自己的修改**：`_framefix_draft.py` 改完并记录 → `valid`；删记录再判 → stale。
5. **自愈**：写日志后不更新 job，再 `check` → `valid`，且 job 被补写。
6. **三态**：老 job → 三个 unknown；`--reuse-as-is` → valid；`--mark-stale --reason` → stale 且 history 有事件。
7. **E1 分组**：只改 `03_prompts/prompts.json` → stale(intent)，不自动重建；改 `preset_profile` → stale(build)，允许重建。
8. **E2**：草稿改一个片段 → stale；重导成功并记录 → valid；删 mp4 → stale(output_missing)；
   记录 valid 但草稿目录被写入 `_export_stale.json` → 仍判 stale(draft_changed)。
9. **E3**：替换 `04_covers/b_group/cover_04_vertical_B.png` → stale；重挂后 valid；
   `draft_cover.jpg` 单独被改 → 仍 valid；`derived.covers_stale=true` → stale(covers_pending_regeneration)。
10. **联动**：`_framefix_draft.py` 改草稿并记录 → E1 valid、E2 自动 stale；重导后 E2 valid。
11. **总开关**：`JY_FRESHNESS=0` 时全部 unknown，watcher 行为与改动前一致。
12. **无副作用**：测试不产生对 `D:\短剧`、`D:\油管打猎` 的任何写入。
13. **归档**：有 sidecar 的 valid 版本、故意改坏的 stale 版本、无 sidecar 的 legacy 版本——
    三种都允许归档；snapshot 分别标 `valid` / `stale` / `legacy`；sidecar 随 mp4 一起搬走。
14. **clean**：三种状态都允许删除；墓碑分别记 `valid` / `stale(reason)` / `legacy`，
    含成品 sha256 与草稿 hash；`--dry-run` 不写墓碑；删除失败墓碑为 `failed`。
15. **人工确认**：`--reuse-as-is` 后 `freshness_of(consumer="archive"|"clean")` 返回
    `manual_confirmed`；进程内改大 mp4 后再判 → `stale`。
16. **反向检测**：`video.outputs[<版本>].state="exported"` 且无 sidecar、无 E2 valid →
    `evidence_missing`（不是 `legacy`）；archive/clean 放行并留痕 + 告警；
    老台账（无 `outputs`）仍为 `legacy`。
17. **覆盖率自检**：AST 扫描所有写导出成功台账的调用点（`es.succeed` / `record_video` /
    `clear_export_stale`），断言所在函数（或其直接调用的同文件助手）必须调用 `record_export`；
    例外仅限两条并写死清单：`cover_watch.note_export_result`（真源写入器本体）与
    `job_manifest._main` 的 `record-video` 子命令（人工补登记，不伪造 sidecar）。
18. **一次性上线基线**：`state="exported"`、无 sidecar、命中冻结清单
    `data/freshness_legacy_baseline.json`（job 身份 + 版本 + 成品 size）→ `legacy`，
    `reason=baseline`，**不告警**；清单外才 `evidence_missing`。清单损坏/digest 不符 →
    整份失效（fail closed，回到 `evidence_missing`）。运行期任何函数都不得改写清单。

## 12. 消费点：归档与 clean（最终口径）

> 本节取代早期「归档拦、clean 保护」的一刀切说法。调度、归档、clean 三个消费点共用同一个入口。

### 12.1 唯一入口 `freshness_of(version)`

```python
freshness_of(job_dir, version, consumer="scheduler")
# -> {
#   "version": "b",
#   "state": "valid|stale|unknown|manual_confirmed|legacy|evidence_missing",
#   "allowed": True/False/None,
#   "reason": "...",
#   "evidence": {
#      "draft": "...", "draft_hash": "...", "input_hash": "...",
#      "cover_hash": "...", "exported_at": "...",
#      "video": {"path": "...", "size_bytes": ..., "sha256": "...", "duration_us": ...},
#      "manual_confirmed": {"by": "...", "at": "...", "why": "..."} | None,
#      "provenance_file": "...", "legacy": true|false
#   }
# }
```

规则：
- 所有判断只在 `freshness_of` 内完成；消费点只声明 `consumer`，不各写一遍判断链。
- 判定不看 mtime，不做内容再推断；只比较写时确权的记录。
- `consumer` 与策略：

| consumer | valid | manual_confirmed | stale | 无证据 |
|---|---|---|---|---|
| `scheduler` | 复用 | 复用 | 重建/重导 | `unknown`，走旧 stage 逻辑 |
| `archive` | 放行 + 标注 | 放行 + 标注 | **放行 + 标注 `stale`** | `legacy`，放行 + 标注 |
| `clean` | 放行 + 墓碑 | 放行 + 墓碑 | **放行 + 墓碑（`reason=stale`）** | `legacy`，放行 + 墓碑 |

**反向检测 `evidence_missing`**（补齐上述「无证据」里唯一的危险分支）：

- job 的 `video.outputs[<版本>].state == "exported"`（`record_video` 写的新台账）却读不到
  `<export_file>.freshness.json`，且没有可用的 E2 `valid` 记录 → 判 `evidence_missing`，
  `reason=export_recorded_no_sidecar`。
- `archive` / `clean`：**放行但标注 `evidence_missing`**，并打一次告警
  （`[freshness][evidence_missing] ...`），绝不降级成 `legacy`。
- `scheduler`：返回 `evidence_missing`、`allowed=None`（走旧逻辑，不假装有效），同样告警。
- 告警按 `(job_dir, version)` 进程内去重，避免 watcher 每 5 秒刷一次。
- 老台账（没有 `video.outputs`，只有顶层 `export_state/export_file`）**不算**
  `evidence_missing`，按 `legacy` 处理——那是本系统上线前的产物，缺 sidecar 属正常。

**一次性上线基线 `legacy(baseline)`**（给「上线前导出、但已经是新台账」的历史产物）：

- 判定顺序固定为：sidecar → E2 `valid` → E2 `stale` → **基线命中 → `legacy(reason=baseline)`**
  → 否则 `evidence_missing`。
- 基线文件：`data/freshness_legacy_baseline.json`（可用 `JY_FRESHNESS_BASELINE` 覆盖路径）。
  只由 `python scripts/freshness_baseline.py --root D:\短剧 --write` 生成；
  再次生成必须显式 `--append`（只增不删）或 `--force`（整份覆盖）。
- 命中条件：`job_id`（优先）或归一化 job 目录 + 版本 + 成品 `size_bytes` 三者一致。
  size 变了视为「上线后重导过」，不再命中。
- 命中后 `state="legacy"`、`reason="baseline"`，快照写入 `legacy_baseline[]`，
  墓碑记 `reason=baseline`；**不告警**。
- 清单是**冻结**的：运行期只读，`freshness_of` / `archive_snapshot` / `write_tombstone`
  都不会写它；目录里新出现的漏写不会自动被吸进清单。
- 清单带 `entries_digest`；手改条目但没重新生成 → `load_baseline()` 判 `digest_mismatch`，
  整份失效（fail closed），这些版本重新回到 `evidence_missing`。

### 12.2 写时确权：产物旁边的 `*.freshness.json`

出片成功（`clear_export_stale` 之后）立即在 mp4 同目录写 `<export_file>.freshness.json`：

```json
{
  "schema": 1,
  "canon": "c1",
  "version": "b",
  "draft": "..._b",
  "draft_hash": "sha256:...",
  "input_hash": "sha256:...",
  "cover_hash": "sha256:...",
  "exported_at": "2026-11-14T15:20:00+08:00",
  "video": {
    "file": "....mp4",
    "size_bytes": 123456789,
    "sha256": "sha256:...",
    "duration_us": 8511500000,
    "nb_frames": 255345
  },
  "state": "valid",
  "manual_confirmed": null
}
```

- `video.sha256` 在导出成功后流式计算一次（不缓存、不猜、不拿大小冒充）。
- sidecar 写失败只 warn，不影响导出结果。
- `draft_hash / input_hash / cover_hash` 只从写入记录取；没有就写 `null`，不反推。
- 判定只看这些写死的值，不重新读输入、不重新算 draft。

### 12.3 人工确认 = 覆盖出身，不是豁免

- `--reuse-as-is` 把该版本记为 `manual_confirmed`（`by` / `at` / `why`），同时写 job 与 sidecar；
  不是新建放行名单，也不是跳过检查。
- 消费点看到 `manual_confirmed` 一律按合法状态处理。
- 之后若产物 `size_bytes` 与确认时不一致 → 确认失效，回到 `stale`，需要重新确认。

### 12.4 归档：不判、只搬

- `pipeline/archive_job.py` 在 move 前对每个声明版本调用 `freshness_of(..., consumer="archive")`，
  结果写入 `06_publish/freshness_archive_snapshot.json`（含 `legacy` / `stale` /
  `evidence_missing` 标注与 `evidence_missing[]` 汇总）；**不阻断归档**。
- sidecar 随 mp4 一起被搬走，即「产物连同出身一起搬走」。
- 现有完整性校验（mp4 / progress.done / 封面三件套、`audit_exemption.json`）保持不动。
- `--dry-run` 只打印判定，不写 snapshot。

### 12.5 clean：不拦、写墓碑

- `pipeline/stream_manifest.py clean` 在删除每个版本成品前调用
  `freshness_of(..., consumer="clean")`，**不阻断删除**。
- 墓碑路径：`06_publish/freshness_tombstones/<version>_<ts>.json`。
  先写 `status:"deleting"`，删除后原子改写为 `deleted`（删除失败改 `failed`）。
- 墓碑字段：`version / draft / draft_hash / input_hash / cover_hash /
  video{path,size_bytes,sha256,duration_us} / state / reason / manual_confirmed / deleted_at`。
- `stale` / `legacy` / `evidence_missing` 都放行，理由写进墓碑。
- `--dry-run` 不写墓碑；`--intermediates-only` 不删成品，不产生墓碑；
  `__jycache__` / `_work_*` 等共享中间产物逻辑不变。

### 12.6 老产物：`legacy` / `legacy(baseline)` / `evidence_missing` 的边界

- **`legacy`**：没有 sidecar、job 里也没有 E2 记录，且 job 的导出台账是**老结构**
  （没有 `video.outputs`，至多顶层 `export_state` / `export_file`）。
  归档放行并标 `legacy`；clean 放行并写墓碑（`state:"legacy"`）；不倒查、不拦截。
- **`legacy(baseline)`**：新台账（`state:"exported"`）但没有 sidecar，却命中一次性冻结基线
  （`job_id`/job 目录 + 版本 + 成品 size 一致）。属于「sidecar 上线前就已导出」的历史产物：
  归档/clean 放行，快照 `legacy_baseline[]`、墓碑 `reason:"baseline"`，**不告警**。
- **`evidence_missing`**：新台账登记过 `exported`、没有 sidecar、也**不在**冻结基线里。
  说明是「上线后导出过、但漏写了出身」，必须响：归档快照的 `evidence_missing[]`、
  clean 墓碑的 `state`、日志告警三处都可见。
- 三者都不拦截归档/clean；区别只在**是否必须被看见**：legacy 静默、baseline 标注、missing 告警。
- 老 job 的 `freshness` 骨架由 `build_job` / `migrate_job` 补齐，缺记录本身不算异常。

### 12.7 与 `audit_exemption.json` 的关系

- 本轮**不动** `audit_exemption.json` 及其读写。
- 功能重叠但维度不同：它放行的是「成品三件套缺件」，本系统标注的是「新鲜度证据缺失/不合法」。
- 退役与否待定；本轮不删除、不迁移、不合并，仅在此记录。

## 13. 非目标

- 不追溯修复历史 job/历史成品；
- 不自动重导、不自动删除、不自动归档；
- 不把 ASR/翻译中间文件纳入 E1（其差异由 E1 输出（草稿）→ E2 捕获）；
- 不把质检结果（`verify.log`）纳入新鲜度判定；
- 不做跨 job、跨剧的全局指纹库。
