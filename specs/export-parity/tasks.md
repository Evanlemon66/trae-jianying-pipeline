# 导出一致性（Export Parity）任务清单

对应规格：`specs/export-parity/spec.md`
前置约束：`pipeline/pipeline.py`、`pipeline/preset_ops.py` 正由另一会话修改，本轮任务全部避开这两个文件。
状态：未开始（等待批准 §8）

## T0 只读审计脚本（不依赖批准，可先做）
- [ ] 新增 `tools/audit_export_parity.py`（只读，不写任何被审计目录）
  - 输入：`<cover_job>` 或 `<export_dir>`；输出：每个版本的 `PASS/FAIL` + 缺失项。
  - 断言 1：排除 `.v<n>.*` 后文件集合 = `{<草稿名>.mp4, <草稿名>.mp4.progress.json, <草稿名>-封面.jpg}`。
  - 断言 2：`progress.done == true` 且 `progress.cover` 非空且文件存在。
  - 断言 3：草稿 `draft_content.json` 顶层 `cover != null`、`materials.drafts` 内对应 id 存在、`Resources/cover/*` 非空。
  - 断言 4：封面可被 PIL 打开、宽高比与 `04_covers` 竖版图一致（±1%）。
  - 断言 5：跨版本比对文件名后缀集合，`progress.cover` 必须指向本版本目录内的文件。
- [ ] 用当前实盘跑一次，期望：A `FAIL(missing -封面.jpg)`、B `PASS`（B 若因其它项失败，逐项列出，不得概括成「跑一遍看看」）。

## T1 选项 1：导出前封面落盘闸门（必须改）
- [ ] 新增 `lib/draft_cover_guard.py`
  - `resolve_version_cover(job_dir, profile) -> str`：`a` 用 `jm.resolve_profile_cover(job_dir, "a")`；`b/c` 用同函数按组解析；解析不到抛 `CoverMissing`。
  - `ensure_cover(draft_dir, cover_path) -> None`：读 `draft_content.json` → `set_cover_composition` → `save_draft` → `set_draft_cover`（复用 `lib/jy_cover.py`，幂等）。
  - 写盘前检查 `draft_content.json` 可解析；失败抛异常，不吞。
- [ ] 修改 `lib/cover_watch.py` `run_export`
  - 位置：L1814 `es.begin(...)` 之后、L1816 `sr._run_export(...)` 之前。
  - 逻辑：`try: guard.ensure_cover(draft_dir, guard.resolve_version_cover(job_dir, prof))`；异常 → `note_export_result(..., failure_class="cover_missing", retryable=False, phase="export", profile=prof)` + toast + `return False`。
  - 不做任何 `pipeline.py` / `preset_ops.py` 改动。

## T2 选项 2：edit 失败禁止进入 export（必须改）
- [ ] 修改 `lib/cover_watch.py` `build_plan`
  - 位置：`if draft is None:`（L994）之前。
  - 条件：`draft is not None` 且 `.edit_state.json` 的 `status in {"retrying","paused"}` 且 `phase == "edit"` 且 `phase_override is None` → `plan.update(action="blocked", reason="上次剪辑未通过（edit_state=<status>），先补跑剪辑再导出")`。
  - `phase_override == "export"` 分支（L970-978）保持现状，人工强制仍可导出，但会命中 T1 的封面闸门。

## T3 选项 3（可选，待批准）
- [ ] `lib/cover_watch.py` L1837 之后增加三件套校验
  - 缺 `-封面.jpg` 或 `progress.cover` 为空 → `note_export_result(..., failure_class="output_incomplete")`，不 `es.succeed`，不 `record_video`，本轮 mp4 改名 `.incomplete` 待下次重导。
  - 若与 `archive_stale_outputs` 命名冲突，优先复用其 `.v<n>` 归档语义，不新增后缀族。

## T4 已有成品处理（默认不执行）
- [ ] 不修改 `<导出根>\八零猎王：赶山捕兽遇旺妻_第01-142集串流\`。
- [ ] 若用户要求补齐：先完成 T1，再 `python lib/cover_watch.py reopen --job "<cover_job>" --yes` 重导 A。
- [ ] 禁止直接复制 `04_covers` 竖版图充当 `-封面.jpg`。

## T5 验证（静态部分可立即执行；实跑需批准）
- [ ] 静态：T0 审计脚本对 A/B 输出符合预期。
- [ ] 单测/回归（不触碰剪映）：
  - [ ] `lib/draft_cover_guard.py` 用临时草稿副本验证幂等（连续调用两次，`materials.drafts` 中 composition 条目数不增加）。
  - [ ] `build_plan` 用构造的 `.edit_state.json` 验证：`retrying` → blocked；`phase_override="export"` → export。
- [ ] 实跑（须用户确认）：选一部短剧或既有剧重导 A，跑 T0 审计，期望 a/b 均 `PASS`。
- [ ] 不建议实跑《八零猎王》A：8.6 GB、约 27 分钟。

## 完成定义（DoD）
1. T0 审计脚本对 A/B 给出逐项判读结果。
2. T1 + T2 落地，且 `git diff` 不含 `pipeline/pipeline.py`、`pipeline/preset_ops.py`。
3. 上述单测通过。
4. 用户批准的实跑验证通过后，同剧所有版本审计均 `PASS`。
