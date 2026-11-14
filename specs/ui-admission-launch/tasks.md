# Tasks：UI 准入链路打通

对应 `specs/ui-admission-launch/spec.md`。未获批准前不执行任何一条。

## T1 准入接入 `ensure_for_export`（4.1）

- 文件：`pipeline_ui/ui_core.py`
  - 新增 `launch_via_manager(readiness='home', force_clean=False, timeout=120)`：构造 `JyInstance`（沿用现有 exe 解析），调用 `ensure_for_export`，成功后 `_activate_and_classify` 返回 `(win, page)`。
- 文件：`pipeline_ui/ui_nav.py`
  - `open_draft` 冷启动分支改调 `launch_via_manager`；删除 `readiness` 形参（统一 home），保留 `cold_wait`/`max_launch`/`allow_cold_restart` 语义。
- 文件：`examples/jy_instance.py`、`lib/jianying_launch.py`
  - `JyInstance.launch(..., entry='export')`、`ensure_for_export(..., entry='export')` 增加透传；准入传 `'admit'`，导出链不改默认。
- 验证：
  - `python -m pytest tests/test_jy_instance.py tests/test_window_locator.py -q` exit=0
  - `python -c "import sys; sys.path.insert(0,'pipeline_ui'); import ui_nav"` 无报错
  - 实跑 `python pipeline_ui\admission_test.py --stop-after export`，日志出现 `实例判定`，台账 `entry=admit`

## T2 崩溃预算与早死预算分离（4.2）

- 文件：`lib/jianying_launch.py`
  - `LaunchBudget` 增 `crash_remaining`（`CRASH_RETRIES=2`）与 `take_crash()`；
  - 崩溃框分支改用 `take_crash()`，重拉前 `settle_s≥3`；
  - 台账 `_record_launch` 增 `crash_retry` 字段。
- 验证：
  - `python -m pytest tests/test_jianying_launch.py -q` exit=0
  - 人为结束 `VEDetector.exe` 一次后跑准入，台账出现 `crash=true` 且后续 `result=ready`

## T3 准入报告落盘时机（4.3）

- 文件：`pipeline_ui/admission_test.py`
  - `main` 的 `finally` 统一 `_dump_report(..., final=completed)`；
  - 报告增 `ok` / `failed_stage` / `failure_class`（`lib/failure_class.classify(error, stage)`）。
- 验证：
  - 正常跑：`_admission_report.json` 存在且 `ok=true`
  - 强制失败：`admission.open.ready_timeout=1` 跑一次，报告存在且 `ok=false`、`failed_stage=cold_start`

## T4 准入配置新增（4.4）

- 文件：`pipeline/presets.json`（仅新增 `admission.open.ready_timeout=120`，不改现值）
- 验证：`python -c "import sys;sys.path.insert(0,'pipeline');import export_config;print(export_config.get_admission_config().get('open'))"` 能读到新键

## T5 端到端交付跑

- 命令：`python pipeline_ui\admission_test.py --stop-after export`
- 通过：编辑页进入 → 各阶段 ok → `stage_results.export.ok=true` → `_admission_report.json` `ok=true`
- 失败：停在任一阶段即停手，凭 `failed_stage` + 台账 `launch_ledger.jsonl` 最后一条定位，不重试超 3 轮

## 顺序与值守

T1（人盯实跑）→ T2/T3/T4（可无人值守）→ T5（人盯，约 10–15 分钟）
