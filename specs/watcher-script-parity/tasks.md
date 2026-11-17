# tasks — series_runner fail-closed 补丁

> 状态：全部完成（用户确认 `full` 复用重测结束后实施），整仓 319 passed / 1 warning。

前置：[spec.md](spec.md)

## T1 `_output_exists` 加 stale 判断
- [x] `pipeline/series_runner.py` 引入 `pipeline as jp`。
- [x] `_output_exists` 命中 `jp.export_is_stale(<草稿目录>)` → 返回 False。
- [x] 无 stale 时保持 `isfile && size>1MB` 原行为。

## T2 导出成功收尾（顺序固定）
- [x] 新增 `_finalize_export(draft, out_mp4, job_dir)`。
- [x] 第一步 `jm.record_video(..., profile=<版本>)`；失败即返回 False，保留 stale、不写 E2。
- [x] 第二步 `jp.clear_export_stale(...)`。
- [x] 第三步 `_record_export_freshness(...)`（sidecar + E2 版本槽，失败只告警）。
- [x] `main` 在 `_run_export` 返回 True 后调用；False → `export_failed`。

## T3 台账多版本
- [x] `_declared_profiles`：`workflow.preset_profile` + 已有 `outputs` 键。
- [x] `_version_of_draft`：`<基础>_b` → `b`，其余 → `a`；各版本分别登记。

## T4 `_run_export` 外层全局导出锁
- [x] 原实现改名 `_run_export_locked`；`_run_export` 为持锁入口。
- [x] watcher 已持锁（同 pid）→ `_own_export_lock()` 复用，不重复加锁。
- [x] 取锁失败：打印持有者，返回 False，不进入导出实现。
- [x] `try/finally` 只在本次取到锁时释放。

## T5 测试
- [x] 新增 `tests/test_series_runner_fail_closed.py`（8 条，见 spec「测试」）。
- [x] 不启停剪映、不跑真实导出；全部临时目录 + monkeypatch。

## T6 回归与提交
- [x] `python -m pytest tests -q` → **319 passed / 1 warning**（基线 311 + 8）。
- [x] 提交只包含本补丁相关文件（`pipeline/series_runner.py`、新测试、对照文档与 spec）。
- [x] 提交后不重启 watcher。

## 完成定义
- 带 stale 的成品不再被 `_output_exists` 当成已完成。
- 脚本导出成功后：台账已登记（多版本各记各的）→ stale 已清 → E2 sidecar 已写。
- 台账失败时不产生“成品在、台账缺”的中间态。
- 脚本导出全程持全局锁，watcher 复用时不死锁、不误释放；失败路径也释放。
- 整仓测试通过，工作区无其他会话文件的改动被带入。
