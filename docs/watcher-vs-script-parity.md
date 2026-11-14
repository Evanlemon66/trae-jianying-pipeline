# watcher 链路 vs 同编排脚本链路：逐段对照

用途：给「手工用 `series_runner.py` / `pipeline.py` 跑一遍」与「交给 `cover_watch.py` 常驻调度」
两条路径做逐段核对，明确脚本链路少做了哪些保护、少做一步的后果是什么。

- 本文是只读核对文档，不描述任何待实施的代码改动。
- 核对基准：仓库当前工作区（`c48bc62` 之后）。
- 代号约定（下文表格统一使用）：
  - **W** = watcher 链路：`lib/cover_watch.py` 的轮次驱动，编辑走 `run_edit`，导出走 `run_export`。
  - **S** = 编排脚本链路：`pipeline/series_runner.py`（生成走 `batch_runner --no-export`，导出走 `robust_auto_export` 子进程）。
  - **P** = 单条直达链路：`python pipeline/pipeline.py <草稿> ...`。

两条脚本链路（S / P）共用同一批底层模块，差异主要集中在“谁来验收、谁来记账、谁来回滚”。
W 的很多保护不在导出子进程里，而在导出**前后**的调度层，所以只复用 `robust_auto_export.py` 是拿不到这些保护的。

---

## 1. 入口与入参

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 触发方式 | 常驻轮次：`cmd_watch` 每 `interval`（默认 5s）扫一轮，`settle`（默认 30s）静默确认；每轮只推进一部（`_pick` 优先级 edit→clone→export） | S：命令行一次性跑 `series_runner.py <targets> --mode full\|generate\|export`；P：命令行单条 `pipeline.py <draft>`。都无轮询、无静默确认 | 脚本链路在复制到一半、封面刚落地、ipt 刚落盘时启动，会把“未稳定”的输入当成终态 |
| 生成入参 | `batch_runner <series_dir> --force --no-export [--profile a,b] [--only N..] [--cut X]`（`cover_watch.py:1381-1389`） | S：`batch_runner <series_dir> --workdir pipeline/_work_<slug> --fresh --no-export [--cover-image 封面.png] [--cut X]`（`series_runner.py:127-142`）；P：`run_pipeline` 进程内直调 | W 用 `--force` 重建并支持 `--only` 断点续剪；S 用 `--fresh` 忽略批次状态从头跑，不传 `--only`；P 完全无批次概念 |
| 导出入参 | `robust_auto_export <draft> <out> --move --fallback-dir <dir> [--expect-duration N] [--cold-start]`（`series_runner.py:299-311`，W 与 S 共用） | S：同上，但 `cold_start` 固定 False、`keep_open` 在 export 模式/全草稿输入时为 True；P：只有 `--move --fallback-dir`（`pipeline.py:3464-3467`） | W 默认每部剧冷启动剪映（`cold_start:true`），S/P 不冷启动；P 不传 `--expect-duration`，没有时长校验与体积估算 |
| 运行环境 | `pythonw` 托管，`_ensure_streams_to_log` 兜底日志；子进程统一 `NO_WINDOW` | S/P 通常前台控制台；S 子进程输出落 `pipeline/_stream_<slug>.log`，P 只在当前终端 | W 的日志落点固定、可回溯；S/P 关掉终端就丢现场 |

---

## 2. 前置检查与闸门

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 触发门槛 | `check_trigger`：4 张封面齐、`03_prompts/prompts.json` 在、job 可解析、`titles.gate=done`、stage 可触发、`video.status!=done`、多账号待补版本（`cover_watch.py:169-220`） | 无。S 只看目录在不在、草稿能不能找到 | 脚本链路可以对“封面没齐/标题没定稿”的剧直接开工，产出无法发布的成品 |
| 字幕闸门 | `run_export` 最先执行 `jm.require_export_allowed`，`--phase export` 也绕不过（`cover_watch.py:2281-2296`） | S：`_run_export` 内 + `main` 各一次 `require_export_allowed`（`series_runner.py:326-333`、`506-519`）；P：`failed_eps ∪ subtitle_missing`（`pipeline.py:3443-3450`） | 三边都有；S 在缺字幕时置 `blocked` 而非失败，P 直接跳过导出但 `run_pipeline` 仍返回成功 |
| 台账 ⇄ 磁盘冲突 | `_output_conflicts`：`outputs.<版本>.state=exported` 但磁盘缺件，或磁盘三件套齐但台账无记录 → blocked（`cover_watch.py:754-790`） | 无 | S/P 导出的成品不会登记 `outputs`，W 下次接手会判成 `disk_present_ledger_missing` 并停住整部剧 |
| 草稿新鲜度闸门 | E1 `stale/externally_modified` → 停，提示 `--reuse-as-is`（`cover_watch.py:1362-1373`） | 无 | S/P 会直接覆盖人工改过的草稿 |
| 业务策略闸门 | `export_policy`（immediate / after_edit_queue_drained / manual）、`workflow.mode=edit_only`、edit 失败退避（`cover_watch.py:1310-1317`、`1446-1454`） | 无 | 脚本链路无法表达“先剪完再导”“等人工放行”，只能靠人记得别启动 |
| 版本退避/暂停 | `retry_gate`（真源 `.export_state.json`，按版本独立退避 + 暂停） | 无 | S 失败后下一轮脚本会立刻重来，可能连续打剪映 |
| 成品是否已导出 | `_draft_exported` fail-closed：`_export_stale.json` 一票否决；只认 sidecar valid/manual_confirmed、legacy 基线、`progress.done=true`（`cover_watch.py:632-666`） | S：`_output_exists` 只判 `isfile && size>1MB`（`series_runner.py:104-107`）；P：无判定，`do_export` 就直接导 | 这是最危险的一处不对称：草稿重建后旧 mp4 仍在，S 会当成“已导出”跳过；P 会直接覆盖 |
| 新剧宽限 | `new_job_grace_min`（默认 10 分钟，job.json 未被改过才跳过） | 无 | 脚本链路没有“让数据落稳”的缓冲 |

---

## 3. 锁

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 单剧锁 | `.watcher.lock`：`acquire_lock(job_dir, phase)` 包住整轮动作，`lock_alive` 跳过在跑任务（`cover_watch.py:296-350`、`3722-3756`） | 无 | W 与 S 可同时操作同一部剧，互相覆盖草稿/成品 |
| 全局导出锁 | `.export.lock`：`export_lock.acquire(owner="cover_watch <name>")`，失败本轮直接退出（`cover_watch.py:2327-2331`；`lib/export_lock.py`） | S：导出**不取**全局锁（`series_runner.py` 无 `export_lock` 引用）；生成段由 `batch_runner` 启动时取锁（`batch_runner.py:346-353`），但 S 未传 `--force`，所以生成期间确实持锁；P：完全不取 | 两个导出流程（W + S、S + S）可以同时打剪映；S 生成持锁、S 导出不持锁，锁语义前后不一致 |
| 生成段锁 | W 的 edit 命令带 `--force`，`batch_runner` 会跳过全局锁（`batch_runner.py:346`） | S 生成不带 `--force`，由 `batch_runner` 取全局锁 | 生成期 W 只持单剧锁，不持全局锁；S 生成持全局锁。两者并发时 W 的导出会被 S 生成挡住，而 W 的编辑不会被 S 挡住 |

---

## 4. 草稿生成与写盘

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 写盘入口 | `batch_runner` → `pipeline.run_pipeline`；所有 `draft_content.json` 写入走 `pipeline/draft_io.py::write_draft_content`（原子写 + 不变量校验 + 写时留痕） | S/P 同一套代码，同样走 `draft_io` | 这一层没有差异 |
| E1 记录 | `draft_io` 侧 `freshness.record_draft_write(job_dir, draft_path, writer)`；W 的 `writer` 覆盖 `pipeline_publish` / `pipeline_cover` 等 | S/P 同样记录；`job_dir` 由 `batch_runner` 固定为 `<series_dir>/cover_job`（`batch_runner.py:340`）或 `run_pipeline` 的 `_resolve_pipeline_job_dir`（`pipeline.py:2765-2773`） | 基本一致；S 若剧集目录里没有 `cover_job`，E1 会落在 `unknown`，W 接手后无法判新鲜度 |
| 备份策略 | `_write_pipeline_backup` 写 `_pipeline_backup.json`（防剪映自动合并串流）；发布/克隆都会刷新 | S/P 同一套代码 | 无差异 |
| 失败续跑 | `--force` + `--only`（断点续剪到指定集）；`edit_state` 记退避/暂停；看门狗 `edit_stall_timeout_s=900`（`cover_watch.py:1932-1974`） | S：`--fresh` 从头跑，无 `--only`、无看门狗、无 edit_state；P：单条直调，无批次概念 | S 中场失败只能整段重来，且卡死时无人杀进程 |

---

## 5. 封面（生成、绑定、E3 落点）

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 封面来源优先级 | 不传 `--cover-image`，由 `batch_runner` 自动探测：`cover_job/04_covers/cover_04_vertical.*` → `封面.png/jpg` → 目录任意图片 → 视频抽帧（`batch_runner.py:414-437`） | S：`<series_dir>/封面.png` 存在就优先传它，否则回落到同一套自动探测；P：由调用者显式传 `cover_image`/`cover_extract_from_video` | S 与 W 的封面优先级不同，同一部剧在两条链路可能用不同图；P 完全依赖人传参 |
| 绑定落盘 | `run_pipeline` → `set_cover_composition` + `set_draft_cover` → `_record_draft_freshness(...,"pipeline_cover")` + `_record_cover_freshness` → E3（`pipeline.py:3421-3438`） | S/P 同一套代码 | 无差异 |
| E3 槽位 | `edges.e3.versions[<版本>]`，读写优先本版本槽，平铺字段只作最近一次写的兼容副本 | 同左 | 无差异；旧 job.json 无 `versions` 时只读兼容 |
| 人工改封面后重绑 | `cover_watch.py rerun --unit cover_bind --versions a,b` 有 journal 与回滚 | S/P 无对应入口，只能整条重跑 | S/P 改封面后无法只补绑定，容易顺带重建草稿 |

---

## 6. 导出命令拼接与执行

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 命令拼装 | `run_export` → `sr._run_export` → `sr._export_cmd`（W 与 S 共用同一份拼装；`--expect-duration` 也在其中） | S 同上；P 自己拼（无 `--expect-duration`） | W/S 的命令可逐字对齐；P 是另一份实现，容易漂移 |
| 冷启动 | 读 `presets.json watch.cold_start`，默认 true，传 `--cold-start`（`cover_watch.py:2396`） | S：`_run_export(..., cold_start=False)` 默认值，不传；P：不传 | 上一部刚导完、UI 假死时，S/P 更容易首启失败 |
| 子进程与日志 | `subprocess.Popen`，stdout 落 `cover_job/export_console.log`，记 cmd/pid/exit（`series_runner.py:341-373`） | S 共用；P 用 `subprocess.call`，只继承当前终端 | P 没有落盘现场，失败后无法复盘 |
| 看门狗/超时 | 按进度 json mtime 判“无进展”，超 `export_stall_timeout`（默认 1800s）杀进程 | S 共用同一个 `_run_export`；P 无超时，`subprocess.call` 永久阻塞 | P 卡死时只能人工杀进程 |
| `keep_open` 与实例复用 | W：每份导出完由子进程自己关，`keep_open=False` | S：export 模式或全草稿输入时 `keep_open=True`，队列末尾 `_close_jianying_after_batch`（`series_runner.py:434-441`、`537-540`） | S 的复用语义只有 W 没有对应分支；复用失败时 S 会整段回落到冷启动（无冷启动参数，风险更高） |
| `died_late` | 拉起层 `jianying_launch` 只对 `died/died_after_ready` 吃重拉预算，晚死不重拉；W 另有 3 轮跨轮重拉（`relaunch_state`） | S：无跨轮重拉，子进程返回非 0 即判失败 | S 遇到拉起失败只记 `export_failed`，不会像 W 那样下一轮优先重拉 |
| 质检 | 导出成功后 `verify_export.py`，rc!=0 不登记成品（`cover_watch.py:2460-2477`） | S：无 verify；P：无 verify | 帧对齐/成品结构问题会直接进入发布目录 |

---

## 7. 预期体积与进度

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 预期体积 | `_expected_export_bytes`：上一版成品（`outputs.<版本>.size_bytes`）→ 归档副本 `.v1/.v2` → 上次完成进度记录 → `--expect-duration × 码率`（`robust_auto_export.py:305-345`） | S：共用同一函数；但 S 不归档、不登记 `outputs`，第一档常落空，主要靠 `--expect-duration`；P：不传时长，只剩“上次进度记录” | P 首版几乎必然“无估算”，25/50/75 里程碑全不弹 |
| `total_source` | 写入 `*.progress.json`，标明体积口径 | S 同左（共用导出器）；P 同左，但口径多为“无估算” | 观察进度时 W/S 可核对口径，P 只能看“无估算” |
| 里程碑 | 导出器按 `_estimate_percent` 打 25/50/75；估算不出就跳过，不编百分比 | S 共用；P 共用 | 差异来自入参（时长/体积），不是代码分支 |
| 时间驱动通知 | 导出器每 300s 一条；W 另有阶段 toast（开始/失败/完成，带正式路径与字节数） | S 只有导出器通知 + 队列级 toast；P 无 | W 的通知能和最终文件对上；S/P 的“完成”提示不带台账校验 |

---

## 8. 产物校验

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 三件套 | `_missing_artifacts(strict_progress=True)`：mp4、`-封面.jpg`、`mp4.progress.json` 缺一判失败（`cover_watch.py:2446-2459`） | S：只判 `os.path.isfile(out_mp4)`（`series_runner.py:375-380`）；P：只看子进程 rc | 缺封面/缺 progress 的“半套成品”会被 S/P 当成成功 |
| mtime 属本轮 | `_success_evidence_missing`：正式名 mp4 与 progress 的 mtime 必须晚于本轮 `export_started-2s`，且 `progress.done=true`（`cover_watch.py:724-751`） | S/P 均无 | 旧成品/旧 progress 冒充本轮结果时，W 能挡住，S/P 挡不住 |
| 质检 | `verify_export.py` 逐份跑 | 无 | 同上 |
| 缺件处置 | 判失败 → 状态机退避/重试；不 `record_video`、不 `succeed`、不弹完成 | S：返回 False → `export_failed`，继续下一部；P：打印后继续，退出码由调用链决定 | S/P 的“失败”没有退避，下一轮脚本可能立即重来 |

---

## 9. freshness（E1 / E2 / E3）

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| E1 | 生成时经 `draft_io` 记录；`build_plan` 消费 `externally_modified` | S/P 同样记录，但**不消费**：不因 E1 stale 停下 | 人工改过的草稿会被 S/P 无提示覆盖 |
| E2 | 成功收尾 `freshness.record_export(job_dir, 版本, draft, mp4)`，写 sidecar `<mp4>.freshness.json` + `edges.e2.versions[版本]`（`cover_watch.py:2519-2527`） | S/P 均不写 | S/P 导出的成品没有出身证据，W 接手只能靠 `progress.done` 兜底，且很可能先被 `_export_stale.json` 判为未导出 |
| `manual_confirmed` | 通过 `lib/freshness.py --reuse-as-is` 写；W 的 `_draft_exported` 认它 | S/P 无入口 | 脚本链路无法把“人工确认这版可用”传给调度层 |
| E3 | 生成/绑定时按版本落 `edges.e3.versions[<版本>]` | S/P 同左（共用 `pipeline`） | 无差异 |

---

## 10. 台账（record_video / outputs / stage）

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| `record_video` | 逐版本 `jm.record_video(...)`，写 `video.outputs[<版本>]`（导出目录、导出文件、size、exported_at、state），并推进 stage（`cover_watch.py:2506-2514`） | S：只写自己的 `pipeline/_series_queue_state.json`（`status=done/export_failed`）；P：不写任何台账 | 脚本链路跑完后 job.json 仍是“未导出”，W 接手会判台账冲突或重复导出 |
| 顶层聚合 | 所有声明版本完成后才 `note_export_result(ok)` + `_derive_video_status`，`video.status=done` 由真源派生 | 无 | 多账号场景下 S/P 无法表达“A 完成、B 未完成” |
| 阶段推进 | `record_video` / `ingest_job` 推进 `stage` | S 生成不调用 `ingest_job`，stage 停在原处；P 同 | 台账与磁盘长期不一致 |
| 状态文件 | `.export_state.json`（导出真源）+ `.edit_state.json`（编辑真源） | `_series_queue_state.json`（独立状态，与真源无同步） | 两套状态互不知情，排障时容易各说各话 |

---

## 11. stale 标记（`_export_stale.json`）

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 谁写 | `pipeline.publish_draft` 发布后写 `mark_export_stale(target,"publish")`（`pipeline.py:341-342`）；W 的 edit 同样经过这里 | S 生成经过 `publish_draft`，同样会写；P 同 | 脚本链路生成完必然留下 stale 标记 |
| 谁清 | 导出成功后 `jp.clear_export_stale(<draft_dir>)`（`cover_watch.py:2519-2520`）；`batch_runner` 自带导出路径也清（`batch_runner.py:253-254`） | S 的导出走 `robust_auto_export`，不经过 `batch_runner` 的清理；P 同 | S/P 导出成功后 stale 仍在：W 下次看到会判“未导出”，把刚做好的 mp4 归档成 `.vN` 再重导 |
| 重导前归档 | 仅当“成品存在且 `export_is_stale`”时 `archive_stale_outputs` 三件套改名 `.vN`（`cover_watch.py:2372-2387`、`pipeline.py:2669-2705`） | S/P 从不归档；覆盖/替换由导出器 `--move` 决定 | 版本历史在脚本链路会断档；W 接手时归档的可能是 S 刚产出的成品 |
| 不清的连锁反应 | `_draft_exported` 一票否决 → `build_plan` 认为未导出 | — | 叠加“台账无记录”，W 会先命中 `disk_present_ledger_missing` 冲突并 blocked |

---

## 12. 失败处理

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 失败分类 | 读子进程落盘的 `failure_class`（权威），缺失则 `fc.classify`（`cover_watch.py:2399-2404`） | S：无分类，只记 `export_failed`；P：打印异常 | S/P 无法区分“重试有用/没用”，人工只能翻日志 |
| 重试预算 | `es.fail` 1/2/4/8/16/30 分钟退避；连续 5 次暂停（`retry_pause_after`） | S：无退避、无暂停；靠 `--reset` 部间 sleep | 失败会变成“立刻重来”的忙等 |
| 拉起重拉 | 跨轮重拉最多 3 轮，到顶转 failed；`died_late` 不重拉 | S/P 无 | 剪映启动抖动时 S 需要人工重跑 |
| 终态 | 不可重试类（素材缺失/磁盘满）直接 failed，不计数、不空转 | S 置于 `export_failed` 后继续下一部 | S 不会把整条队列停下 |
| 未捕获异常 | 杀本进程派生子进程树 → 登记失败 → toast（`cover_watch.py:3738-3757`） | S：异常直接冒泡，子进程可能残留；P：`try/except` 打印“请手动导出” | W 能保证“异常也计数”，S/P 可能留下孤儿进程 |
| 退出码 | 常驻进程，不退出 | S：有失败返回 1；P：导出失败也只打印，进程仍成功返回 | 外部调度器无法从 P 的退出码判断失败 |

---

## 13. 并发与回滚

| 项目 | W | 编排脚本（S / P） | 差异与后果 |
|---|---|---|---|
| 与 watcher 并行 | 单剧锁 + 全局导出锁；`--phase` 人工放行前校验代码指纹 `check_release_safe`（`cover_watch.py:479-518`、`3637-3644`） | 无锁、无指纹校验 | S 可与 W 同时跑同一部剧；S 跑旧代码也不会被拦 |
| 与 pipeline_ui / 另一次 S 并行 | 全局导出锁让第二方直接退出/等待 | S 的导出不取锁，可两份同时导 | 剪映同时接两个导出，成品/草稿状态互相污染 |
| 旧成品归档 | `.vN` 三件套整体改名，失败整组回滚（`archive_stale_outputs`） | 无归档、无回滚 | S 覆盖后无法回到上一版 |
| 草稿发布回滚 | 旧草稿移出剪映扫描根到 `_draft_trash`，发布成功再删；失败 rename 回来（`pipeline.py` `_draft_trash_dir`） | S/P 共用发布代码 | 这一层无差异 |
| 人工回滚 | `cover_watch.py rerun --rollback <journal>` / `--resume` | 无 journal、无回滚点 | S 只能整条重跑 |
| 残留清理 | `_preclean_bundles` + `_consolidate_bundle`，保留含 mp4 的目录 | S 共用；P 无（直接写导出目录） | P 容易留下 `剧名(1)` 去重目录 |

---

## 编排脚本相比 watcher 缺失的关键保护（按危险程度排序）

1. **导出成功后不清 `_export_stale.json`，也不写 E2 sidecar**（S、P）。
   后果：脚本产出的成品在 W 眼里永远“未导出”；W 接手会把它归档成 `.vN` 再重导，
   或者先被台账冲突挡住。属于“看起来成功、实际会被判废”的静默错账。
2. **`_output_exists` 只认 `size>1MB`，不认 stale / freshness**（`series_runner.py:104-107`）。
   后果：草稿重建后旧 mp4 仍在，S 会直接跳过导出，发布旧视频。
3. **导出不取 `.export.lock`**（S；P 也无）。
   后果：与 watcher / 另一次脚本并发时同时操作剪映，成品与草稿状态互相污染。
4. **不写 `record_video` 台账**（S、P）。
   后果：`outputs.<版本>` 永远没有 size/exported_at/state，W 的 `_output_conflicts` 判
   `disk_present_ledger_missing` 并 blocked；多版本状态无法表达。
5. **不做三件套 / mtime / `progress.done` 校验，也不跑 `verify_export`**（S、P）。
   后果：缺封面、旧文件冒充、帧对齐漂移都会被当成成功。
6. **不消费 E1 `externally_modified`**（S、P）。
   后果：人工改过的草稿被无提示覆盖。
7. **无失败分类、退避、暂停与跨轮重拉**（S、P）。
   后果：失败即忙等或整条队列带病继续；拉起抖动只能人工重跑。
8. **无单剧锁**（S、P）。
   后果：与 W 的 `run_edit` / `run_export` 可同时改同一部剧。
9. **不归档旧成品 `.vN`**（S、P）。
   后果：版本历史断档，覆盖后无法回退。
10. **P 不传 `--expect-duration`、无超时、无落盘日志**（P）。
    后果：首版进度无估算、卡死无人杀、失败无现场。

---

## 核对方式（只读）

| 段 | 核对点 |
|---|---|
| 1-2 | `lib/cover_watch.py` 的 `check_trigger` / `build_plan` / `_edit_cmd` / `_export_cmd`；`pipeline/series_runner.py` 的 `_run_pipeline` / `main` |
| 3 | `lib/cover_watch.py::acquire_lock` / `run_export`；`lib/export_lock.py`；`pipeline/batch_runner.py:346-353` |
| 4-5 | `pipeline/batch_runner.py:340,414-437`；`pipeline/pipeline.py:3340-3348,3421-3438`；`pipeline/draft_io.py` |
| 6-8 | `pipeline/series_runner.py:299-384`；`lib/cover_watch.py:2274-2558` |
| 9-11 | `lib/cover_watch.py:632-666,2506-2527`；`pipeline/pipeline.py:2637-2705,341-342` |
| 12-13 | `lib/cover_watch.py:2399-2490,3462-3531,3631-3763`；`lib/export_state.py`；`lib/export_lock.py` |
