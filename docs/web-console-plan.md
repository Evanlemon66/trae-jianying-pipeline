# 本地 Web Console 方案（只做设计，不含实现）

> 目标：把现有「命令行 + watcher」流水线封装成一个只在本机跑的管理面板。  
> 本轮只做方案与参数盘点，**不写实现代码、不改 pipeline/lib、不执行剪辑或归档**。  
> 盘点基准：仓库当前代码（2026-11-15）。

---

## 0. 一句话结论

技术上可行且不需要改现有 pipeline：Console 只做三件事——**读文件拿状态**、**起子进程执行**、**把已有 `--manual` / `--dry-run` 能力做成三段式 UI**。  
真正需要你先拍板的是下面 6 个选择（其余我按推荐值写进方案）：

| # | 选择项 | 推荐 | 备选 |
|---|---|---|---|
| 1 | 后端形态 | 单进程 FastAPI（127.0.0.1:8765）+ 原生 HTML/JS | Flask+Jinja；纯前端（不可行，因为要起子进程） |
| 2 | 串流执行入口 | `pipeline/batch_runner.py`（与「快速上手」一致） | `pipeline_ui/ui_pipeline.py`（参数更多，但 UI 标定依赖重） |
| 3 | 是否暴露 `--force` / `--fresh` / `--no-watch` | 全部不暴露，固定安全值 | 高级模式里放开（不建议） |
| 4 | P0 是否包含真归档/真清理 | 只做 dry-run 计划 + 只读查询 | P0 就放开执行（风险高） |
| 5 | 历史记录落点 | `pipeline_ui/_console/runs.jsonl`（gitignore） | 不落盘，只在内存 |
| 6 | 导出分辨率 | P0 不支持（batch_runner 未暴露） | P1 给 batch_runner 加透传 |

---

## 1. 技术选型

| 维度 | 决策 | 理由 |
|---|---|---|
| 部署 | 本地单机，仅 `127.0.0.1:8765` | 不对外、不做鉴权体系；端口冲突时顺延 8766/8767 |
| 后端 | **必需**。Python 单进程（FastAPI + Uvicorn） | 要 spawn `batch_runner`、读 `.export.lock`、tail 日志、SSE 推流；纯静态页面做不到 |
| 前端 | P0 原生 HTML + 少量 JS（不引入构建链）；P1 再考虑 Vue3 CDN | 减少依赖，避免 Node 工具链；页面以表格/表单为主 |
| 状态来源 | **一律读文件 + 读进程，不引入数据库** | 现有链路已经落盘 `job.json` / `.progress.json` / `_stream_manifests` / `.export.lock` / `watcher.log`，读它们即真源，避免双写不一致 |
| 运行期状态 | 内存对象（当前 run）+ `runs.jsonl`（历史） | 进程重启后仍能展示历史；运行中的 run 以子进程 pid 判活 |
| 并发模型 | 单队列，串行执行 | 剪映是单实例资源，本来就只能串行；同时也避免两次导出抢锁 |
| 路径安全 | 白名单根 + `realpath` 归一 + 拒绝 `..` 穿越 | 只允许：`export_config.resolve_paths()` 给出的 `series_root` / `export_dir` / `archive_root` / `watch_roots`、草稿根、仓库根（只读）；不写死任何盘符 |
| 权限边界 | Console **不写** `job.json` / `cover_job/**` / `presets.json` / watcher 状态 | 与 `pipeline/manual_guard.py` 的硬约束保持一致 |
| 与 watcher 关系 | 只读观察，不接管、不 kill、不改配置 | 见第 7 节 |

**为什么必须有后端**：`batch_runner` 是长任务（几十分钟到数小时），必须由后端持有子进程句柄、做超时与收尾；浏览器刷新不能中断任务。

---

## 2. 环境自检（Doctor）

一个「一键检查」按钮，串行跑下列检查，每项给出 `绿/黄/红 + 证据值 + 修复建议`。全部只读，不创建、不删除任何文件。

| 检查项 | 读取来源 | 判定 | 说明 |
|---|---|---|---|
| Python 解释器 | `sys.executable` / `sys.version_info` | 低于 3.10 标黄 | 当前环境 3.12.10 |
| 依赖完整性 | 逐项 `import` 探测：`psutil`、`uiautomation`、`PIL`、`comtypes`、`requests` | 任一失败标红 | 失败项直接列包名 |
| 剪映安装目录 | `pipeline/export_config.py:259 get_jianying_dir()` | 目录不存在标红 | 默认值见 `export_config.py:34` |
| 剪映版本 | 读安装目录下 `JianyingPro.exe` 版本信息；与 `presets.json:4 jianying_dir` 的版本号比对 | 不一致标黄 | 当前 5.9.0.11632 |
| 剪映进程 | `tasklist` 查 `JianyingPro.exe` | 运行中显示 pid + 窗口数 | 运行中不报错，只提示「开跑前会被 batch_runner 关闭」 |
| 草稿根可写 | `export_config.py:265 get_projects_root()` + 写探针 | 不存在/不可写标红 | 写探针用临时文件，立即删除 |
| `series_root` 可写 | `export_config.get_series_root()` | 不存在/不可写标红（业务根缺失直接 `ConfigError`） | 同时统计一级剧目录数 |
| `archive_root` 可写 | `export_config.get_archive_root()` | 未配置=「归档禁用」标黄；配置了但不存在标黄（归档时才建） | 显示剩余空间 |
| 导出根可写 | `export_config.get_export_dir()` | 不存在/不可写标红 | 输出根允许尚未创建；无默认值，未配置直接报错 |
| 磁盘剩余 | `shutil.disk_usage()`（C/D 两盘） | 剩余 < 2×预估导出体积标红 | 预估口径 `1.05 MB/s × 时间线秒数`（`robust_auto_export.py:1288 _EST_BYTES_PER_SEC`） |
| 全局导出锁 | `.export.lock`（`lib/export_lock.py:19 _LOCK_PATH`） | 存在且 pid 存活=黄（有人在跑）；存在且 pid 死=红（陈旧锁）；不存在=绿 | 展示 `pid / owner / started` 与存活判定 |
| watcher 常驻进程 | ① Windows 计划任务 `watch_jianying` 状态 ② `watcher.log` 最后写入时间 ③ 各剧 `.watcher.lock` pid 存活 | 三源交叉，显示「在跑 / 计划任务就绪但当前无进程 / 未配置」 | 任务计划名以现场实际为准，P0 先读计划任务列表 |
| presets 完整性 | `pipeline/presets.json` | 缺 `default_preset_profile` 指向的组 → 红；`series_root`/`deps_root`/`export_dir` 缺失或无效 → 红（业务根 fail-closed）；`jianying_dir` 探测不到 → 黄 | 当前 `default_preset_profile=presets_a`，4 条预设 |
| ffmpeg / ffprobe | `export_config.py:358 get_ffmpeg()` / `:362 get_ffprobe()` | 找不到标红 | 影响抽帧封面与时长校验 |
| 清单目录 | `pipeline/_stream_manifests/*.json` | 显示条数 + `versions.state=archived` 且目标缺失的条数 | 当前 34 份；矛盾条数直接作为健康指标 |
| 端口占用 | 尝试 bind `127.0.0.1:8765` | 被占则提示改端口 | |

**实现要点**：所有检查结果带 `checked_at` 与「证据值」，不做「安全 / 不安全」的空话；单次全量自检目标 < 2s（除 `tasklist` 两次子进程）。

### 2.1 路径唯一入口与配置迁移

**路径规划统一由 `pipeline/export_config.resolve_paths(mode)` 提供**，Console 不再自己拼路径、也不读盘符字面量：

- 返回 `{paths, sources, issues}`：`paths` 含 `series_root` / `export_dir` / `deps_root` / `archive_root` / `watch_extra_roots` / `watch_roots` / `jianying_dir` / `projects_root` / `presets_root` / `jianying_log_dir`；`sources` 标明每项来自 `env` / `presets.json` / `derived` / `empty` / `error`。
- 三根一致：`series_root` 是**唯一素材根**，`export_dir` 是导出根，`deps_root` 是依赖根；三者都没有默认值，缺失/无效进 `issues`（业务代码直接抛 `ConfigError`，fail-closed）。
- `watch.roots` 只是 watcher 的**额外扫描根**：`watch_extra_roots` 是原始配置，`watch_roots` = `[series_root] + watch_extra_roots` 去重后的有效扫描根。
- 只读校验用 `validate_config(mode)`，返回 `{ok, paths, sources, issues}`；Console 的「路径」区按 `paths` 展示生效值，按 `sources` 标注来源，把 `issues` 逐条渲染成「缺什么 → 为什么 → 怎么修」（每项带 `key` / `message` / `hint`）。

**配置迁移**：新增自检动作 `config.migrate`（`pipeline_ui/console/envinit.py: migrate_config()`，等价于命令行 `python pipeline/migrate_config.py`）。

- 只处理「旧配置把素材根写在 `watch.roots`」的情况：唯一一根 → 迁到 `series_root`；多根须显式给 `--series-root`（不猜）；`export_dir`/`deps_root` 缺失时只报告、拒绝写回。
- 动作支持 `write=false` 只返回计划（`changes`/`missing`），UI 先预览再以 `write=true` 写回；写回走 `save_presets()`，自动备份 `presets.json.bak-<时间戳>`。
- 迁移后可再跑一次 `validate_config()` 复核，用 `ok=true` 作为该检查项通过的判据。

---

## 3. 串流执行 + 参数清单

### 3.1 执行链路（固定）

```
Console → python pipeline\batch_runner.py "<剧集目录>" [参数]
        → pipeline.run_pipeline(...)   # 建稿/预设/裁剪/人声/识别翻译/终检
        → robust_auto_export.py        # 导出（独立子进程）
```

与「快速上手」完全一致，不额外加中间层。

### 3.2 参数清单（全量盘点）

分档说明：
- **必须展示可配置**：日常要用，且改了不破坏一致性。
- **建议展示可配置**：有用，但有副作用/成本，需要提示文案。
- **只读展示**：要让人看见「现在是什么值」，但不允许改。
- **不建议暴露**：会破坏幂等、绕过安全约束、或只在调试用。

> `--cut` 语义：**在整条串流时间线开头裁掉 N 秒**（`pipeline/pipeline.py:3051-3066`，`crop_before_us` + `rebase_timeline`），**不是每一集各裁 N 秒**。UI 必须原样写这句。

#### A. `pipeline/batch_runner.py`（Web 端主入口）

| 参数 | 作用 | 默认值 | 取值 / 枚举 | 来源 | 分档 |
|---|---|---|---|---|---|
| `series_dir` | 剧集目录（位置参数） | 无 | 白名单内绝对路径 | `batch_runner.py:275` | **必须展示可配置**（目录选择器） |
| `--only` | 只处理指定集数 | 无 | `20..30` / `20-30` / `20`，可多次 | `:279` | **必须展示可配置**（集数区间输入框） |
| `--limit` | 本次最多处理几集 | 无 | 正整数 | `:281` | **必须展示可配置** |
| `--cut` | 整条时间线开头裁 N 秒 | 无（不裁） | ≥0 秒 | `:297` | **必须展示可配置**（默认填 30，附语义说明） |
| `--cover-image` | 指定封面图 | 无（走自动探测优先级） | 白名单内图片路径 | `:282` | **必须展示可配置**（可留空） |
| `--profile` | 预设组 | 无（按前缀自动发现） | `presets_a` / `presets_b`，逗号可多组 | `:314` | **必须展示可配置**（下拉） |
| 自动导出开关 | 是否导出 | 开（`auto_export=True`） | 开 / 关 | `:299-301` | **必须展示可配置**（对应 `--no-export`） |
| `--export-dir` | 导出落点根 | `presets.json` 的 `export_dir`（必填） | 白名单路径 | `:302` | 建议展示可配置（P0 先只读显示生效值） |
| `--export-timeout` | 导出等待上限 | 60 分钟 | 正数 | `:310` | 建议展示可配置 |
| `--retries` | 单集失败重试 | 2 | 0–5 | `:285` | 建议展示可配置 |
| `--draft-prefix` | 草稿名前缀 | 目录名 | 文件名安全串 | `:278` | 建议展示可配置（改了等于换清单键，要提示） |
| `--delay` | 每集间隔秒 | 5.0 | ≥0 | `:284` | 建议展示可配置（串流模式只注入一次，影响很小） |
| `--workdir` | 工作目录 | 按剧名派生 | 路径 | `:286` | 只读展示 |
| `--stream` / `--per-episode` | 串流 / 逐集 | 串流 | 枚举 | `:293-296` | 只读展示（固定串流） |
| `--verify` / `--no-verify` | 导出后质检 | 开 | 开关 | `:306-309` | 只读展示（固定开，禁止关） |
| `--wait-export` | 轮询等导出完成 | 由 `--verify` 隐含 | 开关 | `:304` | 只读展示（不单独暴露） |
| `--watch` / `--no-watch` | 是否自启 watcher 守护 | 开 | 开关 | `:319-322` | 只读展示（Web 固定 `--no-watch`） |
| `--pattern` | 视频 glob | 自动探测 | glob | `:276` | 不建议暴露（漏集/误选） |
| `--fresh` | 忽略状态从头跑 | 关 | 开关 | `:288` | 不建议暴露（破坏断点与幂等） |
| `--clean-workdir` | 结束后清中间产物 | 关 | 开关 | `:289` | 不建议暴露（隐式删除） |
| `--auto-merge` | 逐集跑完自动合并 | 关 | 开关 | `:291` | 不建议暴露（串流模式下无效） |
| `--presets` | 显式预设组合 | 无 | `"名1,名2:0.03"` | `:312` | 不建议暴露（语法易错，且与 `--profile` 冲突） |
| `--force` | 绕过 `.export.lock` | 关 | 开关 | `:323` | **不建议暴露**（绕过并发保护） |
| `--kill-jianying` | 开跑前先退剪映 | 关 | 开关 | `:325` | 不建议暴露（会关掉用户正在用的窗口；P2 可做勾选+二次确认） |

#### B. `pipeline/pipeline.py`（被 batch_runner 内部调用）

batch_runner 只透传 `cut_us / workdir / presets / preset_profile / cover`（`batch_runner.py:472-483`），因此以下参数在 Web 串流链路里**不可控**：

| 参数 | 作用 | 来源 | 分档 |
|---|---|---|---|
| `--no-demucs` | 跳过人声分离 | `pipeline.py:3650` | 不建议暴露（跳过会改变音频结构） |
| `--no-presets` | 跳过所有预设 | `:3651` | 不建议暴露 |
| `--no-asr` / `--no-translate` | 跳过识别 / 翻译 | `:3659-3660` | 不建议暴露 |
| `--no-hide-cn` | 不隐藏中文字幕 | `:3661` | 不建议暴露 |
| `--skip-subtitle-check` | 跳过字幕终检 | `:3662` | 不建议暴露 |
| `--no-vad-gate` | 跳过 VAD 门控 | `:3664` | 不建议暴露 |
| `--skip-ai` | 跳过 AI 段 | `:3647` | 不建议暴露 |

#### C. `pipeline_ui/ui_pipeline.py`（另一条链路，仅登记）

| 参数 | 作用 | 默认 | 取值 | 来源 | 分档 |
|---|---|---|---|---|---|
| `--vocal-mode` | 人声分离模式 | `none` | `none` / `vocal` / `background` | `:793` | 建议展示可配置（**仅 ui_pipeline 链路有效**） |
| `--export-resolution` | 导出分辨率 | 无（沿用剪映当前值） | `480P/720P/1080P/2K/4K` | `:795` | **batch_runner 链路不可用**；P1 若需要，给 batch_runner 加透传（导出脚本已支持 `--resolution`，`robust_auto_export.py:2045`） |
| `--normalize` | HEVC→H.264 逐集重编码 | 关 | 开关 | `:814` | 建议展示可配置（转码开关；开启后建稿从秒级变 ~85 分钟，`help` 原文） |
| `--limit` / `--video-range` | 集数选择 | 无 | 见 `:804-808` | | `--limit` 由 batch_runner 统一；`--video-range` 不建议暴露（与 `--only` 重复） |
| `--screen-policy` | 窗口放屏策略 | `primary` | `primary / match-calibration / keep` | `:809` | 不建议暴露（绑定本机显示器标定） |
| `--only` / `--skip` / `--from-step` / `--resume` | 步骤级控制 | 无 | 步骤名 | `:787-792` | 不建议暴露（跳过步骤破坏串流一致性；断点续跑由 Console 内部决定） |
| `--draft-name` / `--video-dir` | 草稿名 / 视频目录 | 自动派生 | 串 | `:798-799` | 只读展示（手填会绕过 batch_runner 的命名与清单键） |

#### D. `pipeline/presets.json`

| 配置键 | 作用 | 默认值 | 来源 | 分档 |
|---|---|---|---|---|
| `export_dir` | 导出根（必填） | 无默认值 | `presets.json` 顶层 | 只读展示（P2 再考虑可编辑） |
| `series_root` | 唯一素材根（必填） | 无默认值 | 顶层 | 只读展示 |
| `archive_root` | 归档根 | 留空 = 禁用归档 | 顶层（`JY_ARCHIVE_ROOT` 优先） | 只读展示 |
| `deps_root` | 依赖根（ffmpeg 等，必填） | 无默认值 | 顶层 | 只读展示 |
| `jianying_dir` | 剪映安装目录 | 自动探测 | 顶层 | 只读展示（Doctor 用它校验版本） |
| `watch.roots` | watcher 额外扫描根 | `[]`（为空只扫 `series_root`） | `watch.roots` | 只读展示；有效扫描根见 `get_watch_roots()` |
| `watch.jobs` / `watch.exclude` | 额外任务 / 排除 | `[]` / `[]` | `:8-10` | 不建议暴露（手改配置项） |
| `watch.settle` | 文件稳定等待秒 | 30 | `:11` | 只读展示 |
| `watch.interval` | 扫描间隔秒 | 5 | `:12` | 只读展示 |
| `watch.export_policy` | 导出策略 | `after_edit_queue_drained` | `:14` | 只读展示 |
| `watch.edit_pause_after` | 连续编辑多少集后暂停 | 5 | `:15` | 只读展示 |
| `watch.new_job_grace_min` | 新任务宽限分钟 | 30 | `:16` | 只读展示 |
| `watch.derive` | 是否派生平台 | `false` | `:13` | 不建议暴露 |
| `admission.*` | 准入测试专用（草稿名/视频范围/open 等待） | 见 `:18-41` | `:18-41` | 只读展示（与正式链路无关） |
| `presets_a` / `presets_b` | 预设组合 | 4 条 / 3 条 | `:42-68` | 建议展示只读（列出名称+alpha；编辑放 P2） |
| `default_preset_profile` | 默认预设组 | `presets_a` | `:69` | 建议展示可配置（下拉） |
| `translate.providers[]` | 翻译供应商 | zhipu / xfyun | `:70-86` | 只读且**密钥脱敏**（只显示名称/模型/是否配置 key） |
| `translate.ollama` | 本地翻译 | enabled=true, qwen3:8b | `:87-91` | 只读展示 |
| `translate.reflect` | 反思式翻译 | false | `:92` | 不建议暴露 |

#### E. 清单 / 台账字段（只读展示）

`pipeline/_stream_manifests/<草稿名>.json`（`stream_manifest.py:49-51,151-177`）：

| 字段 | 含义 | 用途 |
|---|---|---|
| `draft_name` | 草稿名 | 主键 |
| `paths.source_dir` | 源剧目录 | 展示出处、校验不清理源目录 |
| `paths.draft_dir` | C 盘草稿目录 | 「连草稿一起删」时才用 |
| `paths.jycache_dir` | `__jycache__` 缓存目录 | 中间产物大小 |
| `paths.workdir` | `_work_*` 工作目录 | 中间产物大小 |
| `paths.export_file` | 成品 mp4 | 归档/清理的目标 |
| `paths.export_dir` | 成品目录 | bundle 判定 |
| `versions.<p>.state` | `exported` / `archived` / `cleaned` | **归档状态唯一真源** |
| `versions.<p>.archived_to` | 归档目标目录 | 校验「archived 但缺失」 |
| `versions.<p>.export_file/export_dir` | 归档后的新路径 | 同上 |
| `updated_at` / `recorded_at` | 时间戳 | 新鲜度 |
| `shared_with` | 与其他版本共享的目录 | 防误删 |

### 3.3 「扩展参数」区 UI 建议

- 两列布局：左列「本次执行」（目录、集数区间、裁剪秒数、预设组、是否导出）；右列折叠「高级（默认不改）」。
- 高级区只放：`--retries`、`--export-timeout`、`--draft-prefix`、`--export-dir`（P1）。
- 每个开关下面一行小字写「副作用」，例如 `--cut 30`：*整条串流时间线开头裁 30 秒，不是每集裁 30 秒*。
- 提交前做 4 项预检：目录存在且有视频、集号零填充、草稿名是否已存在、`.export.lock` 是否被占。

---

## 4. 任务实时状态

### 4.1 数据源与字段

| 信息 | 文件 / 来源 | 关键字段 | 读取方式 |
|---|---|---|---|
| 当前跑哪部剧 | Console 自身 run 记录 + 子进程命令行 | `series_dir`、`draft_name`（`batch_runner.py:458-466` 生成） | 内存 + `runs.jsonl` |
| 当前阶段 | ① 子进程 stdout 阶段横幅 ② `pipeline_ui/_state.json`（仅 ui_pipeline 链路） ③ `_timings.jsonl` | 横幅 `━ [n] ...`（`pipeline.py:2915/3017/3053/3080/3258/3271/3302/3371/3453`） | 逐行解析 stdout（SSE 推给前端） |
| 编辑阶段进度 | 同上 | 见 4.2 权重表 | 阶段横幅推进 |
| 导出进度 | `<导出根>/<剧名>.progress.json`（运行中）→ `<导出根>/<剧名>/<剧名>.mp4.progress.json`（完成） | `status`、`done`、`percent_value`、`rate_mbps`、`eta_s`、`exported_mb`、`total_mb`、`elapsed_s`（`robust_auto_export.py:455-479`） | 1s 轮询，按 mtime 变化才读 |
| 导出阶段细分 | `progress.json.phase` | `cold_start` / `open_draft` / `export`（`:1889-1917`） | 同上 |
| 导出失败原因 | `progress.json.failure_class` + `errors[]` + `recovery_action` | 失败分类器 `lib/failure_class.py` | 同上 |
| 编辑失败原因 | 子进程退出码 + stdout 尾部 + `job.json`（若有） | `stage`、`video.status`、`history[-1]` | 退出时聚合 |
| 任务台账状态 | `<剧目录>/cover_job/job.json` | `stage`（9 个契约值）、`revision`、`video.status`、`video.export_state`（`None/exported/cleaned`）、`titles.gate` | 3s 轮询（文件不存在则跳过） |
| 成品清单 | `pipeline/_stream_manifests/<草稿>.json` | 见 3.2 E | 任务结束/手动刷新时读 |
| watcher 侧活动 | `.watcher.lock`（每剧） | `pid`、`phase`、`pipeline_pid`、`started`、`updated_at`（`cover_watch.py:300-348`） | 5s 轮询 |
| watcher 日志 | `<剧目录>/cover_job/watcher.log`（10MB 轮转 5 份） | `[FAIL]` / `[warn]` 行 | 增量 offset tail |

**只读原则**：Console 不写 `job.json`、不写 `.export_state.json`、不碰 `cover_job/**`（与 `manual_guard.py` 一致）。

### 4.2 百分比：怎样才算「不假」

三种口径，按可信度从高到低：

1. **有确定总步数 → 阶段权重**（编辑阶段唯一使用）
   用固定权重表 + 只在「观察到下一阶段横幅」时推进，**阶段内不插值**：

   | 阶段（stdout 标记） | 权重 | 累积 |
   |---|---|---|
   | 建稿 / 注入视频（`🎬 单草稿串流`，`batch_runner.py:464`） | 8% | 8% |
   | 人声分离（`━ [1] 人声分离`，`pipeline.py:2915`） | 20% | 28% |
   | 预设合并（`━ [n] 加预设`，`:3017`） | 5% | 33% |
   | 裁剪（`━ [6] 裁剪`，`:3053`） | 3% | 36% |
   | 提取/识别/翻译（`━ [7-9]`，`:3080`） | 35% | 71% |
   | 字幕收尾/终检（`━ [10]`、`[10.2]`、`[10.5]`、`[10.8]`） | 17% | 88% |
   | 帧对齐 + 导出启动（`🏁 串流草稿完成`，`batch_runner.py:504`） | 2% | 90% |
   | 导出编码（`📤` / `━ [11]`） | 10%（其中 90–99% 用 `percent_value`） | 100% |

   权重先按上表固定，P1 用 `_timings.jsonl` 的 `duration_s` 自动校准（`ui_pipeline.py:78-95` 已有该文件格式）。

2. **能拿到总量与已写体积 → 真实百分比**
   仅导出阶段可用：`percent_value = min(99, exported_mb / total_mb × 100)`（`robust_auto_export.py:379-388`）。
   `total_mb` 的来源有优先级：上一版成品 → 归档副本 → 上次 `done` 记录 → `时长 × 1.05MB/s`（`:326-359`）。
   **`percent = "无估算"` 时必须原样显示「无估算」**，不要用假数字顶上。

3. **两者都没有 → 只显示阶段 + 已耗时**
   编辑阶段的人声分离/识别翻译内部没有细粒度心跳，绝不按时间线性插值。

进度条文案建议：`阶段 4/8 · 识别翻译 · 已用 17:32（本阶段无估算）`。

### 4.3 轮询频率与代价

| 数据 | 频率 | 单次代价 |
|---|---|---|
| 子进程 stdout | 持续（管道逐行） | 可忽略；SSE 推送 |
| `progress.json` | 1s（仅存在时） | < 10KB 读 |
| `job.json` | 3s（仅存在时） | < 50KB 读 |
| `.export.lock` 判活 | 5s 或点击时 | 一次 `tasklist` 子进程（30–80ms） |
| `watcher.log` | 1s 增量 tail | 按 offset 读新增字节，单次上限 200 行 |
| 清单全量统计 | 页面加载 / 手动刷新；缓存 30s | 当前 34 个文件，< 1MB |
| 目录体积统计 | **不自动跑**，只在「清理计划」按钮里按需算 | `os.walk`，可能几秒；必须显示 loading |

结论：常规轮询总 IO < 1MB/s 且都是小文件，对剪辑任务影响可忽略；**禁止**在轮询里做全盘 `os.walk`。

---

## 5. 归档与删除

### 5.1 查询视图（只读）

以「剧」为单位聚合 manifest 的 `versions`，每行展示：

| 列 | 来源 |
|---|---|
| 版本 / 草稿名 | `versions.<p>.draft` |
| 台账状态 | `versions.<p>.state`（无则显示 `未归档`） |
| 成品路径 | `versions.<p>.export_file` → 退化到 `paths.export_file` |
| 归档位置 | `versions.<p>.archived_to` |
| 三件套完整性 | `lib/export_bundle.py:audit_bundle`（mp4 + `.progress.json done=true` + `-封面.jpg`） |
| 磁盘占用 | `stream_manifest._entry_size`（按需） |
| 引用情况 | `manual_guard.referenced_owners` + `referencing_drafts`（只读草稿 JSON） |
| 矛盾标记 | `state=archived` 但 `archived_to` 不存在 → 红字「归档缺失」 |

### 5.2 三段式操作（所有写操作统一）

```
[1] 生成计划（只读）
      → archive --manual --dry-run / clean --manual --dry-run / verify --manual
      → 展示「会搬什么 / 会删什么 / 会保留什么 + 原因 + 合计大小」
[2] 人工确认
      → 勾选具体条目（默认全不选）+ 输入剧名确认 + 显示锁状态
[3] 执行并流式回显
      → 真实命令（非 dry-run），stdout 实时推送；结束刷新台账
```

UI 表达「会被删什么 / 会被搬什么 / 哪些保留」：

| 计划行类型 | 展示 |
|---|---|
| 搬运 | `[搬] <成品目录> → <archive_root>\<草稿名>（12.4GB）` |
| 删除 | `[删] <路径>（1.2GB）`，按 缓存 / 工作目录 / 成品 / C盘草稿 分组 |
| 保留 | `[保留] <路径> — 仍被草稿引用: <草稿A>、<草稿B>`（`manual_clean.py:194-201`） |
| 保留 | `[保留] <路径> — 受保护（cover_job / 源目录 / s2.mp4）`（`manual_guard.py:88-110`） |
| 跳过 | `[跳过] 已归档（state=archived，目标存在）`（`manual_archive.py:189-192`） |
| 冲突 | `[冲突] 归档目标已存在且状态不一致` → 禁止执行，只能人工处理（`manual_archive.py` 同名分支） |

### 5.3 命令与语义（沿用现有能力，不改代码）

| 操作 | 命令 | 关键约束 |
|---|---|---|
| 查台账 | `python pipeline\stream_manifest.py list` / `show "<草稿名>"` | 只读 |
| 查矛盾 | `python pipeline\stream_manifest.py verify --manual --job "<草稿名>"` / `--all` / `--scope drama\|staging\|both` | `rc=1` 表示有残留/缺失 |
| 归档计划 | `... archive --manual --job "<草稿名>" --dry-run` | 只搬成品 bundle，源目录不动 |
| 真归档 | `... archive --manual --job "<草稿名>"` | 内部取 `.export.lock`；失败只告警不抢 |
| 清理计划 | `... clean --manual --job "<草稿名>" --dry-run` | 默认整剧全版本；不删 C 盘草稿 |
| 真清理 | `... clean --manual --job "<草稿名>" [--profile b] [--intermediates-only]` | 保护源目录与 cover_job |

**归档根**：`export_config.get_archive_root()`；解析顺序 `JY_ARCHIVE_ROOT` → `presets.json` 顶层 `archive_root` → 旧字段 `watch.archive_root`（仅兼容）。未配置返回空串 = 禁用归档，**没有默认盘符**。

---

## 6. 建议一起接入的功能（按价值排序）

1. **执行历史（run ledger）**：每次启动记录入参、开始/结束时间、退出码、草稿名、成品路径、失败分类；解决「上周那部剧到底用什么参数跑的」。
2. **实时日志面板**：stdout SSE + `watcher.log` 增量 tail + 一键下载完整日志；不用再盯终端或翻 `_evidence_*`。
3. **锁诊断 / 一键接管**：复用 `manual_guard.diagnose_lock()`，显示 `pid/owner/started/存活`，确认陈旧才删锁；解决「锁没人放，任务起不来」。
4. **单队列与排队控制**：同一时刻只跑一个任务，锁被占时排队并显示「前方任务 / 预计释放」；避免两个导出互踩。
5. **清单台账浏览 + 健康体检**：列表/详情/矛盾高亮（archived 缺失、三件套缺件、路径不存在）；一眼看出状态矛盾。
6. **素材预检（封面 / 集号 / 时长）**：按 `batch_runner.py:414-437` 的封面优先级显示「实际会用的是哪张」，校验 9:16、短边 ≥720、集号零填充、总时长是否够裁 30 秒；避免导出完才发现封面不合格。
7. **成片预览 + 快速验收**：内嵌 `<video>` 播放 mp4、直接打开封面/成品所在目录、显示三件套与 `verify_export` 结论。
8. **失败重试入口**：按失败分类给出建议动作（重开剪映 / 重跑导出 / 整剧重跑），P1 只做「整剧重跑」，不做步骤级跳步。

---

## 7. 与 watcher 的共存约束

| 约束 | 具体做法 | 依据 |
|---|---|---|
| 不修改路径解析 | Console 只调用现有 CLI，不 monkey-patch、不改 `presets.json`；生效路径一律来自 `export_config.resolve_paths()` | 设计约束 |
| 不加 `--force` | 前台启动固定不带 `--force`；锁被占就排队 | `batch_runner.py:345-353` 非 force 才取锁 |
| 固定 `--no-watch` | 避免 Console 再拉起一个 watcher 守护进程，形成双调度 | `batch_runner.py:319-322` |
| 不抢 watcher 的锁 | `.watcher.lock` 只读；不 kill watcher、不改 phase | `cover_watch.py:300-348` |
| 不接管 watcher 任务 | 只处理用户明确指定的剧；`job.json` 处于 `TRIGGER_STAGES`（`titles_selected/prompts_ready/covers_ready`）时提示「watcher 可能会接手」 | `cover_watch.py:121`、`:173-222` |
| 归档/清理走 `--manual` | 不用默认 `archive_job` 链路，避免搬整个剧目录、避免与 watcher 的目录迁移冲突 | `manual_archive.py` 模块说明 |
| 同一剧互斥 | 启动前检查该剧 `.watcher.lock` 与 `job.json.stage`；正在跑则禁止启动 | `cover_watch.py:3117-3125` |
| 清理前必须 dry-run | UI 强制先出计划，再允许执行 | `manual_clean` 默认 dry-run 语义 |

> 已知差异（历史坑）：脚本链路的导出**不写 `job.json.outputs`**，watcher 接手会判 `disk_present_ledger_missing`；Console 应把这条作为提示展示，而不是自动改台账（详见 `docs/watcher-vs-script-parity.md:35,127-129`）。

---

## 8. 分期计划

### P0（最小可用，7 项）

1. Doctor 自检页（第 2 节全部检查项）。
2. 单剧启动：目录选择器 + 集数区间 + `--cut 30` + 导出开关 + 预设组下拉；固定 `--no-watch`、固定不 `--force`。
3. 参数只读面板：展示本次实际拼出的完整命令行（可复制），先给人审。
4. 实时状态：阶段 + 已耗时 + 日志流 + 导出百分比（如实显示「无估算」）。
5. 锁状态卡：`.export.lock` holder + 存活 + 「诊断」按钮（只诊断，不接管）。
6. 台账列表：`_stream_manifests` 条数、每剧 `state`、三件套是否完整。
7. 归档/清理 **dry-run 计划展示**（不执行）。

### P1

- 真归档 / 真清理 / `verify --manual` 执行（三段式确认 + 锁排队）。
- 执行历史 `runs.jsonl` + 详情页。
- 陈旧锁接管（复用 `manual_guard`）。
- 封面预检 + 成片预览。
- 失败后「整剧重跑」。
- `--export-resolution` 透传（需给 `batch_runner` 加一个参数，属于后端小改，单独立项）。

### P2

- 队列/多剧批量、优先级。
- `presets.json` 可视化编辑（写入需二次确认 + 备份）。
- 基于 `_timings.jsonl` 的进度权重自动校准、ETA。
- 桌面通知 / 企业微信或飞书提醒（可选）。
- watcher 运行态势图（每剧 phase 时间线）。

---

## 9. 明确不做 + 风险

### 不做

- 不做局域网/公网访问、不做多用户与权限体系。
- 不改现有 pipeline 的算法、不重写剪映 UI 自动化。
- 不接管 watcher 调度、不实现第二套调度器。
- 不做「一键全自动删除」，所有删除必须人工确认。
- 不做定时任务（已有 Windows 计划任务 / `schedule` 能力）。
- 不做导出参数以外的剪映内部设置（码率、帧率等）。

### 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 与 watcher 同时处理同一剧 | 草稿/清单互相覆盖 | 启动前检查 `.watcher.lock` 与 `job.json.stage`；实跑只做互斥提示 + 队列 |
| 同名剧目录 | 草稿名相同 → 覆盖上一次草稿 | 启动前查草稿是否已存在，要求改名或确认覆盖 |
| 长任务期间浏览器关闭 / 电脑休眠 | 前端丢状态 | 状态以后端为准；重连后从 `runs.jsonl` + 子进程 pid 恢复 |
| 陈旧锁误判 | 抢锁导致双导出 | 只做「诊断」，接管必须人工点击；沿用 `unknown 不接管` 原则 |
| `--cut` 语义误解 | 裁错内容 | UI 文案强制写「整条时间线开头」，并在计划摘要里回显 `cut=30s` |
| 清理误删 | 数据丢失 | 只走 `--manual`；强制 dry-run；保护源目录/cover_job；保留被草稿引用项 |
| 大目录体积统计卡顿 | 页面假死 | 只按需计算，显示 loading，可取消 |
| 端口被占 | 启动失败 | 顺延端口并提示；仅监听 `127.0.0.1` |

---

## 10. 需要你圈选的决策（回我 1/2/3 即可）

1. **执行入口**：`batch_runner`（推荐，和快速上手一致）还是 `ui_pipeline`（参数全但标定重）？
2. **P0 范围**：只读 + dry-run（推荐）还是 P0 就放开真归档/真清理？
3. **导出分辨率**：P0 先不支持（推荐），还是 P1 给 `batch_runner` 加 `--export-resolution` 透传？
4. **历史记录**：落 `pipeline_ui/_console/runs.jsonl`（推荐）还是只存内存？
5. **前端**：原生 HTML/JS（推荐）还是 Vue3 CDN？
