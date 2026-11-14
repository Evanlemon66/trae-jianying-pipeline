# spec — series_runner fail-closed 补丁

> 状态：**已实施**（用户确认 `full` 复用重测结束后落码），整仓 319 passed / 1 warning。
> 对照依据：[docs/watcher-vs-script-parity.md](../../docs/watcher-vs-script-parity.md)

## 背景

`pipeline/series_runner.py` 的导出段只复用了 `robust_auto_export` 子进程，没有复用
`cover_watch.py` 导出前后的调度层保护。对照文档已确认三处最高危差异：

1. `_output_exists` 仅按 `isfile && size>1MB` 判“已导出”，不看 `_export_stale.json`
   （`series_runner.py:104-107`）。草稿重建后旧 mp4 仍在 → 脚本直接跳过导出。
2. 导出成功后不清 `_export_stale.json`、不写 E2 sidecar。脚本产出的成品在 watcher 眼里
   永远“未导出”，会被归档成 `.vN` 再重导。
3. 导出段不取全局 `.export.lock`（`series_runner.py:314-384` 无 `export_lock` 引用），
   可与 watcher / 另一次脚本并发操作剪映。

## 决策（用户已给定）

四处一起落，顺序固定。

### D1 `_output_exists` 加 stale 判断

- 文件：`pipeline/series_runner.py`，函数 `_output_exists`。
- 改动：命中 `_export_stale.json` 时返回 `False`，其余判定（`isfile && size>1MB`）不变。
- 实现：复用 `pipeline.export_is_stale(os.path.join(JY_DRAFT_ROOT, draft_name))`；
  新增 `import pipeline as jp`（`series_runner.py` 当前未导入，无循环依赖）。
- 语义：只影响 `main` 的“成品已存在，跳过导出”分支；带 stale 标记时继续走导出。

### D2 成功收尾：台账 → 清 stale → E2（顺序固定）

- 文件：`pipeline/series_runner.py`，函数 `_finalize_export`（由 `main` 在 `_run_export`
  返回 True 后调用）。
- 顺序与回退：
  1. `jm.record_video(job_dir, ..., profile=<版本>)`：写 `video.outputs[<版本>]`
     （`state=exported` / `size_bytes` / `exported_at`）。**失败即整体失败**：保留
     `_export_stale.json`、不写 E2，返回 False，`main` 记 `export_failed`——
     宁可下次重导，也不把“成品在、台账缺”留给 watcher 判 conflict。
  2. `jp.clear_export_stale(<草稿目录>)`：成功后清标记（内部失败只告警）。
  3. `_record_export_freshness(draft, mp4)`：`freshness.job_dir_for_draft` →
     `version_for_draft` → `freshness.record_export`（写 sidecar + E2 版本槽），
     异常只告警。
- 多版本：版本由“实际导出的草稿名 + job 声明版本”推导（`_declared_profiles` /
  `_version_of_draft`），`<基础>_b` 记到 `outputs.b`，不总是写基础版 a。
- 幂等：重复调用只覆盖同一 sidecar / E2 槽 / outputs 条目。

### D3 `_run_export` 外层加全局 `export_lock`

- 文件：`pipeline/series_runner.py`：`_run_export` 为持锁入口，原实现改名为
  `_run_export_locked`。
- 范围：preclean → Popen → 看门狗等待 → consolidate 全段。
- watcher 复用：watcher 在 `run_export` 已持锁（同进程），`_own_export_lock()` 命中时
  直接复用，不重复加锁、不释放 watcher 的锁。
- 取锁失败：打印持有者 pid/owner，返回 `False`（由 `main` 记 `export_failed`），
  不启动子进程、不清理目录。
- 释放：`finally` 中只在“本函数确实取到锁”时 `release()`。

## 非目标（本次明确不做）

| 项 | 原因 |
|---|---|
| `verify_export.py` 质检 | 改动面大，需要单独评估 |
| `cold_start`、失败分类/退避/暂停、跨轮重拉 | 属于调度层能力，不在本次补丁范围 |
| 改动 `cover_watch.py` / `robust_auto_export.py` | 避免并行改同一文件；watcher 兼容靠 `_own_export_lock` 实现 |

## 测试

新增 `tests/test_series_runner_fail_closed.py`（8 条）：

1. 草稿带 `_export_stale.json` 且成品 >1MB → `_output_exists` 返回 False；删标记后 True。
2. `_finalize_export` 成功：清 stale、`outputs.a` 登记 size、写 `<mp4>.freshness.json`。
3. 台账写失败：保留 stale、不写 E2、返回 False。
4. 多版本：a / `_b` 分别登记到 `outputs.a` / `outputs.b`。
5. watcher 接手：两版本登记后 `_output_conflicts` 为空（无 `disk_present_ledger_missing`）。
6. 全局锁被他人持有 → `_run_export` 返回 False 且不进入导出实现。
7. 导出失败 → 自己拿到的锁已释放。
8. watcher 已持锁（本进程 pid）→ 复用且不释放。

测试全部用临时目录 + `monkeypatch`，不碰真实草稿库、不启停剪映、不跑真实导出。

## 验证

```powershell
python -m pytest tests/test_series_runner_fail_closed.py -q
python -m pytest tests -q
```

结果：新用例 8 passed；整仓 **319 passed / 1 warning**（基线 311 + 8）。

## 风险

- D3 会让“watcher 导出中”时脚本导出直接失败（而不是并发），这是预期行为；错误信息
  带持有者，便于人工判断是否等待。
- 台账写入失败时成品已在盘上但状态为 `export_failed`：下次运行会因 stale 继续重导，
  不会误判为已完成。

## 实施记录

- 用户信号：`full` 模式复用重测结束、watcher 已用新代码拉起。
- 已改：`pipeline/series_runner.py`、新增 `tests/test_series_runner_fail_closed.py`。
- 未重启 watcher；未启停剪映；未跑真实导出。
