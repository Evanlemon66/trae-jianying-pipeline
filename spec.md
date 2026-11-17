# Spec：路径配置收口 + watcher 测试暂停 + 运行记录结构化

> 状态：待批准（本轮 Spec，取代上一轮同名文件）
> 关联文档：`docs/console-doctor-followup-implementation.md`
> 本轮范围：Q3 路径配置收口、Q6 环境预设分组与文档、Q7 P0+P1、Q8 P0+P1。

***

## 1. 用户决策

1. **Q3**：希望去掉所有硬编码兜底，新机器也只靠配置 JSON 入参保证可用；并保证所有调用方使用的素材根、导出根、归档根完全一致。
2. **Q6**：环境预设负责“告诉用户该配什么”，环境依赖负责“一键拉齐”；本次只检查环境依赖相关；评估“剪映前置”是否独立；去掉“首次登录/同意协议”；模板草稿、组合预设改为环境预设提醒；计划任务可选、不强制拉起；“缺口”按“实际使用但未检查/未安装”重新核实；素材目录那条先忽略。
3. **Q7**：按建议做 P0+P1。
4. **Q8**：按建议执行（P0+P1）。

***

## 2. 结论与方案

| 项  | 结论                                                                    |
| -- | --------------------------------------------------------------------- |
| Q3 | 支持“去掉硬编码”，但不支持“静默兜底”。采用**唯一配置源 + 统一解析器 + 缺失即报错 + 一次性迁移**              |
| Q6 | 环境预设与环境依赖分栏；剪映相关独立成“剪映前置”；安装脚本拆分 D+B 留到下一轮                            |
| Q7 | 哨兵 `pipeline/_work/watcher.pause` + Console 暂停/恢复                     |
| Q8 | `_run.json` + `run-<id>.log` + `_batch_state` 扩展 + 只读 `run_status.py` |

***

## 3. Q3：路径配置收口

### 3.1 为什么不建议“配置 + 静默兜底”

* 静默兜底会让“配置写错”表现为“运行正常”，问题延迟到导出/归档才暴露。
* 当前已经出现多套来源：`series_root`、`watch.roots`、`batch_runner` 位置参数、`admission` 直接取 `watch.roots[index]`、`install_deps.ps1` 自己的 `D:\AI_Deps` 链。再加一层兜底只会扩大漂移。
* 用户要的是“配置决定可用性”，正确形态是：**配置缺失/无效 → 立即给出可操作错误**；而不是“猜一个路径继续跑”。

### 3.2 目标模型

**唯一真源**：`pipeline/presets.json`（`JY_*` 环境变量仅作为显式覆盖，且必须指向有效目录）。

**业务根（必须显式配置，不自动猜）**：

| 键              | 用途                  | 缺失行为                              |
| -------------- | ------------------- | --------------------------------- |
| `series_root`  | 唯一素材根；所有调用方的“短剧素材根” | `ConfigError`                     |
| `export_dir`   | 成品导出根               | 导出相关调用时 `ConfigError`             |
| `archive_root` | 归档根                 | 可留空 = 禁用归档；配置了但无效 → `ConfigError` |
| `deps_root`    | 依赖/模型/工具根           | `ConfigError`                     |

**watcher 额外扫描根（只影响 watcher，不影响任何业务根）**：

| 键             | 用途                          | 规则                                                           |
| ------------- | --------------------------- | ------------------------------------------------------------ |
| `watch.roots` | 临时/额外素材根，仅 `cover_watch` 扫描 | 为空 → 只扫 `series_root`；非空 → 每项必须存在、是目录、且下面至少有一个 `*/cover_job` |

* 运行时有效扫描根 = `[series_root] + watch.roots` 去重；
* `watch.roots` 不再参与 `series_root` 解析，不再作为 `admission`/`init_draft` 的默认根；
* 临时指定某一部剧的素材目录，用 `admission.video.root` 或 `batch_runner` 位置参数显式覆盖，不走 `watch.roots`。

**派生根（允许系统探测，但必须显示来源）**：

* `projects_root`、`presets_root`、`jianying_log_dir`：来自 `LOCALAPPDATA`/注册表等系统位置；
* `jianying_dir`：配置优先，其次注册表探测；都失败 → `ConfigError`；
* 不再保留 `D:\剪映5.9\...`、`D:\油管打猎\L`、`D:\短剧`、`D:\短剧_存档`、`D:\AI_Deps` 这些字面量。

### 3.3 统一解析入口

在 `pipeline/export_config.py` 新增：

```python
class ConfigError(ValueError):
    """配置缺失/无效：带 key、当前值、修复提示。"""

def resolve_paths(mode="run"):
    """返回统一路径快照 + 来源。mode: run|export|install|console。"""

def validate_config(mode="run"):
    """返回 {ok, paths, issues:[{key,message,hint}]}，不抛异常。"""

def get_watch_roots():
    """watcher 有效扫描根：[series_root] + watch.roots 去重并校验（只读）。"""
```

`resolve_paths()` 返回：

```python
{
  "series_root": "...", "watch_roots": [...], "watch_extra_roots": [...],
  "export_dir": "...", "archive_root": "...", "deps_root": "...",
  "jianying_dir": "...", "projects_root": "...", "presets_root": "...",
  "sources": {"series_root": "presets.json", "export_dir": "env", ...},
  "issues": [...]
}
```

所有调用方只允许使用该快照或对应的 `get_*()`，禁止再自行读 `watch.roots`。

### 3.4 解析规则（去掉所有静默回退）

1. 环境变量存在但目录无效 → `ConfigError`，**不再**回退到 JSON。
2. JSON 字段存在但目录无效 → `ConfigError`，**不再**回退到下一级。
3. `series_root` 不再把 `watch.roots` 当候选，彻底移除“series\_root → watch.roots → D:\短剧”链。
4. `watch.roots` 为空 → 有效扫描根只有 `[series_root]`（派生，不写盘）。
5. `watch.roots` 非空 → 每项必须：绝对路径、存在、是目录、下面至少有一个 `*/cover_job`；不要求位于 `series_root` 下；有效扫描根为 `[series_root]` 与各项去重合并。
6. `export_dir`、`archive_root`、`deps_root` 只从 JSON/环境变量解析，不再有字面量默认值。
7. `archive_root` 留空表示禁用归档；`archive_root` 位于任一有效扫描根下继续由 `archive_job.py` 拒绝。
8. `batch_runner.py` 的位置参数视为“本次显式覆盖”：必须存在，记录 `source="cli"`，不改变全局配置；若不在 `series_root` 下，打印明确警告（不阻断，保留单剧临时跑能力）。
9. `admission.video.root` 是唯一允许的“临时素材根”入口；`root_index` 仅在等于 0 时视为 `series_root`，大于 0 时要求改用 `root` 并报错。
10. `series_root` 要求存在；不存在 → `ConfigError`。目录为空只告警，不阻断建稿前的人工准备。

### 3.5 调用方收口

| 文件                                       | 改动                                                                                                                                                                        |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pipeline/export_config.py`              | 新增 `ConfigError`、`resolve_paths()`、`validate_config()`、`get_watch_roots()`；删除 `DEFAULT_SERIES_ROOT/EXPORT/ARCHIVE/DEPS/JIANYING`；`_resolve_dir/_resolve_target` 改为“无效即抛错” |
| `pipeline_ui/admission_draft.py:167-175` | `_root_of` 改为“`video.root` 显式优先，否则 `series_root`”；`root_index>0` 报错并提示改用 `root`                                                                                           |
| `pipeline_ui/init_draft.py:54-75`        | 不再传 `get_watch_config().get("roots")`，统一走 `resolve_series_dir()` 默认值                                                                                                      |
| `pipeline_ui/console/paths.py:38-49`     | 改用 `resolve_paths()`；缺失项返回空并由 doctor 报错                                                                                                                                   |
| `pipeline_ui/console/envinit.py`         | `inventory()` / `validate_presets()` / `ensure_dirs()` 使用统一快照；`series_root` 必填                                                                                            |
| `pipeline/doctor_core.py:305-316`        | 路径检查显示 `source` 与 `issue`，不再展示兜底路径                                                                                                                                        |
| `pipeline/install_deps.ps1:101-119`      | 删除 `D:\AI_Deps` 与“无 D 盘 → C:\AI\_Deps”兜底；无 `-DepsRoot`/`JY_DEPS_ROOT`/`presets.deps_root` 时直接报错                                                                           |
| `scripts/freshness_baseline.py:209`      | 帮助文案移除 `D:\短剧` 回退描述                                                                                                                                                       |
| `pipeline/presets.example.json`          | 业务根改为空字符串 + `_必填说明`；`watch.roots` 改为 `[]`；移除所有 `D:\...` 示例                                                                                                                |
| `pipeline/presets.json`                  | 本轮不改；由迁移脚本/Console 动作写入                                                                                                                                                   |

### 3.6 一次性迁移

新增 `pipeline/migrate_config.py`：

* `--dry-run`（默认）/ `--write`；
* 备份 `presets.json` 为 `presets.json.bak-<时间戳>`；
* 仅做确定性迁移：
  * `series_root` 为空且 `watch.roots` 恰好一个有效目录 → 写入该目录；
  * `series_root` 为空且 `watch.roots` 多个 → 要求 `--series-root`，不猜；
  * 其它业务根缺失只报告，不猜；
* 原子写回，输出迁移摘要。

Console 环境预设页新增动作 `config.migrate`：调用同一逻辑（复用 `envinit.save_presets` 的备份与原子写），成功后刷新检查。

### 3.7 测试

* `tests/test_path_config.py` 重写：
  * `series_root` 缺失 → `ConfigError`；
  * JSON 无效路径 → `ConfigError`，不回退；
  * `watch.roots` 为空 → 有效扫描根 `[series_root]`；
  * `watch.roots` 非空但目录不存在 → `ConfigError`；
  * `watch.roots` 非空且目录下无 `*/cover_job` → `ConfigError`；
  * `watch.roots` 含 `series_root` 时去重不重复；
  * `admission.video.root` 优先于 `series_root`；`root_index>0` 报错；
  * 所有 `get_*` 与 `resolve_paths()` 返回一致。
* 新增 `tests/test_migrate_config.py`：
  * 单 root 自动迁移；
  * 多 root 不猜、报错；
  * 写入前有备份。

### 3.8 Skill 与契约文件同步（必做）

三根统一后，所有涉及路径语义的 skill、契约、用户手册必须同步，禁止继续描述“回退到 `D:\短剧` / `D:\AI_Deps`”或“`series_root` 取 `watch.roots[0]`”。

| 文件                                                                                                                                                      | 需要修改的内容                                                                                               |
| ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `.trae/skills/jianying-editor/SKILL.md:80,86,91`                                                                                                        | 归档说明改为：剧集默认留在 `series_root`（以及 `watch.roots` 额外根）；`archive_root` 不能位于任一有效扫描根下；`watch.roots` 为空不再是拒绝条件 |
| `.trae/skills/jianying-editor/examples/robust_auto_export.py:1963-1981`                                                                                 | `subtitle_gate()` 的 `roots` 默认值改为 `pc.get_watch_roots()`（已含 `series_root`），docstring 同步               |
| `.trae/skills/drama-cover-builder/SKILL.md:307,340`                                                                                                     | 归档条件改为“有效扫描根”；配置示例说明 `watch.roots` 是额外根，主根来自 `series_root`                                            |
| `.trae/skills/drama-cover-builder/references/handoff-contract.md:96,100,283,603`                                                                        | 明确 `video.export_dir` 是“成品记录字段”，不是全局导出根；全局根只来自 `presets.json`                                         |
| `.trae/skills/drama-cover-builder/references/handoff-contract.md:430,432,566,572,574`                                                                   | 归档契约改为：`archive_root` 为正式键，`watch.archive_root` 仅兼容；拒绝条件改为“位于任一有效扫描根下”；删除“`watch.roots` 未配置即拒绝”       |
| `SKILL.md`（仓库根）                                                                                                                                         | 增加一行契约：路径唯一入口为 `pipeline/export_config.resolve_paths()`，禁止在 skill 内手写根路径                              |
| `使用指南.md:77-95,141-174,183,375,482`                                                                                                                     | 表格改为 `series_root`/`export_dir`/`deps_root` 必填、无内置默认；`watch.roots` 改为额外扫描根；删除“已回退到下一级”；归档条件同步         |
| `快速上手.md:18,32,51,58,94,101-110,133,167`                                                                                                                | 删除 `D:\AI_Deps`、`D:\成片导出` 默认表述；`-DepsRoot` 缺失时要求配置；示例 JSON 用占位路径；故障表同步                                |
| `docs/manual-entry-real-run.md`                                                                                                                         | 机器相关路径改为变量/占位（`<素材根>`、`<归档根>`、`<导出根>`），注明取自 `presets.json`；保留流程，不再把 `D:\短剧` 当默认                       |
| `docs/web-console-plan.md:35,55-61,103,151-154,296,314`                                                                                                 | 白名单根改为由 `resolve_paths()` 提供；默认值表改为“示例/来源”；删除 `D:\油管打猎\L`、`D:\短剧_存档` 作为默认的描述                          |
| `docs/project_status.md:166,167,183,206`                                                                                                                | 增加说明：`D:\短剧`、`D:\短剧_存档` 是当机数据，不是默认值；后续以配置为准                                                           |
| `specs/web-console/spec.md`、`specs/docs-consistency/spec.md`、`specs/export-parity/spec.md`、`specs/export-parity/tasks.md`、`specs/multi-account-todo.md` | 顶部加“路径契约已收口”说明；把描述当前默认值的句子改为引用 `resolve_paths()`                                                      |
| `pipeline/presets.example.json:35-46`                                                                                                                   | `_说明` 改为新契约；业务根置空；`watch.roots` 置空；删除 `D:\成片导出`、`D:\AI_Deps`、`D:\短剧` 字面量                              |
| `pipeline/presets.json`                                                                                                                                 | 本轮不改；迁移后由 `series_root` 承载唯一素材根，`watch.roots` 如仍保留则按额外根语义去重                                           |

验收标准：全仓库对 `D:\短剧`、`D:\成片导出`、`D:\短剧_存档`、`D:\AI_Deps` 的引用只剩“历史数据/示例占位/迁移说明”，不再有任何“默认值”语义。

***

## 4. Q6：环境预设 / 环境依赖 / 剪映前置

### 4.1 页面分组

* **环境预设**：路径字段、`series_root`、`watch.roots`（额外 watcher 扫描根）、GLM key、预设组、剪映前置。
* **环境依赖**：Python、pip 核心/AI、torch/CUDA、ffmpeg、Chromium、OCR、Ollama；只保留“可一键安装/可实测”的项。
* **剪映前置**（建议单独成组，放在环境预设下）：
  * 剪映安装 + 版本（5.9.0.11632）；
  * 交互桌面会话；
  * 草稿根索引一致性；
  * 模板草稿 `default`；
  * 组合预设名匹配。
* 本轮只做**展示分组**：`env.js` 的 `renderGroups()` 把 `jianying` 组渲染到环境预设页；检查函数不变。

### 4.2 文档修订

* 删除“首次登录/同意协议”；
* “安装剪映 5.9”“模板草稿”“组合预设”改为环境预设中的配置/提醒项；
* 计划任务标注“可选”，不强制拉起；
* 素材目录资产准备标注“暂不要求”；
* “缺口”复核：
  * MediaInfo DLL：`pymediainfo` 已装且可导入；`can_parse()` 未检查；真实使用只在 `scripts/vendor/pyJianYingDraft/local_materials.py`，有 ffprobe 回退 → skill 路径缺口，不影响 pipeline；
  * `imageio-ffmpeg`：全仓库无引用 → 不是缺口；
  * VC++ Redistributable：未显式检查，但 torch/rapidocr 导入实测已间接验证 → 诊断加固项；
  * CUDA 终检：已存在（`envinit.py:648-686`、`install_deps.ps1:525`）→ 不是缺口；
  * onnxruntime/numpy：ui\_pylibs 的 rapidocr 已实导入，numpy/pillow 已精确 pin；仅 `scripts/jy_onnx_asr.py` 直接 import onnxruntime，若该脚本属于支持范围再补检查。

***

## 5. Q7 P0：暂停哨兵

### 5.1 `pipeline/watcher_supervisor.ps1`

* 新增 `$pauseFile = Join-Path $logDir 'watcher.pause'`。
* `Invoke-Supervisor` 开头、日志目录创建后、`watcherPy` 检查前：

```powershell
if (Test-Path -LiteralPath $pauseFile) {
    Write-Log '托管已暂停（存在 pipeline/_work/watcher.pause），本次不拉起 watcher'
    return 0
}
```

### 5.2 `pipeline/delayed_start.ps1`

* 新增同一 `$pauseFile`。
* 第 4 步启动前检查，存在则记日志并 `return 0`；第 3 步写 `export_config.json` 保留。

***

## 6. Q7 P1：Console 暂停/恢复

* `envinit.py`：`PAUSE_FILE`、`watcher_paused()`、`task_status().paused`、`set_watcher_paused()`；`_chk_watcher_task()` / `_chk_watcher_runtime()` 展示暂停态与 `task.pause` / `task.resume` 按钮。
* `app.py`：动作分发 `task.pause` / `task.resume`。
* `env.js`：确认文案与调用分支。
* `status.py`：`WATCHER_PAUSE`，`watcher_info()` 返回 `paused`。
* 只写/删哨兵，不杀进程、不改计划任务。

***

## 7. Q8 P0：batch\_runner 运行记录

* `batch_runner.py`：`_Tee`、`_atomic_write_json`、`_start_run`、`_finish_run`。
* `_run.json`：`schema_version/run_id/source/argv/cwd/series_dir/workdir/started_at/ended_at/exit_code/status/log_path`。
* 日志：`run-<run_id>.log`，stdout/stderr 双写。
* `_batch_state.json` 单集新增：`started_at/attempts/error/error_class/artifact`。
* `__main__` 收尾：`SystemExit`、`KeyboardInterrupt`、其它异常均落最终状态。

***

## 8. Q8 P1：只读聚合

* 新增 `pipeline/run_status.py --workdir <dir> [--json]`。
* `status.py` 的 `_batch_state_entries()` 优先读 `_run.json`，旧格式回退。
* `tests/test_external_tasks.py` 新增读取、回退、`run_status.collect` 用例。

***

## 9. 不做

* 不拆分 `install_deps.ps1`、不新增管理员批处理（下一轮）；
* 不改 `lib/cover_watch.py` 的业务逻辑；
* 不删除任何 `_work_*`、日志、清单或草稿；
* 不改计划任务触发频率。

***

## 10. 验证

1. `pytest tests/test_path_config.py tests/test_migrate_config.py tests/test_external_tasks.py`
2. PowerShell 语法检查两个 `.ps1`。
3. 哨兵验证：创建 → 跑 supervisor → 确认“托管已暂停”且退出码 0 → 删除 → 恢复。
4. `python pipeline/migrate_config.py --dry-run` / `--write`（在临时副本上）。
5. `python pipeline/run_status.py --workdir <tmp> --json`。
6. Console：环境预设页出现“剪映前置”；暂停/恢复按钮生效。

***

## 11. 风险与回滚

| 风险                 | 影响                     | 回滚                                                     |
| ------------------ | ---------------------- | ------------------------------------------------------ |
| 旧机器首次升级后 doctor 变红 | 需要一次迁移/填配置             | Console `config.migrate` 或 `migrate_config.py --write` |
| 路径函数改为抛错           | 未配置时更早失败               | 恢复 `_resolve_dir` 旧行为（git revert 单文件）                  |
| `watch.roots` 校验偏严 | 临时目录没放 `cover_job` 会报错 | 按提示补 `cover_job` 或清空该项；不改 `series_root`                |
| 哨兵忘记恢复             | watcher 不工作            | 删除 `watcher.pause`；Console/doctor 持续提示                 |
| `_run.json` 半截     | 读取失败                   | `.tmp + os.replace`；删除文件即回退                            |
