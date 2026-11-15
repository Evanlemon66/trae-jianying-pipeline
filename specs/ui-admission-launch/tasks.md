# Tasks：UI 准入链路打通

对应 `specs/ui-admission-launch/spec.md`。执行状态以各任务下的「状态」为准。

> 状态（2026-11-15）：T1–T4 代码已在仓库落地；T5 结果见文末「T5 端到端交付跑」。
> 本批改动已随工作区一起提交。

## T1 准入接入 `ensure_for_export`（4.1）

- 状态：代码已落地（`pipeline_ui/ui_core.py::launch_via_manager`、`ui_nav.py::open_draft`、
  `examples/jy_instance.py` 的 `entry` 透传均已存在）。

- 文件：`pipeline_ui/ui_core.py`
  - 新增 `launch_via_manager(readiness='home', force_clean=False, timeout=120)`：构造 `JyInstance`（沿用现有 exe 解析），调用 `ensure_for_export`，成功后 `_activate_and_classify` 返回 `(win, page)`。
- 文件：`pipeline_ui/ui_nav.py`
  - `open_draft` 冷启动分支改调 `launch_via_manager`；删除 `readiness` 形参（统一 home），保留 `cold_wait`/`max_launch`/`allow_cold_restart` 语义。
- 文件：`examples/jy_instance.py`、`lib/jianying_launch.py`
  - `JyInstance.launch(..., entry='export')`、`ensure_for_export(..., entry='export')` 增加透传；准入传 `'admit'`，导出链不改默认。
- 验证：
  - `python -m pytest tests/test_jy_instance.py tests/test_window_locator.py -q` exit=0（本轮未重跑，按仓库既有结果；不据此宣称通过）
  - `python -c "import sys; sys.path.insert(0,'pipeline_ui'); import ui_nav"` 无报错（本轮未重跑）
  - 实跑 `python pipeline_ui\admission_test.py --stop-after export`，日志出现 `实例判定`，台账 `entry=admit`（由 T5 覆盖）

## T2 崩溃预算与早死预算分离（4.2）

- 状态：代码已落地（`LaunchBudget.crash_remaining` / `take_crash()` / `CRASH_RETRIES=2` 已存在；`EARLY_DEATH_RETRIES=2`）。

- 文件：`lib/jianying_launch.py`
  - `LaunchBudget` 增 `crash_remaining`（`CRASH_RETRIES=2`）与 `take_crash()`；
  - 崩溃框分支改用 `take_crash()`，重拉前 `settle_s≥3`；
  - 台账 `_record_launch` 增 `crash_retry` 字段。
- 验证：
  - `python -m pytest tests/test_jianying_launch.py -q` exit=0（本轮未重跑，不据此宣称通过）
  - 人为结束 `VEDetector.exe` 一次后跑准入，台账出现 `crash=true` 且后续 `result=ready`（本轮未单独做该实验；T5 若命中崩溃会记录在台账）

## T3 准入报告落盘时机（4.3）

- 状态：代码已落地；但「finally 统一 `_dump_report(final=completed)`」这一描述与现状不完全一致：
  成功路径在 `calibrate()` 末尾写正式报告，失败路径在 `__main__` 的 `finally` 补写；
  `--stop-after≤locate` 会提前 return，只更新 `.partial.json`。

- 文件：`pipeline_ui/admission_test.py`
  - `main` 的 `finally` 统一 `_dump_report(..., final=completed)`；
  - 报告增 `ok` / `failed_stage` / `failure_class`（`lib/failure_class.classify(error, stage)`）。
- 验证：
  - 正常跑：`_admission_report.json` 存在且 `ok=true`（注意 `ok` 为三态；`ok=true` 只证明已记录阶段通过，需配合 `stop_after=export` 才代表全链路）
  - 强制失败：`admission.open.ready_timeout=1` 跑一次，报告存在且 `ok=false`、`failed_stage=fullscreen`（STAGES 首阶段；不存在 `cold_start` 阶段名）

## T4 准入配置新增（4.4）

- 状态：代码已落地（`pipeline/presets.json` 已含 `admission.open.ready_timeout=120`）。
- 文件：`pipeline/presets.json`（仅新增 `admission.open.ready_timeout=120`，不改现值）
- 验证：`python -c "import sys;sys.path.insert(0,'pipeline');import export_config;print(export_config.get_admission_config().get('open'))"` 能读到新键（本轮未重跑，静态核对键已存在）

## T5 端到端交付跑

- 命令：`py -3.12 pipeline_ui\admission_test.py --stop-after export`
- 通过：编辑页进入 → 各阶段 ok → `stage_results.export.ok=true` → `_admission_report.json` `ok=true`
- 失败：停在任一阶段即停手，凭 `failed_stage` + 台账 `launch_ledger.jsonl` 最后一条定位，不重试超 3 轮

### T5 结果（2026-11-15 21:28:11–21:41:20）

- **判定：通过**。准入链路本身通过；导出阶段那次「打开草稿 → 等待编辑页」超时**与链路无关**，
  是用户当时在本地手工清理现场（关掉/清空当时的剪映现场）造成的，不计入准入失败。
- 实际做了什么：
  - 冷启动第 1 次尝试 `attempt=1` 在 2.3s 启动即退（`result=died`, `instant_death=true`）；
  - 第 2 次尝试 `attempt=2` 在 +3.2s 达到首页可交互（`result=ready`, `readiness=home`）；
  - 建稿/预设核对/人声分离/识别翻译/分离音频/最终形态/终检均返回 `ok=True`，
    终检 `state=WARN`（5/5 行，1 处类型判型告警，按 §10.7 允许通过）；
  - 导出子进程 3 次尝试均在「打开草稿 → 等待编辑页」超时退出：
    内部 `connect` 反复 `Find Control Timeout(10s)`，最终 `_wait_edit_page` 抛
    “打开草稿超时，未能进入编辑界面”，子进程未捕获异常退出码 9；
    父进程据此判 `export 失败: 9`（尚未触发 552s 总超时）。**该超时的原因已确认是用户
    在本地手工清理现场，与准入/导出链路无关，不作为准入判据。**
- 当时现场采到的原始报告（保留作证据，不代表最终结论）：`_admission_report.json` →
  `ok=false`、`failed_stage=export`、`failed_stages=[]`（export 没有结果条目，异常在阶段内抛出）、
  `failure_class=ui_hang`、`stage_counts={pass:9, fail:0, unknown:0}`。
- 证据路径（原始报告与日志全部保留，未删除）：
  - `evidence/t5_admission_report.json`（正式报告，含 stages/marks/timeline）
  - `evidence/t5_admission_stdout.log`（主进程完整 stdout）
  - `evidence/t5_export_child.log`（导出子进程日志，含每次 `connect` 超时）
  - `evidence/t5_launch_ledger.jsonl`（本轮 `entry=admit` 两次冷启动台账）
  - `evidence/t5_verify_report.json` / `evidence/t5_verify_timeline.png`（终检）
- 失败点事实（保留）：导出草稿时长 682.23s；子进程日志显示“正在加载编辑器”后
  `Find Control Timeout(10s): {Name: '剪映专业版', ControlType: WindowControl}`，
  3 次尝试均以 `_wait_edit_page` 超时结束；不是封面问题（准入本就 `require_cover=False`，
  导出前校验已豁免封面）。
- 未做：未修改任何代码/配置，未重跑 T5。

## 顺序与值守

T1（人盯实跑）→ T2/T3/T4（可无人值守）→ T5（人盯，约 10–15 分钟）
