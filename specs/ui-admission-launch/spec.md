# Spec：把「UI 准入」这条线真正跑通并交付

- 状态：已实施（T1–T4 代码已落地；T5 结果见 `tasks.md`）；2026-11-15 按工作区代码补齐判据与阶段说明
- 日期：2026-11-14（立项）/ 2026-11-15（现状对齐）
- 范围：启动/就绪判定口径统一、崩溃与假死重试预算、准入报告落盘时机、准入链路接入唯一判定；另补准入结论判据、各阶段成功判据、入口与参数归属

## 1. 背景

剪映 5.9 无无 UI 导出能力（已确认，方向关闭），导出与准入都只能走 UIA。当前导出链
（watcher / series_runner / 直调 robust_auto_export.py）已收敛到
`JyInstance.ensure_for_export()` 唯一口径；立项时 `pipeline_ui/admission_test.py` 走的仍是
另一套（`ui_nav.open_draft` → `ui_core.launch_and_connect` → `ensure_jianying`），
就绪口径、重拉预算、失败分类都与导出链不同。本文档记录把准入链接入同一判定的方案；
截至 2026-11-15，代码已按 §4 落地，现状差异见 §12。

## 2. 现状事实（以当前工作区代码为准）

### 2.1 四个入口

| 入口 | 进入方式 | 传入 |
|---|---|---|
| watcher `lib/cover_watch.py` | 组计划 → `sr._run_export(dn, timeout=export_stall_timeout, cold_start=cfg.get("cold_start"), job_dir=job_dir)` | `presets.json` 无 `cold_start` 键 → `None`（auto）；外层已持 `.export.lock`，`_run_export` 识别本进程持锁后复用 |
| `pipeline/series_runner.py` | CLI `--mode export` → `_run_export(..., cold_start=args.cold_start or None)` | `--cold-start` 为 store_true；未持锁时自行 `export_lock.acquire` |
| 直调 `robust_auto_export.py` | `run(cold_start=args.cold_start)` | `--cold-start` 默认 False（复用/重启仍由管理器判定）；`--retries` 默认 2 → 共 3 次尝试 |
| `pipeline_ui/admission_test.py` | `_open_admission_draft` → `ui_nav.open_draft(timeout=edit_timeout=90, hot_wait=30/0, cold_wait=150, max_launch=2, allow_cold_restart=true, ready_timeout=120)` | 未运行/无窗口 → `_launch_until_card` → `ui_core.launch_via_manager(readiness='home', force_clean=False, timeout=ready_timeout)` → `JyInstance.ensure_for_export(entry='admit')`；台账 `entry=admit` |

前 3 个最终都落到 `robust_auto_export.run()` → `ensure_for_export(force_clean=cold_start)`；
准入链经 `launch_via_manager` 也落到同一个 `ensure_for_export`（就绪口径固定 `home`）。

### 2.2 `ensure_for_export()` 四分支（`examples/jy_instance.py:711-764`）

先 `backend.prime()`（在本线程建 UIA 单例，避免探测线程未 CoInitialize 全判 False）；
`entry` 只影响启动台账分类（`export` / `admit`），不改变判定：

1. `force_clean=True` → `restart(timeout=120s)`；成功 True，否则 `_raise_unready`。
2. 进程不在 → `launch(timeout=120s)`；成功 True，否则 `_raise_unready`。
3. `detect()`：
   - `home`（且 `_home_ready`：能枚举草稿卡片/可用按钮/文本）→ 复用；
   - `editing` → `go_home(timeout=60s, allow_restart=True, restart_timeout=120s)`，失败 `_raise_unready`；
   - `launching`（进程在但窗口/页面不可用）→ `wait_ready("home", +30s)`（`REUSE_READY_GRACE_S`）→ 成功复用，否则 `restart(120s)`，仍失败 `_raise_unready`。
4. 拉起成功后 `_normalize_home_after_start`：5.9 自动恢复草稿会停在编辑页，软关回首页；首页直接放行。

`_raise_unready` 在 `last_outcome.outcome ∈ {died, died_late, died_after_ready, timeout}` 时
抛带 `[launch_failed:…]` 标记的 `JyInstanceError`，否则抛「未到达可交互首页（窗口未就绪/UI 假死）」。

### 2.3 拉起（`lib/jianying_launch.py`）

- `spawn_jianying`：`cwd=exe 目录` + breakaway `Popen` → `OSError` 时 `wmi_launch` + `_find_new_jianying_pid` → 再失败 `plain`；`how` 按实际父进程判定。
- `ensure_jianying` 单次尝试循环（`timeout` 默认 120s，`HOME_READY_TIMEOUT_S`）：
  1. `crash_dialogs()` 命中 → 记录 `crash_title/crash_text/crash_pid/crash_at`，关窗，`outcome=died`，break；
  2. `env_check_windows()` 存在 → 排他，不判 ready；
  3. 否则轮询 `ready_fn`；ready → 3s `_post_ready_watch`（`POST_READY_WATCH_S`）：进程消失 → `died_after_ready`；否则 `ready` + `[startup-ready]` + 台账；
  4. 到 deadline → `timeout` + `[startup-timeout]` + 台账（**不重试**）；
  5. 进程消失 → `died`（≤20s `EARLY_DEATH_WINDOW_S`）/`died_late`（>20s）+ `[startup-died]` + 台账；
  6. 检测器日志已写过但连续 5s（`DETECTOR_STALL_S`）零增长且从未建窗 → `detector_stalled`，停实例并记录。
- 重拉预算（两份独立预算，互不挤占）：
  - 普通死亡：`EARLY_DEATH_RETRIES=2`（每个 `JyInstance` 一份）。判据是「退出时从未建出窗口」或 `died` / `died_after_ready`；
  - 崩溃框：`CRASH_RETRIES=2`，走 `budget.take_crash()`，不再与早死预算共用。

### 2.4 失败分类与重试预算（`lib/failure_class.py` + `robust_auto_export.py`）

| 类别 | 谁判定 | 策略 | 重试 |
|---|---|---|---|
| `launch_failed` | `_raise_unready` / `connect()` 拼 `[launch_failed:…]` | RESTART | 单次运行内最多 3 次尝试（`--retries 2`）；watcher 看 `RELAUNCH_CLASSES` 做跨轮重拉 `RELAUNCH_MAX_ROUNDS=3`，到顶转 `failed` |
| `window_missing` | `connect()` 找不到窗口（`[window_missing]`） | RESTART | 同上，但**不在** `RELAUNCH_CLASSES`，watcher 走普通退避 |
| `window_not_created` | 启动后始终没有窗口 | RESTART | 单次运行内重试，消耗普通启动预算 |
| `window_activate_timeout` | `connect()` 的 `SetActive()` 抛错（`[window_activate_timeout]`） | KILL_RETRY | 单次运行内重试；`_wait_edit_page` 连续 2 次即中止 |
| `ui_hang` | 分类器兜底（打开草稿超时 / 未就绪 / `Find Control Timeout`） | RESTART | 单次运行内重试 |
| `artifacts_incomplete` | 导出后三件套/核心产物缺失 | RESTART | 单次运行内重试 |
| `verify_failed` | 导出后 `verify_export` 不通过 | RESTART | 单次运行内重试 |
| `track_restore_failed` | 最终形态恢复轨道失败 | RESTART | 单次运行内重试 |
| `ui_step_failed` | UI 步骤返回失败 | RESTART | 单次运行内重试 |
| `died_after_ready` | `ensure_jianying` 的 3s 就绪后观察窗 | 归入 `launch_failed` 标记 | 消耗普通启动预算 |

上游钳制：`asset_missing`/`disk_full`/`version_unsupported` 不可重试、不计数；`export_timeout` KILL_RETRY。

### 2.5 启动台账（`pipeline/_work/launch_ledger.jsonl`）

字段：`ts, launch_id, job, job_round, entry, attempt, retry_count, result(ready|timeout|died|died_after_ready), died_at_s, alive_s, ready_at_s, post_ready_died_at_s, env_check_seen, env_check_span, env_check_pid, gpu_probes, notfind, crash, crash_title, pid, how, readiness, late, error`。

定位一次启动卡在哪：`result=timeout & alive_s≈120 & env_check_seen=false` → 窗口/首页始终没渲染；
`env_check_span` 大 → VEDetector 环境检测窗占时；`crash=true` → 看 `crash_title`（`VEDetector has stopped working`）；
`result=died & alive_s<20` → 启动即死（`detector_tail` 只在日志，不在台账）；`died_after_ready + post_ready_died_at_s` → 就绪后死；
`how=wmi` 说明 breakaway 被拒走了兜底。

### 2.6 立项时缺口与当前状态

> 当前状态（2026-11-15，按代码）：1/2/3/5 已修复；4 部分修复（`stop_after≥enable` 会写最终报告，
> `stop_after≤locate` 仍只更新 partial）；6 仍按环境限制「接受并记录」。逐条证据见 §12。

1. **准入链不受 `ensure_for_export` 约束**：`readiness='window'`（只看真 QML 窗口出现），不看首页是否可交互；`retries=0` 把重拉完全交给外层 `max_launch=2`；因此「窗口出现但首页假死」会被准入判成就绪。
2. **准入没有失败分类**：`ui_nav` 报错文案不落 `failure_class`，无法与导出链共用策略表。
3. **VEDetector 崩溃预算不足**：崩溃框路径把 `outcome` 置 `died` 并消耗同一个 1 次早死预算；本机已记录 nvapi 崩 3 次，必然第 2 次就放弃。
4. **准入正式报告只在 export 成功后写**：`_dump_report(..., final=export_ran)`，`export_ran` 仅当 `step_export` 返回 `ok`；失败/中途停在 `_admission_report.partial.json`，交付时拿不到「失败在哪一阶段」的正式记录。
5. **准入台账 entry 口径**：`ui_core.launch_and_connect` 调 `ensure_jianying` 时未显式传 `entry='admit'`，台账里与导出链混为 `unknown`。
6. **VEDetector 冷启动崩溃（nvapi）——已定性为环境限制，按"接受并记录"处理**：冷启动 4–6 秒内 `VEDetector.exe` 以 BEX64 / `0xc0000005` 退出，故障模块 `nvapi64_impl.dll_unloaded`；根因是 NV 设置检测（`type:11`）调用 NVAPI 期间 impl 已被卸载，线程仍在其中执行。证据：检测器日志固定停在 `generateGraphcisCardInfo enter`（崩溃 20–21 KB / 跑完 45–46 KB）；事件 1000 共 75 次、偏移固定 `0x380ee0`（模块 `32.0.16.2114`）；上一版驱动 `32.0.16.1656` 已崩 18 次（偏移 `0x376220`），换驱动无效。副作用：`driver_cache.json` 可能为 0 字节、`EnvDetectSimulate.json` 不更新。兜底：崩溃独立预算 + 就绪口径"首页可交互"。不修的理由：本机需要独显正常工作，不做"禁用独显 / 关显卡检测"对照。**未做调栈取证（需改注册表开 LocalDumps），属已知缺口。**

## 3. 目标与非目标

目标：
- 让准入链与导出链共用同一套「实例是否可用 / 是否需要干净重启」判定与就绪口径。
- 让本机已知环境因素（VEDetector nvapi 崩溃、冷启动 60s 未就绪、4.3s 启动即死）在预算内被吸收，不因第 2 次崩溃直接终止。
- 准入无论成功/失败/停在哪个阶段，都留一份正式记录（含阶段与失败分类）。
- 台账能一眼区分 admit / export 与失败形态。

非目标：
- 不改导出成品、不改 `pipeline/presets.json` 的业务配置值（仅允许新增缺省可选的键）。
- 不引入无 UI 导出。
- 不改 `robust_auto_export` 的导出主流程（打开草稿→导出→收尾）。

## 4. 方案（已实施，以代码为准）

### 4.1 准入接入唯一判定

文件：`pipeline_ui/ui_nav.py`、`pipeline_ui/ui_core.py`

- `ui_nav.open_draft` 的冷启动分支改为调用新增的 `ui_core.launch_via_manager(readiness='home', force_clean=False, timeout=120)`：
  - 内部构造 `JyInstance`（`find_exe=ui_core` 现有的 exe 解析），调用 `ensure_for_export(force_clean=force_clean, timeout=timeout)`；
  - 成功后用 `exporter` 等价的 `rebind_window()` 拿窗口，走现有 `_activate_and_classify` 返回 `(win, page)`；
  - 失败把 `JyInstanceError` 原样抛出（保留 `[launch_failed:…]` / `[window_missing]` / `[window_activate_timeout]` 标记）。
- `_launch_until_card` 的 `max_launch` 保留为外层上限，但每轮改为调用 `launch_via_manager`；`readiness` 形参删除（统一 `home`），`cold_wait` 仍用于等草稿卡片。
- `ui_core.launch_and_connect` 保留给其他调用方，内部显式传 `entry='admit'`；`launch_via_manager` 也走 `entry='admit'`（通过 `JyInstance.launch` 的 `ensure_jianying(entry=...)` 增加一个可选 `entry` 形参，默认 `'export'`，避免影响导出链）。

### 4.2 崩溃预算与早死预算分离

文件：`lib/jianying_launch.py`、`examples/jy_instance.py`

- `LaunchBudget` 增加 `crash_remaining`（默认 `CRASH_RETRIES=2`，即总 3 次，对应本机 nvapi 崩 3 次）；
- `ensure_jianying` 的崩溃框分支改用 `budget.take_crash()`；早死（`died`/`died_after_ready`）仍用 `take()`；
- 台账新增 `crash_retry` 字段（本次是否因崩溃重拉），`crash=true` 已可定位形态；
- 崩溃重拉前 `settle_s` 不小于 3s（仅崩溃路径，避免立刻再撞 nvapi）。

### 4.3 准入报告落盘时机

文件：`pipeline_ui/admission_test.py`

- 成功路径：`calibrate()` 跑完（`stop_after≥enable`）时在末尾 `_save_merged(calib)` +
  `_dump_report(..., final=True)`（约 1132–1133 行）。
- 失败路径：`__main__` 的 `finally` 在 `not completed` 时补写正式报告，`error` 非空；
  随后才做收尾与「仅结构破坏才回滚」。
- 正式报告 `_admission_report.json` 字段：`ok`（三态，见 §9.1）、`failed_stage`、`failed_stages`、
  `failure_class`、`stage_counts`、`stages`、`marks`、`timeline`。
- 进行中仍写 `.partial.json`。
- **已知例外**：`stop_after≤locate` 在 `_save_merged` 后提前 `return`，只更新 partial，
  不写正式报告；与「跑完即写」不完全一致，见 §12。

### 4.4 准入配置

文件：`pipeline/presets.json` 的 `admission.open`（仅新增，不改现值）

- 新增 `"ready_timeout": 120`（映射 `ensure_for_export(timeout=…)`）；
- `cold_wait`/`max_launch`/`allow_cold_restart` 语义不变；
- `allow_cold_restart=false` 的实际语义：`ui_nav.open_draft` 在进程不在/无窗口时直接报错，
  **根本不进入冷启动分支**；它不会调 `launch_via_manager`，也不存在「管理器不杀进程」的开关。
  （§4.4 旧稿的这条描述与代码不符，见 §12。）

## 5. 验收判据

| 步骤 | 通过判据 | 失败判据 |
|---|---|---|
| 静态自检 | `python -m pytest tests/test_jy_instance.py tests/test_jianying_launch.py tests/test_window_locator.py tests/test_recovery_actions.py tests/test_export_retry.py tests/test_failure_class.py` 全 exit=0 | 任一 exit≠0 |
| 准入 dry-run（不拉起） | `python -c "import sys; sys.path.insert(0,'pipeline_ui'); import ui_nav, ui_core"` 无 ImportError；`ui_nav.open_draft` 签名可用 | ImportError / 签名不符 |
| 准入实跑 | `python pipeline_ui\admission_test.py --stop-after export`：日志出现 `实例判定：首页可交互 → 复用` 或 `拉起`；`_admission_report.json` 生成且 `ok=true`、`stage_counts.fail=0`、`stages.export.ok=true` | 停在 `open_draft`、窗口在但无编辑页、报告未生成或 `ok!=true` |
| 台账 | 新增记录的 `entry=admit`，`result=ready`，`ready_at_s` 有值 | `entry=unknown` / `result=timeout` 且 `env_check_seen=false` |
| 崩溃吸收 | 人为制造 1 次 VEDetector 崩溃（关闭其进程）后仍能在总 3 次内 ready | 第 2 次崩溃即终止 |
| 失败留痕 | 把 `admission.open.ready_timeout` 设为 1 强制超时，`_admission_report.json` 仍生成且 `ok=false`、`failed_stage=fullscreen`、`failure_class=launch_failed`（冷启动失败不设独立的 `cold_start` 阶段） | 只留 `.partial.json` |

## 6. 执行顺序与值守

1. 4.1 口径统一（需人盯一次准入实跑，确认首页判据与编辑页进入）
2. 4.2 崩溃预算（可无人值守，靠台账断言）
3. 4.3 报告落盘（可无人值守，用强制超时用例验证）
4. 4.4 配置新增（可无人值守）

## 7. 风险与回滚

- 风险：`readiness` 从 `window` 提到 `home` 会让准入启动等待变长（上限 120s），冷盘机器上首次可能多等 30–60s。
- 风险：崩溃重拉 3 次会在 VEDetector 持续崩溃时把单次准入拉长到约 6 分钟；台账 `crash_retry` 可识别，超过即按 `launch_failed` 终态。
- 回滚：4.1/4.2/4.3 均为独立提交，`ui_nav` 保留 `launch_and_connect` 旧路径，可单点回退；`presets.json` 仅新增键，删除即恢复。

## 8. 决策记录（均已落地）

- 准入门槛从 `readiness='window'` 提升为 `home`（2026-11-14 已同意并实施）。
- 崩溃预算从 1 次提为 3 次（`CRASH_RETRIES=2`，已实施；普通早死预算独立为 `EARLY_DEATH_RETRIES=2`）。
- 准入正式报告改为「跑完即写」，失败也落正式报告；`stop_after≤locate` 的早停例外见 §4.3/§12。
- 不修 VEDetector nvapi 崩溃（环境限制），以崩溃预算 + 首页可交互口径兜底（已实施）。

## 9. 准入结论判据（通过 / 失败 / 豁免）

### 9.1 阶段结果的三态与报告字段

`pipeline_ui/admission_test.py::_dump_report` 只对 `STAGES` 内的阶段做统计，过程打点键
（`start`、`audio`、`cleanup` 等）不参与判定：

- `ok=true`：该阶段有结果且返回 `ok=True`；
- `ok=false`：该阶段有结果且返回 `ok=False`；
- `ok=null`：该阶段出现在报告里但没有 `ok=True/False` 结果（通常是只读到 UI 入参或中途未完成），
  计入 `unknown_stages`，**不折算成失败**。

总判定 `ok` 的优先级：

1. 有 `error` 或任一 `failed_stages` → `false`；
2. 否则有 `unknown_stages` → `null`（unknown，不是通过）；
3. 否则有 `passed_stages` → `true`；
4. 否则 → `false`。

因此 **`ok=true` 只证明「本轮已记录到的阶段」都通过，不证明全链路**。例如
`--stop-after tracks` 也可能得到 `ok=true`。整轮 T5 通过必须同时满足：
`--stop-after export`、进程 exit=0、正式报告 `ok=true`、`stage_counts.fail=0`、
`stage_counts.unknown=0`、`stages.export.ok=true`。

`failed_stage` 归属优先级：阶段结果显式 `ok=false` → 阶段内 `_RUN_STATE['failed_stage']`
（如 `enable` 勾选失败）→ 按打点时间线推断。`failure_class` 由
`lib/failure_class.classify(error, stage)` 映射，导入失败时为 `null`，不影响落盘。

### 9.2 通过

- 单阶段通过：该阶段 `ok=True`，且没有抛异常；`locate` 是纯过程阶段（OCR 定位不到只告警，
  不写 `ok=False`），因此它不能单独作为通过证据。
- 整轮通过（T5）：`--stop-after export`，全链路阶段都跑到；`export.ok=True`；
  正式报告 `ok=true`、`unknown=0`、`fail=0`；退出码 0。
- 允许的容差通过：`verify.state=WARN`（见 §10.7）、`translate_lang_ok=false` 但英文文本
  已实际增加并达到门槛、`wait` 命中「30s 内无处理提示」的缓存命中（见 §10.3）。

### 9.3 失败

- 任一阶段抛异常或返回 `ok=False` → 正式报告 `ok=false`，写 `failed_stage` 与 `failure_class`；
  未捕获异常退出码 1。
- 冷启动阶段失败没有独立的 `cold_start` 阶段名：STAGES 首阶段是 `fullscreen`，
  因此报告里通常是 `failed_stage=fullscreen`（见 §12）。
- 失败后只有「结构破坏」才回滚草稿：主轨消失/段数下降、`materials.videos` 减少、
  字幕文本归零等；纯业务失败（找不到控件、导出超时、封面缺失）不回滚，保留现场。
- 回滚发生在正式报告落盘之后；回滚结果写 `rollback` 字段。

### 9.4 豁免与容差

| 豁免/容差 | 生效条件 | 边界（仍必须满足） |
|---|---|---|
| 封面豁免 | 仅准入导出：`step_export(OUT_DIR, require_cover=False)` → 子进程 `--no-cover`；准入草稿没有 `cover_job` | `mp4` 与 `progress_*.json` 仍是硬门槛；封面缺失只记录告警。普通 pipeline/watcher 默认 `require_cover=True`，缺封面判失败 |
| 终检 WARN | `run_verify` 的 `state=WARN`（颜色判型、隐藏行类型不可验证、3<|dy|≤25、多余行、无基线） | `state=FAIL`（期望行缺失、隐藏态不符、UIA 类型不符且无通道冲突）不放行 |
| 人声分离缓存命中 | 开关已确认、30s 缓存窗内无「处理中」提示 | 后续 `extract` 的分离对账仍会校验主轨/素材/音轨，误判会被抓回 |
| 翻译语言非阻断 | `translate_lang_ok=false`，但英文字幕数量实际增加并达到 `min_english_texts` | 没有英文文本仍判失败 |
| `--stop-after` 早停 | 只跑到指定阶段 | **不是豁免**：后续阶段未执行；`stop_after≤locate` 不写正式报告（§12） |
| `--keep-window` | 结束后不关剪映，便于人工检查 | **不是验收豁免**，只影响收尾 |
| 现场人工干扰 | 实跑期间有人在本地手工清理现场（关掉/清空当时的剪映现场），导致该阶段的超时与链路本身无关 | 只豁免被干扰的那一段，且必须在报告/结论里写明「失败原因与链路无关」并保留原始报告与日志；同一次跑批里其它阶段仍按本文判据逐项成立。2026-11-15 的 T5 导出阶段「打开草稿 → 等待编辑页」超时即按此条处理，见 `tasks.md` |

## 10. 阶段说明与成功判据

### 10.0 阶段名对照

| 用户阶段 | 准入 `STAGES` | `pipeline_ui` 步骤 | 实现 |
|---|---|---|---|
| 建稿 | `fullscreen`（含 `_ensure_draft` + 打开 + 全屏） | `[0] init_draft` | `admission_draft.py` / `init_draft.py` |
| 预设合并 | `tracks`（校验；合并发生在建稿内） | `[0]`（`[1] presets` 是占位） | `init_draft.py::merge_preset_into_draft` |
| 人声分离 | `audio_tab` / `locate` / `enable` / `wait` | `[3] vocal_separate` | `ui_vocal_separate.py` |
| 识别翻译 | `recognize` | `[4] recognize_translate` | `ui_recognize_translate.py` |
| 分离音频 | `extract` | `[5] finalize` 前半 | `ui_extract_audio.py` |
| 最终形态 | `extract` 内（恢复全部轨道 + 只隐藏中文轨） | `[5] finalize` 后半 | `ui_extract_audio.py` |
| 终检 | `verify` | `[5] finalize` 内 `run_verify` | `ui_verify.py` |
| 导出 | `export` | `[6] export` | `ui_pipeline.py::step_export` + `robust_auto_export.py` |

### 10.1 建稿（准入 `fullscreen`）

做什么：`_ensure_draft` 先按文件层 + UI 层 + 基线决定复用/打开/重开/重建；重建走
`init_draft` 的暂存目录原子发布（模板骨架 → 注入视频 → 合并预设 → 校验 → 预裁 → 发布），
然后 `open_draft` 打开草稿、确认编辑页、进入全屏。

成功判据：

- 文件层：`draft_content.json` 存在且可解析，duration>0，主视频轨段数>0；
- 复用前基线：轨道类型集合一致、文本数不增加、总时长偏差≤1%；无基线按脏处理并重建；
- 重建校验：主轨段数=视频数、轨道数>1、总时长偏差≤max(2s, 1%)、发布成功；
- UI 层：`connect_jianying` 返回窗口且 `page=edit`（页面类型为 None 时按 edit 处理但告警）；
- `fullscreen` 阶段 `ok=True`，记录 `env`（窗口矩形、DPI 等）。

失败判据：草稿文件无效且重建失败、打不开草稿、始终到不了编辑页、全屏失败。

### 10.2 预设合并（准入 `tracks`）

做什么：建稿阶段按 `preset_profile` 解析 `presets.json`，逐个
`find_preset_by_name` → `merge_preset_into_draft`；随后 `tracks` 阶段识别主轨、锁定并隐藏
预设轨，核对轨道行数。

成功判据：

- 预设解析成功，`expected_track_count = 1 + len(presets)`；
- 实际扫描到的轨道数等于 `expected_track_count`；
- 主轨可识别，预设轨均已锁定+隐藏，`tracks` 阶段 `ok=True`。

失败判据：读不到预设文件/名称、找不到主轨、实际轨道数与期望不一致。
注意：`find_preset_by_name` 找不到某个预设时只打印并跳过，**不会立刻报错**；
最终由轨道数不一致在 `tracks` 阶段判失败。`pipeline_ui [1] presets` 只是占位
（直接返回 `ok=True`，无 UI 动作），不能用它证明预设真的合并成功。

### 10.3 人声分离（准入 `audio_tab` / `locate` / `enable` / `wait`）

做什么：切到「音频」面板并确认面板已切换；OCR 定位「人声分离」开关；点击并轮询确认开关
变为选中/处理中/百分比；选择人声模式；按 End 触发刷新，等待分离完成提示消失。

成功判据：

- `audio_tab.ok=True`：面板确认为音频、基础页签已切、滚动≤20 轮找到「人声分离」；
- `enable.ok=True`：`ensure_vocal_on` 在 20s 内确认已开启，且模式选择没有返回 False；
- `wait.ok=True`：`wait_vocal_done` 成功（默认总超时 600s、出现超时 120s，缓存窗 30s）；
- `locate` 仅记录 OCR 结果，定位不到不影响阶段 `ok`。

失败判据：面板切不过去、滚动 20 轮仍找不到开关、单击后 20s 内无法确认开启、模式下拉不可用、
分离迟迟不开始或超时。准入在此阶段**不复用** `run_vocal_separate` 的「产物稳定 + 处理提示
消失」双条件，只做 `wait_vocal_done`；产物的最终对账在 `extract` 阶段完成。

### 10.4 识别翻译（准入 `recognize`）

做什么：全选主轨 → 点识别字幕 → 选语言 → 轮询轨道/文本增量 → 检查英文文本数量 →
校验字幕包结构 → 取消全选。

成功判据（`run_recognize_translate` 返回 `ok=True`）：

- 中文识别有效：`curr_text >= pre_text+2` 或 `materials.texts >= texts_before+2`；
- 英文翻译有效：新增英文字幕数 ≥ `min_english_texts`（默认 1）；
- 字幕包结构无阻断项：没有「全部译文同一时间点」「每行仅一段译文」等结构性异常；
- 取消全选成功（失败会抛异常）。

失败判据：无中文字幕增量、无英文字幕增量、字幕结构阻断、轮询超时、取消全选失败。
`translate_lang_ok` 只作为记录信号，不是硬门槛；英文实际数量才是门槛。

### 10.5 分离音频（准入 `extract`）

做什么：锁定/隐藏顶部 2 条文本轨 → 选主轨 → `Ctrl+Shift+S` → 等待分离 → 校验分离结果 →
恢复全部轨道。

成功判据（`run_extract_audio` 返回 `ok=True`）：

- 分离对账 3 条同时成立：① 原主轨仍在、类型仍为 video、段数未减少；
  ② `materials.videos` 未减少；③ 新增音频轨且至少一段引用视频原声；
- 顶部 2 条文本轨存在并可滚动到顶；
- `restore_all_tracks` 成功（无未响应/覆盖不足/滚动不可用/仍有锁定或隐藏）。

失败判据：没有音轨新增、主轨消失或段数下降、`materials.videos` 减少、恢复轨道失败。
准入的 `extract` 包含 `pipeline [5]` 的分离+恢复，但 `verify` 单独成阶段，便于区分失败归属。

### 10.6 最终形态（准入 `extract` 内，阶段标记「6 草稿最终形态」）

做什么：恢复全部轨道 → 只隐藏中文副字幕轨（顶部两条文本行中的第二条）→ 保持英文轨可见 →
扫描一次轨道写入 `final_layout.tracks`。

成功判据：

- 除中文副字幕轨外，所有轨道均可见、未锁定；
- 中文副字幕轨的隐藏态经 UIA 复核为隐藏；
- `final_layout.tracks` 非空，含 `type/ymid/hidden/type_source/confidence`；
- 阶段最终 `ok=True`。

失败判据：恢复轨道失败、顶部文本行不是预期的两条、隐藏复核不通过、滚动不到顶、
`final_layout` 写不出来。这里的隐藏规则是「只隐中文、不隐英文」，不是全隐藏。

### 10.7 终检（准入 `verify`）

做什么：以 `final_layout.tracks` 为期望，实际扫描轨道行做逐行对账，输出
`_verify_report.json` 和 `verify_timeline.png`。

判定（`ok = state != FAIL`）：

- PASS：期望行全部匹配，类型一致（UIA 高置信），纵向偏差 |dy|≤3，隐藏态一致；
- WARN（仍然通过）：无基线、通道冲突、颜色判型、隐藏行类型不可验证、3<|dy|≤25、
  多余行、低置信类型不符；
- FAIL：期望行缺失（|dy|>25）、隐藏态不符、UIA 类型不符且无通道冲突可解释。

失败判据：`state=FAIL`。WARN 必须在报告里保留原因，不能静默忽略。

### 10.8 导出（准入 `export`）

做什么：`step_export` 先取全局导出锁，解析草稿名（参数或当前活动草稿），读草稿时长，
用隐藏子进程调 `robust_auto_export.py <draft> <mp4> --move [--expect-duration]
[--resolution] [--no-cover]`，轮询到超时，按子进程返回码判 `ok`。

成功判据（准入）：

- `step_export(OUT_DIR, require_cover=False)` 子进程返回码 0；
- 子进程导出后校验通过：`mp4` 存在且非空、`progress_*.json` 存在且 `done=true` 且属于本轮；
- 封面缺失不阻断（封面豁免）；
- `export.ok=True`。

失败判据：导出锁被占用、拿不到草稿名、脚本不存在、超时、子进程返回码非 0、
产物缺失/校验脚本不通过/成功证据不足。普通 pipeline/watcher 的导出后校验默认要求
`mp4 + 封面 + progress` 三件套；准入只是把封面这一项显式豁免。

## 11. 入口与参数归属（`--video-range` / `--draft-name`）

| 入口 | 命令示例 | 是否接受 `--draft-name` / `--video-range` | 作用 |
|---|---|---|---|
| 准入 harness | `python pipeline_ui\admission_test.py --stop-after export` | **否** | 校准 UI 并做端到端冒烟；草稿名/视频范围来自 `pipeline/presets.json` 的 `admission` 段 |
| 全流程 UI pipeline | `python pipeline_ui\ui_pipeline.py --draft-name X --video-range 10-20 ...` | **是**，只作用于 `[0] init_draft` | 在业务草稿上跑 `[P]`→`[6]`；消费 `ui_calibration.json`，不再校准 |
| 独立建稿 | `python pipeline_ui\init_draft.py --draft-name X --video-range 10-20 ...` | **是** | 只建稿并写入草稿目录，不跑 UI 链路、不写校准 |
| JSON pipeline | `python pipeline\pipeline.py <draft> --video ...` | **否** | 非 UI 的 JSON 管线，草稿是位置参数，视频用 `--video` |
| watcher / series_runner | 无 CLI | **否** | 草稿名来自任务/队列/`cover_job`，视频范围来自任务配置 |

准入 harness 与全流程 pipeline 的区别：

- 目的：准入产出 `ui_calibration.json`（坐标/位置真值）和 `_admission_report.json`；
  全流程消费校准，`[P]` 预检缺校准会直接中止。
- 草稿：准入用 `admission.draft_name`（当前 `ui_pipeline_ac`）及专用 `video.range`；
  全流程用 `--draft-name` 或建稿默认值，不维护准入基线/回滚。
- 控制：准入有 `--stop-after`、partial/正式报告、时间线、截图，失败只做结构回滚；
  全流程按步骤 `ok` 短路、写 `state.json` 支持断点续跑，不做校准写入。
- 导出：准入 `require_cover=False`（封面豁免）；全流程默认三件套（含封面）。
- 启动：两者都走 `ensure_for_export`，准入 `entry=admit`，导出链 `entry=export`。

准入自身没有 `--draft-name`/`--video-range`，要改这两项只能改
`pipeline/presets.json` 的 `admission.draft_name` / `admission.video.range`；不要把它们误加到
`admission_test.py` 的命令行上。

## 12. 本次核对发现的文档与代码不一致

1. §2.1/§2.6.1 旧稿称准入走 `launch_and_connect(readiness='window')` 且不经过
   `ensure_for_export`；实际已改为 `launch_via_manager → ensure_for_export(entry='admit')`。
2. §2.2 的行号 `jy_instance.py:547-596` 已过期；`ensure_for_export` 当前在 711-764 行。
3. §2.3 旧稿写 `_post_ready_watch` 8s、`EARLY_DEATH_RETRIES=1`；实际为
   `POST_READY_WATCH_S=3.0`、`EARLY_DEATH_RETRIES=2`，且崩溃框用独立的
   `CRASH_RETRIES=2` / `take_crash()` 预算。
4. §4.3 旧稿写「`finally` 统一 `_dump_report(final=completed)`」；实际成功路径在
   `calibrate()` 末尾写正式报告，失败路径在 `__main__` 的 `finally` 补写。
5. §4.3 旧稿暗示「跑完即写正式报告」；实际 `--stop-after≤locate` 会在 `_save_merged`
   后提前返回，只更新 `.partial.json`，不写 `_admission_report.json`。
6. §4.4 旧稿称 `allow_cold_restart=false` 时 `launch_via_manager` 不杀进程；实际
   `open_draft` 在这种情况下根本不进入冷启动分支，直接报错。
7. §5 旧稿把 `ok` 当布尔且写 `failed_stage=cold_start`；实际 `ok` 为三态
   （true/false/null），冷启动失败归属首阶段 `fullscreen`。
8. `tasks.md` T3 的验收也写 `failed_stage=cold_start`，与 STAGES/S12.7 不一致。
9. 报告 `stages` 里可能同时出现 `audio`（`audio_tab` 的 `ui_params` 打点键）和
   `audio_tab`（结果键）；`_dump_report` 只统计 `STAGES`，因此 `audio` 不会影响 `ok`，
   但读报告时不能把 `audio` 当成独立阶段。
10. `lib/failure_class.py` 里 `launch_failed` 的注释仍写 `EARLY_DEATH_RETRIES=1`，
    而常量已是 2；本文以常量与实际重试行为为准。
