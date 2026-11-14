# Spec：无字幕策略「跑完 + 清单 + 导出前拦」（v2）

范围：只实现无字幕策略；不跑这部剧、不启 watcher、不动 `<剧集根>` 数据。

v2 相对 v1 补齐的决策点：`job_dir` 显式传递（不再只靠向上查找）、内存兜底拦截（job.json 缺失时也不导出）、通知集中到单一模块且进程内去重、`series_runner` 被拦后的状态与通知、`robust_auto_export.py` 直调入口也设闸门、watcher 日志去重、导出闸门与 `.export_state.json` 的一致性。

## 1. 规则映射

| 规则 | 实现 |
| --- | --- |
| 1 记录（三档 ASR 全失败 / 最终零字幕 → 记集号；升序去重；不中断整轮） | `pipeline/pipeline.py` 逐集统计 + `job_manifest.sync_subtitle_missing` 落 `job.json` |
| 2 清除（重剪且**字幕非空**才移除；只重跑不算） | `pipeline/pipeline.py` 本集字幕条数判定 → `repaired_eps`；`sync` 用 `(existing ∪ failed) − repaired` |
| 3 导出前硬拦（所有导出入口） | `job_manifest.require_export_allowed` + 5 个入口：`run_pipeline(do_export)`、`cover_watch.run_export`、`batch_runner --auto-export`、`series_runner._run_export`、`robust_auto_export.py` 直调 |
| 4 收尾通知（整轮结束清单非空只弹一条，列集号） | `lib/subtitle_guard.notify(job_dir)`（进程内按 job_dir 去重），由 `batch_runner` 两条分支与 `series_runner` 被拦时调用 |

## 2. 数据契约

`job.json` → `derived.subtitle_missing`：`array<int>`，正整数集号，**严格升序且去重**，缺省 `[]`。

写入条件（同时满足才记该集）：

- 该集实际执行了 `run_asr_chain`；
- 最终落到该集的字幕条数为 0（ASR 三档全空，或 ASR 有结果但裁 cut 后零条）。

移除条件：

- 该集再次执行 ASR，且**本集最终字幕条数 > 0**；
- 仅“又跑了一遍但字幕仍空”不满足（会继续作为 failed 记录，保持清单）。

未进入 ASR 的集（找不到视频等）既不算 failed 也不算 repaired，不改清单。

持久化/兼容：

- `build_job()` 预置 `derived.subtitle_missing = []`；`migrate_job()` 自动为旧 job 补齐（无需迁移脚本）。
- `validate_job()` 校验：`derived` 必须是对象；`subtitle_missing` 必须是非 bool 正整数数组且严格升序去重。非法形态 `update_job` 直接 ValueError，不落盘。
- job.json 缺失时：`subtitle_missing()` 返回 `[]` 并在 stderr 告警；`sync_subtitle_missing()` 不创建 job.json（创建仍只由 `create_job` 负责），返回内存排序结果并告警。

`ExportBlocked(missing)`：`missing` 为升序去重集号；`str(e)` 固定 `缺字幕未修复：第1、3、8集`（`format_episodes` 支持空列表 → `（无）`）。

## 3. 文件级改动

### 3.1 `lib/job_manifest.py`

1. `build_job()` 增加 `"derived": {"subtitle_missing": []}`。
2. `validate_job()` 在 `workflow` 校验后增加 `derived` 形态校验（见 §2）。
3. 新增模块级 API（放在 `update_job` 之后）：

```python
class ExportBlocked(RuntimeError): ...
def format_episodes(eps) -> str
def get_subtitle_missing(job: dict) -> list[int]        # 过滤非正整数，sorted(set)
def subtitle_missing(job_dir: str) -> list[int]
def sync_subtitle_missing(job_dir: str, *, failed_eps=(), repaired_eps=()) -> list[int]
def require_export_allowed(job_dir: str) -> list[int]   # 非空 raise
def find_job_dir(start_path: str, *, max_depth: int = 8) -> Optional[str]
```

`sync_subtitle_missing` 细节：

- `new = sorted((set(current) | {正 int failed}) - {正 int repaired})`；
- 仅当 `new != current` 时调用 `update_job(job_dir, {"derived": {"subtitle_missing": new}}, event="subtitle_missing", detail=...)`（保持 revision/history 单写入口）；
- detail：`新增缺字幕：第3、8集；已修复：第5集`（只拼非空段）；
- 无 `job.json`：stderr 告警 + 返回 `new`，不写盘；
- 写盘异常向上抛，由调用方兜底。

`find_job_dir`：文件→取目录；自身含 `job.json` 直接返回；否则逐级向上查 `<ancestor>/cover_job/job.json`，最多 `max_depth` 层；找不到 None；不抛异常。

### 3.2 `lib/export_state.py`

新增：

```python
def clear_block(job_dir, *, failure_class="subtitle_missing", now=None) -> dict | None
```

- 仅 `status=="failed"` 且 `failure_class` 精确匹配时生效；
- 置 `pending`，清 `last_error`，`failure_class="unknown"`，`next_retry_at=0`，`time` 心跳，`progress=None`；
- `attempts` / `consecutive_failures` 保留（可追溯）；
- 走 `_save_if_changed`，幂等；真源不存在返回 `_read_raw`（None），不创建。

### 3.3 `lib/subtitle_guard.py`（新增，唯一通知实现）

```python
notify(job_dir) -> bool   # 清单非空 → 生产 toast 一条；进程内按 abspath(job_dir) 去重，重复调用不重弹
```

- 标题：`⚠️ 缺字幕需人工处理`
- 正文：`缺字幕未修复：第…集（共 N 集）；请重剪这些集，字幕非空后才会移出清单；未修复前不会导出。`
- 清单空 / job.json 不存在 → False，不弹。
- 导入方式：`sys.path` 注入 `<repo>/scripts`，`from utils.notifier import toast`；测试可替换 `subtitle_guard.toast`。
- 不在 watcher 内调用（watcher 无“整轮”概念，只写日志，避免每轮刷屏）。

### 3.4 `pipeline/pipeline.py`

1. 顶部 `import job_manifest as jm`。
2. `run_pipeline(...)` 新增可选关键字 `job_dir=None`（默认 None，保持旧调用兼容）。解析顺序：
   - 入参 `job_dir`；
   - 否则 `jm.find_job_dir(video_paths[0])`；
   - 在「同步前」和「导出前」各解析一次（允许 job_dir 在运行中途才出现）。
3. 逐集循环：

```python
ep_num = int(re.search(r"第\s*(\d+)\s*集", u["name"]).group(1)) if 匹配 else u.get("idx")
before = len(cn_all)
cn_subs = run_asr_chain(v)
if not cn_subs:
    failed_eps.add(ep_num); continue
# ...按 cut 过滤并 append 后...
if len(cn_all) == before:
    failed_eps.add(ep_num)
else:
    repaired_eps.add(ep_num)
```

   - 只有真正执行 `run_asr_chain` 的集参与统计（`skip_local_ai`/跳过 ASR 的集不参与）；
   - 循环内不发通知、不写盘（避免逐集 IO 与刷屏）。
4. 循环结束后、翻译修复轮前：`jm.sync_subtitle_missing(...)` 一次；异常 `print` 后继续整轮。
5. 模块级纯函数（供测试）：

```python
def _classify_subtitle_outcome(ep_num, *, asr_empty: bool, appended: int) -> str | None
```

6. Step 11 导出前：

```python
if do_export:
    missing = 文件清单 ∪ 本轮 failed_eps（内存兜底）
    if missing: 打印 ExportBlocked 消息 + 修复说明; do_export = False
    else: jm.require_export_allowed(job_dir)（job_dir 为空时跳过）
```

   - 内存兜底保证：job.json 尚未生成、但本轮有零字幕集时也不会导出；
   - 被拦后本轮其余收尾（记录视频、写 job）照常，不抛异常。

### 3.5 `lib/cover_watch.py`

1. `run_export(job_dir, plan, ...)` **第一条语句**：

```python
try:
    jm.require_export_allowed(job_dir)
except jm.ExportBlocked as e:
    打印完整消息 + 修复说明
    if dry_run: return False          # 不写状态
    es.fail(job_dir, str(e), retryable=False,
            failure_class="subtitle_missing", phase="export")
    return False
```

   - 必须先于 `es.gate`、`begin`、`_preclean_bundles`、`Popen`，`--phase export` 也绕不过；
   - 已是 `failed/subtitle_missing` 时日志只追加一行短信息（不重复打全量集号），避免每轮刷日志；
   - watcher 不因此弹通知。
2. `note_edit_result(job_dir, ok=True, ...)`：成功且 `jm.subtitle_missing(job_dir)` 为空 → `es.clear_block(job_dir, failure_class="subtitle_missing")`；非空保持 failed。清空后才可能进入下一轮导出。

### 3.6 `pipeline/batch_runner.py`

1. 新增 `job_dir = os.path.join(series_dir, "cover_job")`（两分支共用）。
2. 两处 `run_pipeline` 调用（串流、逐集）都显式传 `job_dir=job_dir`。
3. 串流分支：`_toast("🎬 串流草稿已就绪")` 后 → `subtitle_guard.notify(job_dir)`；`if args.auto_export` 启动后台导出前：
   `try jm.require_export_allowed(job_dir)`，被拦 → 打印完整集号 + 修复说明，跳过导出（不再弹第二条通知）。
4. 逐集分支：批量完成 toast 后 → `subtitle_guard.notify(job_dir)` 一次。
5. 单集失败不弹 toast（保持现状）。

### 3.7 `pipeline/series_runner.py`

1. 路径注入补 `<repo>/lib`；导入 `job_manifest as jm`、`subtitle_guard`、`export_config as pc`。
2. `_job_dir_for(target, is_draft)`：
   - 剧集目录 → `<target>/cover_job`；
   - 草稿名 → 遍历 `pc.get_watch_config().get("roots")`，匹配 `<root>/<剧名>/cover_job`（草稿名以剧名开头）；唯一命中返回，多命中/无命中打印候选并返回 None。
3. `_run_export(draft_name, timeout=1800, *, cold_start=False, job_dir=None)` **第一条**：
   - `job_dir = job_dir or _job_dir_for(...)`；为空则不拦（保持旧行为）；
   - 被拦 → 打印完整集号 + 修复说明，`return False`；不建目录、不 `_preclean_bundles`、不 `Popen`。
4. 调用点补 `job_dir`；被拦时：
   - 该剧状态写 `blocked`（不进入失败重试计数）；
   - `subtitle_guard.notify(job_dir)` 一次（整轮收尾，不按集）；
   - 继续下一部，最终 `main()` 返回 1。

### 3.8 `.trae/skills/jianying-editor/examples/robust_auto_export.py`

1. 解析参数后、启动剪映前，调用本地闸门：

```python
def subtitle_gate(draft_name, roots=None) -> list[int]
```

   - `roots` 取 `pipeline/presets.json` 的 `watch.roots`（复用 `export_config`，找不到则 None）；
   - 用与 `series_runner._job_dir_for` 相同的匹配规则找 `cover_job`；
   - 清单非空 → 打印完整集号 + 修复说明，`sys.exit(2)`；找不到 job_dir → 照旧运行（不误伤独立使用）。
2. 只加这一处闸门，不改导出流程。

### 3.9 `.trae/skills/drama-cover-builder/references/handoff-contract.md`

1. §2 字段表 `workflow.*` 与 `publish.status` 之间新增 `derived.subtitle_missing` 行：array，升序去重正整数；写入/移除条件见 §2；缺省 `[]`。
2. 新增小节「导出前硬拦」：
   - 五个入口统一调 `require_export_allowed`；`robust_auto_export` 直调在 `sys.exit(2)`；
   - 被拦写 `.export_state.json`：`status=failed / failure_class=subtitle_missing / phase=export`，不可重试；
   - `--phase export` 不放行；
   - `note_edit_result(ok=True)` 且清单空 → `clear_block` 复位 `pending`；
   - 整轮结束由 `subtitle_guard.notify` 发一条通知。
3. §8 阶段产物表 pipeline 行补 `derived.subtitle_missing`。

## 4. 测试（沿用独立脚本 + assert；不依赖 pytest）

### 4.1 `tests/test_subtitle_missing.py`
1. 记录：`sync(failed_eps={8,3,3})` → `[3,8]`；revision 递增、history 有事件；子进程读回 `[3,8]`。
2. 清除：`[3,8]` + `repaired_eps={8}` → `[3]`。
3. **反例**：`[3]` + 只重跑（`failed_eps={3}, repaired_eps=空`）→ 仍 `[3]`。
4. 校验：`[3,3]` / `[8,3]` / `[0]` / `["3"]` / `[True]` / 非数组 → ValueError。
5. 兼容：`build_job` 有 `[]`；无 `derived` 的旧 job 经 `migrate_job` 得 `[]`；job.json 缺失时 `subtitle_missing=[]`、`sync` 不创建文件。
6. `_classify_subtitle_outcome`：`asr_empty=True`→failed；`appended=0`→failed；`appended=2`→repaired。

### 4.2 `tests/test_export_gate.py`
1. `require_export_allowed` 抛 `ExportBlocked`，message 含全部集号且升序。
2. `cover_watch.run_export(job_dir, None)`：返回 False，`cw.subprocess.Popen` 未被调用；`.export_state.json` = `failed/subtitle_missing/export`；`consecutive_failures` 未增；`export_state.gate(..., phase_override="export")` 仍给出 failed 原因。
3. `dry_run=True` 被拦：返回 False 且 `.export_state.json` 未被创建/改写。
4. `series_runner._run_export("draft", job_dir=...)`：False，Popen 未调用，导出目录未被创建。
5. `clear_block`：failed+匹配 → pending、last_error 清空、attempts 保留；不匹配 → 原样；清空清单后 `require_export_allowed` 放行。
6. `robust_auto_export.subtitle_gate(draft, roots=[temp_root])`：草稿对应 job 清单非空 → 返回集号；清单空/找不到 job → `[]`。

### 4.3 `tests/test_subtitle_missing_notify.py`
1. 清单 `[1,3,8]` → `notify` 返回 True，`subtitle_guard.toast` 调用 **1 次**，标题/正文含「第1、3、8集」。
2. 重复调用 `notify` 同一 job_dir → 仍只有 1 次（进程内去重）。
3. 清单空 / job.json 不存在 → 0 次、返回 False。
4. 源码结构守卫：`batch_runner.py` 中 `notify(job_dir)` 恰好 2 处；`series_runner.py` 被拦分支 1 处；且 `require_export_allowed` 出现在 `auto_export` 启动之前。

### 4.4 回归
`Get-ChildItem tests\*.py | ForEach-Object { python $_.FullName }` 全部通过（含 smoke、notifier 隔离、gate_cut、edit_pause_after、watcher_heartbeat）。测试期不得出现真实桌面通知。

## 5. 验收

```powershell
cd "<repo>"
python tests\test_subtitle_missing.py
python tests\test_export_gate.py
python tests\test_subtitle_missing_notify.py
Get-ChildItem tests\*.py | ForEach-Object { python $_.FullName }
```

通过后一次性报告：改动文件、测试结果、缺字幕从记录到拦截的实际行为样例。

## 6. 明确不做

- 不自动重试缺字幕集；不改 ASR 候选链 / GPU / watcher 心跳与指纹。
- 不改 `--phase export` 语义，不给 watcher 加通知。
- 不重构 batch_runner / series_runner / robust_auto_export 的其他流程。
- 不写 `<剧集根>`，不启动 watcher，不导出。
