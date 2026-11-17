# Console 实时进度 / ETA 与串流阶段提速 —— 方案对比（未实施，待批准）

状态：draft（只分析，不修改任何代码）
日期：2026-11-17
适用代码：`pipeline_ui/console/*`、`pipeline/pipeline.py`、`pipeline_ui/console/static/view.js`

---

## 0. 结论速览

| 用户问题 | 结论 | 本轮是否改 |
|---|---|---|
| ① SSE 多久推一次 | 现状 1s/次快照、仅变化才写、15s 心跳；**推送逻辑不动，只改 UI 文案** | 只改文案 |
| ② 进度一直"无估算" | 不是没有数据，是**读错了字段**：`run.stage.percent` 有值，但环形图只读 `run.percent`，而该字段在非导出阶段始终为 `null`；同时 `progress_basis` 运行中从不更新 | 是（低风险，可观测性改造） |
| ③ 注入/人声分离太慢是否因重编码 | **否**。注入实测 0.19s/集（缓存复用，日志文案误导）；demucs 11.3s/集是模型推理，与视频编码完全无关，"不编码只分离"省不到时间 | 否（解释 + 可选日志修正） |

真实耗时结构（本次 198 集运行，`_timings.jsonl` 记录的**已计时部分**合计 7312.4s = 121.9 分钟；导出不在内，未计时阶段另计）：

| 阶段 | 总耗时 | 次数 | 均值 | p50 | p90 | min | max | 占比* |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 翻译 | 3494.3s | 198 | 17.65s | 14.30s | 29.73s | 4.70s | 83.10s | **47.8%** |
| 人声分离 | 2166.3s | 191 | 11.34s | 10.60s | 11.80s | 9.70s | 39.20s | **29.6%** |
| ASR | 1384.4s | 198 | 6.99s | 6.40s | 8.70s | 5.80s | 39.80s | 18.9% |
| VAD 门控 | 163.1s | 1（整季一次） | 163.1s | — | — | — | — | 2.2% |
| 字幕/音频终检 | 58.2s | 1（整季一次） | 58.2s | — | — | — | — | 0.8% |
| 注入视频 | 37.0s | 1（整季一次） | 37.0s | — | — | — | — | 0.5% |
| 保存发布 | 9.0s | 1（整季一次） | 9.0s | — | — | — | — | 0.1% |

**未纳入计时（`timed_step` 未包裹，所以 `_timings.jsonl` 里根本不存在）：**
预设合并、裁剪、隐藏中文字幕、残缺集补翻、帧对齐、导出。

\* 占比 = 该阶段 / 已计时总计（7312.4s），**不是** / 墙钟时间。未计时阶段的时间没有被算进分母。

同一次运行的墙钟对账（`_run.json` started_at=10:05:56，`_batch_state.json` 落盘=12:11:39，导出前）：

- 墙钟合计 **7543s（125.7 分钟）**
- 已计时 **7312.4s（97.0%）**
- 未计时 **230.6s（3.0%）**

这正是"对任务耗时没对齐"的直接原因：耗时表只有 7 个桶，串流链路有一半阶段是盲区。
ETA 基线只能覆盖已计时的阶段，剩余阶段目前无法估。

分布特征（对 ETA 很重要）：
- 翻译 p50 14.3s，但 p90 29.7s、max 83.1s——max 里包含"全链失败 → sleep 45s → 重试"的退避时间；
- ASR max 39.8s、人声分离 max 39.2s，均为降级/回退模型导致的离群值；
- 因此 ETA 必须用**中位数**而不是均值，且要容忍离群；均值（17.65s）会被重试拉高。

> 瓶颈是**翻译**（58 分钟），不是注入，也不是 demucs。

---

## 1. 问题①：SSE 推送频率

### 现状（事实）

`pipeline_ui/console/app.py#L501-L514`：

- 事件循环 `time.sleep(1.0)`：每秒 `RUNNER.snapshot()` 一次；
- **只有 payload 变化才写** `event: state`，否则不产生网络写；
- `beat % 15 == 0` 发 `: ping\n\n`（15s 保活，防代理/浏览器断连）；
- 空闲时同样每秒算一次快照。

### 评估

- 1s 对本地 127.0.0.1 + 单任务足够，延迟低于人眼可感知阈值；
- 变化才推送已经避免了无效流量，正常运行时实际推送频率由日志行决定（约每集数行）；
- 改成 500ms 只会增加 CPU/重绘，不会让 ETA 更准；
- 改成"运行 1s / 空闲 3-5s"可省一点 CPU，但引入状态分支和首帧延迟风险，收益 < 成本。

### 决定

**推送逻辑维持现状，只改 UI 文案。**

`pipeline_ui/console/static/view.js#L541` 当前为：

```html
<div><h2>实时状态</h2><p class="card-sub">SSE 推送 · 每秒一次快照</p></div>
```

按用户要求改为能体现 15s 心跳的文案：

```html
<p class="card-sub">SSE 推送 · 1s 采样 · 15s 心跳</p>
```

说明：1s 采样指服务端每秒检查一次快照；有变化立即推送；15s 是保活 ping。该文案同时准确反映"15s 一次"的保活节奏，避免写成"每 15s 才推一次"（那会与实际行为不符）。

本 spec 仍不涉及 SSE 协议、推送频率、心跳周期的代码改动。

---

## 2. 问题②：进度一直"无估算"

### 2.1 根因（三处，互相独立）

1. **环形图读的字段运行中恒为 null**
   - `pipeline_ui/console/static/view.js#L552`：`const pct = (run.percent == null) ? null : ...`；
   - `#L604-L605`：`pct == null` 时直接渲染"无估算 / 等待阶段"；
   - 而 `run.percent` 只在 `runner.py#L812`（整次结束）与导出分支 `#L959-L967`、多账号 `#L642-L644` 赋值；
   - `runner.py#L331-L332` 初始化 `"percent": None, "progress_basis": "无估算"`；
   - 结果：建稿 / 人声分离 / ASR / 翻译 全程 `run.percent == null` → 界面就是"无估算"。

2. **阶段权重明明存在，但没被环形图消费**
   - `runner.py#L30-L49` 的 `STAGES` 每项第三列就是阶段起始百分比（build 0 / demucs 8 / presets 24 / cut 30 / asr 34 / hide 60 / vad 63 / verify 71 / repair 85 / framefix 88 / export 90）；
   - 日志命中阶段时 `runner.py#L538-L541` 只写 `run["stage"] = {key,label,percent}`，没有同步写 `run["percent"]`。

3. **`progress_basis` 运行中不更新**
   - `_basis()`（`runner.py#L928-L939`）只在 `_finalize`（`#L812-L813`）调用一次；
   - 所以顶部 chip 在整段运行里一直显示初始化值"无估算"。

附带缺陷：阶段权重与真实耗时占比严重不匹配——demucs 只占 8→24（16 点）却吃掉 29.6% 时间；翻译没有独立阶段，被塞进 `asr 34→60` 的 26 点里，却占 47.8% 时间。即使把字段打通，进度条也会长时间"黏"在 34 不动。

### 2.2 方案对比

| 方案 | 做法 | 精度 | 改动面 | 稳定性 | 优雅度 |
|---|---|---|---|---|---|
| A 最小修补 | 日志命中阶段时同步 `run["percent"] = stage.percent`；`_basis` 每行更新 | 阶段级（8→24→34…），阶段内仍静止 | 2 文件 ~6 行 | 高 | 一般（治标，进度仍会长时间卡住） |
| B 结构化进度文件 | pipeline 写 `<workdir>/_progress.json`（stage/done/total），Console 只读消费 | 集级 + 段内线性插值 | 2 文件 ~40 行，新增 1 个只读函数 | 高（原子写、失败降级） | 高（进度成为流水线产物，不再靠刮日志） |
| C 历史基线 ETA | 复用已有 `<workdir>/_timings.jsonl`，按最近 N 次同阶段均值推剩余时间 | 分钟级 ETA | Console 侧 ~30 行 | 高（只读、无样本即不显示） | 高（数据已经在写，只是没人读） |
| D WebSocket / 重构推送层 | 换成双向推送 | — | 大 | 中 | 低（解决的不是真问题） |

### 2.3 推荐：B + C，A 作为 B 落地前的过渡

理由：
- B 把"进度"变成 pipeline 的**产物**（和 `_batch_state.json`、`_run.json` 同一层级），Console 只读，符合仓库现有"只读聚合"约定（`status.py` 开头即声明"全部读现有落盘文件，不写"）；
- C 的数据源 `_timings.jsonl` **已经存在**（`pipeline.py#L3569` 每次运行追加 total/stages/detail），零新增埋点；
- A 只保留"B 未覆盖时的兜底"（B 的读写任一失败 → 退回阶段权重，绝不显示"无估算"）。

### 2.4 设计细节（decision-complete）

#### 2.4.1 新进度文件

路径：`<workdir>/_progress.json`（workdir 与 `_batch_state.json` 同级）。

Schema v1：

```json
{
  "v": 1,
  "stage": "demucs",
  "label": "人声分离",
  "done": 37,
  "total": 198,
  "started_at": 1763451000.0,
  "updated_at": 1763451400.0,
  "run_id": "20261117-100556-5994"
}
```

写入规则：
- 原子写：`_progress.json.tmp` + `os.replace`（与 `step_cache.save` 同款）；
- 全函数 `try/except Exception: pass`，写失败绝不影响主流程；
- 进入阶段写 `done=0`，每集结束写 `done=i`；
- `run_id` 由 pipeline 侧取 `os.environ.get("JY_RUN_ID")`（batch_runner 已在环境里带 run 上下文；无则空串）。

写入点（复用现有循环，不新增循环）：
- `pipeline/pipeline.py#L2950` 人声分离逐集循环：`stage="demucs"`，`total=len(episodes)`；
- `pipeline/pipeline.py#L3150` 识别/翻译逐集循环：`stage="asr"`（识别完成）→ `stage="translate"`（翻译开始），`total=len(units)`；
- 建稿：`pipeline/pipeline.py#L1959` 注入循环，`stage="build"`，`total=len(video_paths)`；
- 阶段切换（presets/cut/hide/vad/verify/repair/framefix）只需写 `stage/label/done=0/total=0`，用于让 UI 阶段名同步。

#### 2.4.2 Console 读取

新增 `status.read_pipeline_progress(workdir)`（只读，返回 dict 或 None），在 `runner.py::snapshot()` 里 `status.read_progress(...)` 之后调用：

```
base = STAGE_PERCENT[stage]            # 复用 STAGES 表
nxt  = STAGE_PERCENT[next_stage]
pct  = base + (nxt - base) * (done/total)   # total>0 时
```

约束：
- `run["percent"]` **单调不回退**：内部记 `run["_pct_floor"]`，`percent = max(floor, pct)`；
- `run["progress_basis"]` 写成人话：`"人声分离 37/198"`、`"翻译 12/198"`；
- 文件缺失/损坏/`v` 不认 → 返回 None，退回 A 行为（阶段权重），**永不显示"无估算"**；
- 导出阶段逻辑完全不变（继续用 `percent_value` + `_export_percent`）。

#### 2.4.3 ETA

优先级：
1. 当前阶段瞬时速率：`elapsed_since_stage_start / done`（要求 `done >= 3`，取最近 3 次样本中位数）；
2. 后续阶段历史基线：读同 workdir `_timings.jsonl` 最近 3 条，`sum(stage.total_s)/count / episodes` × 剩余集数；
3. 两者皆无 → 不显示 ETA（显示"估算中"，而不是"无估算"）。

展示规则：
- 文案：`预计剩余 18 分钟`（<1 分钟显示"<1 分钟"）；
- 每 5s 重算一次，避免每秒抖动；
- 允许小幅波动，但**不允许 ETA 无故增大 >20%**（超限则沿用上次值）；
- 位置：`hero-title` 下方一行；不放进 chip（chip 保留 `progress_basis`）。

#### 2.4.4 阶段权重重排（可选，单独开关）

按实测时间占比重排 0→90（导出仍占 90→99），并新增 `translate` 阶段键：

| 阶段 | 现值 | 建议值 |
|---|---:|---:|
| build 建稿/注入 | 0 | 0 |
| demucs 人声分离 | 8 | 2 |
| presets 预设合并 | 24 | 30 |
| cut 裁剪 | 30 | 31 |
| asr 识别 | 34 | 32 |
| translate 翻译（新增） | — | 50 |
| hide 隐藏中文 | 60 | 87 |
| vad VAD | 63 | 88 |
| verify 终检 | 71 | 89 |
| repair 补翻 | 85 | 89 |
| framefix 帧对齐 | 88 | 89 |
| export 导出 | 90 | 90 |

说明：只影响视觉节奏，不影响正确性；若不想动阶段表，可只做 B+C，进度仍会按旧权重走。

### 2.5 多入口复用（Console / 手工 CLI / watcher）

**结论：可以复用，不需要为 watcher 单独造一套。**

原因：进度文件由 `run_pipeline()` 在自身循环里写，**谁调用 pipeline，谁就自动拥有它**。三个入口的 workdir 规则本来就是同一套：

| 入口 | 启动命令 | workdir |
|---|---|---|
| Console | `runner.build_command()` 显式带 `--workdir` | `pipeline_ui/_console/workdir/<run_id>` |
| 手工 CLI | `batch_runner.py <剧目录>` | 默认 `pipeline/_work_<safe>_<hash>` |
| watcher | `cover_watch.py#L1332` 起 `batch_runner.py <剧目录> --force --no-export`（**不带 --workdir**） | 同上默认目录（实测 `pipeline/_work_山河重生…_95e2dbde`） |

所以 watcher 跑出来的 `_progress.json` 天然落在默认 workdir，Console 已经能通过 `status.cmd_workdir()` 从命令行解析出该目录，只需多读一个文件即可展示 watcher / 手工任务的进度与 ETA。

需要补的一处：`status.external_tasks()` 目前对 watcher 与手工任务给的是 `percent: None`（`status.py#L674`、`#L690`、`#L709`，来源只有 `_batch_state.json`）。改为**优先读 `_progress.json`**，即可让"外部任务"卡片也显示数字。

注意事项：
- watcher 与手工 CLI 共用默认 workdir → 文件会被最新一次运行覆盖。用 `run_id` + `updated_at` 双重校验，超过 5 分钟视为过期不展示；
- `series_runner.py` 的导出阶段不经过 pipeline 主循环，仍走导出进度文件，不在本节范围；
- watcher 自身不需要读这个文件（它有自己的 `.watcher.lock` / `job.json`）；若以后想在 job.json 里记 percent，直接复用同一读取函数。

### 2.6 明确不做

- 不改 SSE 协议与推送频率；
- 不改 `runs.jsonl` / `_run.json` 结构；
- 不新增数据库、不引入 WebSocket；
- 不在 pipeline 里做 ETA 计算（职责留在 Console）。

---

### 2.7 Console 展示阶段清单（**待用户审核，先不实施**）

先澄清一个前提：**`timed_step` 不会自动出现在 Console**。Console 串流模式的时间线是 `runner.py#L30-L49` 硬编码的 `STAGES`（11 行），`timed_step` 只写日志 + `_timings.jsonl`。

所以"全阶段计时"与"UI 展示"是两件事：
- **记录层**：细粒度，11+ 个桶全量写 JSONL（供分析）；
- **展示层**：可以合并成更少的行，避免刷屏。

当前串流模式展示 11 行：建稿·注入视频 / 人声分离 / 预设合并 / 裁剪 / 识别翻译 / 隐藏中文字幕 / VAD 门控 / 终检 / 残缺集补翻 / 帧对齐 / 导出。

其中「识别翻译」把 ASR（1384s）和翻译（3494s）塞进同一行，导致最大头看不见；「隐藏中文字幕」单独一行但只有 ~155s；「残缺集补翻」本次为 0。

**方案 B（推荐，8 行）**：

| # | 展示行 | 合并来源 | 本次实测 |
|---|---|---|---:|
| 1 | 建稿 · 注入视频 | build（含初始化） | 37s |
| 2 | 人声分离（含音频提取） | demucs + demucs_input wav | 2191s |
| 3 | 预设合并 + 裁剪 | presets + cut | 待计时（估 <10s） |
| 4 | 识别字幕（含音频提取） | asr + audio_ep wav | 1407s |
| 5 | **翻译字幕** | translate（新增独立行） | 3494s |
| 6 | 字幕整理 | hide + vad + repair | 318s |
| 7 | 终检 + 帧对齐 | verify + framefix | 67s |
| 8 | 导出（含多账号） | export | 进行中 |

**方案 C（极简，6 行）**：在 B 基础上再合并 #3→#1、#7→#6，只保留：建稿注入 / 人声分离 / 识别字幕 / 翻译字幕 / 字幕整理与终检 / 导出。

**方案 A（维持现状，11 行）**：不改展示，只把数字补准。

请用户从 A/B/C 中选一个（或指定哪几行要合并/拆分）；确认后再写进 `tasks.md` 执行。

## 3. 问题③：注入视频 / 人声分离耗时归因

### 3.1 注入视频：0.19s/集，已经是缓存命中，不存在"重编码拖慢"

事实：
- `pipeline/pipeline.py#L1965-L1966`：只要 `_norm != vp` 就打印 `🔧 自动转封装`；
- `scripts/utils/media_normalizer.py#L95-L97`：`should_normalize_video_for_jianying()` 放行标准 1080x1920 / 1920x1080 + h264 + yuv420p；
- 同文件 `#L109-L110`：需要转码时先查 `__jycache__/<stem>.__jy_norm__.mp4`，`_is_cache_fresh` 命中直接返回；
- **缓存命中时 `_norm != vp` 依然成立**，所以每集都打印"自动转封装"，但并没有真的编码。

实测：198 集注入合计 37.0s。若真在转码，单集就要数秒，198 集至少十几分钟。

结论：
- 提速空间 ≈ 0；
- 真正的成本在首次运行（一次性），后续由 `__jycache__` 复用；
- "不编码只分离"对注入无意义，且去掉规范化会带来剪映导入失败风险（非 h264 / yuv420p / 奇数尺寸素材）。

可选（1 行日志修正，仅当用户希望消除误导）：
`normalize_video_for_jianying` 返回 `(path, reused: bool)`，或调用侧比较 `mtime`，把日志拆成 `♻️ 复用转封装缓存` / `🔧 自动转封装`。

### 3.2 人声分离：11.3s/集是模型推理，与视频编码无关

事实：
- `pipeline/pipeline.py#L2964-L2969`：demucs 的输入不是视频，而是 `run_ffmpeg_extract_audio()` 抽出的 wav；
- `timed_step("人声分离/epN")` 只包住 `run_demucs()`（`#L2971-L2972`），音频抽取不在计时内；
- `pipeline/pipeline.py#L2236` 命令为：
  `python -m demucs -n htdemucs_ft --two-stems=vocals -o <out> <wav>`
  没有 `-d`、没有 `--shifts`、没有 `--segment`；
- `pipeline/_env_profile.json`：`demucs.model = "htdemucs_ft"`（4 个 fine-tuned 模型 bag），`fallback = "htdemucs"`。

结论：11.3s/集是 **htdemucs_ft 推理**成本。视频编不编码、转不转封装，对这个数字没有任何影响。

### 3.3 提速选项对比

| 选项 | 预计节省 | 代价 / 风险 | 建议 |
|---|---:|---|---|
| 去掉视频规范化 | ~0 | 首次运行可能导入失败 | 不做 |
| 只分离不编码 | ~0 | 无意义（demucs 本来就只读 wav） | 不做 |
| `htdemucs_ft` → `htdemucs` | 约 25 分钟（1 模型 vs 4 模型） | 人声干净度下降，需试听 A/B | 可选，走 profile 开关 |
| 跳过 demucs（不挂独立人声轨） | 约 36 分钟 | 改变成片音频结构，静音/字幕逻辑可能依赖 | 仅在明确不要人声轨时 |
| 2 路并行 demucs | 约 18 分钟 | 8GB 显存 OOM 风险、进程管理复杂 | 本轮不做 |
| `--shifts` / `--segment` 调参 | 10-20% 不确定 | 质量可能下降，需实测 | 不做（收益不确定） |

**本轮建议：不加任何 demucs 参数，也不换模型。** 先拿到可观测性（B+C），再让用户用数据决定是否换 `htdemucs`。

### 3.4 顺带提示（不在本 spec 范围）

翻译 3494s（47.8%）是最大头，`translate_texts()` 按集串行调用云 API（`pipeline.py#L3257-L3258`）。若要真正提速，收益最大的是翻译并发/批量，而不是注入或 demucs。此项单独评估，不进本 spec。

---

### 3.5 本次运行的实际时间线（产物 mtime 反推，仅供核对）

本次运行（Console run `20261117-100556-5994` / batch run `20261117-100556-9bbe9f`，198 集，`--profile a,b`，导出前）：

| 时间块 | 起止 | 墙钟 | 已计时 | 未计时 |
|---|---|---:|---:|---:|
| 启动 → 建稿注入 → 人声分离 | 10:05:56 → 10:42:52 | 2216s | 37.0 + 2166.3 = 2203.3s | ~13s |
| ASR + 翻译（逐集交替：每集先 ASR 再翻译） | 10:42:52 → 12:05:05 | 4933s | 1384.4 + 3494.3 = 4878.7s | ~54s（每集抽音频） |
| 隐藏中文字幕 + 补翻 + VAD | 12:05:05 → 12:10:23 | 318s | VAD 163.1s | ~155s |
| 字幕/音频终检 | 12:10:23 → 12:11:21 | 58s | 58.2s | ~0 |
| 保存发布 + 帧对齐 + B 组预设/发布 | 12:11:21 → 12:11:39 | 18s | 9.0s | ~9s |
| 导出（2 账号串行） | 12:11:39 起 | 进行中 | — | — |
| **合计（导出前）** | 10:05:56 → 12:11:39 | **7543s** | **7312.4s** | **230.6s** |

锚点来源：
- 10:05:56 = `_run.json.started_at`；
- 10:42:52 = 最后一个 `demucs_input_epNN.wav` 的 mtime；
- 10:43:04 = 第一个 `audio_epNN.wav` 的 mtime（ASR 起点）；
- 12:05:05 = 最后一个 `translation_epNN.json` 的 mtime；
- 12:10:23 = `_vad_gate_report.json` 的 mtime；
- 12:11:21 = `_subtitle_audit.json` 的 mtime；
- 12:11:39 = `_batch_state.json` 的 mtime（导出开始前最后一次落盘）。

**结论：未计时阶段并不慢（合计 230.6s，3.0%），问题只是"看不见"。** 真正值是让每个阶段都进计时表，而不是去优化它们。

### 3.6 全阶段计时改造（必做）

目标：一次运行只写一条 `_timings.jsonl` 记录，且 `timed_s + untimed_s == wall_s`（误差 < 2%）。

设计（复用现有原语，不新建模块）：
- `timed_step` 是进程级累加器（`pipeline._TIMINGS`），`batch_runner` 与 `run_pipeline` 同进程，直接 `pl.timed_step(...)` 即可共用；
- 给 `_timing_summary(workdir, tag, final=False)` 增加 `final`：
  - `final=False`：只打印，不写盘（`run_pipeline` 内部保持现有行为）；
  - `final=True`：写一条 JSONL 并清空 `_TIMINGS`（由 `batch_runner` 在 `finally` 调用，覆盖导出与收尾）；
  - `pipeline.py` 单集直调入口在 `__main__` 里调 `final=True`，不会与 batch_runner 重复写；
- 记录 schema 增加 `started_at / ended_at / wall_s / timed_s / untimed_s`，untimed 即 `wall_s - timed_s`。

必加 `timed_step` 的位置（当前只有 7 个桶）：

| # | 阶段 | 文件与位置 |
|---|---|---|
| 1 | 初始化/草稿准备 | `pipeline/pipeline.py` `run_pipeline` 开头 |
| 2 | 自动封面 | `pipeline/pipeline.py` 封面设置调用处 |
| 3 | 预设合并 | `pipeline/pipeline.py` `加预设:` 调用处 |
| 4 | 裁剪 | `pipeline/pipeline.py` `[6] 裁剪` 调用处 |
| 5 | 隐藏中文字幕 | `pipeline/pipeline.py` `[10] 隐藏中文字幕` 调用处 |
| 6 | 帧对齐（pipeline 内若有） | `pipeline/pipeline.py` 帧对齐调用处 |
| 7 | 帧对齐（batch 侧） | `pipeline/batch_runner.py::_framefix_draft` 调用处 |
| 8 | B 组克隆/换预设/发布 | `pipeline/batch_runner.py` 账号预设草稿就绪前后 |
| 9 | 多账号串行导出 | `pipeline/batch_runner.py` 串行导出调用处（`导出/a`、`导出/b`） |
| 10 | 导出后质检 | `pipeline/batch_runner.py` `verify_export` 调用处 |
| 11 | 归档/收尾 | `pipeline/batch_runner.py` 归档调用处 |

已包裹的不重复加：注入视频、人声分离、ASR、翻译、VAD、字幕/音频终检、残缺集补翻、保存发布。

验收：下一次运行 `_timings.jsonl` 中 `wall_s` 与真实起止时间差 < 2s；`untimed_s` ≥ 0 且 < `wall_s` 的 2%；每个上表阶段都能在记录里找到独立条目（`count` 允许为 0，如本次补翻）。

### 3.7 wav 生成（音频抽取）到底算哪个阶段

**现状：抽 wav 有两次，且两次都在 `timed_step` 外面，所以既没进 `_timings.jsonl`，也没进 Console 阶段。**

| 用途 | 代码位置 | 产物 | 本次实测（mtime 反推） |
|---|---|---|---|
| 给 demucs 用 | `pipeline/pipeline.py#L2966-L2969`（`run_ffmpeg_extract_audio`，在 `with timed_step("人声分离/epN")` **之前**） | `demucs_input_epNN.wav` | 191 集 × 0.130s ≈ **24.8s** |
| 给 ASR 用 | `pipeline/pipeline.py#L3163-L3166`（`_extract_audio_with_heal`，在 `with timed_step("ASR/epN")` **之前**） | `audio_epNN.wav` | 198 集 × 0.113s ≈ **22.3s** |

合计 **≈ 47.1s（占墙钟 0.6%）**。

为什么没标识：两处调用都写在 `with timed_step(...)` 的上面，计时器包住的是 `run_demucs()` / `run_asr_chain()`，不含 ffmpeg 抽音频。日志里只有 `🎬 先从视频提取音频...` / `✅ 提取到: ...`，没有耗时。

改造建议（并入 T12，不单独建阶段）：
- 把 `run_ffmpeg_extract_audio` / `_extract_audio_with_heal` 挪进对应的 `with timed_step(...)` 里，或单独 `timed_step("音频提取/demucs")`、`timed_step("音频提取/ASR")`；
- 推荐**挪进去**（更符合语义）：人声分离耗时 = 抽音频 + 推理；ASR 耗时 = 抽音频 + 识别；
- UI 上不新增行，只让现有两行的数字变准。

---

## 4. 验收标准

1. 运行中任意时刻，环形图**不再出现"无估算"**；显示的数字单调不减。
2. 人声分离阶段显示 `37/198` 这类集级进度，且阶段内数字连续变化。
3. ETA 在有 ≥1 个历史样本或当前阶段 `done>=3` 后出现，误差在 ±3 分钟内（对 198 集量级）。
4. `_progress.json` 写入失败（如磁盘只读）时，pipeline 行为与现在完全一致，Console 退回阶段权重。
5. 导出阶段显示与现在一致（`percent_value` / `串行导出 i/n`）。
6. 断点续跑命中缓存时，`done/total` 仍然正确推进（缓存分支也要写 `done`）。
7. 无新增第三方依赖；`pytest tests/test_step_cache.py`、`tests/test_verify_stdout.py` 仍通过。
8. 实时状态卡片文案显示 `SSE 推送 · 1s 采样 · 15s 心跳`。
9. watcher 或手工 CLI 驱动的任务，在"外部任务"卡片中也能显示阶段 + `done/total`（依赖 workdir 可解析）。
10. 下一次运行 `_timings.jsonl` 只有一条记录，`untimed_s ≥ 0` 且 `< wall_s×2%`，11 个阶段均可检索到独立条目。

## 5. 风险与回滚

| 风险 | 缓解 |
|---|---|
| `_progress.json` 写盘拖慢主流程 | 每集 1 次小文件原子写（<1ms）；`try/except` 全包 |
| 多任务共用 workdir 互相覆盖 | 文件带 `run_id`；Console 只采纳 `run_id` 匹配的一份，否则忽略 |
| ETA 抖动/跳变 | 中位数 + 5s 刷新 + 上限约束 |
| 阶段表重排导致历史记录口径变化 | 权重重排独立为可选项（T6），可单独回滚 |

回滚：删 `_progress.json` 读写两处即可恢复现状；无 schema 迁移、无数据迁移。

## 6. 变更清单（预计）

| 文件 | 变更 |
|---|---|
| `pipeline/pipeline.py` | 新增 `_write_progress(...)`（~20 行）+ build/demucs/asr/translate 调用（4 处）；`_timing_summary` 增加 `final` 与墙钟字段（~15 行）；补 6 处 `timed_step` |
| `pipeline/batch_runner.py` | 补 5 处 `timed_step`（帧对齐/克隆/导出/质检/归档）；`finally` 里 `final=True` 收口写盘（~12 行） |
| `pipeline_ui/console/status.py` | 新增 `read_pipeline_progress(workdir)`（~25 行，只读）；`external_tasks()` 优先读进度文件（~10 行，watcher/手工任务复用） |
| `pipeline_ui/console/runner.py` | `snapshot()` 消费进度文件、写 `run.percent`/`progress_basis`/`eta_s`；`_pct_floor` 单调保护（~30 行） |
| `pipeline_ui/console/static/view.js` | 环形图下方新增 ETA 行；`progress_basis` 空值兜底（~8 行）；SSE 文案改为 `SSE 推送 · 1s 采样 · 15s 心跳`（1 行） |
| `tests/` | 新增 `test_progress_file.py`（读写/损坏降级/单调性/过期判定） |

不涉及：`app.py`（SSE 逻辑不变）、`media_normalizer.py`、`run_demucs` 参数、环境画像、`cover_watch.py`。
