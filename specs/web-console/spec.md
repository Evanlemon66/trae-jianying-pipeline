# SPEC：本地 Web Console（方案阶段，待批准后实施）

状态：**草案，未批准**  
范围：只读现状 + 起子进程执行；不改 `pipeline/`、`lib/` 的默认行为。  
完整设计与参数盘点见 `docs/web-console-plan.md`（本文件是决策摘要与实施边界）。

## 路径契约更新（已收口）

- 路径唯一入口是 `pipeline/export_config.resolve_paths()`（`validate_config()` 返回 `ok/paths/sources/issues`），Console 不自行拼路径。
- `series_root` 是唯一素材根；`watch.roots` 仅 watcher 的额外扫描根（为空只扫 `series_root`）。
- 业务根 `series_root`/`export_dir`/`deps_root` 无默认值，缺失或无效直接 `ConfigError`（fail-closed）。

---

## 1. 目标

1. 在一台本机上用一个页面完成：环境自检 → 选剧启动串流剪辑 → 看实时阶段/进度 → 查归档与清理状态（先 dry-run）。
2. 所有状态**读现有落盘文件**，不引入第二套状态机、不写 `job.json` / `cover_job/**`。
3. 与 watcher 共存：不抢锁、不改配置、不接管调度。

## 2. 非目标

- 不做远程访问、多用户、鉴权体系。
- 不重写剪映 UI 自动化；不改现有 `pipeline.py` / `batch_runner.py` 语义。
- 不做自动删除；所有写操作必须 dry-run → 人工确认 → 执行。
- 不做定时调度（已有计划任务）。

## 3. 关键决策（待用户确认）

| 编号 | 决策 | 推荐值 |
|---|---|---|
| D1 | 执行入口 | `pipeline/batch_runner.py`（与「快速上手」一致） |
| D2 | P0 写操作范围 | 只做 dry-run 计划展示，不执行 |
| D3 | 导出分辨率 | P0 不支持（batch_runner 未暴露）；P1 立小项加透传 |
| D4 | 历史记录 | `pipeline_ui/_console/runs.jsonl` |
| D5 | 前端 | 原生 HTML + 原生 JS，无构建链 |
| D6 | 后端 | FastAPI + Uvicorn，仅 `127.0.0.1:8765` |

## 4. 架构（计划新增文件，尚未创建）

```
pipeline_ui/console/
  app.py            # FastAPI 入口：路由 + 静态页 + SSE
  doctor.py         # 环境自检（只读）
  runner.py         # 单队列执行器：spawn batch_runner，维护 run 状态
  status.py         # 读 job.json / progress.json / manifest / lock 的聚合层
  paths.py          # 路径白名单与 realpath 校验
  static/index.html # 单页 UI
  static/app.js
pipeline_ui/_console/runs.jsonl   # 执行历史（gitignore）
```

约束：
- `runner.py` 只允许构造以下命令：`python pipeline/batch_runner.py <dir> [--only|--limit|--cut|--cover-image|--profile|--no-export|--export-timeout|--retries|--draft-prefix] --no-watch`。
- 禁止出现 `--force`、`--fresh`、`--clean-workdir`、`--kill-jianying`、`--per-episode`、`--auto-merge`。
- `status.py` 不 import `cover_watch` 的写路径，只读其落盘文件。

## 5. 数据契约

| 数据 | 路径 | 读取字段 |
|---|---|---|
| 运行状态 | `<导出根>/<剧名>.progress.json` | `status/done/percent_value/rate_mbps/eta_s/phase/failure_class/errors` |
| 完成心跳 | `<导出根>/<剧名>/<剧名>.mp4.progress.json` | 同上 + `final_mb/duration_s/cover` |
| 台账 | `<剧目录>/cover_job/job.json` | `stage/revision/video.status/video.export_state/titles.gate` |
| 清单 | `pipeline/_stream_manifests/<草稿>.json` | `paths.*`、`versions.<p>.{state,archived_to,export_file,export_dir}` |
| 全局锁 | `.export.lock` | `pid/owner/started` |
| watcher 锁 | `<剧目录>/cover_job/.watcher.lock` | `pid/phase/pipeline_pid/started/updated_at` |
| watcher 日志 | `<剧目录>/cover_job/watcher.log` | 增量 tail |

进度口径：编辑阶段用固定阶段权重（只在阶段横幅推进时加分，阶段内不插值）；导出阶段用 `percent_value`；无估算时显示「无估算」。权重表见 `docs/web-console-plan.md` §4.2。

## 6. 安全边界

- 白名单根：`export_config.resolve_paths()` 给出的 `series_root`、`export_dir`、`archive_root`、`watch_roots`、`get_projects_root()`、仓库根（只读）。
- 所有路径 `realpath` 归一后必须落在白名单内；拒绝 `..`、盘符切换、UNC。
- 后端只写两处：`runs.jsonl` 与自身日志；不写业务文件。
- 归档/清理只允许调用 `stream_manifest.py` 的 `--manual` 子命令；先 `--dry-run`。
- 锁被占：排队；「接管陈旧锁」只在 P1 提供，且复用 `manual_guard.diagnose_lock()` 的三态判定（unknown 不接管）。

## 7. 验收标准

1. Doctor 页能对 16 项检查逐项给出「绿/黄/红 + 证据值」，全量耗时 < 3s。
2. 启动一部剧后，页面 2s 内显示「当前阶段」，阶段切换与 stdout 横幅一致；导出阶段百分比与 `.progress.json` 的 `percent_value` 一致。
3. 不传 `--force` 时，若 `.export.lock` 被占，任务进入排队且不启动子进程。
4. 归档/清理页先展示 dry-run 计划，未点击确认时磁盘上无任何变化（用文件 mtime/大小核对）。
5. 全流程不修改 `job.json`、不新增/删除 `cover_job/**` 下文件（除既有 pipeline 行为）。
6. 浏览器关闭再打开，能恢复展示正在运行的 run 与历史记录。

## 8. 批准门

- 用户确认第 3 节 D1–D6 后，才进入实施。
- 实施期任何需要改 `pipeline/` 或 `lib/` 的项（如导出分辨率透传）必须单独立项、单独批准。
