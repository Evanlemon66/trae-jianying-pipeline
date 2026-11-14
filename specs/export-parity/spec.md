# 导出一致性（Export Parity）规格

> 状态：只读定位完成，方案待批准；本规格不含任何代码改动。
> 约束：`pipeline/pipeline.py`、`pipeline/preset_ops.py` 正由另一会话修改，本规格的落点必须避开这两个文件（除非用户明确允许等待/合并）。
> 反例样本：《八零猎王：赶山捕兽遇旺妻》2026-11-14 07:09–07:58 实跑。

## 1. 目标

同一部剧的所有版本（`a` 基础版 + `_b`/`_c` 克隆版）导出后，成品目录的文件集合必须完全一致：

```
<草稿名>.mp4
<草稿名>.mp4.progress.json      # done=true 且 cover 字段非空且指向真实文件
<草稿名>-封面.jpg
```

不允许出现「某版本少封面 / progress.cover 为空 / 元件齐全但内容为空」的版本差异。

非目标：
- 不重构导出调度、不改 `series_runner` 的多版本编排。
- 不修「B 版质检帧漂移」（既有问题，另案）。
- 不为已经导出的旧成品做批量补救（除非用户明确要求）。

## 2. 产物基线（唯一事实来源）

| 文件 | 写者 | 写入时机 | 缺失后果 |
|---|---|---|---|
| `<草稿名>.mp4` | 剪映导出 → `robust_auto_export` 搬运（`.trae/skills/jianying-editor/examples/robust_auto_export.py` L975-981） | 导出完成后 `--move` | 视为未导出，`_draft_exported` 返回 False（`lib/cover_watch.py` L604-620） |
| `<草稿名>.mp4.progress.json` | `robust_auto_export._write_progress`（L277-325），`_flush_task`（L243-250、L1005） | 运行中写同级 `<草稿名>.progress.json`，`done=True` 时改写进成品目录并删除同级 | 缺文件时 `_draft_exported` 兼容视为已完成（L620），不会重导 |
| `<草稿名>-封面.jpg` | 剪映导出时按草稿顶层 `cover`（composition）生成；`robust_auto_export` 归位（L982-1000，候选名 `_cover_candidates` L1200-1213） | 与 mp4 同一次导出 | 仅打印 `⚠️ 未找到封面（剪映未生成或命名异常）`（L992-993），导出仍返回成功 |
| `<草稿名>.v<n>.*`（条件性） | `pipeline.archive_stale_outputs`（`pipeline/pipeline.py` L2579 起），由 `run_export` L1799-1812 触发 | 同一草稿重导且存在 `_export_stale.json` | 属于历史版本归档，会令版本目录文件数多于 3；一致性审计需排除 `.v<n>` |

补充：
- 运行期旁路文件 `<剧名>.progress.json`、`<剧名>(n)/` 去重目录，由 `series_runner._preclean_bundles`（L136-160）与 `_consolidate_bundle`（L163-196）清理，不属于版本差异。
- 封面文件是否生成，取决于草稿顶层 `cover` 是否为 `null`；`lib/jy_cover.py` L141-149 已注明：`cover=null` 时剪映 5.9 会用第一帧覆盖 `draft_cover.jpg`，且导出不产出 `-封面.jpg`。

## 3. 版本间不一致的全部岔口（A 基础版 vs 克隆版）

按执行顺序：

### D1（必须改）基础版构建期只写 `draft_cover.jpg`，不建 composition
- 位置：`pipeline/pipeline.py` L2793-2802。
- 行为：`set_draft_cover(...)` 只落 `draft_cover.jpg`；顶层 `cover` 仍为 `null`，`Resources/cover/` 不创建。
- 对照：克隆路径 `pipeline/preset_ops.py` L413-462 依次 `set_cover_composition` → `save_draft` → `set_draft_cover`，发布前已完备。
- 后果：基础版在「后续任一环节失败」时，已发布草稿天然缺 composition。

### D2（必须改）`Step 10.9` 是基础版唯一补 composition 的位置，且可被异常跳过
- 位置：`pipeline/pipeline.py` L3322-3343（`if cover_image or cover_extract_from_video:` → `set_cover_composition` + `save_draft` + `set_draft_cover`）。
- 跳过路径：Step 10.8「残缺集自动补翻 + 复验」失败即 `raise`（L3275-3320；raise 点 L3286、L3308、L3316），Step 10.9 永不执行。
- 实跑证据：`D:\短剧\八零猎王：赶山捕兽遇旺妻\cover_job\06_publish\pipeline.log` L3991 已发布草稿 → L4012 进入 10.8 → L4014 补翻脚本 rc=1（ollama 不可达）→ L4028 终检未过 → L4029-4036 RuntimeError（栈顶为 pipeline.py `run_pipeline`）。全日志只有 L176 的 `设置草稿封面: cover_image=...cover_04_vertical.png`（L2794 的 print），没有 `设置草稿封面(导出前原子落盘)`。
- 结果：A 草稿 `cover=null`、`Resources/cover` 不存在、`draft_cover.jpg` 在导出时被首帧覆盖（32,461 B，07:36:14）。

### D3（必须改）edit 失败不阻断 export：草稿存在即放行
- 位置：`lib/cover_watch.py` L994-1003（`if draft is None:` 才查 `eds.gate`），L1038-1066（有草稿即走 export 分支）。
- 实跑证据：`watcher.log`：`07:09:16 edit exit rc=1` / `edit 失败第 1 次[unknown]，60s 后重试` → `07:09:35 export start`（间隔 19s，重试根本未发生）。
- 后果：把「未通过终检的草稿（含缺 composition 的草稿）」直接送进导出。

### D4（必须改）导出链完全不写 composition，也不校验封面
- `lib/cover_watch.py` L1816-1819：`sr._run_export(dn, timeout=..., cold_start=..., job_dir=...)`，无封面参数。
- `pipeline/series_runner.py` L250-316：命令为 `[python, AUTO_EXPORTER, draft, out_mp4, --move, --fallback-dir, dir]`，不触碰 `draft_content.json`。
- `robust_auto_export.py` L982-993：找不到封面只告警；L1003-1005 仍置 `status=success` 并 `_flush_task(done=True, ..., cover=None)`。
- 后果：导出器对「草稿能否产出封面」零责任感；缺封面依然 rc=0。

### D5（必须改）缺封面成品被判为「已完成」，不会自愈
- `lib/cover_watch.py` L604-620 `_draft_exported`：只看 mp4 + `progress.done`。
- L1866 `es.succeed(job_dir, profile=prof)`、L1868 `clear_export_stale`。
- 实跑证据：`.export_state.json` 07:58:05 `status=done`，a/b 均 `done/100%`；A 成品只有 2 个文件。
- 后果：文件缺失是终态，后续轮询不会补。

### D6（可选）质检失败不影响版本状态
- `lib/cover_watch.py` L1839-1844：`verify rc != 0` 只 toast 警告，仍 `exported.append`。
- 实跑证据：B `verify rc=1`（帧级漂移 -1.900s），仍在 L1854-1866 被登记为成功。
- 与「文件齐全」正交，但属于同一类「一致性不可判读」问题。

### D7（不用改）去重目录/旁路 progress 的清理
- `series_runner._preclean_bundles` L136-160、`_consolidate_bundle` L163-196、`robust_auto_export._progress_paths` L264-274 逻辑自洽；A/B 实际均未留下 `剧名(n)` 或同级 progress。

### D8（不用改，但审计需兼容）历史版本归档
- `pipeline/pipeline.py` L2579 `archive_stale_outputs` 会把旧三件套改名 `.v<n>`。这是设计行为，验证脚本必须排除该后缀，否则会误报「多文件」。

## 4. 修法选项

> 所有选项都只影响「之后的导出」，不自动改写已导出的 A 成品。

### 选项 1（推荐，必须改）：导出前统一「封面落盘闸门」作为公共步骤
- 落点：新增 `lib/draft_cover_guard.py`（新文件，避开正被修改的 `pipeline.py` / `preset_ops.py`），并在 `lib/cover_watch.py` `run_export` 的循环内、`es.begin(...)`（L1814）之后、`sr._run_export`（L1816）之前调用。
- 行为：
  1. 解析该版本封面：`a` → `04_covers/cover_04_vertical.*`；`b/c` → `04_covers/<组>/cover_04_vertical_<组>.*`（与 `job_manifest.resolve_profile_cover` 同口径，直接复用 `pipeline.preset_ops` 的解析结果或 `jm.resolve_profile_cover`）。
  2. 打开 `<草稿>/draft_content.json`，`set_cover_composition` → `save_draft` → `set_draft_cover`（复用 `lib/jy_cover.py`）。
  3. 解析不到封面 → **硬失败**（`RuntimeError` + `note_export_result(..., failure_class="cover_missing", retryable=False)`），不进入导出。
- 影响面：`lib/cover_watch.py` 新增约 30 行 + 1 个新模块；`pipeline.py` / `preset_ops.py` 零改动。
- 风险：
  - 重复写 composition：`set_cover_composition` 内部已先摘除旧 composition（`jy_cover.py` L162-168），幂等。
  - 写入时机与剪映冷启动：`--cold-start` 在 `sr._run_export` 内部拉起剪映，闸门写盘早于冷启，安全。
  - 与另一会话冲突：无（不碰其正在改的文件）；但若对方也在 `run_export` 外侧加钩子，需要合并时对齐。
  - 对已导出成品：无影响；A 若要补封面必须显式重导（见 §6）。

### 选项 2（推荐，必须改）：edit 失败禁止进入 export（除非 `--phase export` 人工放行）
- 落点：`lib/cover_watch.py` `build_plan`，在 L994 之前增加判定：`draft` 存在但 `.edit_state.json` 的 `status ∈ {retrying, paused}` 且 `phase == "edit"` 时 → `blocked`，理由写明「上次剪辑未通过，先重试剪辑」；`phase_override == "export"` 保持放行（人工强制语义）。
- 影响面：仅调度决策；不改导出链。
- 风险：
  - 旧草稿（无 `.edit_state.json`）不受影响。
  - A 这种「草稿已发布、仅 2 集缺翻译」的场景会被挡住直到 edit 修复；符合「不能少文件」的要求，但会延迟发布。
  - 需与 `export_policy` 组合验证，避免 `after_edit_queue_drained` 死锁。

### 选项 3（可选，兜底）：导出后校验三件套，不齐则回滚该版本为失败
- 落点：`lib/cover_watch.py` L1837 之后（`sr._run_export` 成功、verify 之前）：检查 `-封面.jpg` 是否存在且 >0 B；同时读 `progress.cover` 非空。
- 行为：不齐 → `note_export_result(..., failure_class="output_incomplete")`，不 `es.succeed`、不 `record_video`，并删除本轮 mp4/progress（或改名 `.incomplete`）。
- 影响面：单版本失败可重试；不会产出「看起来成功」的残品。
- 风险：重导成本高（单次 27 分钟）；需与 `archive_stale_outputs` 的命名策略配合，避免残留。

### 选项 4（不推荐）：让 `robust_auto_export` 接受 `--cover` 并在缺封面时补齐
- 影响面：导出器被 `series_runner`、`batch_runner`、人工调用三方共用，改动面最大。
- 风险：掩盖「草稿 composition 缺失」的真实根因；补齐的封面是文件拷贝，与剪映渲染的封面可能在裁剪/格式上不一致，反而制造新的版本差异。

### 决策
- 必须改：选项 1 + 选项 2（互相独立，缺一不可：1 保证写全，2 保证不把坏草稿送出去）。
- 可选：选项 3（作为回归护栏；若导出耗时可接受则建议同批落地）。
- 不用改：选项 4；D7、D8。

## 5. 验收判据（可判读）

对每个版本目录 `<export_dir>\<草稿名>\`：

1. 文件集合断言（排除 `.v<n>.*`）：
   `{<草稿名>.mp4, <草稿名>.mp4.progress.json, <草稿名>-封面.jpg}` 三件齐全，且无其它非归档文件。
2. `progress.json`：`done == true` 且 `cover` 为非空字符串，且 `os.path.isfile(cover)` 为真。
3. 草稿侧：`draft_content.json` 顶层 `cover != null`，`cover.cover_draft_id` 对应的 `materials.drafts` 条目存在，`Resources/cover/<uuid>.*` 存在且 >0 B。
4. 封面几何：`-封面.jpg` 可被 PIL 打开，宽高比与 `04_covers` 对应竖版图一致（±1%），短边 ≥ 720 px（沿用封面规格）。
5. 跨版本比对：同剧 a/b（c…）三件套的**文件名后缀集合**一致；`progress.cover` 各自指向本版本目录内的封面（不得指向 A 的封面）。
6. 回归判据（不触发导出的静态检查）：对已有成品目录跑审计脚本，A 当前应报 `missing: -封面.jpg`，B 应 `PASS`；修复后重导 A，A 转 `PASS`。

需要实跑导出的验证（**须用户确认后再执行**）：
- 一次真实导出（建议单版本短剧或既有剧重导 A）验证判据 1-5。
- 不建议直接重导《八零猎王》A 版（8.6 GB、约 27 分钟），除非用户明确要求。

## 6. 已导出成品的处理

- 默认不动：A 版 `D:\油管打猎\L\八零猎王：赶山捕兽遇旺妻_第01-142集串流\` 保持现状。
- 若要求补齐：`python lib/cover_watch.py reopen --job "<cover_job>" --yes`（L1962 起）退回 `covers_ready` 后重导 A；重导前必须先满足选项 1，否则仍会缺封面。
- 不允许「只把 04_covers 的竖版图复制成 `-封面.jpg`」了事：那会与剪映实际渲染的封面不一致，且 `progress.cover` 仍为空，不满足判据 2/5。

## 7. 三档结论

| 档位 | 项 | 理由 |
|---|---|---|
| 必须改 | D1/D2：基础版 composition 只在 Step 10.9 落盘且可被 10.8 异常跳过 | A 缺封面的直接根因 |
| 必须改 | D3：草稿存在即放行 export，edit 失败不阻断 | 把缺 composition 的草稿送进导出 |
| 必须改 | D4/D5：导出器不写不校验封面、缺封面仍判完成 | 缺文件成为终态，无法自愈 |
| 可选 | D6：verify 失败仍登记成功 | 与文件齐全正交，但影响「一致性」可判读 |
| 可选 | 选项 3：导出后三件套校验 | 护栏，增加一次失败重试成本 |
| 不用改 | D7 去重/旁路清理；D8 `.v<n>` 归档 | 现状自洽，验证脚本兼容即可 |

## 8. 待批准项

1. 是否批准「选项 1 + 选项 2」作为落地范围（避开 `pipeline.py` / `preset_ops.py`）。
2. 选项 3 是否同批落地。
3. A 版成品是否重导补齐（默认不重导）。
4. 实跑验证是否现在执行（默认不执行）。
