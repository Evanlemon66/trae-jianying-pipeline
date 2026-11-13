# 多账号预设 / 多账号封面 后续跟踪

> 本文件只记录**尚未完成**的事项，便于后续排期。已完成部分见文末「已完成」。
> 改动涉及 watcher 常驻进程时，改完必须重启 watcher 才生效（`check_release_safe` 会用代码指纹拒绝旧的 `--phase` 放行）。

## 待办

### P1-1 账号封面目前只做门禁校验，没有真正被使用

- **现象**：`04_covers/cover_01_hit_b.png`（或 `04_covers/b/cover_01_hit.png`）能通过门禁，但 B 的克隆草稿仍使用基础竖版封面，`job.json` 的 `cover_map` 也只是基础 3 张。
- **场景**：两个账号需要不同封面发布。
- **需要先决策**：
  - 方案 A：封面只在发布时人工用（现状够用，不加代码）；
  - 方案 B：写进 B 草稿封面（clone 时调 `jy_cover.set_cover_composition`）；
  - 方案 C：B/C 都做，并把账号封面登记进 job（需扩 `cover_map` schema，当前硬校验 3 项）。
- **涉及文件**：`pipeline/preset_ops.py`（clone）、`lib/cover_watch.py`（run_clone）、`lib/job_manifest.py`（`validate_job` 的 cover_map 校验）。
- **验收**：指定账号封面后，B 草稿打开看到的封面是 B 的图；`job.json` 能查到每个账号用的是哪几张。

### P1-3 B 的成品没有进清理清单（✅ 已落地）

- **现象（历史）**：`record_video` 只给基础草稿登记 `cleanup.external`，`stream_manifest.py clean` 不会删除 B 的 mp4 / B 的草稿目录，磁盘持续增长。
- **最终契约**：
  - `clean --job "<草稿名>"` **默认清整部剧所有版本**（基础版 + 各 `_<版本>` 克隆版）；只清某一版本必须显式 `--profile <版本>`（`--profile a` = 基础版）；
  - 单版本清理时 `__jycache__` / `_work_*` / C 盘草稿等共享产物保留，并提示「仍有 N 个版本引用」；`--intermediates-only` 只允许整剧，不能与 `--profile` 同用；
  - `pipeline/_stream_manifests/` 每版本一份 `<草稿名>.json`，记 `shared_with`，体积与清理按 realpath 去重；
  - 各版本成品入 `job.json.video.outputs.<版本>`（`{draft, export_dir, export_file, size_bytes, state, exported_at, history[]}`）；单版本清完回写该版本 `state=cleaned`，全部清完才写顶层 `video.export_state=cleaned`；
  - 归档新增前置：**所有声明版本都已完成**（`video.outputs` 每个版本 `state` ∈ `exported`/`cleaned`），否则跳过，`--force` 可绕过；
  - CLI 入参统一 `--profile`（`pipeline.py` / `batch_runner.py` / `rebase_presets.py` / `init_draft.py` / `ui_pipeline.py`），不复用 `--only`（在 `batch_runner` 是集段含义）。

### P2-1 发布回执按平台存，同平台多账号会互相覆盖

- **现象**：`06_publish/<platform>.json` 每平台一份状态，同一平台两个账号无法区分；`receipts/` 虽有多条记录，但状态字段只有一份。
- **场景**：两个抖音号 / 两个视频号分别发布同一部剧。
- **改法**：平台键扩展为 `<platform>@<账号>`（保留旧键兼容），或 `publish.accounts[]` 结构。
- **涉及文件**：`lib/job_manifest.py`（publish 相关）、`scripts/`（发布回执写入侧）。
- **验收**：同平台两个账号各自独立的 `status / receipt`，互不覆盖。

### P2-2 账号 ↔ 预设组没有显式映射

- **现象**：profile 靠 dispatch / 全局配置传入（`a,b`），名字写错（B 本应用 `presets_b` 却传了 `a`）不会报错，只会生成两份一样的预设草稿。
- **改法**：`pipeline/presets.json` 增加 `accounts: {"a": "presets_a", "b": "presets_b"}`，解析时优先查映射；未知账号直接报错。
- **涉及文件**：`pipeline/export_config.py`、`pipeline/pipeline.py`（`resolve_presets`）、`pipeline/presets.example.json`。
- **验收**：传 `--profile b` 时，即使调用方没写组名也稳定命中 `presets_b`；传不存在的账号名报错。

### P2-4 ingest 改名后 prompts.items[].file 可能失效

- **现象**：`ingest` 会把任意命名的封面改名到契约名，但 `prompts.items[].file` 里写的还是旧文件名，任务区会出现「prompt 指向的图不存在」。
- **现状**：目前门禁不校验 prompts 文件是否存在（避免误拦），所以不会报错，但后续按账号取图时会取不到。
- **改法**：`ingest` 改写 prompts.items[].file（或按 slot+group 重新绑定），保证与改名结果一致。
- **涉及文件**：`lib/job_manifest.py`（`ingest` 的 `_fill` 与 prompts 收口段）。

### P3-1 克隆草稿命名歧义（✅ 已落地）

- **现象（历史）**：克隆名规则是 `<基础草稿名>_<profile>`。若基础草稿名本身以 `_a` 结尾，人眼不易分辨哪份是基础、哪份是克隆。
- **最终契约**（不改双下划线方案，按下列规则收敛歧义）：
  - **基础版草稿名不加后缀**，台账版本名固定记 `a`；扩展版为 `<基础草稿>_<版本>`；
  - 版本名匹配 `[a-z][a-z0-9]{0,7}`（小写字母开头，后接 0—7 位小写字母/数字，如 `b`、`c`、`d`、`b2`）；大写、下划线、中文一律非法；
  - 成品名与草稿同名；从成品名反查版本：先剥同版本重跑序号 `.v<n>`，再看 `_<版本>` 后缀，无该后缀即基础版 `a`；
  - 同版本重跑不覆盖旧成品，改名保留 `.v<n>` 三件套（mp4 / `-封面.jpg` / `.progress.json`），任一改名失败整组回滚硬失败；
  - 配置声明 `workflow.preset_profile`（或顶层 `preset_profile`）支持 `"a,b"` / `"a，b"` / `["a","b"]` / `"presets_a,presets_b"`，可声明 3 个以上版本；优先级 `workflow.preset_profile` > `job.preset_profile` > 全局 `watch.preset_profile` > 反推。
- **注意**：改名会改变 `_account_draft_name` 的推导，必须同步 `cover_watch` / `batch_runner` / 测试。

### P3-2 多份导出是串行的

- **现象**：剪映同一时刻只能导一份，所以 N 个账号 = N 倍导出时间，watcher 一轮只推进一部剧。
- **改法**：无需改逻辑，必要时在 `plan` / 通知里提示预计份数与串行特性。

## 已完成（本轮）

- [x] 剪映回写风险改为只盯「这份源草稿是否正在被写入」，去掉「剪映在运行就告警」的误报（`preset_ops._read_stable_source`）。
- [x] 门禁不再死守「正好 4 张」：基础 4 张 + 任意多张账号图；每个出现过的账号必须 4 张齐全；备份名不算账号。
- [x] 修复账号封面被误映射并 `os.replace` 覆盖的安全问题（账号图不参与基础识别与改名）。
- [x] 修复 `video.status=done` 永久挡住后来新增账号（`cover_watch._pending_account_outputs`，仅认 job 自己声明的预设组）。
- [x] 修复 `export_state=exported` 早退挡住附加账号成品（`cleaned` 仍为终态）。
- [x] 附加账号成品写入 `video.account_outputs[]`。
- [x] 修复旧草稿兜底 GC 误删封面 composition（引用根补上 tracks 之外的顶层字段）。
- [x] 修复 `prompts.count 应为 4` 硬校验：支持 A/B 多组（count=4×组数），每组四槽齐全即可；`重回81，东北猎王护全家` 已验证 0 errors。
- [x] 账号封面识别兼容线上实际命名：`04_covers/b_group/cover_01_hit_B.png`（目录 `_group` 归一为 `b`，文件 `_B` 后缀冗余可识别）。
- [x] P2-3 多组标题选择接入 job：`select --groups "t1 t2 n1，t5 t9 n12"` / `ingest` 写 `titles.selected_groups`（首组同步 `titles.selected`），`validate` 校验每组 3 项、组内不重复、编号存在、首组一致；`titles.json` 同步写 `group_names`。
- [x] P1-2 watcher 反推账号组：`workflow.preset_profile` 为空时，从 `prompts.json` 的 `items[].group` 与 `04_covers/<x>_group` 反推，仅保留 `presets.json` 里存在的组且至少 2 组；`_pending_account_outputs` 同样走反推，已导出任务补 B 账号不会再被 `video.status=done` 挡住。
- [x] P1-3 多版本清理落地：默认整剧清、`--profile` 单版本、共享产物保留并提示引用数、realpath 去重、`video.outputs.<版本>` 回写、归档前置「所有声明版本已完成」。
- [x] P3-1 版本命名收敛：基础版不加后缀记 `a`，扩展版 `_<版本>`（`[a-z][a-z0-9]{0,7}`），同版本重跑 `.v<n>` 三件套。

## 验证命令

```powershell
py -3.12 -m pytest tests/test_preset_profiles.py tests/test_ui_pipeline_platform.py -q
py -3.12 tests/test_validate_fix.py
py -3.12 tests/test_watch_guardrails.py
py -3.12 tests/test_export_gate.py
```
