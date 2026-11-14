# 流水线稳定性风险台账

> 盘点范围：`lib/`、`pipeline/`、`pipeline_ui/`、`scripts/`、`.trae/skills/jianying-editor/` 及 `specs/`。
> 生成时间：2026-11-14。基于当前工作区（含未提交改动）。
> 本次仅做只读核查：未修改代码、未启停剪映或 watcher、未运行导出、未触碰 `D:\短剧` 下 job 数据。盘点时 B 版导出正在进行。
>
> 排序原则：按“会不会静默产出错误成品”从高到低，不按改动成本排序。
> 证据不足的条目已显式标注“未验证”，不写成结论。

## 总览

| ID | 优先级 | 风险 | 是否可能静默产出错误成品 |
|---|---|---|---|
| R01 | P0 | 导出收尾“假完成/假成功”回归 | 是 |
| R02 | P0 | 复用判定退化成“有 mp4 就算已导出” | 是 |
| R03 | P0 | `--profile b` 映射到基础草稿 A | 是 |
| R04 | P0 | 剪映“恢复备份”复活旧坏稿 | 是 |
| R05 | P0 | 工作目录/素材缺失的草稿仍可导出 | 是 |
| R06 | P1 | freshness E2 单槽，A/B 互相覆盖证据 | 是（sidecar 缺失时） |
| R07 | P1 | 剪映退出回写被判“外部修改” | 否（多为误报 stale，但可能诱导人工放行错稿） |
| R08 | P1 | 冻结基线需人工 `--append`，漏写/误写会变哑 | 是（误 append 时） |
| R09 | P1 | 归档/clean 对 stale 一律放行 | 是 |
| R10 | P1 | 串流自动合并只在“腰斩”时恢复，部分合并不处理 | 是 |
| R11 | P2 | 导出中断留下 `status=exporting` 死记录 | 否（阻塞可用性） |
| R12 | P2 | 拉起后中途自退（约 118s）只重试一次 | 否（空等/失败） |
| R13 | P2 | UIA 依赖中文窗口名/COM 初始化 | 否（探测失败） |
| R14 | P2 | 进度百分比依赖上一版体积，首版无估算 | 否（通知缺失） |
| R15 | P2 | 长片单文件导出无断点续传 | 否（重来，不产出错成品） |
| R16 | P2 | `.removed_` 同名残留卡片可能被 UIA 命中 | 是（打开/导出错草稿） |

---

## R01 导出收尾“假完成/假成功”回归

- **风险描述**：导出收尾的“跳过判定”和“强制重导”曾经互相抵消，出现“没有真导出，却登记 exported 并弹完成”的假成功。现已修，但同一段逻辑仍是全链路最易回归的位置。
- **触发条件**：`--phase export` 强制重导；`_draft_exported` 的判定结果与 `targets` / `forced` 集合不一致；`targets` 为空但 `remaining` 也为空的分支。
- **影响面**：job 台账 `video.status=done`、UI 弹“导出完成”，实际 mp4 是旧文件或根本不存在；后续发布会静默使用旧成品。
- **当前已有的缓解**：
  - `plan["force_reexport"] = True` 显式标记强制重导，`run_export` 据此不得按 `_draft_exported` 跳过。
  - 跳过分支只写 `skipped` 日志，不登记 `exported`、不 `record_video`、不 `succeed`、不 `record_export`、不弹完成。
  - `targets` 为空且无剩余版本时才收尾；仍有 `remaining` 时只记日志。
- **剩余风险**：判定、跳过、登记、通知仍耦合在同一函数；空 `targets` 分支仍会 `note_export_result(..., True)`；新增分支很容易再次出现“跳过 + 成功”并存。
- **优先级**：P0。
- **建议动作**：为“skip ⇒ 不得 record/succeed/通知”加不变量测试；把“是否真的导出”收敛为单一返回对象；完成通知只在 `verify_export` 通过后发出。
- **证据**：
  - `lib/cover_watch.py:1396-1399`（强制重导标记及“假成功”注释）
  - `lib/cover_watch.py:2294-2326`（forced 集合、空 targets 收尾、跳过只记日志）

## R02 复用判定退化成“有 mp4 就算已导出”

- **风险描述**：`_draft_exported` 依赖三层判据：`_export_stale.json` 标记 → freshness `valid` → “mp4 存在 + progress.done”。任一前置证据缺失就会退化到最后一层，最终 fail-open。
- **触发条件**：草稿目录 `_export_stale.json` 丢失或写入失败；freshness job 缺失/状态 unknown；旧版产物没有 `.progress.json`；同名旧 mp4 残留。
- **影响面**：改完预设重建同名草稿后，旧 mp4 被当成新成品直接跳过，后续发布旧视频。
- **当前已有的缓解**：
  - `_export_stale.json` 命中直接判未导出。
  - freshness `consumer="scheduler"` 为 `valid` 时判已导出。
  - progress 标记 `done` 才判完成。
- **剩余风险**：三层证据都缺失时直接 `return True`（“旧版无 progress：有成品即视为已完成”）；`mark_export_stale` 写失败只 `warn`；`clear_export_stale` 失败也只 `warn`。
- **优先级**：P0。
- **建议动作**：无 progress 时返回 unknown 而不是 done；标记写入失败升级为 job 失败；对“只有 mp4”的复用场景强制人工确认。
- **证据**：
  - `lib/cover_watch.py:628-653`
  - `pipeline/pipeline.py:2649-2660`（写标记失败仅告警）
  - `pipeline/pipeline.py:2671-2678`（清标记失败仅告警）

## R03 `--profile b` 实际指向基础草稿 A

- **风险描述**：旧版 `_export_targets` 假定 `profiles[0]` 一定是基础版本 `a`，只有 `profiles[1:]` 才加 `_<版本>` 后缀。`--profile b` 过滤后 `profiles=['b']`，目标退化成基础草稿 A，导致“导 b 实际导 a”。
- **触发条件**：调用 `_export_targets(base, ["b"])` 时未显式传 `base_profile`；任何新增调用方沿用默认值 `"a"`。
- **影响面**：B 版成品文件内容实为 A，文件名/台账却记录为 B；属于静默错版。
- **当前已有的缓解**：
  - `_export_targets` 已改为三参，显式接收 `base_profile`。
  - `build_plan` 与 `run_export` 均从 job 声明集合取基础版本后传入。
  - `tests/test_export_targets_profile.py` 覆盖 b-only、a-only、a+b、克隆缺失四种情况。
- **剩余风险**：该修复位于当前工作区未提交改动（`git status` 显示 `M lib/cover_watch.py`），尚未进入 HEAD；参数仍有默认值 `"a"`，未来调用方漏传会重新 fail-open；`plan["base_profile"]` 未设置时回退 `_job_profiles()[0]`，依赖声明顺序。
- **优先级**：P0。
- **建议动作**：把 `base_profile` 改为必填参数（去掉默认值）；提交修复并补一条真实 `--profile b` 的计划/目标/台账端到端测试。
- **证据**：
  - `lib/cover_watch.py:798-815`（三参映射与注释）
  - `lib/cover_watch.py:1396`、`lib/cover_watch.py:2292-2302`（调用方传参）
  - `tests/test_export_targets_profile.py:29-45`

## R04 剪映“恢复备份”复活旧坏稿

- **风险描述**：草稿目录残留的备份文件会让剪映下次打开时判定“上次未正常结束”并弹恢复提示；人工点“恢复备份”可把旧内容复活成 live 草稿。
- **触发条件**：异常退出/崩溃留下 `draft_content.json.bak`；流水线自有的 `_pipeline_backup.json`、`.before_restore.bak` 长期留在草稿目录；用户在剪映 UI 选择恢复。
- **影响面**：live 草稿回退到修复前内容（坏时间轴、合并前的串流段），随后导出静默错成品；也可能被 freshness 判成外部修改而阻断。
- **当前已有的缓解**：
  - `cleanup_draft_temp_files` 清理 `*.tmp`、`draft_content.json.bak`、`draft_meta_info.json.bak`。
  - 发布成功后清理暂存区空壳。
  - 发布时写 `_pipeline_backup.json`，导出前可用于比对/恢复串流段。
- **剩余风险**：`_pipeline_backup.json` 与 `.before_restore.bak` 不在清理名单，会长期留在草稿目录；恢复入口在剪映 UI，代码无法阻止；“恢复备份”具体会读哪个文件未验证（代码注释只说明会弹恢复提示）。
- **优先级**：P0。
- **建议动作**：把自有备份移出草稿目录或纳入清理白名单；导出前强制对比 `_pipeline_backup.json` 与 live 段数，发现回退自动恢复并告警；在操作手册中明确禁止在流水线草稿上点“恢复备份”。
- **证据**：
  - `pipeline/pipeline.py:241-268`（备份文件清理与“会弹恢复提示”注释）
  - `pipeline/pipeline.py:366-377`（`_pipeline_backup.json` 写入）
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1671-1679`（`.before_restore.bak`）
  - 未验证：剪映 UI“恢复备份”的实际取文件规则。

## R05 工作目录/素材缺失的草稿仍可导出

- **风险描述**：剪映草稿只记录素材绝对路径，不复制素材。工作目录被清理后，草稿仍留在剪映索引里，点开大面积媒体缺失，导出可能静默出黑帧/缺音。
- **触发条件**：清理工作目录、搬移目录、删除人声分离中间产物；`prune_workdirs.py --apply`；草稿目录缺少 `draft_content.json` 但仍在剪映索引中。
- **影响面**：草稿“能打开”但素材缺失；导出成品缺画面/缺音，且不一定报错。用户现场记录某 146 集草稿 293 个素材路径中 292 个不存在（未验证）。
- **当前已有的缓解**：
  - `referenced_media_paths()` 扫描草稿引用后再清理；解析失败返回 `None`，调用方放弃清理。
  - `clean_workdir(protect_referenced=True)` 默认保护仍被引用的文件。
  - `prune_workdirs.py` 只删未被任何草稿引用的文件，默认 dry-run。
- **剩余风险**：缺少 `draft_content.json` 的草稿目录被直接 `continue`，其素材会被判为“未引用”；剪映索引与草稿目录不同步；导出前没有集中式素材存在性校验（未验证是否有其他检查）。
- **优先级**：P0。
- **建议动作**：把“缺稿目录”视为不可判定并阻断清理/导出；导出前抽检素材路径存在性；与剪映索引做一次对账，清理索引中的空壳草稿。
- **证据**：
  - `pipeline/pipeline.py:3508-3532`（引用扫描、缺失稿目录跳过、解析失败放弃）
  - `pipeline/prune_workdirs.py:68-85`（只删除未引用文件）
  - 未验证：146 集 / 293 素材 / 292 缺失的现场数据未在仓库内复核。

## R06 freshness E2 单槽，A/B 互相覆盖证据

- **风险描述**：job 级 `edges.e2` 只有一个槽，A/B 两个版本共用。任一版本写草稿都会把 E2 置 stale，最后一次导出会覆盖 `output` 记录，导致另一版本的证据被误判。
- **触发条件**：同 job 多版本先后编辑/导出；某个版本的 `.freshness.json` sidecar 缺失；`record_draft_write` 写入任一版本草稿。
- **影响面**：刚导出的另一版被判 stale（重复导出、阻塞）；sidecar 缺失时可能用最后覆盖的记录误判 valid，静默复用旧成品。
- **当前已有的缓解**：
  - 每成品 sidecar 记录 `version` 与 `size_bytes`，优先校验版本与体积。
  - 草稿目录 `_export_stale.json` 优先判 stale。
- **剩余风险**：`default_freshness` 只定义一个 `EDGE_E2`；`record_draft_write` 无版本地置 stale；`record_export` 直接覆盖单槽；sidecar 缺失时的 record 兜底分支不校验版本。
- **优先级**：P1。
- **建议动作**：E2 按版本分槽（如 `edges.e2[version]`）；或在 record 兜底前校验记录版本；补 A/B 交叉导出测试。
- **证据**：
  - `lib/freshness.py:276-278`（单槽定义）
  - `lib/freshness.py:681-684`（写草稿无版本置 stale）
  - `lib/freshness.py:783-791`（导出覆盖单槽）
  - `lib/freshness.py:1214-1236`（sidecar 有版本校验，record 兜底无版本校验）

## R07 剪映退出回写被判“外部修改”

- **风险描述**：剪映打开/退出会重新序列化草稿，5.9 还会自动合并串流 video segments。SHA/内容变化可能超出 `serialization_only` 的容忍范围，被判 `externally_modified`。
- **触发条件**：打开草稿后退出；剪映版本优化合并 segments；字段重排/补默认值超出 weak hash 覆盖范围。
- **影响面**：误判 stale → 反复重导/阻断；若操作员用 reuse-as-is 强行放行，合并导致的音画不同步会被带进成品。
- **当前已有的缓解**：
  - canon 排除易变字段；strong hash 不同但 weak hash 相同 → `valid(serialization_only)`。
  - 写草稿日志命中 → `valid(write_log_hit)`。
  - `_restore_streams_from_backup` 在打开前用 `_pipeline_backup.json` 检测段数骤降并恢复。
- **剩余风险**：weak hash 无法容忍语义变化；串流恢复只在当前段数 `< 备份段数/2` 时触发；无法区分“剪映回写”和“人工修改”。
- **优先级**：P1。
- **建议动作**：记录剪映退出前后的 draft diff 摘要并入库；把已知“剪映回写”字段纳入 allowlist；串流检测改为段数 + 总时长双指标。
- **证据**：
  - `lib/freshness.py:994-1001`
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1594-1601`
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1657-1663`

## R08 冻结基线需人工 `--append`，漏写/误写会变哑

- **风险描述**：sidecar 上线前的历史产物靠一份冻结基线按 `legacy(baseline)` 放行。基线不会自动扩充，必须人工 `--append`；漏写会让新历史产物落入 `evidence_missing`，为消警而误 append 则永久降级为 legacy（只标注不告警）。
- **触发条件**：新发现 sidecar 之前导出的产物但未 append；手改基线文件导致 `entries_digest` 不符；把实际已 stale 的成品 append 进基线。
- **影响面**：漏写 → scheduler 阻断/告警噪音；误 append → 该成品后续变化也不再判 stale，检测能力实质变哑；digest 不符 → 整份基线失效，全部退回 `evidence_missing`。
- **当前已有的缓解**：
  - 基线只读、只增不删，默认 dry-run；`--force` 需显式指定。
  - `entries_digest` 校验，手改即整份失效（fail-closed，宁可误报）。
  - 命中需 job 身份 + 版本 + 成品 size 三者一致。
- **剩余风险**：无自动扩充、无覆盖率告警；append 的取舍完全靠人；整份 digest 失效时没有分级降级。
- **优先级**：P1。
- **建议动作**：增加基线覆盖率报告与 `evidence_missing` 连续出现告警；append 前强制 dry-run 审阅；CI 校验 digest 与条目来源。
- **证据**：
  - `scripts/freshness_baseline.py:4-19`（人工维护说明）
  - `lib/freshness.py:1032-1036`（legacy(baseline) 语义）
  - `lib/freshness.py:1056-1088`（digest 校验）
  - `lib/freshness.py:1259-1283`（命中 legacy / 否则 evidence_missing）

## R09 归档/clean 对 stale 一律放行

- **风险描述**：freshness 消费策略中，`stale` 与 `evidence_missing` 对 `archive`/`clean` 都是 `allowed=True`；归档只记录快照和墓碑，不阻断。
- **触发条件**：成品被判 stale 或 evidence_missing 后执行归档/清理。
- **影响面**：过期或无证据的成品被搬进存档，下游取到错版本；墓碑只能追溯，不能阻止。
- **当前已有的缓解**：
  - 归档写 `freshness_archive_snapshot.json`，登记 `evidence_missing`。
  - 删除成品前写墓碑，保留成品 hash / 草稿 hash / 删除原因。
- **剩余风险**：没有阻断、没有人工确认；“不判定”是显式设计。
- **优先级**：P1。
- **建议动作**：归档默认阻断 stale/evidence_missing，需 `--allow-stale` 加理由；批量层汇总 stale 数量并提示。
- **证据**：
  - `lib/freshness.py:1285-1293`
  - `pipeline/archive_job.py:407-417`
  - `pipeline/stream_manifest.py:637-654`

## R10 串流自动合并只在“腰斩”时恢复，部分合并不处理

- **风险描述**：`_restore_streams_from_backup` 只在当前段数小于备份段数一半时恢复；部分合并（例如 117 段合成 70 段）不会被识别。恢复写入还是非原子 `json.dump`。
- **触发条件**：剪映 5.9 打开草稿自动合并串流；合并比例不足 50%；恢复过程中进程中断。
- **影响面**：音画不同步的草稿被导出，静默产出错成品；非原子写可能留下半截草稿。
- **当前已有的缓解**：
  - 发布时写 `_pipeline_backup.json`，导出前比对各轨段数。
  - 写前再复制一份 `.before_restore.bak`，备份失败则跳过恢复。
- **剩余风险**：50% 阈值是经验值；部分合并完全绕过；非原子写没有回滚；恢复后未登记 freshness。
- **优先级**：P1。
- **建议动作**：改为“段数 + 总时长”双条件检测；使用临时文件 + `os.replace` 原子写；恢复后调用 `record_draft_write`。
- **证据**：
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1651-1682`

## R11 导出中断留下 `status=exporting` 死记录

- **风险描述**：导出子进程被杀/崩溃后，`.export_state.json` 可能停在 `exporting`；心跳停摆需超过 180s 才判 stale，而 `reap_stale` 只在导出执行链调用，没有后台定时回收。
- **触发条件**：导出进程崩溃/被终止；watcher 未走到下一次 export；job 处于 paused/failed 不走 reap。
- **影响面**：该 job 长时间显示“导出进行中”并阻塞调度；需要等下一轮导出或手工处理。
- **当前已有的缓解**：
  - 心跳取 `max(heartbeat_ts, progress_mtime)`，子进程还在写盘就不会误回收。
  - `STALE_AFTER_S=180`，`reap_stale` 落盘回收到 `retrying`。
  - `gate` 在内存中也会把 stale exporting 推断为 retrying；`--phase export` 可人工强放行。
- **剩余风险**：没有独立的后台 reaper；reap 只在真正进入导出执行链时发生，被 gate 拦下的 job 不会触发；手工删除状态文件会丢失败计数。
- **优先级**：P2。
- **建议动作**：watcher 每个 tick 对全部 job 调一次 `reap_stale`；导出超过 N 分钟告警；提供只清理 stale 状态的 CLI。
- **证据**：
  - `lib/export_state.py:33-37`（崩溃恢复语义）
  - `lib/export_state.py:68`（180s 阈值）
  - `lib/export_state.py:316-339`（reap 实现）
  - `lib/export_state.py:582-614`（gate 与人工放行）
  - `lib/cover_watch.py:2263`（当前唯一 reap 调用点）

## R12 拉起后中途自退（约 118s）只重试一次

- **风险描述**：拉起后 20s 内消失算“启动即死”，重拉 1 次；超过 20s 才退出归为 `died_late`，不触发早退重试。现场观测进程约活 118s 后自退，只能靠上层 attempt 和 `open_draft` 墙钟兜底。
- **触发条件**：剪映启动约 2 分钟后自退；存活时间超过 `EARLY_DEATH_WINDOW_S=20s`。
- **影响面**：空等 `open_draft` 墙钟 120–240s（现场约 3.5 分钟）后失败，长队整体变慢。
- **当前已有的缓解**：
  - 早退 20s 内重拉 1 次，预算全周期共用。
  - `open_draft` 墙钟按集数放宽：基础 120s，90 集后每集 +0.5s，上限 240s。
  - 上层 UI 类失败可重试（默认共 3 次），实例管理器统一拉起/关闭。
- **剩余风险**：`died_late` 不重拉；墙钟是纯等待；118s 与 3.5 分钟为现场观测，未在仓库日志中复核。
- **优先级**：P2。
- **建议动作**：`died_late` 也在预算内重拉一次；在 60/90s 加健康探针提前失败；记录 `alive_s` 分布用于定阈值。
- **证据**：
  - `lib/jianying_launch.py:48-57`（20s 窗口、1 次预算）
  - `lib/jianying_launch.py:874-883`（`died_late` 不进入重试）
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1185-1228`（open_draft 墙钟）
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1876-1889`（上层重试循环）
  - 未验证：118s / 3.5 分钟为现场观测。

## R13 UIA 依赖中文窗口名/COM 初始化

- **风险描述**：部分 UI 操作依赖中文窗口名“剪映专业版”和固定 ClassName；`uiautomation` 的 COM 初始化异常会影响探测。
- **触发条件**：英文/其他语言 UI；剪映版本修改 ClassName；线程未初始化 COM 或 `CoInitialize` 报错。
- **影响面**：找不到窗口/草稿卡片，剪辑或导出失败，重试空转。
- **当前已有的缓解**：
  - ClassName 优先、中文名回退。
  - `Find Control Timeout`、`COMError` 显式归入 `ui_hang`。
  - 每次 UIA 探测带独立线程超时，整体有墙钟 deadline。
- **剩余风险**：中文窗口名硬编码；`CoInitialize` 报错没有专门的初始化/重试代码（未验证）；ClassName 随剪映版本变化。
- **优先级**：P2。
- **建议动作**：改为按进程 exe/pid 定位窗口；显式初始化并重建 COM；启动时做一次窗口探测自检。
- **证据**：
  - `pipeline_ui/jianying_ui.py:16-22`、`pipeline_ui/jianying_ui.py:46-50`
  - `lib/failure_class.py:113-120`
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:522-548`
  - 未验证：`CoInitialize` 报错的具体触发条件与堆栈。

## R14 进度百分比依赖上一版体积，首版无估算

- **风险描述**：百分比优先用上一版成品体积，其次用上次 `done` 进度记录，最后才用时长估算。watcher/batch 路径调用导出时不传 `--expect-duration`，首版没有旧成品时只能显示“无估算”。
- **触发条件**：首次导出（无旧 mp4）；`series_runner._run_export` 未传 `--expect-duration`。
- **影响面**：无百分比 → 25%/50%/75% 里程碑通知被跳过，只剩 300s 时间驱动通知；看门狗首字节宽限固定 420s。
- **当前已有的缓解**：
  - 三级估算链（上一版文件 → 上次 done 记录 → 时长×码率）。
  - 每 30s 写进度文件与日志；无百分比时明确写“无估算”，不造假。
  - `pipeline_ui` 路径会传 `--expect-duration`。
- **剩余风险**：watcher 路径不传时长；首版必然无参照；UI 与 watcher 两条路径行为不一致。
- **优先级**：P2。
- **建议动作**：从草稿总时长计算并传入 `--expect-duration`；或按分辨率码率估算；统一 UI/watcher 两条调用路径的参数。
- **证据**：
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:277-309`
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:342-365`
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1033-1043`
  - `pipeline/series_runner.py:298-303`（未传 `--expect-duration`）
  - `pipeline_ui/ui_pipeline.py:421`（UI 路径传了）

## R15 长片单文件导出无断点续传

- **风险描述**：长片单个 mp4 导出没有断点续传；中断后上层 attempt 从零重跑，不接管上一轮的半成品。
- **触发条件**：8.7GB 量级导出中断；看门狗判卡死；剪映崩溃/被关闭。
- **影响面**：整段重来，重复耗时与磁盘峰值；多次失败可能耗尽重试预算。
- **当前已有的缓解**：
  - 看门狗按“产物无增长”判卡死，失败分类后重启重试。
  - 分片转正式文件导致体积回落时仍算有进度。
  - 本轮内可接管实际在增长的 mp4（仅限本次 `start_time` 之后）。
- **剩余风险**：不接管上一 attempt 的半成品；没有 offset/分片续传；8.7GB 为现场观测（代码注释示例中出现 8.7 GB）。
- **优先级**：P2。
- **建议动作**：记录中途产物，重启后按时长/大小校验决定是否续用；或按剧集拆分导出后再合并。
- **证据**：
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:992-1006`
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1010-1030`
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1685-1692`
  - `.trae/skills/jianying-editor/examples/robust_auto_export.py:1876-1889`

## R16 `.removed_` 同名残留卡片可能被 UIA 命中

- **风险描述**：发布/重建会生成 `<draft_name>.removed_<ts>` 残留目录，其中的 `draft_meta_info.json` 仍写着原草稿名，剪映首页可能多出一张同名卡片；UIA 按 Name 严格匹配时可能命中废弃卡片。
- **触发条件**：发布/重建失败留下 `.removed_` 目录；目标目录不存在时清理逻辑直接跳过。
- **影响面**：打开或导出错误草稿（旧内容），静默产出错成品；也可能反复命中失败。
- **当前已有的缓解**：
  - 发布成功后 `_cleanup_removed_leftovers` 清理早于本次操作的 `.removed_` 残留。
  - 目标目录不存在（回滚窗口）时跳过清理，避免误删。
- **剩余风险**：目标目录不存在时不清理；剪映索引有缓存；UIA 的 Name 匹配无法区分新旧卡片。
- **优先级**：P2。
- **建议动作**：发布前后各做一次索引对账；把 `.removed_` 移出剪映扫描根；UIA 增加 mtime 或唯一标识校验。
- **证据**：
  - `pipeline/pipeline.py:209-238`

---

## 附：核查方法与边界

- **已核查**：`lib/freshness.py`、`lib/cover_watch.py`、`lib/export_state.py`、`lib/jianying_launch.py`、`lib/failure_class.py`、`pipeline/pipeline.py`、`pipeline/series_runner.py`、`pipeline/prune_workdirs.py`、`pipeline/archive_job.py`、`pipeline/stream_manifest.py`、`scripts/freshness_baseline.py`、`pipeline_ui/jianying_ui.py`、`.trae/skills/jianying-editor/examples/robust_auto_export.py`、`tests/test_export_targets_profile.py`。
- **未执行**：未改代码、未改 `D:\短剧` job 数据、未启停剪映/watcher、未跑导出、未重跑测试。
- **未验证清单**：
  - 剪映 UI“恢复备份”的实际取文件规则（R04）。
  - 146 集草稿 293 素材 / 292 缺失的现场数据（R05）。
  - 进程存活约 118s、空等约 3.5 分钟（R12）。
  - `uiautomation` 的 `CoInitialize` 报错条件与堆栈（R13）。
  - 8.7GB 单文件导出为现场观测，代码中仅作为示例量级出现（R15）。
  - 部分串流合并的实际发生频率（R10）。
- **后续维护**：新增风险请补充“证据（文件 + 行号）”；无法指到代码或数据的，继续标注“未验证”，不要升级为结论。
