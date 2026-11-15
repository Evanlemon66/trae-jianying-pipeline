# 手工入口真实流程验收手册（`D:\短剧` + watcher 共存）

适用场景：用一部新剧，完全按《快速上手》的真实流程跑（剧集目录就放在 `D:\短剧\<剧名>`、watcher 保持运行、默认导出目录、真实清单目录、真实归档根），跑完后用手工入口做归档与复核，验证「`archived` 但归档缺失」这类状态矛盾能不能真的报出来。

> 本文只做流程与依据整理。执行前先看第 1 节现场快照；下面的命令不包含任何删除真实数据、`--all`、`--force` 操作。

---

## 0. 三个结论（先回答质疑）

1. **测试剧可以放 `D:\短剧\<剧名>`，与 watcher 共用根目录没问题**——前提是这部剧没有 `cover_job\job.json`。watcher 只扫描 `<root>\<剧名>\cover_job\job.json` 发现任务，而且还要通过「4 张封面 + prompts.json + titles.gate=done」的触发判定；纯 `batch_runner` 流程不会创建 `job.json`，所以不会被调度。真正的冲突来自「同一部剧既有 job.json、又手工跑脚本」，详见第 2.1 节。
2. **归档可以直接用真实归档根 `D:\短剧_存档`**。代价是生产归档池里会留下这部测试剧的目录、清单 `paths.export_file` 会被改写成归档路径；如果这部剧同时有 `job.json`，job 台账不会跟着改，之后默认的 `archive_job` 会因「目标已存在」或「成品三件套缺失」而跳过/冲突。新剧没有 job 时，直接用真实根测是可行的。
3. **清单可以直接用真实目录 `pipeline\_stream_manifests`**（已被 `.gitignore` 忽略）。只要剧名唯一，不会碰到别的剧。风险是台账永久留痕、`list / verify --all / clean --all` 都会带上它、同名重跑会叠加版本；清单写入不是原子写、也没有文件锁，所以同一部剧不能同时开两个跑批。

---

## 1. 现场快照（2026-11-15 盘点）

| 项 | 实测 | 判定 |
|---|---|---|
| 剪映版本 | `5.9.0.11632`（`presets.json` 指向该安装目录） | 通过 |
| 剪映默认草稿模板 | `%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\default` 存在 | 通过 |
| 组合预设 | 预设库 9 个；`presets_a` 的 4 个名字都能对上 | 通过 |
| Python | `3.12.10` | 通过 |
| ffmpeg / ffprobe | `D:\AI_Deps\tools\ffmpeg\bin\` 下均在 | 通过 |
| 磁盘 | C 盘 102.3 GB、D 盘 112.0 GB 空闲 | 通过 |
| 剧集根 | `D:\短剧` 现有 23 部；测试剧名无冲突 | 可放 |
| 导出池 | `D:\油管打猎\L` 现有 42 个目录 | 默认导出位置 |
| 归档池 | `D:\短剧_存档` 现有 71 个目录 | 默认归档位置 |
| 清单 | `pipeline\_stream_manifests` 现有 34 份，均无 `versions` | 可新增 |
| `.export.lock` | **占用中**：pid 27136，owner=`pipeline_ui export`，21:32:57 起 | **当前不能开跑** |
| watcher | 计划任务 `TraeDramaWatcher` 为 Ready；本次盘点未发现有 `watch_jianying` 进程 | 以现场为准 |

> 上面两条运行态是会变的。每次开跑前重新执行第 2 步的预检，不要沿用这里的结论。

---

## 2. 逐步操作手册

所有命令都在仓库根目录执行：

```powershell
Set-Location "d:\剪映5.9\剪映5.9版+jianying-skill\国内用户下载这个（trae+skill）\trae-jianying-skill"
```

### Step 0 预检（剪映、锁、依赖、清单）

**目的**：确认导出不会被版本/依赖/锁挡住，且测试剧名没和现有台账撞名。

```powershell
python -V
python pipeline\doctor.py
python pipeline\pipeline.py --list-presets
python pipeline\stream_manifest.py list
Test-Path .export.lock
if (Test-Path .export.lock) { Get-Content .export.lock -Raw }
```

**预期**

- `doctor` 末尾 `结论: N 项通过 / N 项警告 / N 项缺失`，必需项都是 `[通过]`；
- `--list-presets` 打印本机预设库、`当前解析来源`、`生效预设`，退出码 0；
- `list` 打印现有 34 份清单，没有你的测试剧名；
- `Test-Path` 为 `False`；若为 `True`，看清 `pid/owner` 再决定等多久。

**失败怎么判**

- `doctor` 出现 `[缺失] 剪映安装与版本`：版本不是 `5.9.0.11632`，导出预检会直接拒绝，先修安装。
- `[缺失] 依赖根目录` / ffmpeg 不存在：ASR、人声分离、抽帧封面都会降级或失败。
- `.export.lock` 存在：`✗ 已有导出在跑（pid=... owner=...）` 是同一把锁；**等它结束**，不要 `--force`。手工归档遇到锁会直接 `⛔` 返回 1，不会排队。

---

### Step 1 放素材与封面

**目的**：让 `batch_runner` 按真实规则发现 20–40 集并选中正确封面。

目录约定：

```
D:\短剧\<剧名>\
├─ 第01集.mp4
├─ 第02集.mp4
├─ ...
├─ 第30集.mp4
└─ 封面.png        # 1080x1920 竖图
```

命名硬要求（依据 `pipeline\batch_runner.py:397-412`、`:458-466`）：

- 默认只探测目录第一层的 `*.mp4 / *.mkv / *.mov`；没有才去 `__jycache__\*.mp4`、`**\*.mp4`。
- 文件列表是 `sorted(set(videos))`，**按字符串排序**，不是自然排序。所以集号必须**零填充**：`第01集`…`第30集`。写成 `第1集、第10集、第2集` 会排成 1、10、11…2，草稿名会变成错误的区间。
- 草稿名取目录名：多集 `<剧名>_第01-30集串流`，单集 `<剧名>_第01集`。目录名里不要出现 `/ \ :`（清单文件名会把它们换成 `_`）。

封面取用优先级（`pipeline\batch_runner.py:414-437`）：

1. `<剧名>\cover_job\04_covers\cover_04_vertical.{png,jpg,jpeg,webp}`
2. `<剧名>\封面.png / 封面.jpg / 封面.jpeg / 封面.webp`
3. 目录里排序后的第一张图片（`.png/.jpg/.jpeg/.webp/.bmp`）——**兜底，容易误选 logo 之类的图**
4. 都没有：从第一集抽帧

**关于「放一个封面.png 就够」**：够，但要求是**图片本身竖版 9:16**。`lib\jy_cover.py:83-96` 对用户给的图只做「复制或转 JPEG」，**不裁剪、不缩放**；成品三件套校验要求竖版、比例 9:16 容差 2%、短边 ≥720（`lib\export_bundle.py:27-31`、`:183-202`）。所以 1080×1920 最稳；放一张 16:9 的图，导出能完成，但归档时会被判「封面不是竖版 / 比例异常」。

**核对命令**

```powershell
$Name = "手工验收剧"
Test-Path "D:\短剧\$Name"
(Get-ChildItem "D:\短剧\$Name" -Filter *.mp4).Count
& "D:\AI_Deps\tools\ffmpeg\bin\ffprobe.exe" -v error -select_streams v:0 `
  -show_entries stream=width,height -of csv=p=0 "D:\短剧\$Name\封面.png"
```

**预期**：目录存在；mp4 数量 = 目标集数（20–40）；封面输出 `1080,1920`。

**失败怎么判**

- 集数少于预期：先看扩展名（`.ts/.flv` 不会被默认探测）或文件是否在子目录。
- 封面输出不是 `1080,1920`：换图，或改用 `--cover-image` 显式指定。
- 目录里有 `cover_job\04_covers\cover_04_vertical.*`：它会覆盖 `封面.png`，以运行时打印的 `🖼️ 自动封面:` 为准。

---

### Step 2 跑真实流程（20–40 集、裁 30 秒、自动导出、自动质检）

**目的**：完全按《快速上手》第一条出片命令跑整部剧，不隔离任何目录。

```powershell
python pipeline\batch_runner.py "D:\短剧\手工验收剧" --cut 30 --no-watch
```

说明（依据 `快速上手.md` §5、`pipeline\batch_runner.py`）：

- 位置参数是**剧集目录**，不是草稿名；默认行为是「生成串流草稿 → 自动导出 → 自动质检」，并持有全局导出锁 `.export.lock`（`batch_runner.py:345-353`）。
- `--cut 30` 的语义是**整条串流时间线开头裁掉 30 秒**（`pipeline.py:3051-3066`，多集时明确打印「串流：整条时间线前移」），**不是每集各裁 30 秒**。30 集会只影响第一集开头这一段。
- 不写 `--limit` 就是整部剧；`--no-watch` 只关闭批量进度守护，不影响导出。
- 默认导出到 `presets.json` 的 `export_dir`（当前 `D:\油管打猎\L`），成品目录名 = 草稿名。
- 想放宽导出超时：`--export-timeout 120`（单位分钟，默认 60）。20–40 集串成一条长片时建议放宽。

**预期输出（按出现顺序）**

```
🎬 单草稿串流: 30 集 → 手工验收剧_第01-30集串流
   第01集  第01集.mp4
   ...
🖼️ 自动封面: 封面.png
  ✂️ cut_us     : 30000000  (30.0s)
  ...
━ [6] 裁剪 30.00s 之前（串流：整条时间线前移） ━━━━━━━━
  📏 裁剪前 XXXX.XXs → 裁剪后 XXXX.XXs
🏁 串流草稿完成: 手工验收剧_第01-30集串流
  📐 帧对齐: 30集, 总长 XXXX.Xs → XXXX.Xs
📤 后台导出 → D:\油管打猎\L\手工验收剧_第01-30集串流\手工验收剧_第01-30集串流.mp4
  ⏳ 等待导出完成（上限 60min，每 25s 轮询一次）...
    ✅ 导出完成（耗时 X.Xmin，...）
```

> 串流模式打印的是 `🏁 串流草稿完成` / `📤 后台导出`；《快速上手》里的 `🏁 批量完成` / `📤 串行导出` 是整季多草稿模式的文案。

**失败怎么判**

| 你看到 | 含义 | 处理 |
|---|---|---|
| `✗ 目录里没找到视频` | 路径错或扩展名不在默认探测范围 | 检查目录和文件 |
| `✗ 已有导出在跑（pid=... owner=...）` | 锁被占 | 等；不要 `--force` |
| `⚠️ 预设 '...' 不存在` | 预设名和剪映库对不上 | 只告警，成片会少图层；用 `--list-presets` 复核 |
| `⚠️ 第N集 第M次尝试失败:` | 单集生成失败 | 看 traceback 最后一行；用 `--only N` 重跑该集 |
| `❌ 帧对齐断言失败，终止本次导出` | 草稿/素材问题 | 不要继续导出，先查素材 |
| `❌ 导出异常: <草稿名>: ...` | 导出子进程起不来 | 看剪映能否启动、`pipeline\_env_profile.json` |
| `⏳ 等待导出完成` 后超时 | 导出卡住 | 看 `export_console.log`；放宽 `--export-timeout` |
| `⚠️ 导出子进程成功但成品未落盘` | 无 mp4 | 到导出目录找同名目录，查磁盘 |
| `❌ 验证未通过`（通知） | 质检失败，但成品通常已在 | 看「帧级漂移 / 字幕帧位」两行 |

**现场文件**

- 批量状态：`pipeline\_work_<剧名>_<hash8>\_batch_state.json`
- 导出进度：`D:\油管打猎\L\<草稿名>\<草稿名>.mp4.progress.json`
- 导出控制台：`export_console.log`（有 job 在 `cover_job\`，无 job 在 `pipeline\_work\`）

---

### Step 3 核对三件套与真实清单

**目的**：确认手工入口要读的清单和磁盘事实一致。

```powershell
$Draft = "手工验收剧_第01-30集串流"
$Out   = "D:\油管打猎\L\$Draft"
Get-ChildItem $Out | Select-Object Name,Length
Get-Content "pipeline\_stream_manifests\$Draft.json" -Raw
```

**预期**

- 三个文件都在：`<草稿名>.mp4`、`<草稿名>.mp4.progress.json`（`done:true`）、`<草稿名>-封面.jpg`。
- 清单里有 `draft_name`、`paths.source_dir / draft_dir / jycache_dir / workdir / export_file`，**没有 `versions`**。

**失败怎么判**

- 缺封面：说明 draft_cover 没有落盘，回看 Step 2 的 `🖼️ 自动封面:`。
- `progress.json` 里 `done` 不是 `true`：导出没完成，别往下走。
- 清单没有 `export_file`：导出后回写失败（`_record_export_manifest` 只告警），用清单里的 `export_dir` 或磁盘路径核对。

> 手工层只读清单 + 磁盘，不读 `job.json`、`.watcher.lock`、`.export_state.json`（`pipeline\manual_guard.py:44-53`）。所以这一步的清单就是唯一依据。

---

### Step 4 归档计划（dry-run，真实归档根）

**目的**：先看它准备把哪个成品包搬到哪里，不落盘。

```powershell
python pipeline\stream_manifest.py archive --manual --job "手工验收剧_第01-30集串流" --dry-run
```

不写 `--archive-root` 时使用真实归档根（`export_config.get_archive_root()`：`JY_ARCHIVE_ROOT` → `presets.json` 的 `archive_root` → 默认 `D:\短剧_存档`）。

**预期**

```
清理归档 [手工验收剧_第01-30集串流]（手工判定层，profile=全部）:
  [模式] 只搬成品 bundle；源剧集目录原地不动；不读 job/锁/export_state
  [计划] 手工验收剧_第01-30集串流: D:\油管打猎\L\... -> D:\短剧_存档\手工验收剧_第01-30集串流 (XXX MB)
  合计: XXX MB
  （dry-run：未移动任何文件，记录未改动）
  （源剧集目录原地不动: D:\短剧\手工验收剧）
```

**失败怎么判**

- `无记录: <名字>`：草稿名不对，用 `stream_manifest.py list` 里的名字。
- `[错误] ... 成品路径缺失 / 成品不在盘上`：清单与磁盘不一致，先回 Step 3。
- `[冲突] ... 成品三件套不完整`：按列出的缺失项修（多半是封面）。
- `[受保护]`：路径命中 `cover_job`、`_broken`、源目录等保护区，换目标或先人工处理。

---

### Step 5 真归档（写入 `versions`）

**目的**：这是唯一会给清单写 `versions.<p>.state=archived` 的入口，也是后续矛盾检查能生效的前提。

```powershell
python pipeline\stream_manifest.py archive --manual --job "手工验收剧_第01-30集串流"
Get-Content "pipeline\_stream_manifests\手工验收剧_第01-30集串流.json" -Raw
```

**预期**

```
  [已搬] 手工验收剧_第01-30集串流: D:\短剧_存档\手工验收剧_第01-30集串流
  （源剧集目录原地不动: D:\短剧\手工验收剧）
```

清单里出现：

```json
"versions": {
  "a": {
    "draft": "手工验收剧_第01-30集串流",
    "state": "archived",
    "export_file": "D:/短剧_存档/手工验收剧_第01-30集串流/手工验收剧_第01-30集串流.mp4",
    "export_dir": "D:/短剧_存档/手工验收剧_第01-30集串流",
    "archived_to": "D:/短剧_存档/手工验收剧_第01-30集串流"
  }
}
```

并且 `paths.export_file` 被改写到归档路径（`pipeline\manual_archive.py:91-103`）。**源剧集目录 `D:\短剧\手工验收剧` 不会被移动**（手工层与默认 `archive_job` 的区别）。

**失败怎么判**

- `⛔ 全局导出锁被占用（pid=... owner=...）`：等对方结束再重试；不会排队。
- `[失败] ... 目标已存在`：同名归档已存在，先确认是不是上一次的残留。
- `[skip] 已归档（state=archived，目标存在）`：重复执行，属正常幂等。

---

### Step 6 归档后复核

**目的**：确认 `state=archived` 与磁盘一致，rc=0。

```powershell
python pipeline\stream_manifest.py verify --manual --job "手工验收剧_第01-30集串流"
"rc=$LASTEXITCODE"
```

**预期**

```
=== verify scope=drama ===
--- 本剧对账 ---
对账 [手工验收剧_第01-30集串流] state: a=archived
  [OK] 清单与磁盘一致，无残留
本剧汇总[手工验收剧_第01-30集串流]: 残留 0 项，缺失 0 项 → rc=0
```

**失败怎么判**

- `[残留] versions.a: ...`：归档目标不在盘上，或三件套缺件。按提示路径去查。
- `[缺] source_dir ...`：源目录被挪走/改名；只提示，不影响 rc。
- `[错误] verify 的手工语义`：漏了 `--manual`（默认路径没有 `verify`）。

---

### Step 7 制造并验证「归档缺失」（只改名，不删除）

**目的**：让 `state=archived` 与磁盘不一致，确认检查真的报出来。

```powershell
Rename-Item "D:\短剧_存档\手工验收剧_第01-30集串流" "手工验收剧_第01-30集串流.__missing__"
python pipeline\stream_manifest.py verify --manual --job "手工验收剧_第01-30集串流"; "rc=$LASTEXITCODE"
```

**预期（矛盾在这里暴露）**

```
对账 [手工验收剧_第01-30集串流] state: a=archived
  [残留] versions.a: D:/短剧_存档/手工验收剧_第01-30集串流 — state=archived 但归档目标不存在
本剧汇总[手工验收剧_第01-30集串流]: 残留 1 项，缺失 0 项 → rc=1
rc=1
```

**判读**

- 出现上面这行 + rc=1：检查生效。
- 如果只报 `[残留] paths.export_file: ... — 清单记录但成品不存在`，说明清单里**没有 `versions`**（没走 Step 5，或用了手写清单），走的是退化分支，不算验证到 archived 分支。
- 如果什么都不报：先确认改名是否真的落在清单记录的路径上（注意盘符/斜杠规范化）。

---

### Step 8 还原现场

```powershell
Rename-Item "D:\短剧_存档\手工验收剧_第01-30集串流.__missing__" "手工验收剧_第01-30集串流"
python pipeline\stream_manifest.py verify --manual --job "手工验收剧_第01-30集串流"; "rc=$LASTEXITCODE"
```

**预期**：回到 `[OK]`，rc=0。

---

### Step 9（可选）只清理这部测试剧

```powershell
python pipeline\stream_manifest.py clean --manual --job "手工验收剧_第01-30集串流" --dry-run
# 确认 [计划]/[待删] 只包含这部剧后，再执行：
python pipeline\stream_manifest.py clean --manual --job "手工验收剧_第01-30集串流"
```

会删归档包并回写 `state=cleaned`，清单保留。**禁止 `clean --all`**：本机有 34 份真实台账，`--all` 会把它们一起纳入范围。

如果连清单也不想留，自行确认后删除 `pipeline\_stream_manifests\<草稿名>.json`（这是测试剧的台账，不属于真实数据）。

---

## 3. 三个质疑的逐条依据

### 3.1 为什么以前建议不放 `D:\短剧`？共用到底行不行

**先给结论**：共用可以。只要这部剧没有 `cover_job\job.json`，watcher 不会发现、不会建草稿、不会抢锁、不会写清单。

依据：

- **发现条件**：`lib\cover_watch.py:3260-3276` 只扫描 `<root>\<剧名>\cover_job`，且要求 `job.json` 存在才收进任务列表；`exclude` 可按目录名或路径后缀排除（`:3263-3274`）。
- **触发条件**：`check_trigger`（`:173-222`）还要求 4 个封面槽位齐全、`03_prompts\prompts.json` 存在、`titles.gate == "done"`、`stage` 在可触发集合、且不是 `archived/cleaned`。只有视频 + `封面.png` 的目录不满足任何一条。
- **纯 `batch_runner` 不建 job**：`batch_runner` 只是把 `series_dir\cover_job` 作为 job 目录传入；没有 job 时，新鲜度记录是 no-op（`lib\job_manifest.py:948-949`：「无 job.json 时不创建文件」）。
- **watcher 的 edit 不拿锁**：watcher 的剪辑命令是 `batch_runner <series_dir> --force --no-export`（`lib\cover_watch.py:1309-1322`），而 `batch_runner` 只有在**不带 `--force`** 时才获取 `.export.lock`（`pipeline\batch_runner.py:345-353`）。也就是说，一旦 job.json 存在，watcher 的 edit 可以和你手工开的跑批**同时跑**。
- **watcher 的 export 拿锁**：`lib\cover_watch.py:2183-2187`，拿不到就记日志「稍后重试」，不会抢。
- **清单写入没有锁**：`pipeline\stream_manifest.py:151-177` 是「读 JSON → 改 → 直接 `open("w")` 写」，无文件锁、非原子；两个跑批同时写同一 `<草稿名>.json` 会后写覆盖先写。
- **job.json 写入同样无锁**：`lib\job_manifest.py:826-849` 是读-改-写、`revision+1`，并发时同样存在覆盖窗口。
- **脚本导出不写 job 台账**：脚本链路导出后只回写清单（`.trae\skills\jianying-editor\examples\robust_auto_export.py:1436-1448`），不会写 `job.video.outputs`；watcher 之后看到「盘上有成品、台账里没有」会报 `disk_present_ledger_missing` 并阻断（`docs\watcher-vs-script-parity.md:35`、`:127-129`）。

**共存时的注意点**

1. 新剧目录里**不要**出现 `cover_job\job.json`；不要用会 `create_job` 的封面流程给这部测试剧建 job。
2. 如果这部剧已经有 job.json：不要在它上面手工跑 `batch_runner`。让 watcher 跑，或把目录名/路径加进 `watch.exclude` 后再手工跑（改 `pipeline\presets.json` 属于生产配置，需你确认）。
3. 任何时候只允许一个跑批；开跑前看 `.export.lock`，被占就等，不要 `--force`。
4. 手工 `archive/clean` 也要拿同一把锁（`manual_guard.acquire_export_lock`），遇到占用会 `⛔` 返回，不会插队。

**冲突怎么处置**：以「谁先拿到锁/谁在跑」为准。手工跑批已经启动、watcher 随后尝试 export → watcher 记日志重试；watcher 正在 edit（不拿锁）而你又启动手工跑批 → 应立即停手工跑批，不要杀 watcher，也不要删除中间产物，等 watcher 这一轮结束后再核对草稿与清单。

### 3.2 为什么以前建议隔离归档根？直接用 `D:\短剧_存档` 行不行

**行，可以直接用**。手工归档只搬成品 bundle，不动源目录（`pipeline\manual_archive.py:159-160`、`:243-256`）。但有四个实际后果：

1. **生产归档池留痕**：`D:\短剧_存档\<草稿名>` 多一条测试剧目录；当前已有 71 个目录，混进去后靠名字区分。
2. **清单被改写**：`paths.export_file / export_dir` 指到归档路径，`versions.a` 写入 `archived`（`manual_archive.py:91-103`）。之后若同名再导出，清单会先被导出侧改回新路径，但 `versions.a` 仍是 archived，状态会分叉。
3. **和默认归档链冲突**（仅当这部剧有 job.json）：手工搬走后，`job.video.outputs` 仍指向原导出路径，默认 `archive_job` 的三件套审计会判缺件（`pipeline\archive_job.py:225-238`）；即使忽略审计，目标已存在时它会 `[skip] 归档目标已存在（源目录仍在，需人工处理冲突）`（`:338-340`），源目录搬不走。
4. **全量命令会带上它**：`verify --all`、`clean --all` 都会把这部测试剧纳入；`clean --all` 尤其危险。

所以：新剧、无 job、唯一命名时可以放心用真实归档根；如果你不想让生产归档池出现测试条目，才需要隔离。以前建议隔离是为了避免第 1、3、4 条，不是因为真实根「不安全」。

### 3.3 为什么以前建议临时清单目录？用真实清单目录行不行

**行。** 真实清单目录是 `pipeline\_stream_manifests`（`pipeline\stream_manifest.py:40`），已被 `.gitignore:50` 忽略；用唯一剧名新增一份 JSON，不会读写其它 34 份。

风险与注意点：

1. **永久留痕**：清单记录本身永不删除（`stream_manifest.py:33-34`），会一直出现在 `list` 里。
2. **全量范围**：`verify --all` / `clean --all` 会纳入这份测试台账。
3. **同名覆盖**：`record()` 直接按草稿名映射文件（`:49-51`），会把字段写进已有清单（`:166-170`）。所以必须先 `list` 确认新剧名唯一，别和真实剧同名。
4. **无锁写入**：`:171-172` 非原子、无文件锁；同一部剧的 watcher 与手工跑批并发时会丢字段。
5. **重复测试会叠加**：同一草稿名再跑一次 `record` 会合并字段；`archive` 仍然认为已归档，需要先判断是不是要保留旧版本。

结论：真实清单目录可以直接用；只要求剧名唯一、同一时刻只有一个跑批。

---

## 4. 可直接复制发给助手的提示词

```text
按「快速上手」的真实流程，用《手工验收剧》第01–30集在本机跑一遍，不要用隔离目录：
- 素材放在 D:\短剧\手工验收剧\，文件名是 第01集.mp4 … 第30集.mp4
- 封面用 D:\短剧\手工验收剧\封面.png（1080×1920 竖图）
- 跑：python pipeline\batch_runner.py "D:\短剧\手工验收剧" --cut 30 --no-watch
  要求自动导出到默认目录、导出后自动质检；20–40 集这种长串流把 --export-timeout 放宽到 120
- 开跑前先 doctor，并检查 .export.lock；锁被占就等，不许用 --force
- 全程不要动别的剧、不要停 watcher、不要 --all，不要改 pipeline/lib 代码
- 出片后先核对三件套和清单，再执行：
  python pipeline\stream_manifest.py archive --manual --job "手工验收剧_第01-30集串流" --dry-run
  确认计划无误后再真归档，然后用 verify --manual 复核
- 最后只把 D:\短剧_存档\手工验收剧_第01-30集串流 改名制造「归档缺失」，确认 verify 报
  [残留] versions.a ... state=archived 但归档目标不存在 且 rc=1，再改回来
- 不要删除任何东西；不要对有 cover_job\job.json 的剧同时手工跑脚本
```
