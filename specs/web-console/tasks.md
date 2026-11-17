# TASKS：本地 Web Console

> 未批准前不实施。勾选项为实施顺序，P0 全部完成再进 P1。

## P0（最小可用）

- [ ] T1 脚手架：`pipeline_ui/console/app.py`，仅监听 `127.0.0.1:8765`，静态页可打开。
- [ ] T2 `paths.py`：白名单根 + `realpath` 校验 + 拒绝 `..`，单测覆盖越权用例。
- [ ] T3 `doctor.py`：16 项只读检查（Python/依赖/剪映版本与进程/草稿根/短剧根/归档根/导出根/磁盘/锁/watcher/presets/ffmpeg/清单/端口），输出结构化 JSON。
- [ ] T4 `runner.py`：单队列、spawn `batch_runner`、固定 `--no-watch`、无 `--force`、stdout 逐行推送、退出码与耗时落 `runs.jsonl`。
- [ ] T5 `static/index.html` + `app.js`：启动表单（目录、集数区间、`--cut`、是否导出、预设组）+ 命令行预览 + 提交前 4 项预检。
- [ ] T6 `status.py`：阶段解析（stdout 横幅权重表）+ `progress.json` 百分比 + `job.json` 只读 + 锁状态；SSE 推送。
- [ ] T7 台账页：清单条数、每剧 `state`、三件套完整性、`archived` 但目标缺失高亮。
- [ ] T8 归档/清理 dry-run 计划页：只读调用 `archive --manual --dry-run` / `clean --manual --dry-run`，渲染「搬/删/保留 + 原因 + 大小」。

## P1

- [ ] T9 真执行通道：三段式确认 + 锁排队；调用 `archive --manual` / `clean --manual` / `verify --manual --scope`。
- [ ] T10 陈旧锁接管：复用 `manual_guard.diagnose_lock()`，unknown 不接管。
- [ ] T11 历史记录详情页：`runs.jsonl` 检索、日志下载。
- [ ] T12 封面预检：按 `batch_runner.py:414-437` 优先级显示实际使用的封面，校验 9:16 与短边 ≥720。
- [ ] T13 成片预览：`<video>` + 打开目录 + 三件套链接。
- [ ] T14 失败重试：整剧重跑（不跳步）。
- [ ] T15（独立立项）`batch_runner --export-resolution` 透传。

## P2

- [ ] T16 多剧队列与优先级。
- [ ] T17 `presets.json` 可视化编辑（备份 + 二次确认）。
- [ ] T18 进度权重用 `_timings.jsonl` 自动校准 + ETA。
- [ ] T19 通知（桌面/飞书）。
- [ ] T20 watcher 态势时间线。

## 验证清单（每个任务都要跑）

- 只读校验：执行前后对 `job.json`、`cover_job/**`、`presets.json` 做 mtime+hash 快照，必须无变化（除预期写入的 `_console/runs.jsonl`）。
- 越权校验：构造 `..\..`、`C:\Windows`、UNC 路径，必须 4xx。
- 锁校验：手工创建 `.export.lock`（活 pid）→ 启动被拒/排队；删除后恢复。
- 进度校验：导出阶段页面百分比与 `progress.json.percent_value` 一致；无估算时显示「无估算」。
