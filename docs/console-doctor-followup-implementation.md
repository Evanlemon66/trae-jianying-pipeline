# Console/Doctor 四个追问：核查结论与实施方案

> 本轮只做只读核查与文档：**不改代码、不改配置、不执行导出、不删除任何文件**。
> 盘点基准：仓库当前工作区（2026-11-16）；行号以当前文件为准。
> **状态：本文是「路径三根统一」改造前的历史基线**，文中 `D:\短剧`、`D:\AI_Deps`、`DEFAULT_SERIES_ROOT`、`watch.roots` 作为 `series_root` 候选等描述均指旧行为；现状以 `pipeline/export_config.resolve_paths()` 与根目录 `spec.md` 为准（业务根无默认、缺失即 `ConfigError`）。
> 关联文档：上一轮 `spec.md` §7/§8B、[`docs/web-console-plan.md`](web-console-plan.md)。

---

## 0. 结论摘要

| # | 追问 | 结论 | 推荐动作 | 改动面 |
|---|---|---|---|---|
| 3 | `series_root → watch.roots[0] → D:\短剧` 末端硬编码 | 代码里只有一处常量 `DEFAULT_SERIES_ROOT`，位于 `pipeline/export_config.py:35`；真正的问题是 `_resolve_dir()` 在最后一层**无条件**返回它，即使目录不存在 | 先让它可见（默认来源告警），再对 `series_root` 做“默认值不存在就失败”的收口 | `export_config.py` + 测试 + 文档；不动 pipeline/lib |
| 6 | UAC 能否统一掉 | 脚本不能取消 UAC；只能做到“最多一次”或“提前授权后零次”。当前 Console 每次点安装都 `Start-Process -Verb RunAs`（`envinit.py:1126-1138`） | 桌面有人：`-DetectOnly` 预检 + 单次管理员批处理；无人值守：最高权限计划任务（有明确限制） | 可选新增 `install_deps_admin.ps1` + Console 编排；本轮只文档 |
| 7 | watcher 停掉后自动拉起 | 来源不止一个：`TraeDramaWatcher` 计划任务每 5 分钟跑 supervisor，supervisor 发现无 `cover_watch` 就 WMI 拉起；卡死还会 kill 后重启；另有 XML 的 10 分钟/登录触发口径和 `delayed_start.ps1` | 增加“测试暂停哨兵”+ Console 暂停/恢复按钮；计划任务禁用作为零代码兜底 | `watcher_supervisor.ps1`、`delayed_start.ps1`、Console 环境页；不动 `lib/cover_watch.py` |
| 8 | 手工串流 vs 助手执行的信息差 | 差距不在“谁能读文件”，而在**有没有结构化运行记录**：`runs.jsonl` 只由 Console 写；`_batch_state.json` 只有 `status/finished_at`；stdout 没有自动落盘 | `batch_runner` 自动写 `_run.json` + `run.log`，并提供只读 `run_status.py --json` | `batch_runner.py`、新增只读 CLI、Console 聚合；低风险 |
| 文档 | 上一轮承诺的“实施文档”是否落盘 | `docs/` 下没有上一轮的新实施文档；实际产物是仓库根 `spec.md`（2026-11-16 11:15，Spec 状态）和 `tasks.md`（同时间，状态“已完成”） | 本文件即补齐 | 新增 1 个 Markdown |

---

## 1. 问题 3：`series_root` 链路末端为什么还有硬编码

### 1.1 事实链：硬编码到底在哪

实际解析链是：

```
get_series_root()
  → _resolve_dir("series_root", "JY_SERIES_ROOT", DEFAULT_SERIES_ROOT, candidates=watch.roots)
      → 环境变量 JY_SERIES_ROOT（且目录存在）
      → presets.json 的 series_root（且目录存在）
      → watch.roots 中任意一个存在的目录
      → DEFAULT_SERIES_ROOT（不检查是否存在）
```

先纠正一个细节：`get_series_root()` **不是**只取 `watch.roots[0]`，而是把整个 `watch.roots` 列表作为候选，返回第一个真实存在的目录（`pipeline/export_config.py:285-293`）。当前本机 `watch.roots[0] = D:/短剧` 且该目录存在，所以本机实际命中的是 **watch.roots 候选**，不是第 35 行的兜底值。兜底值只在前面全部失败时才会被使用。

| 位置 | 内容 | 作用 |
|---|---|---|
| `pipeline/export_config.py:35` | `DEFAULT_SERIES_ROOT = r"D:\短剧"` | 代码里唯一的短剧根兜底常量 |
| `pipeline/export_config.py:69-90` | `_resolve_dir()` | 解析顺序；**第 90 行无条件 `return default`** |
| `pipeline/export_config.py:73` | 注释“最后一层永远返回 default（保持现状）” | 设计意图 |
| `pipeline/export_config.py:283-293` | `get_series_root()` | 把 `watch.roots` 读成候选列表 |
| `pipeline/presets.json:43` | `"series_root": ""` | 本机未显式配置素材根 |
| `pipeline/presets.json:46-48` | `"roots": ["D:/短剧"]` | 本机实际命中的候选 |
| `pipeline/presets.example.json:43,46` | 同样是空 `series_root` + `D:\短剧` | 新机模板复制后仍然带这个路径 |
| `data/freshness_legacy_baseline.json:9` | `"D:/短剧"` | 历史基线数据，不参与运行期解析 |
| `tests/test_path_config.py:57` | 断言 `get_series_root() == DEFAULT_SERIES_ROOT` | 现有测试把这个兜底当契约 |

同一套“最后一层兜底”还出现在这些地方：

| 函数/位置 | 兜底值 | 说明 |
|---|---|---|
| `get_jianying_dir()` `export_config.py:259-261` | `D:\剪映5.9\...\5.9.0.11632` | 最机器相关的硬编码；新机换路径时优先靠注册表探测/`JY_JIANYING_DIR` |
| `get_export_dir()` `export_config.py:221-223` | `D:\油管打猎\L` | 输出型路径，缺失时首次使用会创建 |
| `get_deps_root()` `export_config.py:226-229` | `D:\AI_Deps` | 与 `install_deps.ps1:112` 同值 |
| `get_archive_root()` `export_config.py:296-303` | `D:\短剧_存档` | 归档根 |
| `install_deps.ps1:101-119` | `D:\AI_Deps` | 安装脚本自己的同一套优先级 |

### 1.2 为什么最后还留一层硬编码

不是遗漏，是兼容性约定：

1. `_resolve_dir()` 的 docstring 明确写了“最后一层永远返回 default（保持现状）”（`export_config.py:70-73`）。
2. 常量上方注释写着“内置兜底值（presets.json 缺失或字段为空时使用；这是本机现有默认，保持不变）”（`export_config.py:31`）。
3. 下游（doctor、Console 白名单、watcher 扫描、freshness 脚本）都假定 `get_series_root()` 返回非空字符串；返回 `None` 会迫使所有调用方增加空值分支。

所以它是“为了不破坏旧机器”的保底，而不是“忘了删”。问题在于：**保底值不校验存在性**，新机器上会静默拿到一个不存在的 `D:\短剧`。

同一个函数里还有一处自相矛盾的注释：第 72 行写“显式配置但目录不可用时……绝不静默使用错误路径”，第 73 行却写“最后一层永远返回 default（保持现状）”，而第 90 行的 `return default` 并不检查 `isdir`。这正是“配置错了也看起来正常”的来源。

另外，模板文件本身也在强化这个链路：`presets.example.json:12` 明确写着“留空时回退 `watch.roots[0]`，再回退 `D:\短剧`”，并且第 46 行直接给出 `"roots": ["D:\\短剧"]`。即使代码里的常量被删掉，新机器复制模板后仍然会带着这个字面量。代码实际行为与模板文案还有一点小偏差：`_resolve_dir()` 是遍历整个 `watch.roots`，返回第一个存在的目录（`export_config.py:87-89`），不是只取 `[0]`。

### 1.3 什么条件下生效，哪些分支会绕过它

兜底值生效需要同时满足：

1. `JY_SERIES_ROOT` 未设置，或设置后目录不存在（`export_config.py:75-80`）；
2. `presets.json` 的 `series_root` 为空，或设置后目录不存在（`export_config.py:81-86`）；
3. `watch.roots` 为空，或其中没有任何一个条目是真实存在的目录（`export_config.py:87-89`）。

此时第 90 行直接返回 `D:\短剧`，**即使该目录不存在**。

会绕过这个兜底的分支：

| 分支 | 位置 | 是否绕过 | 说明 |
|---|---|---|---|
| 命令行显式传剧目录 | `pipeline/batch_runner.py:275` | 是 | `batch_runner` 的位置参数直接就是剧目录，全程不调用 `get_series_root()` |
| 环境变量 | `export_config.py:75-80` | 是 | 目录存在才命中；不存在会 warn 并继续下一级 |
| `presets.json.series_root` | `export_config.py:81-86` | 是 | 同上；配置写错不会硬失败，只会静默回退 |
| `watch.roots` 任意一项 | `export_config.py:87-89` | 是 | 第一个存在的条目生效，不一定是 `[0]` |
| `admission` / `init_draft` | `pipeline_ui/admission_draft.py:167-175`、`pipeline_ui/init_draft.py:54-55,75` | 是 | 直接读 `watch.roots[root_index]`，不走 `get_series_root()`；roots 为空时抛 `ValueError` |
| Console / Doctor 路径检查 | `pipeline_ui/console/paths.py:38-49`、`pipeline/doctor_core.py:305-316` | 否 | 都走 `ec.get_series_root()`，因此会继承这个兜底 |
| freshness 基线 | `scripts/freshness_baseline.py:40,220` | 否 | 默认 `[get_series_root()]` |

一个容易踩的组合：`series_root` 写了一个不存在的路径 → 第 86 行只 warn → 继续看 `watch.roots` → 如果 `watch.roots` 也没有 → 落到 `D:\短剧`。用户看到的是“配置了但没生效”，日志只有一行 `[配置提示]`。

### 1.4 现有配置能不能覆盖

能，但有一个前提：**目标目录必须真实存在**。

| 配置组合 | 结果 |
|---|---|
| `JY_SERIES_ROOT` 指向存在目录 | 覆盖，命中 |
| `series_root` 指向存在目录 | 覆盖，命中 |
| `watch.roots` 含存在目录 | 覆盖，命中（第一个存在的） |
| `series_root` 指向不存在目录，`watch.roots` 有效 | 先 warn，再命中 `watch.roots` |
| 三者都空/无效，`D:\短剧` 存在 | 命中兜底，行为与旧机器一致 |
| 三者都空/无效，`D:\短剧` 不存在 | **仍然返回 `D:\短剧`**，doctor 报路径缺失，Console 白名单为空根 |

所以“能不能覆盖”的准确答案是：**能覆盖，但配置错误不会阻断；唯一会静默失败的是全空/全无效的新机器。**

### 1.5 可选方案

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A. 只让兜底可见 | 命中 `DEFAULT_SERIES_ROOT` 时打一条 `[配置提示]`，doctor 显示“来源：内置默认” | 零行为变化，最小风险 | 新机器仍会拿到错误路径，只是多一行提示 |
| B. 兜底不存在就失败 | `get_series_root()` 发现最终落在 `DEFAULT_SERIES_ROOT` 且目录不存在 → 抛 `ValueError`，提示配置 3 个位置 | 消除“静默错误路径”；旧机 `D:\短剧` 存在时完全兼容 | 全新机器首次 doctor 会红；需要文档说明 |
| C. 彻底删除默认值 | `DEFAULT_SERIES_ROOT = ""`，`presets.example.json` 的 `watch.roots` 改为 `[]` | 最干净，强制显式配置 | 破坏 `tests/test_path_config.py:57` 契约；对旧机器是硬切换 |
| D. 只改文档 | 文档写“必填” | 零代码 | 代码仍然静默兜底，问题不解决 |

### 1.6 推荐方案

**先 A，再 B；C 留到下一次不兼容变更时做。**

- A 解决“不知道用了默认值”；
- B 解决“默认值不存在还继续跑”。B 的触发条件很窄：只有“三层全部失败 + `D:\短剧` 不存在”才抛错，旧机器不受影响；
- C 会把 `presets.example.json` 的 `watch.roots` 也清空，新用户必须先配素材根，属于产品决策，本轮不建议混进来。

实现要点（B）：

```python
def get_series_root():
    ...
    path = _resolve_dir("series_root", "JY_SERIES_ROOT",
                        DEFAULT_SERIES_ROOT, candidates=roots,
                        label="短剧素材根目录")
    if path == DEFAULT_SERIES_ROOT and not os.path.isdir(path):
        raise ValueError(
            "短剧素材根目录未配置：请设置 JY_SERIES_ROOT、"
            "pipeline/presets.json 的 series_root，或 watch.roots 中任一存在的目录")
    return path
```

不改 `_resolve_dir()` 的签名，避免影响其余 7 个路径解析。

### 1.7 改动面

| 文件 | 改动 | 风险 |
|---|---|---|
| `pipeline/export_config.py` | `get_series_root()` 增加默认来源判断/告警 | 低；只影响素材根 |
| `pipeline/doctor_core.py` | 可选：路径检查显示解析来源 | 低；只读展示 |
| `tests/test_path_config.py` | 新增“默认值不存在 → 抛错”“默认值存在 → 兼容”两条用例 | 低 |
| `pipeline/presets.example.json` | 本轮不动；C 方案再改 | 无 |
| `快速上手.md` / `使用指南.md` | 补充“series_root 必填/回退顺序”的准确表述 | 无 |

不动：`lib/cover_watch.py`、`batch_runner.py`、`presets.json` 生产配置。

### 1.8 风险与回滚

| 风险 | 影响 | 回滚 |
|---|---|---|
| B 让全新机器 doctor 由“绿但路径假”变“红” | 首次配置多一步 | 删除 `get_series_root()` 中新增的 3 行判断 |
| 某台旧机器确实没有 `D:\短剧` 但依赖兜底 | 原本也跑不通；现在会更早报错 | 同上 |
| 告警刷屏 | watcher 每轮调用会重复打印 | 复用 `_warn()` 的每进程去重（`export_config.py:43-49`） |

### 1.9 验证方式（下一轮实施时）

```powershell
# 1) 正常：watch.roots 命中
python -c "import sys; sys.path.insert(0,'pipeline'); import export_config as ec; print(ec.get_series_root())"

# 2) 三处全空 + D:\短剧 不存在：应给出可操作报错
$env:JY_SERIES_ROOT=""
# 临时把 presets.json 的 series_root/watch.roots 置空（仅测试目录，不动生产）

# 3) doctor 路径检查应显示同一个来源
python pipeline\doctor.py
```

---

## 2. 问题 6：UAC 能否统一掉，以及安装后还剩哪些人工步骤

### 2.1 现状事实

- `install_deps.ps1` 确实没有 `Read-Host` / `pause`：全文检索无任何交互等待，唯一的“人工步骤”是末尾打印的提示文字。
- 三处 winget 都带 `--accept-source-agreements --accept-package-agreements`：`install_deps.ps1:74,358,396`。
- Console 的“全量安装依赖”在 Console 未提权运行时每次点击都会弹 UAC：`pipeline_ui/console/envinit.py:1126-1138` 用 `Start-Process -Verb RunAs` 启动 wrapper。
- 长路径开关写 HKLM：`install_deps.ps1:213-225`；失败只 `Warn`，不阻断。
- `-DetectOnly` 只探测 + 写 `pipeline/_env_profile.json`，不安装：`install_deps.ps1:191-196`。
- 安装脚本本身不装剪映：`快速上手.md:38-43`。

### 2.2 先纠正一个前提：哪些动作真的会弹 UAC

用户列的“VC++/ffmpeg/Chromium、写 `D:\AI_Deps`、开长路径”并不都触发 UAC：

| 动作 | 位置 | 是否触发 UAC | 说明 |
|---|---|---|---|
| Console 启动安装 | `envinit.py:1126-1138` | **是（未提权时）** | `Start-Process -Verb RunAs` 本身就会弹 |
| 系统长路径 HKLM | `install_deps.ps1:213-225` | **是** | 写 `HKLM:\SYSTEM\...\FileSystem`；失败只 warn |
| ffmpeg 的 winget | `install_deps.ps1:357-358` | **常见是** | `Gyan.FFmpeg` 是机器级 MSI；若 `DepsRoot\tools\ffmpeg` 已有或 PATH 已有，则不会走这里 |
| VC++ 2015-2022 Redistributable | 当前未安装 | **补装时需要** | 属于 `spec.md:162-170` 列出的缺口 |
| Playwright Chromium | `install_deps.ps1:333-340` | **否** | `python -m playwright install chromium` 默认装到用户目录 |
| 写 `D:\AI_Deps` | `install_deps.ps1:101-119,201-211` | **一般否** | 普通建目录；只有卷 ACL 限制当前用户时才需要提权 |
| Python / Ollama 的 winget | `install_deps.ps1:74,396` | **通常否** | 取决于包的作用域；Python 默认用户级，Ollama 以实际安装器为准 |

因此真正的 UAC 来源是：**Console 的 RunAs 本身、长路径、ffmpeg MSI（以及未来补装的 VC++）**。Chromium 和 `D:\AI_Deps` 不是主要矛盾。

### 2.3 可选方案与代价

| 方案 | 做法 | UAC 次数 | 无人值守 | 代价/限制 |
|---|---|---|---|---|
| A. 保持现状 | 每次点“全量安装依赖”弹一次 | 每次 1 次 | 否 | 人不在电脑前就卡住 |
| B. 一次性管理员引导 | 首次由人在场以管理员跑一次完整安装；之后日常不再提权 | 一次性 1 次 | 首次否，之后是 | 最简单；但“首次”仍需人 |
| C. 最高权限计划任务 | 预先创建 `RunLevel=Highest` 的安装任务，之后用 `schtasks /Run` 触发 | 创建任务时 1 次，之后 0 次 | 是（有条件） | 必须保持当前用户交互登录；用 SYSTEM 会把用户级环境变量/工具装进 SYSTEM 配置，**不可用** |
| D. DetectOnly + 单次管理员批处理 | 先 `-DetectOnly`（不提权）列出“需要管理员的 N 项”，再用一个 elevated 批处理只做长路径/VC++/ffmpeg，其余非提权 | 1 次（仅当有 admin 项） | 预检可无人；执行仍需一次授权 | 需要拆分脚本与幂等；收益是“最多一次”且可解释 |
| E. 离线依赖包 | 预置 ffmpeg zip、wheels、Chromium、模型、VC++ 安装包到 `DepsRoot`/仓库 | 取决于是否仍需长路径/VC++ | 大幅减少网络与 ffmpeg UAC | 包体大；模型许可；仍需处理长路径 |
| F. 预置镜像/Intune/Autopilot | 系统镜像或企业下发时预开长路径、预装 VC++/ffmpeg | 0 次 | 是 | 超出仓库范围；适合批量部署 |
| G. 关 UAC / `runas /savecred` / PsExec -s / UI 自动点 UAC | — | 0 次 | 是 | **不可行且越界**：降低系统安全边界，或把凭据留在本机；不建议在任何版本采用 |

关键结论：**脚本无法“取消”UAC。** 能做到的只有两种：
1. 把多次降为一次（B/D）；
2. 在无人值守前完成一次“预授权”（C/F），之后零弹窗。

### 2.4 推荐方案

**桌面有人的常规场景：D + B。**

1. Console 先跑 `install_deps.ps1 -DetectOnly`（不提权），把结果分成“普通项 / 需要管理员项”。
2. 只有存在管理员项时，才弹一次 UAC，执行一个最小化的 `install_deps_admin.ps1`：
   - 开 `LongPathsEnabled`（`install_deps.ps1:213-225` 的逻辑原样搬过去）；
   - 需要时安装 VC++ Redistributable；
   - 仅在 `DepsRoot\tools\ffmpeg` 与 PATH 都没有 ffmpeg 时，才走 winget MSI。
3. 管理员批处理结束后，主安装脚本以普通权限继续跑 pip / 模型 / Chromium / Ollama。
4. 最后用 `-DetectOnly` 再复核一次，确保提权项已生效（长路径需重启才对已有进程生效，见 `install_deps.ps1:219`）。

**真正无人值守（人不在电脑前）：C。**

- 任务必须用**当前用户 + `InteractiveToken` + 最高权限**，不能用 SYSTEM；
- 机器必须保持该用户已登录（锁屏可以，注销不行）；
- 触发方式 `schtasks /Run /TN <安装任务名>`，stdout/stderr 落到 `pipeline_ui/_console/env_jobs/<id>/`；
- 任务创建本身仍需一次管理员授权；如果连这一次都无法保证，只能走 F（镜像/下发）。

**不建议把 E 作为前置条件**：它能让安装更快、更稳，但不能消除长路径/VC++ 的提权需求；可以作为可选加速包。

### 2.5 全量安装完成后，是否就满足全部需要

**不满足。** `install_deps.ps1` 解决的是 Python 依赖、模型、ffmpeg、Ollama、Chromium 这些“可脚本化”的部分；下面这些仍必须人工或额外处理：

| # | 剩余步骤 | 依据 | 能否自动化 |
|---|---|---|---|
| 1 | 安装剪映专业版 5.9（仅支持 5.9.0.11632） | `快速上手.md:38-43`；脚本不代装 | 否（需安装包/人工） |
| 2 | ~~首次手动启动剪映，完成登录并同意协议，再关闭~~ | 原 `快速上手.md:20`、`使用指南.md:23` | **已删除**：本地建草稿/导出无需登录，本条不成立 |
| 3 | 在剪映新建空草稿模板 `default`（或 `pipeline_test`/`demo_test`） | `快速上手.md:75`；`pipeline.py` 按此顺序找模板 | 否（首次需人工）；**已在环境预设页“剪映前置”提醒** |
| 4 | 在剪映“我的预设 → 组合预设”创建/导入预设，名字与 `presets_a/b` 一致 | `快速上手.md:76` | 否（预设库是本机资产）；**已在环境预设页“剪映前置”提醒** |
| 5 | 复制 `presets.example.json` → `presets.json`，填 `series_root`/`export_dir`/`deps_root`（`jianying_dir`/`archive_root` 可选） | `快速上手.md:84-88`；`presets.example.json` | 半自动（Console 可编辑；**业务根已必填、无字面量默认值**） |
| 6 | 填智谱 GLM-4-Flash API key（翻译主链路） | `presets.example.json:33`；`install_deps.ps1:577` | 否（密钥） |
| 7 | 确认 Ollama 模型已拉取且与 `presets.translate.ollama.model` 一致 | `install_deps.ps1:414-418,578` | 半自动（脚本会 pull，失败需人工重跑） |
| 8 | 注册 `TraeDramaWatcher` 计划任务 | `envinit.py`；`使用指南.md` | **可选配置**：不注册只影响自动托管，不强制拉起 |
| 9 | 长路径开关生效需重启 | `install_deps_admin.ps1` | 否（重启） |
| 10 | 运行库缺口（按“实际被代码使用”复核，见 §2.5.1） | 本轮核对 | 已补：VC++ 检测/安装、MediaInfo 可解析性、numpy 一致性；CUDA 终检已有 |
| 11 | 把短剧素材目录放到 `series_root` 下；watcher 路径还需要 `cover_job/job.json` 与封面等触发资产 | `presets.example.json`；`docs/manual-entry-real-run.md:11` | 暂不要求（本轮忽略） |

尤其注意第 3、4 项：它们不在 `install_deps.ps1` 的 Step 里，缺了会分别表现为“创建空白草稿失败”“预设被静默跳过”，所以放在**环境预设**页提醒，而不是塞进一键安装。

#### 2.5.1 五项“缺口”逐条复核（以代码实际调用为准）

| 缺口 | 代码里是否真的用 | 原来的检查/安装 | 本轮结论与处理 |
|---|---|---|---|
| MediaInfo DLL | 仅 `.trae/skills/.../local_materials.py` 通过 `pymediainfo` 读取，失败**自动回退 ffprobe** | 未检查 | **不是主链路缺口**。补 `MediaInfo.can_parse()` 检查：失败仅 warn 并说明走 ffprobe |
| imageio-ffmpeg | 全仓库检索 **0 处调用** | 未检查、未安装 | **不是缺口**，从清单移除，不再安装/检查 |
| VC++ Redistributable | `onnxruntime`（rapidocr UI OCR）与 `torch` 的必需运行库 | 未检测、未安装 | **真缺口**。新增 `install_deps_admin.ps1` 检测 `vcruntime140(_1).dll` 与注册表，缺失时 winget 安装；依赖自检新增一行 |
| CUDA 终检 | `torch.cuda.is_available()` 决定 ASR 设备 | `install_deps.ps1` 末尾与 Console 依赖报告**已有** | 已覆盖，无需新增 |
| onnxruntime/numpy 冲突 | `funasr-onnx` 元数据 pin 与独立 `ui_pylibs` 的 `numpy==2.2.4` | **已消除**：`ui_pylibs` 目录已整体移除，rapidocr 与主链路共用全局 numpy/onnxruntime，依赖自检按 `requirements.txt` 统一校验 | 无 |

### 2.6 风险与回滚

本轮不改代码，无回滚动作。下一轮若落地拆分脚本：

| 风险 | 缓解 |
|---|---|
| 管理员批处理只做了一半（长路径开了、ffmpeg 失败） | 每项独立幂等 + 最后 `-DetectOnly` 复核 + 摘要列出 `[OK]/[MISS]` |
| 以 SYSTEM 跑任务污染用户环境 | 明确禁止；任务必须 `InteractiveToken` |
| UAC 取消导致 job 卡在 running | `start_install()` 已有 `launch-failed` 写 `exit`（`envinit.py:1133-1138`），复用同一机制 |
| 旧脚本被替换后无法回退 | 保留 `install_deps.ps1` 入口不变，新增 admin 脚本作为可选步骤；失败可直接回到现状 |

---

## 3. 问题 7：watcher 停掉后为什么自动拉起，怎样只在测试时彻底停住

### 3.1 自动拉起的来源

不是单一来源，至少有六条：

| # | 来源 | 位置 | 触发方式 | 作用 |
|---|---|---|---|---|
| 1 | Windows 计划任务 `TraeDramaWatcher` | `pipeline_ui/console/envinit.py:1005-1016` | `schtasks /Create /SC MINUTE /MO 5` | 每 5 分钟运行一次 `watcher_supervisor.ps1` |
| 2 | 另一套任务 XML 口径 | `pipeline/_work/TraeDramaWatcher.new.xml:24-45`、`pipeline/watcher_launch.vbs:15-23` | 每 10 分钟 + 登录触发，经 `wscript` 隐藏启动 | 只有用 XML 注册/导入时才生效；任务名相同，会覆盖第 1 套 |
| 3 | supervisor 自身 | `pipeline/watcher_supervisor.ps1:126-234` | 每次被任务唤起 | 发现无 `cover_watch` 进程就 `Win32_Process.Create` 拉起 |
| 4 | 卡死看门狗 | `pipeline/watcher_supervisor.ps1:152-181` | 心跳超过 `stall_heartbeat_s`（默认 900s） | `taskkill /F /T` 后重启 |
| 5 | `delayed_start.ps1` | `pipeline/delayed_start.ps1:224-233` | 延迟结束且 `job.json` 就绪 | 主动 WMI 拉起 watcher |
| 6 | `batch_runner --watch` | `pipeline/batch_runner.py:319-322,370-395` | 默认开启 | 启动的是 `scripts/utils/watcher.py`（进度通知器），**不是** `lib/cover_watch.py` |

补充说明：

- `cover_watch` **没有进程内重启循环**：它只有任务级/导出级的重试与退避（`lib/cover_watch.py:543-590,1192-1217`），进程退出后必须靠 supervisor 拉起；所以“自动复活”不是它自己做的。
- `watch_jianying.py` 只是“监控某草稿 JSON 变化”的调试脚本（`watch_jianying.py:1-5`），不是 watcher 守护，也不是拉起来源。
- `watcher_stall.ps1` 是残留进程清理工具（`使用指南.md:380`），不是拉起来源。
- 第 2 条的 XML 还有分发隐患：`TraeDramaWatcher.new.xml:43` 的动作参数写死了本机绝对路径 `d:\剪映5.9\...\watcher_launch.vbs`。分发到别的机器后，即使导入这个 XML，任务也会因为路径不存在而拉不起来；当前 Console 的注册逻辑（`envinit.py:1005-1016`）用的是运行时路径，不受影响。
- Console 自己启动的批量任务固定追加 `--no-watch`（`pipeline_ui/console/runner.py:4`），所以 Console 任务不会额外拉起第 6 条里的进度通知器。
- 用户手工跑 `batch_runner` 时如果没加 `--no-watch`，会看到“Watcher 已后台启动”，容易误以为是 `cover_watch`；这是第 6 条的干扰项。

所以“我明明停掉了，过几分钟又活了”的主因是 **第 1 + 第 3 条**：计划任务每 5 分钟唤醒 supervisor，supervisor 发现进程不在就拉起。若进程还在但心跳停了，则是第 4 条杀掉再拉起。

### 3.2 现有的“停”能力盘点

| 能力 | 位置 | 现状 |
|---|---|---|
| 查询计划任务 | `envinit.py:531-538` `task_status()` | 只读 `schtasks /Query`，返回 registered/enabled |
| 注册/启用计划任务 | `envinit.py:1005-1016`、`app.py:417-418` | 只有 `task.register`，没有 `task.disable` |
| 停止 watcher | `pipeline_ui/console/status.py:410` | 注释明确“只读，不做停止控制” |
| 哨兵/暂停文件 | 无 | supervisor 不检查任何暂停标记 |
| 命令行手动禁用 | Windows 自身 | `schtasks /Change /TN TraeDramaWatcher /DISABLE` 可用，但 Console 不暴露 |

结论：当前没有任何“官方暂停开关”；要停只能手工杀进程，然后被计划任务重新拉起。

### 3.3 可选方案

| 方案 | 做法 | 影响面 | 恢复方式 | 评价 |
|---|---|---|---|---|
| A. 哨兵文件 | supervisor 启动前检查 `pipeline/_work/watcher.pause`，存在就记一行日志并退出 0 | 计划任务照常触发但空转；不杀当前进程 | 删除哨兵；下一次任务（≤5 分钟）自动恢复，或手动 `schtasks /Run` | 推荐；语义清晰、可持久、易审计 |
| B. 禁用计划任务 | `schtasks /Change /TN TraeDramaWatcher /DISABLE` | 立即停止后续唤醒；不影响当前进程 | `/ENABLE` | 零代码兜底；但 Console 看不到“为什么停了” |
| C. Console 按钮 | 环境页“暂停托管/恢复托管”，内部写/删哨兵或禁用/启用任务 | 与 A 或 B 相同 | 点按钮 | 推荐作为 A 的 UI 外壳 |
| D. 只杀进程 | `taskkill /PID <cover_watch>` | 5 分钟内会被重新拉起 | 无需恢复（会自动回来） | 只能用于临时打断，不能“彻底停住” |
| E. 改任务触发器/删除任务 | `schtasks /Delete` | 彻底移除自动托管 | 重新注册 | 过重，容易忘恢复 |

### 3.4 推荐方案：哨兵 + Console 按钮

**哨兵路径**：`pipeline/_work/watcher.pause`（`_work` 已在忽略范围内，不会污染仓库）。

“彻底停住”的完整语义是：**哨兵阻止后续拉起 + 当前没有活动任务**。如果此刻还有导出在跑，正确做法是等它结束（或走既有的安全停止流程），而不是在测试暂停时强杀进程；暂停按钮在有活动任务时只写哨兵、不杀进程，并明确提示“当前任务仍在运行”。

**最小改动（P0，纯后端）**：

1. `watcher_supervisor.ps1` 在 `Invoke-Supervisor` 开头、WMI 扫描之前检查：
   - 文件存在 → `Write-Log "托管已暂停（哨兵存在）"` → `return 0`；
   - 不杀当前进程，不修改任何任务。
2. `delayed_start.ps1` 在 `Start-Watcher` 之前做同样检查，存在则记日志并返回 0。
3. `doctor` / Console 的 `task_status()` 增加 `paused` 字段：哨兵存在时显示“已暂停（测试模式）”，避免用户忘记恢复。

**P1（有 UI）**：

- 环境页新增“暂停自动托管 / 恢复自动托管”：
  - 暂停：写哨兵；如果当前有 `cover_watch` 且没有 `.watcher.lock` / 导出锁，再提供“停止当前 watcher”按钮；有活动任务时拒绝，提示先等导出结束。
  - 恢复：删除哨兵；可选立即 `schtasks /Run /TN TraeDramaWatcher`，不必等 5 分钟。
- 按钮只写哨兵，不改 `presets.json`、不碰 `job.json`，符合 Console 现有“不写业务文件”的边界（`paths.py:1-5`）。

**为什么不用“直接禁用任务”作为主方案**：

- 任务被禁用后，`task_status()` 只显示“已注册但被禁用”，没有“这是测试暂停”的语义；
- 某些机器会用 XML 重新注册任务，禁用状态可能被覆盖；
- 哨兵是文件，跨任务重注册仍然有效，且可以被 doctor/Console 明确展示。

### 3.5 改动面

| 文件 | 改动 | 风险 |
|---|---|---|
| `pipeline/watcher_supervisor.ps1` | 开头增加哨兵判断 + 日志 | 低；只影响“是否拉起” |
| `pipeline/delayed_start.ps1` | `Start-Watcher` 前增加同样判断 | 低 |
| `pipeline_ui/console/envinit.py` | `task_status()` 增加 `paused`；新增 `task.pause` / `task.resume`（P1） | 低 |
| `pipeline_ui/console/app.py` | 暴露 `task.pause` / `task.resume`（P1） | 低 |
| `pipeline_ui/console/static/*` | 环境页按钮（P1） | 低 |
| `pipeline/doctor.py` / `doctor_core.py` | 自检显示“托管已暂停” | 低 |
| `lib/cover_watch.py` | **不动** | 无 |

### 3.6 风险与回滚

| 风险 | 影响 | 缓解/回滚 |
|---|---|---|
| 忘记删哨兵，watcher 长期不工作 | 新剧不再自动处理 | Console 顶部/doctor 持续显示“托管已暂停”；日志每轮写一行 |
| 暂停时仍有活动任务 | 用户以为“已停”，实际还有导出在跑 | 暂停按钮先检查 `.watcher.lock` 与导出锁；有活动任务时只写哨兵、不杀进程，并提示 |
| 计划任务仍每 5 分钟空转 | 极小的日志/CPU 开销 | 可接受；必要时同时禁用任务（方案 B） |
| 回滚 | — | 删除两处哨兵判断；已存在的哨兵文件可直接删除，无配置残留 |

### 3.7 验证方式

1. 创建 `pipeline/_work/watcher.pause`；
2. 手工结束 `cover_watch`；
3. 等待 ≥6 分钟（5 分钟任务 + 余量），确认没有新的 `cover_watch` 进程；
4. 查看 `pipeline/_work/watcher_supervisor.log` 出现“托管已暂停”；
5. 删除哨兵，等待 ≤5 分钟或 `schtasks /Run /TN TraeDramaWatcher`，确认进程恢复。

---

## 4. 问题 8：手工串流与助手执行的信息差

### 4.1 先定义三条链路

| 链路 | 入口 | 运行记录落点 |
|---|---|---|
| Console 任务 | `pipeline_ui/console/runner.py` 启动 `batch_runner`/`ui_pipeline` | `pipeline_ui/_console/runs.jsonl` + `logs/<run_id>.log` |
| 手工串流 | 用户在终端直接跑 `python pipeline\batch_runner.py ...` | `pipeline/_work_*/_batch_state.json`（逐集状态）；无 run 记录 |
| 助手执行 | 助手在自己的 Shell 里跑同一条命令 | 同“手工串流”；另有工具返回的 stdout/exit code，但不落盘 |
| watcher 托管 | `lib/cover_watch.py` | `cover_job/job.json`、`_work/watcher_heartbeat.json`、`.watcher.lock`、`watcher_console.log` |

### 4.2 逐项对比：能看到什么、看不到什么、为什么

| 维度 | Console 任务 | 手工串流（终端） | 助手执行 | 真源/原因 |
|---|---|---|---|---|
| 阶段 | 有：正则匹配阶段表，映射到“建稿/人声分离/预设/裁剪/识别翻译/终检/导出” | 只有滚动文本；需要自己看关键词 | 同手工，但助手可以再读文件 | 阶段表在 `runner.py:27-46`；正则解析在 `runner.py:49-61`，只存在于 Console 进程内，未落盘 |
| 进度 | 有：阶段权重 + 导出 `percent_value` | 只有 stdout 里的百分比/行数；无统一百分比 | 同手工 | `runner.py:658-708`；导出进度文件由导出器写（`status.read_progress`，`status.py:168-195`） |
| 日志 | 有：`pipeline_ui/_console/logs/<run_id>.log` | 终端回滚缓冲，关闭即丢；除非自己 `Tee-Object` | 工具返回的 stdout，可能被截断/超时丢弃 | `runner.py:304` 定义 `log_path`，`runner.py:385` 打开写入；手工/助手没有自动 tee |
| 产物 | 有：正则提取 `后台导出 → <path>`，并可从清单补 | 能看到 stdout 打印的路径；需自己记 | 能看到打印路径，也能读 `_stream_manifests` | 产物路径正则 `runner.py:54,432`；权威清单 `pipeline/_stream_manifests/<草稿>.json`（`stream_manifest.py:40`；建稿时登记 `pipeline.py:3351-3362`；导出后回填 `lib/job_manifest.py:1750-1790,1949-1955`） |
| 失败分类 | 有：导出进度里的 `failure_class`，或 `subprocess_exit_nonzero` | 只有 traceback 文本，没有类别 | 同手工；助手能读 traceback 并自行判断 | `runner.py:547,706-707`；分类器在 `lib/failure_class.py`，主要服务导出阶段 |
| 清单/台账状态 | 有：`status.job_info()` 读 `cover_job/job.json` | 需要自己打开 `job.json` | 助手可以读同一文件 | `status.py:199`；`job.json` 由 watcher/流程写 |
| run id | 有：`YYYYmmdd-HHMMSS-xxxx` | 无 | 无 | `runner.py:293` 只给 Console 生成 |
| 退出码 | 有：`run["exit_code"]` | 终端 `$LASTEXITCODE`，不看就丢 | 工具结果里有 | `runner.py:538`；手工没有持久化 |
| 重试次数 | 有：单集重试的异常文本，但没有结构化 `attempts` | 能看到每次 `⚠️ 第N次尝试失败` | 同手工 | `batch_runner.py:714-734` 只 print；`_batch_state` 不记 attempts |
| 参数/环境 | 有：`params` + `cmd` 完整记录 | 只有 shell 历史/自己记得 | 工具调用里有完整命令 | `runner.py:295-313`；手工/助手无统一记录 |
| 进程 pid | 有：`_proc.pid`，停止时按 pid 杀 | 用户可在任务管理器看 | 助手可查 `tasklist`/WMI | `runner.py:324-340` |
| 逐集明细 | 有：`_batch_state.json` 被 Console 聚合为 `manual:` 行 | 有 `_batch_state.json`，但字段很少 | 同手工 | `batch_runner.py:193-207,703-744`；Console 聚合见 `status.py:766-831` |

`status.py` 自己把差距写在了注释里：

> 这份快照只有逐集 `status/finished_at`，没有 run id、退出码和完整阶段，因此状态是推断值，不能当作与 `runs.jsonl` 等价的运行记录。  
> —— `pipeline_ui/console/status.py:768-770`

Console 把手工跑出来的 `_work_*` 快照标成 `source: "manual"`、`id: "manual:<workdir>"`，`started_at` 为空、`artifact` 为空、`attempts`/`last_error` 为 `None`（`status.py:805-830`）。这就是用户看到的“手工串流只有一行，点进去没有细节”。

**手工/助手明确看不到的项**（不是没权限读文件，而是根本没有落盘）：

- 没有 Console 生成的 `run_id`，也没有 `runs.jsonl` 里的运行记录；
- 没有统一阶段百分比，只有原始 stdout 文本；阶段映射只存在于 Console 进程内；
- 没有稳定的 `failure_class`，只有 traceback 文本（导出阶段的分类除外）；
- 没有 `artifact` 回填、`attempts` 计数、`started_at/ended_at`；
- 助手额外拥有工具返回的退出码和完整命令，但这份输出本身不会落盘，超时/截断后不可追溯。

原因可以归成三条：**stdout 没有结构化、pipeline 侧没有 run 记录、失败分类只在导出状态机里。**

### 4.3 差距的根因

1. **`batch_runner` 是 print-first**：阶段、进度、错误都只是人类可读文本，没有结构化事件；`runner.py` 的正则只服务 Console，手工/助手不复用。
2. **运行台账只在 Console**：`runs.jsonl` 由 `runner.py:647-654` 写；手工/助手没有等价的 run 记录。
3. **`_batch_state.json` 刻意最小化**：逐集只写 `status/ep/draft/finished_at`（`batch_runner.py:703-744`），失败不写 `error`/`error_class`/`attempts`/`artifact`。
4. **没有自动日志 tee**：手工/助手不重定向就没有完整日志；Console 有 `logs/<run_id>.log`（`runner.py:304,385`）。
5. **失败分类只覆盖导出侧**：`lib/failure_class.py` 的分类由导出状态机写入 `.export_state.json`；batch 单集失败的 traceback 没有进入分类器。
6. **stdout 编码/缓冲差异**：Console 显式设置 `PYTHONIOENCODING=utf-8`、`PYTHONUNBUFFERED=1`（`runner.py:365-367`）；手工 Windows 控制台可能是 GBK，中文/emoji 会乱码，助手看到的又是另一套编码。
7. **助手的输出是临时的**：工具返回的 stdout 不会自动落盘，命令超时被转后台后更难取；手工的终端回滚缓冲也会丢。

### 4.4 缩小差距的建议

按优先级：

| 优先级 | 动作 | 落点 | 效果 |
|---|---|---|---|
| P0 | `batch_runner` 自动写 `pipeline/_work_*/_run.json`：`run_id`、`argv`、`started_at`、`ended_at`、`exit_code`、`status`、`workdir`、`log_path` | `batch_runner.py` 入口/收尾 | 手工/助手也有 run 记录 |
| P0 | 自动 tee stdout/stderr 到 `_work_*/run-<run_id>.log` | `batch_runner.py` 或统一的 `tee` 包装 | 日志不再依赖终端回滚 |
| P0 | 扩展 `_batch_state.json` 单集字段：`started_at`、`attempts`、`error`、`error_class`、`artifact` | `batch_runner.py:703-744` | Console 的 `manual:` 行能显示失败原因与产物 |
| P1 | stdout 增加结构化事件行，例如 `@@EVENT {"type":"stage","key":"export",...}`，保留原有人类文本 | `batch_runner.py` + `runner.py` 解析 | 机器可解析，人工也可读 |
| P1 | 新增只读 `pipeline/run_status.py --workdir <dir> --json` | 新文件 | 助手不用猜文件格式；Console 也可复用 |
| P2 | Console `external_history()` 读取 `_run.json`，填充 `started_at/artifact/attempts/last_error` | `status.py:766-849` | 手工/助手记录与 Console 记录同构 |
| P2 | 失败分类接入 batch 单集异常：复用 `lib/failure_class.classify()` | `batch_runner.py` | 单集失败也有稳定类别 |
| P3 | 统一 `runs.jsonl` 与 `_run.json` 的 schema，增加 `source: console|manual|assistant|watcher` | 两边 | 跨入口检索/对比 |

不建议的做法：

- 不要要求“所有运行都必须走 Console”来抹平差距——用户明确需要手工/助手入口；
- 不要把 `runs.jsonl` 变成 pipeline 的硬依赖（Console 是可选层）；
- 不要为了结构化而破坏现有 stdout 文案（`runner.py` 的 regex 和用户习惯都依赖它）。

### 4.5 推荐方案与改动面

**推荐先做 P0 三项 + `run_status.py`（P1）**，因为它们同时解决手工和助手两侧，且不改 Console 的既有契约。

| 文件 | 改动 | 风险 |
|---|---|---|
| `pipeline/batch_runner.py` | 生成 `_run.json`；tee 日志；扩展 `_batch_state` 字段 | 中低；需要保证原子写、异常路径也收尾 |
| `pipeline/run_status.py`（新增） | 只读聚合 `_run.json`/`_batch_state`/`job.json`/清单 | 低；纯只读 |
| `pipeline_ui/console/status.py` | `_batch_state_entries()` 优先读 `_run.json`，缺省回退旧逻辑 | 低；向后兼容 |
| `pipeline_ui/console/runner.py` | 可复用同一解析器（可选） | 低 |
| `tests/test_external_tasks.py` | 新增 `_run.json` 读取、旧格式回退用例 | 低 |

### 4.6 风险与回滚

| 风险 | 影响 | 缓解/回滚 |
|---|---|---|
| 日志体积增长 | 磁盘占用 | 按 run 分文件 + 保留 N 份；复用 supervisor 的轮转思路 |
| 并发写 `_run.json` | 半截 JSON | 临时文件 + `os.replace` 原子替换（仓库已有此模式） |
| 旧 Console 不识别新字段 | 忽略即可 | 新字段只增不改；`_batch_state` 旧字段保持不变 |
| 状态语义漂移 | 手工行从“推断”变“可信” | 在 `_run.json` 里写 `schema_version`，Console 只对已知版本做“可信”标记 |
| 回滚 | — | 删除 `_run.json` 写入与读取分支，`_batch_state` 继续按旧字段工作 |

### 4.7 验证方式

1. 终端跑一集（`--no-watch --limit 1`），确认生成 `_run.json` 与 `run-*.log`；
2. `python pipeline\run_status.py --workdir <workdir> --json` 输出与 Console 任务页字段一致；
3. 中途 Ctrl+C / kill，确认 `_run.json` 的 `status` 为 `stopped` 或 `failed`，且 `error` 有内容；
4. 用旧版 `_batch_state.json`（无新字段）跑 Console，确认仍能显示 `manual:` 行（回退路径）。

---

## 5. 上一轮“实施文档”落盘核查

### 5.1 实际文件与准确路径

| 文件 | 准确路径 | 时间 | 状态 |
|---|---|---|---|
| 上一轮 Spec | `spec.md`（仓库根） | 2026-11-16 11:15 | 标题《Console 弹窗统一 + 环境检查收敛 + presets/watcher 说明》，状态“待批准（Spec）” |
| 上一轮任务清单 | `tasks.md`（仓库根） | 2026-11-16 11:15 | 状态“已完成” |
| 更早的 Console 设计稿 | `docs/web-console-plan.md` | 2026-11-15 21:53 | 只做设计，不含实现 |

### 5.2 为什么用户“没看到”

上一轮的实际产物是**仓库根目录的 `spec.md` + `tasks.md`**，不是 `docs/` 下的实施文档。如果当时的承诺是“放到 `docs/`”，那这份文件确实没有落盘；`docs/` 里唯一相关的是更早的 `web-console-plan.md`，不是本轮追问对应的实施文档。

### 5.3 本文件定位

本文件就是对上一轮“实施文档”的补齐，同时回答本次四个追问。它与 `spec.md` 的分工：

- `spec.md`：上一轮的变更 Spec（弹窗统一、检查收敛、8A/8B 结论）；
- 本文件：四个追问的事实核查 + 可执行方案 + 改动面/风险/回滚。

---

## 6. 总改动面与建议实施顺序

| 顺序 | 事项 | 文件 | 风险 | 是否本轮 |
|---|---|---|---|---|
| 0 | 本实施文档 | `docs/console-doctor-followup-implementation.md` | 无 | 是 |
| 1 | Q3-A：默认来源可见 | `pipeline/export_config.py`、`doctor_core.py` | 低 | 否 |
| 2 | Q7-A：watcher 暂停哨兵 | `pipeline/watcher_supervisor.ps1`、`delayed_start.ps1` | 低 | 否 |
| 3 | Q8-P0：`_run.json` + 日志 tee + `_batch_state` 扩展 | `pipeline/batch_runner.py` | 中低 | 否 |
| 4 | Q8-P1：`run_status.py --json` | 新文件 | 低 | 否 |
| 5 | Q6-D：DetectOnly + 单次管理员批处理 | `install_deps.ps1` 拆分/新增 admin 脚本 | 中 | 否 |
| 6 | Q3-B：默认值不存在则失败 | `export_config.py`、测试 | 中低 | 否 |
| 7 | Q7-C：Console 暂停/恢复按钮 | `envinit.py`、`app.py`、`static/*` | 低 | 否 |

顺序理由：先做只读/低风险、能立刻减少误判的项（Q3-A、Q7-A、Q8-P0），再做需要 UI 或安装脚本拆分的项。

---

## 7. 本轮验证清单

- [x] 四个问题的现状事实均已定位到文件与行号；
- [x] `install_deps.ps1` 无 `Read-Host`/`pause`，三处 winget 均带 `--accept-*`；
- [x] 确认 UAC 真正来源（Console RunAs、长路径、ffmpeg MSI；Chromium/`D:\AI_Deps` 通常不需要）；
- [x] 确认 watcher 自动拉起至少六条来源，且 Console 没有停止控制；
- [x] 确认 `_batch_state.json` 字段缺失与 `runs.jsonl` 只由 Console 写；
- [x] 确认上一轮实施文档未落到 `docs/`，实际为根目录 `spec.md` + `tasks.md`；
- [x] 本轮未改代码、未改配置、未删除文件、未启停 watcher/剪映。

---

## 8. 明确不做的事

- 不关闭/降低 UAC，不使用 `runas /savecred`、PsExec `-s`、UI 自动点 UAC；
- 不修改 `pipeline/presets.json` 生产配置；
- 不修改 `lib/cover_watch.py`；
- 不删除任何 `_work_*`、日志、清单或草稿；
- 不启停 watcher、不拉起/关闭剪映、不执行导出；
- 不把 Options 里的“建议”当作已实现——本文件只给方案，落地需另行确认。

---

## 9. 证据索引

| 主题 | 位置 |
|---|---|
| 路径兜底常量 | `pipeline/export_config.py:31-36` |
| 路径解析器 | `pipeline/export_config.py:69-90` |
| 素材根解析 | `pipeline/export_config.py:283-293` |
| 归档根/剪映/导出/依赖根 | `pipeline/export_config.py:221-229,259-261,296-303` |
| 本机配置 | `pipeline/presets.json:37-48` |
| 新机模板 | `pipeline/presets.example.json:4-15,43-46` |
| admission 直接用 watch.roots | `pipeline_ui/admission_draft.py:167-175`、`pipeline_ui/init_draft.py:54-55,75` |
| 安装脚本提权入口 | `pipeline_ui/console/envinit.py:1106-1139` |
| 长路径 HKLM | `pipeline/install_deps.ps1:213-225` |
| winget 安装 | `pipeline/install_deps.ps1:74,357-358,396` |
| DetectOnly | `pipeline/install_deps.ps1:191-196` |
| 安装后手工事项 | `pipeline/install_deps.ps1:567-580`、`快速上手.md:20,71-88` |
| 计划任务注册 | `pipeline_ui/console/envinit.py:1005-1016` |
| 任务状态查询 | `pipeline_ui/console/envinit.py:531-538` |
| supervisor 拉起/卡死重启 | `pipeline/watcher_supervisor.ps1:126-234` |
| XML 任务口径 | `pipeline/_work/TraeDramaWatcher.new.xml:24-45` |
| 延迟拉起 | `pipeline/delayed_start.ps1:224-233` |
| batch_runner 的进度 watcher | `pipeline/batch_runner.py:319-322,370-395` |
| Console 只读边界 | `pipeline_ui/console/status.py:410` |
| Console 运行记录 | `pipeline_ui/console/runner.py:288-313,647-654` |
| Console 阶段解析 | `pipeline_ui/console/runner.py:27-61` |
| 手工快照聚合 | `pipeline_ui/console/status.py:766-849` |
| 逐集状态写入 | `pipeline/batch_runner.py:193-207,703-744` |
| 清单真源 | `pipeline/stream_manifest.py:40`、`pipeline/pipeline.py:3351-3362`、`lib/job_manifest.py:1750-1790,1949-1955` |
| 失败分类器 | `lib/failure_class.py:1-24` |
| 上一轮 Spec | `spec.md:1-19,142-170` |
| 上一轮任务 | `tasks.md:1-20` |

---

## 11. 本轮落地记录（已实施，2026-11）

> 上文 §1–§10 是问题清单与方案分析，属于“改造前基线”；本节记录实际改动，与代码一致。

### 11.1 路径三根统一（Q3）

- `pipeline/export_config.py` 成为唯一真源：`ConfigError`（key/message/hint）、
  `_resolve_required_dir`、`resolve_paths(mode)`、`validate_config(mode)`。
- 删除全部业务根兜底常量（`D:\短剧`、`D:\成片导出`、`D:\短剧_存档`、`D:\AI_Deps`、`C:\AI_Deps`）。
  业务根 `series_root` / `export_dir` / `deps_root` 未配置或显式无效 → 直接 `ConfigError`（fail-closed）。
- **素材根唯一**：`series_root` 只读 `JY_SERIES_ROOT` 或 presets.json 顶层键，不再从 `watch.roots` 回退。
- **`watch.roots` 降级**：仅 watcher 额外扫描根。为空 → 只扫 `series_root`；非空 → 每项须存在、
  是目录、含 `*/cover_job`（不要求位于 `series_root` 下，支持临时素材根）。
  有效扫描根 = `[series_root] + watch.roots` 去重，唯一入口 `get_watch_roots()`。
- 调用方收口：`archive_job.py`、`cover_watch.py`、`series_runner.py`、`admission_draft.py`、
  `init_draft.py`、`admission_test.py`、`doctor_core.py`、`console/paths.py`、`install_deps.ps1`、
  `scripts/freshness_baseline.py`、skill 示例 `robust_auto_export.py`。
- 归档校验：`archive_root` 必须在所有有效扫描根之外；解析失败或有效扫描根为空 → 拒绝执行（fail-closed）。
- 一次性迁移：`pipeline/migrate_config.py --dry-run|--write`（写前备份 `presets.json.bak-<时间戳>`）；
  Console 环境页新增 `config.migrate` 检查项与动作。
- 测试：`tests/test_path_config.py`、`tests/test_migrate_config.py`、`tests/test_archive_job.py`（含配置解析失败用例）。

### 11.2 环境预设 / 环境依赖（Q6）

- 环境页分组调整：`config` + `jianying`（改名为“剪映前置”）渲染到「环境预设」页；其余组件留在「环境依赖」页。
- 删除“首次登录并同意协议”一条（本地建草稿/导出无需登录）。
- 空草稿模板 `default` 与组合预设 `presets_a/b` 改为**环境预设页提醒项**，不进一键安装。
- `TraeDramaWatcher` 计划任务标记为**可选**（warn，不强制拉起）。
- 一键安装改为三段式（`install_deps_admin.ps1` 新增）：
  1. 普通权限跑 `install_deps.ps1 -DetectOnly`；
  2. `install_deps_admin.ps1 -DetectOnly` 输出 `ADMIN_NEEDED=0|1`，仅必要时弹一次 UAC，
     只做长路径开关 / VC++ Redistributable / 缺失 ffmpeg 的 winget 安装；
  3. 回归普通权限跑 `install_deps.ps1` 主安装。
- 依赖自检新增：VC++ Redistributable 可加载性、`MediaInfo.can_parse()`（warn + ffprobe 回退说明）、
  ui_pylibs 与全局 numpy 版本一致性（该目录后已整体移除，此项随之删除）。`imageio-ffmpeg` 经检索无任何调用，从缺口清单移除。
- 主脚本 `install_deps.ps1` 非管理员运行时不再尝试写 HKLM，改为提示由 `install_deps_admin.ps1` 处理。

### 11.3 watcher 暂停（Q7，P0+P1）

- 哨兵文件：`pipeline/_work/watcher.pause`（内容可写原因，仅记日志）。
- `pipeline/watcher_supervisor.ps1`：启动前发现哨兵 → 直接返回 0，不拉起、不做卡死重启。
- `pipeline/delayed_start.ps1`：开始与真正拉起前各检查一次，命中则退出，不写裁剪配置、不拉起。
- Console：`task.pause` / `task.resume`（`envinit.pause_watcher()` / `resume_watcher()`），
  `status.watcher_info()` 增加 `paused` / `pause_path`；环境页运行态条目按暂停状态给对应按钮。
- 暂停只阻止**自动拉起**，不杀正在跑的任务，避免打断导出。

### 11.4 运行记录（Q8，P0+P1）

- `batch_runner.py` 新增运行记录：`<workdir>/_run.json` + `run-<run_id>.log`（stdout/stderr tee），
  字段：`schema_version/run_id/source/argv/cwd/series_dir/workdir/started_at/ended_at/exit_code/status/log_path`。
- `_batch_state.json` 扩展：顶层 `run` 快照（不计入集数）、单集 `started_at/attempts/error/error_class`、
  导出成功回填 `_artifacts`（草稿 → 成品路径）。
- `pipeline/run_status.py`（只读）：列表 / `--workdir` / `--series` / `--json` / `--tail N`。
- Console：`status.batch_state_summary()` 附带 `run`、新增 `latest_runs()`；`/api/runs` 增加 `pipeline_runs`。
- 测试：`tests/test_external_tasks.py` 覆盖 `_run.json` 落盘/收尾、tee 日志、`run` 不计入 total。

### 11.5 双击启动 + 登录自启（Q9）

- 根目录 `启动Console.bat`：双击即起服务并打开浏览器；服务已在跑则只开浏览器（幂等）。
- 登录自启开关已收进 Console：环境初始化 → 环境依赖 → 独立二级页签「Console 服务」，
  通过 `GET/POST /api/console/autostart` 读写「启动」文件夹里的 `TraeJianyingConsole.lnk`；
  登录时静默起服务、不弹浏览器；默认关闭。根目录不再放 `开机自启_开/关.bat`。
- 实现：`scripts/console_launcher.ps1`（UTF-8 BOM），动作 `start|autostart-on|autostart-off|status`；
  `start` 用 `pythonw.exe`（回退 `python.exe`）隐藏窗口拉起 `pipeline_ui\console\app.py`，
  健康检查 `/api/health` 最多等 20 秒，日志写 `pipeline_ui/_console/logs/server.out.log|server.err.log`。
- 端口固定 `127.0.0.1:8765`；排障用 `powershell -File scripts\console_launcher.ps1 -Action status`。
- 注意：在 Trae 的 Shell 里跑 `start` 会被作业对象回收，双击/登录自启不受影响；Trae 内调试仍用 `run_in_background`。

### 11.6 配置与页面细节修正（Q10）

- `presets.json` 顶部改为「先配这四项」：`series_root`、`export_dir`、`archive_root`、`deps_root`
  紧跟 `_说明`；删除与 `presets_a` 重复的旧裸键 `presets`；`watch.roots` 置空（只扫 `series_root`）。
- `presets_root` 明确为「剪映组合预设库目录」，不参与预设组解析：
  `pipeline.py --list-presets` 与 `cover_watch._available_preset_profiles()` 均改为只统计
  `isinstance(v, list)` 的 `presets_*` 键（之前会把 `presets_root` 误认成组 `root`）。
- Console 环境页「环境预设」标签：编辑区拆成「基础四件套（与 presets.json 顶部顺序一致）」与
  「其他路径（留空则自动探测）」两段，顺序与 JSON 一致；标签上 `presets_root` 注明「是目录，不是预设组」。
- 首屏自动盘点：进入环境页即调用 `/api/env/inventory` 渲染分组（含「剪映前置」），
  不再需要先点「完整检查」才看得到内容。

### 11.7 界面一致性改造（2026-11，A/B/C/D）

- 检查入口唯一：全页只在 `page-head` 保留一个「完整检查 / 重新检查」按钮；
  各页签与健康条只展示状态摘要，不再各自放检查按钮（后端目前只有全局检查，没有分域检查）。
- 「Console 服务」在「环境依赖」下是独立二级页签（不跟随「托管」），
  切到该页签时隐藏依赖明细，只留登录自启开关与快捷方式路径。
- 任务页：右侧状态卡在宽屏下 sticky 跟随；顶部新增环境引导条（预设缺失 / 检查失败时出现，可一键跳环境页）。
- 状态栏「服务 / 当前任务 / 导出锁」可点击：服务 → Console 服务页签，其余 → 任务页。
- 历史页新增搜索 + 状态筛选；归档页新增草稿名筛选；空态补「去提交任务」入口。
- 配置编辑器：有未保存改动时显示「未保存」标记，`Ctrl/Cmd+S` 保存，「放弃修改」前二次确认，关页前提示。
- 依赖安装进行中显示不确定进度条；安装卡与实时状态区加 `aria-live`。
- 切页记录并恢复滚动位置；深色主题提高弱文本对比度。

