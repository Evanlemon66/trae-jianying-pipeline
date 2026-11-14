# Spec：四条修复（1 / 3 / 4 / 5）

## 结论

严格按原始需求实现四条，**不额外扩大行为**。每条独立 commit，不 push。不碰 `presets.json`、`dist/`。

---

## ① 健康检查：补「进程活着但卡死」

### watcher 侧（`lib/cover_watch.py`）

- 新增 `_WORK_DIR = <repo>/pipeline/_work`、`HEARTBEAT_NAME = "watcher_heartbeat.json"`、`HEARTBEAT_INTERVAL_S = 30`。
- 新增 `_WatcherHeartbeat`：
  - `start()`：同步写一次心跳 → 起 daemon 线程，每 30s 写一次。
  - 主循环每轮调用 `update(stage=..., job=..., phase=..., round=...)`，立即刷新。
  - 写入复用 `state_io.atomic_write(_WORK_DIR, HEARTBEAT_NAME, payload)`；payload 含 `pid` / `updated_ts`（epoch）/ `started_ts` / `stage` / `job` / `phase` / `round`；`state_io` 自动补 `updated_at` / `writer` / `version`。
  - 写失败只打印告警，不影响主流程。
- `cmd_watch`：`start()` → `try: while True: ... finally: stop()`；执行 chain 前把 `stage` 置为 `edit`/`export`、`job` 置为任务目录，`finally` 回 `idle`。
- 说明：主循环每轮刷新满足「至少每 30 秒一次」；后台线程保证长导出/长剪辑期间心跳仍新鲜，避免 supervisor 误杀正常长任务。

### supervisor 侧（`pipeline/watcher_supervisor.ps1` + 新 `pipeline/watcher_stall.ps1`）

- 新增 `pipeline/watcher_stall.ps1`（纯函数，便于测试，不执行任何主流程）：
  - `Get-HeartbeatTimestamp`：优先 `updated_ts`，回退 `updated_at`（ISO）。
  - `Get-WatchStallSeconds -RepoRoot`：读 `pipeline/presets.json` 的 `watch.stall_heartbeat_s`；缺失/异常回落 900。
  - `Get-WatcherStallDecision`：返回 `noheartbeat | unparsable | pidmismatch | startup | healthy | stalled`。
- `watcher_supervisor.ps1`：
  - 顶部 dot-source `watcher_stall.ps1`（失败只记日志，不阻断）。
  - `$heartbeatFile = <repo>/pipeline/_work/watcher_heartbeat.json`。
  - 「已在运行」分支读取心跳：
    - 心跳不存在 / 不可解析 / pid 与运行进程不一致 / 刚启动（`started_ts` 距今 < 180s）→ **不判卡死**，各记一行日志后 `return 0`。
    - 心跳存在且 `now - updated_ts > stall_heartbeat_s` → `stalled`：
      - `Write-Log "判定卡死 → 重启: pid=... 心跳停滞超过 ...s"`
      - `taskkill /F /T /PID` 杀掉所有 cover_watch 进程
      - 轮询最多 10s 等进程消失；仍未消失 → 记日志 `return 1`（不叠加启动）
      - 已消失 → 删除旧心跳文件 → 落入原有启动块重新拉起，并记 `判定卡死 → 重启: 旧进程已终止`
- `pipeline/presets.example.json` 的 `watch` 段新增 `"stall_heartbeat_s": 900`（`presets.json` 不动）。

### 测试

- `tests/test_watcher_heartbeat.py`：start 后文件存在且含 `pid/updated_ts/started_ts/stage/job`；`update` 后内容变化；`stop` 后线程退出。
- `tests/test_watcher_stall.ps1`：dot-source 纯函数，构造 6 种心跳，断言 `stalled / healthy / noheartbeat / startup / pidmismatch / unparsable`。
- PowerShell 语法解析：`[Parser]::ParseFile` 0 错误。

---

## ③ `select` 在 `covers_ready` 之后可重选

文件：`lib/job_manifest.py`

- `select_titles`：
  - 当前 stage 早于 `titles_selected`（`prepared` / `titles_done`）→ 行为不变，写 `stage = "titles_selected"`。
  - 当前 stage 已是 `titles_selected` 或更后（`prompts_ready` / `covers_ready` / `video_ready` ...）→ **不写 stage**，只更新 `titles.selected` / `titles.selected_at` / `titles.selected_by` / `titles.auto_mode` / `cover_map` / `vertical_cover`，并追加：
    ```json
    "derived": {
      "covers_stale": true,
      "covers_stale_at": "<ISO>",
      "reason": "重选发生在 <stage> 之后，04_covers 需重新生成"
    }
    ```
  - history `detail` 明确写「重选（当前 <stage>）：stage 保持不变，04_covers 需重新生成」，不静默。
- `update_job` 的 gate 守卫文案修正：
  - 原文：`titles.gate 不能从 done 回退为 pending（重选请用 select 命令）`
  - 改为：`titles.gate 不能从 done 回退为 pending；如需更换标题请用 select 命令重写 selected（select 不会把 gate 打回 pending）`
  - 守卫本身保留。
- 不新增 `build_job` 的 `derived` 默认字段；旧 job 由 select 的 patch 增补，`validate_job` 不拒绝额外字段。
- **不修改 `check_trigger` / `ingest_job`**（本次不扩大范围）；`derived.covers_stale` 作为显式标记供人或 agent 处理，并在 history 留痕。

### 测试（新增 `tests/test_select_reselect.py`）

1. 从 `prepared` 调 `select_titles`：stage 变 `titles_selected`，无 `derived.covers_stale`。
2. 构造 stage=`covers_ready` 的 job 调 `select_titles`：不抛错、stage 仍 `covers_ready`、`titles.selected` 已更新、`derived.covers_stale is True`、history detail 含「04_covers 需重新生成」。
3. `update_job` 回退 stage 仍抛错；gate 回退文案已修正（不含「重选请用 select 命令」原句）。

---

## ④ 两个锁改原子抢占

文件：`lib/export_lock.py`、`lib/cover_watch.py`

- 抢占：
  ```python
  fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
  ...
  with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(data, f, ...)
  ```
  写失败清理自己创建的空锁后上抛。
- `FileExistsError` → 读已有 pid：
  - pid 已死 → 再读一次确认仍是同一 pid → 删除 → **重试抢占一次**。
  - pid 活着 → 返回 `False`。
- `release()`：
  - 读锁；`pid != os.getpid()` 或读不到 → **不删**。
  - 是自己的锁 → 删除；`OSError` 吞掉。
- `cover_watch` 的 `.watcher.lock` 与 `export_lock` 的 `.export.lock` 语义一致。

### 测试（新增 `tests/test_lock_atomic.py`）

覆盖两组锁：
1. 两个线程用 `threading.Barrier` 同时 acquire → 恰好一个 True。
2. 预写死 pid（mock `_pid_alive=False`）→ acquire 成功接管，锁内 pid 为自己。
3. 预写活的其他 pid（`os.getppid()`）→ release 后文件仍在。
4. 自己 acquire 后 release → 文件不存在。

---

## ⑤ `plan` 不许落盘

文件：`lib/export_state.py`、`handoff-contract.md`

- `gate()`：
  - `phase_override == "export"` 直接放行（不变）。
  - `st = _read_raw(job_dir)`；缺失时用 `_infer_initial(...)` 在内存推断（复用现有优先级逻辑），**不调用 `migrate()`、不备份 `.export_retry.json`、不写盘**。
  - 再 `_effective(st, progress_mtime)` 做 stale 内存推导。
  - 其余状态判定文案不变。
- `load()` / `migrate()` 保持原语义；真正创建统一由 `es.begin()` 负责（`run_export` 已有调用）。
- `handoff-contract.md`：
  - 第 18 行：`进入导出闸门时 cover_watch 自动写` → `真实导出开始时 cover_watch 自动写`。
  - §7.1 第 374 行：改为「**仅当该剧真实导出开始时（`lib/export_state.py` 的 `begin()`）创建**；只做计划/闸门评估（如 `cover_watch plan`）不会创建或迁移该文件。」

### 测试（新增 `tests/test_plan_nowrite.py`）

1. 构造「有草稿、无 `.export_state.json`」的剧。
2. `es.gate(...)` 前后断言文件不存在。
3. `cover_watch.build_plan(...)`（或 `cmd_plan`）前后断言文件不存在。
4. 对照：`es.begin(...)` 后文件存在且 `status=exporting`。

---

## 回归

```powershell
python tests\test_state_io.py
python tests\test_watch_guardrails.py
python tests\test_archive_job.py
python tests\test_edit_state.py
python tests\test_export_state.py
python tests\test_log_rotation.py
python tests\test_vertical_title.py
python tests\test_gate_cut.py
python tests\test_watcher_heartbeat.py
python tests\test_select_reselect.py
python tests\test_lock_atomic.py
python tests\test_plan_nowrite.py
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_watcher_stall.ps1
```

PowerShell 语法解析（`watcher_supervisor.ps1` 与 `watcher_stall.ps1`）必须 0 错误；**不实际执行 supervisor**。

## 提交

1. `feat(watcher): 心跳卡死检测与 supervisor 自动重启`
2. `fix(job): select 支持 covers_ready 后重选并标记封面重做`
3. `fix(lock): 导出锁与 watcher 锁改原子抢占`
4. `fix(export): plan 闸门改纯内存推导不再落盘`

仅按文件 `git add`；不包含 `SKILL.md`、`presets.json`、`dist/`、`pipeline/_work/`。

## 风险

- 心跳线程会持续跳动，长导出不会被 supervisor 误杀；导出卡死仍由 `export_stall_timeout`、剪辑卡死由 `edit_stall_timeout_s` 处理。
- 陈旧锁接管有微小 TOCTOU（读 pid → 确认 → 删除 → O_EXCL 重试），`O_EXCL` 保证不会双持有。
- 重选后 `derived.covers_stale` 只做标记与 history 留痕，watcher 当前不会自动阻断；需要人工/agent 重建封面后重新 ingest。若要自动阻断，需另开一条改动。
