# 项目状态总账（trae-jianying-skill）

> 用途：作为项目状态的唯一总账。后续新任务的状态以本文件为准，其它文档（`specs/*/spec.md`、
> `docs/stability-risks.md`）只描述各自范围，不替代本文件。
>
> 对账基线：HEAD `2ed4d92`（2026-11-15 05:09:29 +0800）。
> 对账时间：2026-11-15。只读核对 `git log` / `git status` / `specs/` / `tests/` / 仓库内证据文件；
> 未改代码、未跑导出、未启停剪映与 watcher。
>
> 每条按「结论 / 证据 / 尚未验证」三段写。没验证的不写成已验证。

---

## 0. 先读：所有「已落地」都还没进入 HEAD

**结论**：状态里说的「已落地」，准确含义是「工作区已实现」；HEAD 之后没有任何提交。

**证据**（`git status`）：

- 未跟踪（5 个）：`pipeline/manual_clean.py`、`pipeline/manual_guard.py`、
  `pipeline/manual_archive.py`、`pipeline/manual_verify.py`、`tests/test_manual_clean_gates.py`
- 已修改未提交（14 个）：`lib/failure_class.py`、`lib/jy_page_detect.py`、`pipeline/jianying_ui.py`、
  `pipeline/stream_manifest.py`、`pipeline/verify_draft.py`、
  `pipeline_ui/admission_test.py`、`pipeline_ui/jianying_ui.py`、`pipeline_ui/ui_calibration.py`、
  `pipeline_ui/ui_core.py`、`pipeline_ui/ui_extract_audio.py`、`pipeline_ui/ui_recognize_translate.py`、
  `pipeline_ui/ui_track_ops.py`、`pipeline_ui/ui_vocal_separate.py`、`tests/test_ui_pipeline_gates.py`
- 规模：14 modified / +2273 −133；`git log` 最新提交仍是 `2ed4d92`

**尚未验证**：这批改动是否可一次性通过全量 `pytest tests/`（本轮没跑测试）。

---

## 1. 已落地并有验证

### 1.1 手工三入口 `clean` / `archive` / `verify --manual`

**结论**：手工侧独立模块，默认路径零改动；四道闸（AST 隔离 / import 图 / 变更面 / 行为对照）通过。

**证据**：

- `pipeline/manual_clean.py`、`pipeline/manual_guard.py`、`pipeline/manual_archive.py`、`pipeline/manual_verify.py`
- `pipeline/stream_manifest.py`：`_parse_manual_args()`、`--manual` 分发；无 `--manual` 时拒绝手工语义
- `tests/test_manual_clean_gates.py`：四个闸门用例 + 锁兜底 6 例 + clean/archive/verify 功能用例

**尚未验证**：四道闸的通过结论来自本轮之前的一次运行，本次对账未重跑；且全部为未提交状态（见 §0）。

### 1.2 `export_lock` 手工侧兜底

**结论**：0 字节坏锁与 PID 复用不再永久卡死；共享库 `lib/export_lock.py` 一个字节未改。

**证据**：

- `pipeline/manual_guard.py`：文件头注明「不改共享锁」，`acquire_export_lock()` / `release_export_lock()` 只调用 `export_lock.acquire/release`
- `git status` 中不存在 `lib/export_lock.py` 的改动

**尚未验证**：0 字节锁、PID 复用两个现场故障没有留对应的运行日志/截图，现有证据是代码与单测用例；
`tests/test_manual_clean_gates.py` 的锁兜底 6 例本轮未重跑。

### 1.3 `verify` scope 拆分

**结论**：`scope` 三值 `drama` / `staging` / `both`，默认 `drama`；本剧 rc 不再被全局 staging 残留拖红。

**证据**：`pipeline/manual_verify.py`：`SCOPES` 定义、`verify_staging()`、`verify(scope=...)` 的合并口径
（`both` 取 `max(本剧 rc, 全局 rc)`）。

**尚未验证**：只做了代码层确认，本轮未跑 `--scope drama/staging/both` 的实跑对照。

### 1.4 UI 准入六项

**结论**：最大化断言、主轨选中重写、音频面板滚轮、时间线到顶、`[1/6]` 字幕轨不足硬失败、校准原子写，均已实现。

**证据**：

- 最大化断言：`pipeline_ui/ui_core.py`（`require_maximized`，注释记录 `SW_RESTORE` 取消最大化的根因）、
  `pipeline_ui/ui_extract_audio.py`（分离音频前置断言）、`pipeline_ui/jianying_ui.py`（`IsZoomed` 判定）
- 主轨选中：`pipeline_ui/ui_core.py` `select_main_track()`（Home + Ctrl+A，判据 = 播放器时间码 00:00:00 +
  主轨白描边段数）
- 音频面板滚轮：`pipeline_ui/ui_vocal_separate.py`（`mouse_event` `MOUSEEVENTF_WHEEL`）
- 时间线到顶：`pipeline_ui/ui_track_ops.py` `scroll_timeline_to_top()`（拖 thumb 优先、pitch/thumb 双校验、
  签名缓存持久化、缓存不匹配退回完整两轮确认）
- `[1/6]` 字幕轨不足：`pipeline_ui/ui_extract_audio.py`（`len(subs) != 2` 计入 `bad` → 中止）
- 校准原子写：`pipeline_ui/ui_calibration.py` `_atomic_dump()`（临时文件 → fsync → `os.replace`）

**尚未验证**：这些行为的实跑覆盖只有 §1.6 那一次 10–20 集准入；最大化断言与 `[1/6]` 硬失败没有独立的
失败用例实跑记录（单测见 `tests/test_ui_pipeline_gates.py`，本轮未跑）。

### 1.5 零时长判据改结构口径

**结论**：同起点堆叠 ≥3 段直接阻断；零时长段达到 3 段或占该轨 >1% 才阻断，否则 warn；按中/英轨分别统计。

**证据**：`pipeline/verify_draft.py`（`_ZERO_DUR_BLOCK_COUNT = 3`、`_ZERO_DUR_BLOCK_RATIO = 0.01`、
按轨 `kind` 统计、`zero_dur_warn` 与 `zero_dur_stats` 分列）；`tests/test_ui_pipeline_gates.py` 有单条/两条
零时长仍判 warn 的用例。

**尚未验证**：正面回归（阈值边界：3 段但 <1%、>1% 但 <3 段）未跑；状态里也把它列在「待观察」。
`stacked` 判据本轮只读了代码，未见命中样例。

### 1.6 `ui_pipeline` 10–20 集实跑

**结论**：11 集成片，时长 687.33s，成品 698,787,829 B 且带音轨；封面缺失按用户决定豁免记通。

**证据**：`pipeline_ui/_evidence_20261115/admission_record_ui_pipeline_ac_10_20.json`
（含分段耗时、phase2 退出码 1 = 仅封面校验、`open_items` 记录封面待补）；
对账时复核成品仍在：`workdir_r10_20/ui_pipeline_ac_10_20.mp4`，698,787,829 B，2026-11-15 20:26:49。

**尚未验证**：封面未生成（记录里是待补项，不是已完成）；同一路径只跑过一次，耗时稳定性未评估。

### 1.7 `__jybuild__` 39 个残留构建目录清理

**结论**：清理 39 个目录、31.19MB。

**证据**：清单 `pipeline/_work/jybuild_residual_20261115.txt`（头部标注「条数: 39 总大小: 31.19 MB」，
逐条列目录名/时间/文件数/体积）；对账时复核 `C:\Users\唐唐\AppData\Local\JianyingPro\User Data\Projects\__jybuild__`
当前子目录数为 0。

**尚未验证**：删除动作没有逐条删除日志，只有删除前清单 + 当前目录为空这两点；清单之外是否有新增残留未核。

---

### 1.8 路径三根统一（工作区已实现）

**结论**：路径已收口到 `pipeline/export_config.py` 单一真源。`resolve_paths(mode)` 返回 `{paths, sources, issues}`，`validate_config(mode)` 额外给 `ok`，业务代码与 Console 都只读这一层。

**契约**：

- `series_root`（唯一素材根）、`export_dir`（导出根）、`deps_root`（依赖根）**业务根 fail-closed**：环境变量 > `presets.json` > 系统派生根探测 > `ConfigError`；三者都没有默认值，`D:\短剧` / `D:\成片导出` / `D:\AI_Deps` 一类兜底已全部删除。
- `watch.roots` 降级为 watcher 的**额外扫描根**：为空只扫 `series_root`；非空时每项须存在、是目录、且含 `*/cover_job`。有效扫描根 = `[series_root] + watch.roots`（`get_watch_roots()`）。
- `archive_root` 留空 = 禁用归档（`JY_ARCHIVE_ROOT` > 顶层 `archive_root` > 兼容 `watch.archive_root`）。
- 旧配置迁移工具：`python pipeline/migrate_config.py --dry-run|--write`（写回前备份 `presets.json.bak-<时间戳>`）；Console 自检里有 `config.migrate` 项。

---

## 2. 已否决

| 方向 | 结论 | 证据 |
|---|---|---|
| 改 watcher 去写清单 | 否决 | `pipeline/manual_guard.py` 头部说明「不改共享锁/不依赖 watcher 产物」；手工侧独立判定层 |
| 直接改共享库 `lib/export_lock.py` | 否决 | `lib/export_lock.py` 无改动；兜底落在 `manual_guard.py` |
| 类型锚点重排 | 不做 | 仓库内无对应实现痕迹，也无 spec 载体（口头决议，无文档） |

---

## 3. 进行中

### 3.1 报告 `ok=False` 假失败修复（五段历史阶段不写 `ok`）

**结论**：代码已改，验证未完成。

**证据**：`pipeline_ui/admission_test.py` `_dump_report()` 已改为阶段三态：`ok is None` 记为 unknown 单列，
只统计 `STAGES` 内的真实阶段（`start`/`audio`/`cleanup` 等打点键不再把整轮拖成 unknown）。
**与状态输入的差异**：状态写「进行中」，代码层面看主体已实现（见 §5 I3）。

**尚未验证**：未用历史 `ok=False` 报告复现验证；未提交。

### 3.2 `specs/ui-admission-launch` 判据与阶段说明补齐

**结论**：文档落后于实现，未补齐。

**证据**：`specs/ui-admission-launch/spec.md` 状态仍是「待批准」；`tasks.md` 的 T1–T5 无完成标记。
而 T1/T2/T3/T4 对应代码已存在（`ui_nav` 走 `launch_via_manager`、`select_main_track`、三态报告、
`presets.json` 的 `admission.open.ready_timeout`）。

**尚未验证**：T5 端到端（`admission_test.py --stop-after export`）在本轮之外的最近一次结果没有落盘记录。

### 3.3 新剧走手工入口的前置条件与执行序列

**结论**：尚未成文。

**证据**：`docs/` 目前只有 `stability-risks.md`、`watcher-vs-script-parity.md`，无此文档。

**尚未验证**：无产物可核。

---

## 4. 待用户决定（未定，不执行）

| 事项 | 现状 | 仓库内证据 |
|---|---|---|
| 4 条脏清单记录（`剧A串流`、`剧A串流_b`、`A_b`、`迷你验证剧B_第01集串流`）建议删记录、不修指向 | 待定 | 无。job 台账在 `<素材根>`（`series_root`）数据目录，不在仓库内，本次未核 |
| 3 部剧约 53GB：删 or 先归档到 `<归档根>`（`archive_root`） | 待定 | 无。同上 |
| 半截行识别何时插队 | 已排期、未开始 | 无载体：`specs/` 无对应 spec/tasks（见 §5 I6） |
| 零时长正面回归 + 滚动条 thumb 高度分布 + 导出耗时稳定性，三项合批观察 | 待启动 | `tests/` 无对应用例 |

---

## 5. 与仓库现状的对账不一致

| ID | 不一致 | 事实 | 建议（不擅自改） |
|---|---|---|---|
| I1 | 「已落地」被读成已交付 | HEAD 之后所有改动都未提交（§0） | 提交前一律按「工作区已实现」表述；分批提交时只带对应文件 |
| I2 | `specs/ui-admission-launch/spec.md` 写「待批准」，实现已落地 | 状态行与代码矛盾 | 补状态与阶段说明（属进行中 3.2） |
| I3 | 状态写「报告假失败修复进行中」，代码主体已完成 | `_dump_report` 三态已实现 | 改记为「已改未验」，补一次历史报告复现验证 |
| I4 | `tests/test_manual_clean_gates.py` 是脚本式用例（模块尾部 `sys.exit`），文件名却匹配 pytest 收集规则 | `pyproject.toml` 只有 `testpaths = ["tests"]`，未排除该文件 | 需确认 `pytest tests/` 是否在收集期被 `SystemExit` 打断（未实测）；若会，按仓库既有「脚本式用例改子进程执行」约定包一层 |
| I5 | `presets.json` 已加 `admission.open.ready_timeout=120`，`presets.example.json` 未同步 | 示例文件搜不到该键；代码默认值 120，新机器不影响行为 | 补示例键，避免「示例即配置」的误读 |
| I6 | 「半截行已排期」在仓库里没有落点 | `specs/` 无对应目录 | 要么建 spec，要么在本文 §4 保留为未排期 |
| I7 | 4 条脏清单 / 53GB 两部剧的处置写进了状态，但仓库内无证据 | 数据在 `<素材根>`（`series_root`），本次未核 | 保持「待定」，不得写成已核对 |
| I8 | 工作区残留现场产物未处置 | 未跟踪：`pipeline_ui.progress.json`（`ui_pipeline_ac`，`status=failed`，`attempt=3`）、`workdir_r10_20/`、`pipeline_ui/_evidence_20261115/`、`pipeline_ui/_draft_snapshots/`、`pipeline_ui/verify_timeline.png`、`pipeline_ui/ui_pipeline_ac.mp4.verify.log` | 提交时明确哪些入库（证据）、哪些不入库（运行残留） |

---

## 6. 未验证清单（汇总）

- 全量 `pytest tests/` 是否能通过当前工作区（本轮未跑）。
- 四道闸 / 锁兜底 6 例的最近一次运行结果（本轮未重跑）。
- 最大化断言、`[1/6]` 字幕轨不足硬失败、时间线到顶失败路径，均无独立实跑失败证据。
- 零时长新判据的正面回归（阈值边界两侧）。
- 滚动条 thumb 高度分布、导出耗时稳定性（均未采样）。
- 报告 `ok=False` 假失败修复未用历史报告复现。
- T5 端到端准入的最近一次结果未落盘。
- `__jybuild__` 清理只有「删除前清单 + 当前为空」，无逐条删除日志。
- §4 全部待定项的现场数据（仓库外）。

---

## 7. 维护约定

1. 新条目必须写「结论 / 证据 / 尚未验证」；证据指到 `文件:行` 或产物路径，不写「应该」「大概」。
2. 未提交的改动只能写「工作区已实现」，不得写「已交付」。
3. 仓库外数据（`series_root` 素材根、`archive_root` 归档根、剪映草稿根等）一律标注「未在仓库内复核」。
4. 状态变化时改本文，不同步改写 `specs/*/spec.md` 的验收口径；spec 只补自身状态行。
5. 本文不记录排查过程，只记录可复核的结论与证据。
