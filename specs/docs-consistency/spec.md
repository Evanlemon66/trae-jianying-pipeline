# 文档一致性修订规格（快速上手.md / 使用指南.md）

状态：**待批准**。本轮为只读审查，未改动 `快速上手.md`、`使用指南.md` 及任何代码/配置。

## 1. 审查基线

以仓库当前真实实现为准，逐项对照：

| 对照对象 | 关键事实 |
| --- | --- |
| `pipeline/export_config.py` | 解析顺序 `env > presets.json > 自动探测 > 内置默认`；`preset_profile_name()` 只取单组（不拆逗号）；`parse_preset_profiles()` 才拆逗号（仅 watcher/batch 用）；`get_ui_pylibs_dir()` 先找 `<repo>/pipeline_ui/pylibs`，再回退 `<deps_root>/ui_pylibs` |
| `pipeline/doctor.py` | 检查项：layout / python / deps / ui_pylibs（`blocking=False`）/ ffmpeg / jianying / session / config_paths / draft_index / watcher_task / watcher_runtime；标签 `[通过]/[警告]/[缺失]`；非阻断 `[缺失]` 不影响退出码；`WATCHER_TASK_NAME = "TraeDramaWatcher"`，修复文案为"需要登录后自动守护时再按文档注册该任务" |
| `pipeline/pipeline.py` | 空白草稿模板只认 `default` / `pipeline_test` / `demo_test`（L463），缺失时只打印 `✗ 创建空白草稿失败 (找不到模板)`（L2871）；`--list-presets` 在默认组缺失时 `sys.exit(2)` |
| `pipeline/series_runner.py` | `export_console.log` 落在 cover_job（无 job 时 `pipeline/_work`），不在成品目录 |
| `lib/cover_watch.py` / `pipeline.py` | 导出失效标记实际文件名 `_export_stale.json`（下划线） |
| `pipeline/verify_export.py` | 只打印结论，不写 `.verify.json` |
| `pipeline/presets.example.json` | `_说明` 称"手动单部构建时 `--profile` 仍支持逗号分隔"，与实现不符 |
| `pipeline/install_deps.ps1` | `#Requires -Version 5.1`；参数 `-DepsRoot/-DetectOnly/-TorchChannel/-OllamaModel/-WhisperModel/-SkipOllama/-SkipModels/-SkipWhisperWarmup/-SkipPlaywrightBrowser` 均存在；Step 4b 安装 `ui_pylibs`；成功末行 `全部依赖就绪` |
| `.gitignore` | `pipeline/presets.json` 已忽略（文档表述正确） |

## 2. 问题清单与修订决定

### P0 断链（照文档走到不了第一部成片）

| ID | 文件/章节 | 现状 | 修订 |
| --- | --- | --- | --- |
| D1 | 快速上手 §0 L20；使用指南 §1 | 只写"剪映不要求提前打开"，历史版本要求的"首次手动登录一次"被删 | 恢复一条前置：首次安装后手动启动剪映、登录并同意协议后关闭；否则导出可能停在登录/协议弹窗 |
| D2 | 快速上手 §2 与 §5 之间；使用指南 §1/§5 | 未提"需要一个空草稿模板"，而 `pipeline.py` 只认 `default`/`pipeline_test`/`demo_test` | 增加步骤：在剪映新建空草稿并命名 `default`（内容可空），作为自动化建草稿的模板；缺失时报错只有"找不到模板" |
| D3 | 快速上手 §3.2 L107；使用指南 §3.2.1 | 只要求预设名与预设库一致，未说明新机器预设库为空，导致 `presets_a` 全部被跳过 | 在 §3.2 前加一步：先在剪映「我的预设 → 组合预设」创建并命名，名字照抄进 `presets_a` |
| D4 | 使用指南 §5.7 L367-369 | 只有 `watcher_supervisor.ps1` 一句话，无 `TraeDramaWatcher` 注册命令；`doctor` 修复文案又指向文档 | 补最小注册示例（任务计划程序或 `schtasks`/`Register-ScheduledTask`），并注明"不注册只影响自动守护，不影响手动运行" |

### P1 与实现不一致

| ID | 文件/章节 | 现状 | 修订 |
| --- | --- | --- | --- |
| D5 | 使用指南 L51 / L176 / L403 / L435 | 写成 `.export_stale.json`（L306 正确） | 统一为 `_export_stale.json` |
| D6 | 使用指南 §6.2 L400 | 把 `export_console.log` 列在成品目录 | 改到 cover_job（无 job 时 `pipeline/_work`），并同步 §8 L452、§9 L468 的排查指向 |
| D7 | 使用指南 §6.2 L399 | 列出 `<草稿名>.mp4.verify.json` | 删除该行，改为"质检结论仅打印，不落盘" |
| D8 | 快速上手 L175；使用指南 L313、L465 | "存在 `[缺失]` 即退出码 1" | 改为"仅阻断项缺失返回 1；`ui_pylibs` 的 `[缺失]` 与 watcher 两项不改变退出码" |
| D9 | 快速上手 L173；使用指南 §3.2.3/§5.5 | 检查项清单缺 `ui_pylibs`、`watcher 计划任务`、`watcher 运行态` | 补齐为与 `doctor.py` 一致的三类，并分别标注"非阻断/运维信息" |
| D10 | 使用指南 §4 L175 | 称 batch_runner 有"新鲜度记录" | 改为"导出质检 + 成品存在性"；sidecar freshness 由 watcher/series_runner 写 |
| D11 | `pipeline/presets.example.json` `_说明` | 称手动单部构建 `--profile` 支持逗号 | 改为"逗号分隔仅 `cover_watch`/`batch_runner` 支持，`pipeline.py` 单次只取一组" |
| D12 | 使用指南 §3.2 `ui_pylibs` 行 | 只写默认 `<deps_root>\ui_pylibs` | 补第一候选 `<repo>/pipeline_ui/pylibs`；快速上手 L67 同步一句 |
| D13 | 使用指南 §5.1 L216 | 导出打印 `✅ 导出完成!` | 改为 `🎉 导出完成（已搬运/未搬运）…`，`✅ 导出完成` 说明为桌面通知标题 |

### P2 结构与自相矛盾

| ID | 文件/章节 | 现状 | 修订 |
| --- | --- | --- | --- |
| D14 | 快速上手 §3.1 L81/L87-L89 | "以下 6 项都要配"与各行"可留空/可自动发现/不写则回退"冲突 | 拆成两档：最低可出片集合 vs 完整交付集合；或将可回退项降为 [建议] |
| D15 | 快速上手 §4.2 L183 | 断言 `--list-presets` 退出码 0 | 补例外：默认组缺失时退出 2（与 §5.1 一致） |
| D16 | 使用指南 §2 运行期文件表 | 缺 `.edit_state.json`（cover_job，编辑状态机） | 补一行 |
| D17 | 快速上手 §0 L18、§2 | 默认依赖根 `D:\AI_Deps` | 补一句"没有 D 盘时先用 `-DepsRoot` 指定" |
| D18 | 使用指南 §1/§2 | 只给 `install_deps.ps1`，未提 `requirements.txt` | 加一句手动安装入口（仍需自备 ffmpeg/模型），避免读者以为必须跑脚本 |
| D19 | 使用指南 §10 L483-488 | "pipeline_ui/ 两个入口"与实际脚本数不符 | 改为"交互式入口脚本"并点名 `jianying_ui.py`、`ui_recognize_translate.py`、`init_draft.py` |

### P3 措辞

| ID | 文件/章节 | 现状 | 修订 |
| --- | --- | --- | --- |
| D20 | 快速上手 L248；使用指南 L452 | `⚠️ 导出超时（>60min），终止` | 与 batch_runner 实际输出 `⚠️ 导出超时（>{timeout_min}min）` 对齐，或注明"终止"由调用方追加 |
| D21 | 快速上手 §3.3 JSON | `jianying_dir` 写死示例值 | 加注释行说明可留空走自动探测，避免读者误以为必须写死 |

## 3. 不做的事

- 不修改 `pipeline/presets.json`（含真实密钥）与任何代码逻辑。
- 不改文档的章节编号体系，只做局部改写/补段。
- 不新增第三方依赖或截图。

## 4. 验收

1. 两份文档中所有文件名/键名/环境变量/退出码可用 `grep` 对上代码；
2. 按"快速上手 §0 → §2 → §3 → §4 → §5"顺序可无中断走通；
3. 与 `python pipeline\doctor.py` 实际输出逐行对照无差异；
4. `python pipeline\pipeline.py --list-presets` 的两种退出码（0/2）均有说明。
