# Spec：把「UI 准入」这条线真正跑通并交付

- 状态：待批准（只读梳理已完成，未改代码、未拉起剪映、未跑准入）
- 日期：2026-11-14
- 范围：启动/就绪判定口径统一、崩溃与假死重试预算、准入报告落盘时机、准入链路接入唯一判定

## 1. 背景

剪映 5.9 无无 UI 导出能力（已确认，方向关闭），导出与准入都只能走 UIA。当前导出链
（watcher / series_runner / 直调 robust_auto_export.py）已收敛到
`JyInstance.ensure_for_export()` 唯一口径；但 `pipeline_ui/admission_test.py` 走的仍是
另一套（`ui_nav.open_draft` → `ui_core.launch_and_connect` → `ensure_jianying`），
就绪口径、重拉预算、失败分类都与导出链不同。本次目标是让准入链复用同一套判定并交付。

## 2. 现状事实（以当前工作区代码为准）

### 2.1 四个入口

| 入口 | 进入方式 | 传入 |
|---|---|---|
| watcher `lib/cover_watch.py` | 组计划 → `sr._run_export(dn, timeout=export_stall_timeout, cold_start=cfg.get("cold_start"), job_dir=job_dir)` | `presets.json` 无 `cold_start` 键 → `None`（auto）；外层已持 `.export.lock`，`_run_export` 识别本进程持锁后复用 |
| `pipeline/series_runner.py` | CLI `--mode export` → `_run_export(..., cold_start=args.cold_start or None)` | `--cold-start` 为 store_true；未持锁时自行 `export_lock.acquire` |
| 直调 `robust_auto_export.py` | `run(cold_start=args.cold_start)` | `--cold-start` 默认 False（复用/重启仍由管理器判定）；`--retries` 默认 2 → 共 3 次尝试 |
| `pipeline_ui/admission_test.py` | `_open_admission_draft` → `ui_nav.open_draft(timeout=edit_timeout=90, hot_wait=30/0, cold_wait=150, max_launch=2, allow_cold_restart=true)` | 未运行/无窗口 → `_launch_until_card` → `launch_and_connect(max_wait=420, retries=0, readiness='window')`；**不经过 `ensure_for_export`** |

三者（前 3 个）最终都落到 `robust_auto_export.run()` → `ensure_for_export(force_clean=cold_start)`。

### 2.2 `ensure_for_export()` 四分支（`examples/jy_instance.py:547-596`）

先 `backend.prime()`（在本线程建 UIA 单例，避免探测线程未 CoInitialize 全判 False）：

1. `force_clean=True` → `restart(timeout=120s)`；成功 True，否则 `_raise_unready`。
2. 进程不在 → `launch(timeout=120s)`；成功 True，否则 `_raise_unready`。
3. `detect()`：
   - `home`（且 `_home_ready`：能枚举草稿卡片/可用按钮/文本）→ 复用；
   - `editing` → `go_home(timeout=60s, allow_restart=True, restart_timeout=120s)`，失败 `_raise_unready`；
   - `launching`（进程在但窗口/页面不可用）→ `wait_ready("home", +30s)`（`REUSE_READY_GRACE_S`）→ 成功复用，否则 `restart(120s)`，仍失败 `_raise_unready`。

`_raise_unready` 在 `last_outcome.outcome ∈ {died, died_late, died_after_ready, timeout}` 时
抛带 `[launch_failed:…]` 标记的 `JyInstanceError`，否则抛「未到达可交互首页（窗口未就绪/UI 假死）」。

### 2.3 拉起（`lib/jianying_launch.py`）

- `spawn_jianying`：`cwd=exe 目录` + breakaway `Popen` → `OSError` 时 `wmi_launch` + `_find_new_jianying_pid` → 再失败 `plain`；`how` 按实际父进程判定。
- `ensure_jianying` 单次尝试循环（`timeout` 默认 120s，`HOME_READY_TIMEOUT_S`）：
  1. `crash_dialogs()` 命中 → 记录 `crash_title/crash_text/crash_pid/crash_at`，关窗，`outcome=died`，break；
  2. `env_check_windows()` 存在 → 排他，不判 ready；
  3. 否则 `poll_with_timeout(ready_fn, 8s)`；ready → 8s `_post_ready_watch`（`POST_READY_WATCH_S`）：进程消失 → `died_after_ready`；否则 `ready` + `[startup-ready]` + 台账；
  4. 到 deadline → `timeout` + `[startup-timeout]` + 台账（**不重试**）；
  5. 进程消失 → `died`（≤20s `EARLY_DEATH_WINDOW_S`）/`died_late`（>20s）+ `[startup-died]` + 台账。
- 重拉预算：仅 `died`/`died_after_ready` 且 `budget.take()` 成功（`EARLY_DEATH_RETRIES=1`，每个 `JyInstance` 一份）。

### 2.4 失败分类与重试预算（`lib/failure_class.py` + `robust_auto_export.py`）

| 类别 | 谁判定 | 策略 | 重试 |
|---|---|---|---|
| `launch_failed` | `_raise_unready` / `connect()` 拼 `[launch_failed:…]` | RESTART | 单次运行内最多 3 次尝试（`--retries 2`）；watcher 看 `RELAUNCH_CLASSES` 做跨轮重拉 `RELAUNCH_MAX_ROUNDS=3`，到顶转 `failed` |
| `window_missing` | `connect()` 找不到窗口（`[window_missing]`） | RESTART | 同上，但**不在** `RELAUNCH_CLASSES`，watcher 走普通退避 |
| `window_activate_timeout` | `connect()` 的 `SetActive()` 抛错（`[window_activate_timeout]`） | KILL_RETRY | 单次运行内重试；`_wait_edit_page` 连续 2 次即中止 |
| `ui_hang` | 分类器兜底（打开草稿超时 / 未就绪 / `Find Control Timeout`） | RESTART | 单次运行内重试 |
| `died_after_ready` | `ensure_jianying` 的 8s 就绪后观察窗 | 归入 `launch_failed` 标记 | 消耗启动预算 |

上游钳制：`asset_missing`/`disk_full`/`version_unsupported` 不可重试、不计数；`export_timeout` KILL_RETRY。

### 2.5 启动台账（`pipeline/_work/launch_ledger.jsonl`）

字段：`ts, launch_id, job, job_round, entry, attempt, retry_count, result(ready|timeout|died|died_after_ready), died_at_s, alive_s, ready_at_s, post_ready_died_at_s, env_check_seen, env_check_span, env_check_pid, gpu_probes, notfind, crash, crash_title, pid, how, readiness, late, error`。

定位一次启动卡在哪：`result=timeout & alive_s≈120 & env_check_seen=false` → 窗口/首页始终没渲染；
`env_check_span` 大 → VEDetector 环境检测窗占时；`crash=true` → 看 `crash_title`（`VEDetector has stopped working`）；
`result=died & alive_s<20` → 启动即死（`detector_tail` 只在日志，不在台账）；`died_after_ready + post_ready_died_at_s` → 就绪后死；
`how=wmi` 说明 breakaway 被拒走了兜底。

### 2.6 已确认的缺口

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

## 4. 方案（待批准后实施）

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

- `_dump_report` 的 `final` 判据改为「本轮是否跑到收尾」：在 `main` 的 `finally` 统一调用
  `_dump_report(..., final=completed)`；`completed = bool(stage_results) and not interrupted`；
- 正式报告 `_admission_report.json` 增加 `ok`（全部阶段 ok）、`failed_stage`、`failure_class`（用 `lib/failure_class.classify(error, phase=stage)`）；
- 进行中仍写 `.partial.json`，语义不变。

### 4.4 准入配置

文件：`pipeline/presets.json` 的 `admission.open`（仅新增，不改现值）

- 新增 `"ready_timeout": 120`（映射 `ensure_for_export(timeout=…)`）；
- `cold_wait`/`max_launch`/`allow_cold_restart` 语义不变；
- `allow_cold_restart=false` 时 `launch_via_manager` 不杀进程：`force_clean=False` 且进程在但不可用时不重启，直接报 `window_activate_timeout`/`ui_hang`。

## 5. 验收判据

| 步骤 | 通过判据 | 失败判据 |
|---|---|---|
| 静态自检 | `python -m pytest tests/test_jy_instance.py tests/test_jianying_launch.py tests/test_window_locator.py tests/test_recovery_actions.py tests/test_export_retry.py tests/test_failure_class.py` 全 exit=0 | 任一 exit≠0 |
| 准入 dry-run（不拉起） | `python -c "import sys; sys.path.insert(0,'pipeline_ui'); import ui_nav, ui_core"` 无 ImportError；`ui_nav.open_draft` 签名可用 | ImportError / 签名不符 |
| 准入实跑 | `python pipeline_ui\admission_test.py --stop-after export`：日志出现 `实例判定：首页可交互 → 复用` 或 `拉起`，且 `_admission_report.json` 生成，含 `ok` 与 `stages.export.ok` | 停在 `open_draft`、窗口在但无编辑页、报告未生成 |
| 台账 | 新增记录的 `entry=admit`，`result=ready`，`ready_at_s` 有值 | `entry=unknown` / `result=timeout` 且 `env_check_seen=false` |
| 崩溃吸收 | 人为制造 1 次 VEDetector 崩溃（关闭其进程）后仍能在 3 次内 ready | 第 2 次崩溃即终止 |
| 失败留痕 | 把 `admission.open.ready_timeout` 设为 1 强制超时，报告仍生成且 `ok=false`、`failed_stage=cold_start` | 只留 `.partial.json` |

## 6. 执行顺序与值守

1. 4.1 口径统一（需人盯一次准入实跑，确认首页判据与编辑页进入）
2. 4.2 崩溃预算（可无人值守，靠台账断言）
3. 4.3 报告落盘（可无人值守，用强制超时用例验证）
4. 4.4 配置新增（可无人值守）

## 7. 风险与回滚

- 风险：`readiness` 从 `window` 提到 `home` 会让准入启动等待变长（上限 120s），冷盘机器上首次可能多等 30–60s。
- 风险：崩溃重拉 3 次会在 VEDetector 持续崩溃时把单次准入拉长到约 6 分钟；台账 `crash_retry` 可识别，超过即按 `launch_failed` 终态。
- 回滚：4.1/4.2/4.3 均为独立提交，`ui_nav` 保留 `launch_and_connect` 旧路径，可单点回退；`presets.json` 仅新增键，删除即恢复。

## 8. 需批准

- 是否同意把准入链从 `readiness='window'` 提升为 `home`（会改变准入的启动等待时长）。
- 是否同意崩溃预算从 1 次提为 3 次（对应已观测的 nvapi 三次崩溃）。
- 是否同意准入正式报告改为「跑完即写」（失败也落正式报告，partial 仅进行中）。
