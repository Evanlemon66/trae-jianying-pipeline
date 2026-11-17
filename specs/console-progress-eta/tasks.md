# tasks.md —— Console 进度/ETA（方案 B 实施中）

前置：`specs/console-progress-eta/spec.md` 已评审通过；用户已批准方案 B（8 行，合并项用 `+` 注明）。

## 阶段 0：门禁

- [x] T0 用户确认方案（B+C 为主，A 为兜底；权重重排 T9 是否纳入；watcher 复用是否本轮做）
  - 验收：用户在对话中明确回复批准范围

## 阶段 1：进度产物（pipeline 侧）

- [x] T1 `pipeline/pipeline.py` 新增 `_write_progress(workdir, stage, label, done=0, total=0)`
  - 位置：`timed_step` / `_timing_summary` 附近（`pipeline.py` 顶部 ~L35-L80 区域）
  - 要求：原子写 `<workdir>/_progress.json`（`.tmp` + `os.replace`）；`try/except Exception: pass`；schema 见 spec §2.4.1；`run_id` 取 `os.environ.get("JY_RUN_ID","")`
  - 验收：手工调用生成合法 JSON；目录只读时不抛异常

- [x] T2 建稿阶段接入
  - 文件：`pipeline/pipeline.py#L1951-L2007`
  - 改动：循环前 `_write_progress(workdir,"build","建稿 · 注入视频",0,len(video_paths))`；每集 `📼` 打印后 `done+1`
  - 注意：`workdir` 由调用方传入，不得在该函数内新建默认目录
  - 验收：`_progress.json` 的 `done` 从 0 递增到 N

- [x] T3 人声分离阶段接入
  - 文件：`pipeline/pipeline.py#L2950-L2978`
  - 改动：循环前写 `("demucs","人声分离",0,len(episodes))`；**缓存命中分支与实跑分支都**在集末写 `done=i`
  - 验收：命中 `♻️` 缓存时 `done` 同样推进；`total` 等于 `len(episodes)`

- [x] T4 识别 / 翻译阶段接入
  - 文件：`pipeline/pipeline.py#L3150-L3258`
  - 改动：识别开始写 `("asr","识别字幕",0,len(units))`；每集识别结束写 `done`；翻译开始前写 `("translate","翻译",0,len(units))`；每集翻译结束写 `done`
  - 验收：两个阶段各自有连续 `done/total`；翻译全链失败的分支也要写 `done`

- [x] T5 其余阶段只写阶段名（无需计数）
  - 文件：`pipeline/pipeline.py` 中 `timed_step("VAD门控")`、`timed_step("字幕/音频终检")`、`timed_step("残缺集补翻")`、`timed_step("保存发布")`、`timed_step("导出")` 调用点
  - 改动：各前置一行 `_write_progress(..., total=0)`
  - 验收：UI 阶段名与日志一致，不出现回退到旧阶段

## 阶段 2：Console 消费

- [x] T6 `pipeline_ui/console/status.py` 新增 `read_pipeline_progress(workdir)`
  - 要求：只读；`json` 解析失败 / `v != 1` / `stage` 不在表内 → 返回 `None`；补充 `_mtime`
  - 验收：文件缺失、截断、字段缺失三种情况均返回 `None` 不抛异常

- [x] T7 `pipeline_ui/console/runner.py::snapshot()` 接入
  - 位置：`runner.py#L941-L970`（现有 `status.read_progress` 之后）
  - 改动：
    1. workdir 取 `run["workdir"]`（`build_command` 里已算出，落进 run 记录）→ 缺失时回退 `os.path.join(paths.WORK_ROOT, run["id"])`；
    2. `prog2 = status.read_pipeline_progress(workdir)`，校验 `run_id` 匹配（空 run_id 时按 mtime 5 分钟内）；
    3. `total>0` 时 `pct = base + (next-base)*done/total`，写 `run["percent"]`；
    4. `run["progress_basis"] = f"{label} {done}/{total}"`；
    5. `run["_pct_floor"] = max(旧值, pct)`，`percent` 取 `max(floor, pct)`；
    6. 计算 `run["eta_s"]`（规则见 spec §2.4.3）
  - 约束：导出阶段（`stage.key == "export"`）**直接跳过**本逻辑，保持现有 `percent_value` 行为
  - 验收：非导出阶段 `run.percent` 不再为 `None`；导出阶段回归不变

- [x] T8 `pipeline_ui/console/static/view.js` 展示
  - 位置：`view.js#L541`、`#L599-L620`
  - 改动：
    1. `pct == null` 时显示 `0%` + 文案"初始化中"，**移除"无估算"**；
    2. `hero-info` 内新增一行：`run.eta_s != null ? "预计剩余 X 分钟" : "估算中"`；
    3. `run.progress_basis || "无估算"` → `run.progress_basis || "阶段权重"`；
    4. SSE 文案：`SSE 推送 · 每秒一次快照` → `SSE 推送 · 1s 采样 · 15s 心跳`（见 spec §1 决定）
  - 验收：浏览器刷新后运行中的任务显示数字 + ETA；失败/停止任务仍显示最终态；文案含 15s

## 阶段 3：可选优化（单独开关）

- [x] T9 `pipeline_ui/console/status.py::external_tasks()` 复用进度文件（watcher / 手工任务）
  - 位置：`status.py#L696-L728`
  - 改动：`workdir` 可得时优先 `read_pipeline_progress(workdir)`，有值则覆盖 `percent` 与 `stage`，并加 `eta_s`；无值保持现有 `_batch_state.json` 口径
  - 约束：`updated_at` 超过 5 分钟视为过期，不采用；不写任何文件
  - 验收：watcher 驱动的任务在"外部任务"卡片里显示 `人声分离 37/198` 这类进度（spec §2.5）

- [x] T10 阶段权重重排（spec §2.4.4）+ 新增 `translate` 阶段键
  - 文件：`pipeline_ui/console/runner.py#L30-L49`
  - 约束：`STAGES` 与 `UI_STAGES` 顺序不变，只改第三列；`translate` 需有可识别的日志/进度来源（依赖 T4）
  - 验收：198 集整季运行中，进度百分比随时间近似线性增长，不在 34% 处长期停滞

- [ ] T11 注入日志消除误导（可选）
  - 文件：`pipeline/pipeline.py#L1965-L1966`、`scripts/utils/media_normalizer.py#L109-L110`
  - 改动：区分 `♻️ 复用转封装缓存` 与 `🔧 自动转封装`
  - 验收：缓存命中运行日志中不再出现"自动转封装"

- [x] T12 全阶段计时改造（**必做**，spec §3.6）
  - [x] T12a `_timing_summary(workdir, tag, final=False)`：`final=True` 才写 JSONL 并清空 `_TIMINGS`；记录增加 `started_at/ended_at/wall_s/timed_s/untimed_s`
  - [x] T12b `pipeline/pipeline.py` 补 `timed_step`：初始化、自动封面、预设合并、裁剪、隐藏中文字幕、帧对齐；并把 `demucs_input wav`（L2966-2969）与 `audio_ep wav`（L3163-3166）挪进对应 `timed_step`（spec §3.7）
  - [x] T12c `pipeline/batch_runner.py` 补 `timed_step`：帧对齐、B 组克隆/换预设/发布、串行导出（`导出/a`、`导出/b`）、导出后质检、归档
  - [x] T12d `batch_runner` 在 `finally` 调用 `pl._timing_summary(workdir, tag, final=True)`；`pipeline.__main__` 单集直调同样 `final=True`
  - 约束：只加计时器与一次收口写盘，不改任何业务逻辑与执行顺序
  - 验收：下一次运行 `_timings.jsonl` 只有一条记录；`wall_s` 与真实起止差 < 2s；`untimed_s` ≥ 0 且 < `wall_s`×2%；11 个阶段均可检索到条目

- [x] T13 Console 展示分组（**已选方案 B**：8 行，合并项用 `+` 注明，spec §2.7）
  - 方案 A：维持 11 行，只补数字
  - 方案 B（推荐 8 行）：建稿注入 / 人声分离 / 预设+裁剪 / 识别字幕 / 翻译字幕 / 字幕整理 / 终检+帧对齐 / 导出
  - 方案 C（6 行）：B 再合并 #3→#1、#7→#6
  - 文件：`pipeline_ui/console/runner.py#L30-L49`（STAGES）、`static/view.js` 时间线渲染
  - 验收：每行都有 `done/total` 或耗时；合计数字与 `_timings.jsonl` 对得上

## 阶段 4：验证

- [x] T14 单测：`tests/test_progress_file.py`
  - 用例：正常写入→读取；写失败（只读目录）主流程不中断；损坏 JSON → `None`；`v` 不匹配 → `None`；`done > total` 不越界；`updated_at` 过期 → `None`
  - 命令：`python -m pytest tests/test_progress_file.py -q`

- [x] T15 回归
  - 命令：`python -m pytest tests/test_step_cache.py tests/test_verify_stdout.py -q`
  - 验收：全绿

- [ ] T16 端到端（真机）
  - 步骤：Console 提交一次 `--limit 3` 的小任务 → 观察 ① 环形图有数字 ② demucs 显示 `n/3` ③ ETA 出现 ④ 导出阶段显示 `导出 x%` ⑤ 外部任务卡片（若 watcher 在跑）也有数字 ⑥ `_timings.jsonl` 含全部阶段且 `untimed_s` < 2%
  - 验收：spec §4 的 1-10 条全部满足
