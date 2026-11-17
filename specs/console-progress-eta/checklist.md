# checklist.md —— Console 进度 / ETA（方案 B）验收

## 进度产物（pipeline 写）
- [x] `_write_progress` 原子写 `<workdir>/_progress.json`，字段含 `v/stage/label/done/total/started_at/updated_at/run_id`
- [x] 写失败（同名目录 / 只读）不抛异常，主流程不受影响（`test_write_failure_does_not_raise`）
- [x] 建稿注入循环逐集推进 `done`
- [x] 人声分离（含 wav 抽取）逐集推进 `done`，缓存命中同样推进
- [x] 识别/翻译逐集推进，额外带 `asr_done` / `tr_done`
- [x] 预设合并+裁剪、隐藏中文字幕+VAD、终检+补翻+帧对齐 各写一次阶段名

## Console 只读消费
- [x] `status.read_pipeline_progress` 对缺失 / 损坏 / `v` 不符 / 超过 5 分钟 → 返回 None
- [x] `status.progress_percent` 按阶段权重插值，`done>total` 收敛到下一阶段起点，`export` 返回 None
- [x] `runner.snapshot()` 写入 `percent / stage_label / progress_basis / eta_s`，导出阶段完全跳过
- [x] `percent` 单调不回退（`_pct_floor` 写回 `_current`，跨快照生效）
- [x] ETA：阶段内 ≥3 集样本走瞬时速率，否则退历史 `_timings.jsonl` 每集中位数，再否则显示"估算中"
- [x] watcher / 手工 CLI 复用同一进度文件（`external_tasks()` 优先读 `_progress.json`）

## 展示
- [x] 阶段表为方案 B 的 8 行，合并项用 `+` 注明（建稿 · 注入视频 + 草稿初始化 等）
- [x] 环形图不再出现"无估算"：无数据时 `0%` + "初始化中"
- [x] 运行中显示"预计剩余 X 分钟 / <1 分钟 / 估算中"
- [x] chip 兜底由"无估算"改为"阶段权重"
- [x] 实时状态卡片文案为 `SSE 推送 · 1s 采样 · 15s 心跳`（仅文案，协议未改）

## 验证
- [x] `python -m pytest tests/test_progress_file.py tests/test_runner_stage.py tests/test_external_tasks.py -q` 全绿
- [x] `python -m pytest tests/test_step_cache.py -q` 全绿
- [x] `py_compile` 通过（pipeline / batch_runner / status / runner）
- [ ] 真机：当前导出任务结束后重启 Console，提交 `--limit 3` 复核环形图、`n/3`、ETA、导出百分比（待任务结束，不打断运行中的导出）
