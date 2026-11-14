# R04 / R05：恢复备份源收敛 + 素材缺失 fail-closed

> 状态：已落地（2026-11-14），整仓测试 282 passed / 1 warning。未重启 watcher。

## 1. 背景（代码事实）

### R04 恢复备份源

`pipeline/pipeline.py::cleanup_draft_temp_files`（L241-268）只删顶层 `*.tmp` 与
`draft_content.json.bak*` / `draft_meta_info.json.bak*`。以下已知恢复源会漏掉：

- `draft_content.json.bak_<ts>`（`backup_draft` L421-425 生成）
- `draft_content.json.before_restore.bak`（`robust_auto_export` 串流恢复前留的现场）
- `draft_content.json.repairbak_<ts>` / `.splitbak_<ts>` / `.verifybak_<ts>`
  （`draft_io.write_draft_content(backup_suffix=...)` 生成）
- `_pipeline_backup.json`（`_write_pipeline_backup` L366-377，发布后会被重写）
- `.backup/` 目录（剪映自己的备份目录）

同时 `create_empty_draft`（L495-500、L542-547）和 `merge_drafts.merge`（L321-324）
各有一份小范围清理逻辑，三处白名单会漂移。

### R05 素材缺失

- `pipeline/pipeline.py::referenced_media_paths`（L3508-3532）对没有
  `draft_content.json` 的草稿目录直接 `continue`，该草稿引用的素材全部被判“未被引用”。
  `pipeline/prune_workdirs.py` L48 与 `clean_workdir` L3545 都依赖这个函数删文件。
- `.trae/skills/jianying-editor/examples/robust_auto_export.py::_preflight_media`
  （L183-242）在 `ffprobe` 找不到时直接 `return 0, []`，连“文件是否存在/过小”也跳过，
  属于 fail-open；有 ffprobe 时缺失文件已经会抛异常，并被 `lib/failure_class.py`
  归为 `asset_missing`（不可重试、终态）。

## 2. 目标

1. R04：备份源白名单收敛为唯一实现，覆盖全部已知恢复源与后续新增的 `.bak*` 后缀；
   绝不误删正常草稿文件。
2. R05：引用扫描在草稿内容缺失时不得把整份草稿的素材当成“未被引用”（优先从
   `_pipeline_backup.json` 恢复引用，恢复不了就整体中止）；导出预检在无 ffprobe 时
   仍对缺失/空文件 fail-closed 阻断，并列出具体路径。

## 3. 实现方案（决定）

### 3.1 新增 `pipeline/restore_backups.py`（R04 唯一白名单）

无重依赖（只用 `os/re/shutil`），供 pipeline / merge_drafts 共用，避免 `merge_drafts`
为此 import 整个 pipeline。

- `EXACT_BACKUP_NAMES = frozenset({"_pipeline_backup.json"})`
- `BACKUP_DIR_NAMES = frozenset({".backup"})`：顶层目录整体视为备份源，递归删除。
- `_BACKUP_SUFFIX_RE = re.compile(r"(?:\.(?:bak|tmp)(?:[._-][^\\/]*)?|bak_\d+)$", re.I)`
  - 覆盖 `.bak`、`.bak_<ts>`、`.bak_vad_gate`、`.before_restore.bak`、`.tmp`、
    `.repairbak_<ts>`、`.splitbak_<ts>`、`.verifybak_<ts>`。
  - 新增备份只要沿用 `<原文件>.bak[_后缀]` / `.tmp` / `*bak_<数字>` 命名即自动命中。
- `is_restore_backup_source(name) -> bool`：精确名（大小写不敏感）或后缀正则命中。
- `prune_restore_backups(draft_dir) -> list[str]`：
  - 只扫顶层：文件名命中白名单才删；`.backup` 目录整体 `rmtree`。
  - 不递归 `Resources/` 等正常子目录；`draft_content.json`、`draft_meta_info.json`、
    `draft_cover.jpg`、`draft_settings`、`.locked`、`draft_content.json.pre_framefix.json`
    （framefix 校验要用）都不在规则内，保持不动。
  - 单文件删除失败静默跳过（与现状一致）。

### 3.2 `pipeline/pipeline.py`

- `cleanup_draft_temp_files(draft_dir)`：函数名与签名不变（`admission_test.py` L831
  在用），实现改为 `from restore_backups import prune_restore_backups` 并直接返回其
  结果；docstring 补充恢复源说明。
- `create_empty_draft` 两处内联 junk 循环（L495-500、L542-547）删除，改调
  `cleanup_draft_temp_files(new_dir)`，让模板带过来的旧备份源一起清掉。

### 3.3 `pipeline/merge_drafts.py`

- 顶部 `from restore_backups import prune_restore_backups`，`merge()` 中 L321-324 的
  内联清理替换为 `prune_restore_backups(out_dir)`（复制源草稿后立即清，避免旧备份
  随合并稿发布）。

### 3.4 `pipeline/pipeline.py::referenced_media_paths`（R05 引用扫描）

- 新增私有 `_refs_from_draft(draft) -> set[str]`：收集 `materials.videos` 与
  `materials.audios` 的本地 `path`（跳过空值与 `http(s)`），用 `_media_key` 归一。
- 遍历草稿根：
  - 非目录 → 跳过（保持原行为）。
  - 有 `draft_content.json` → 解析；损坏仍返回 `None`。
  - **没有** `draft_content.json` 且目录带草稿标记（`draft_meta_info.json` /
    `_pipeline_backup.json` / `.backup/` / 任意 `draft_content.json*` 文件）：
    1）能读 `_pipeline_backup.json` → 用它的引用并入（保护范围只会变大，安全）；
    2）读不到 → 打印缺失草稿名并 `return None`（调用方必须放弃清理）。
  - 无草稿标记的普通目录 → 跳过（不算草稿，没有引用可言）。
- 返回值契约不变：`set` 或 `None`；`prune_workdirs` / `clean_workdir` 无需改。

### 3.5 `robust_auto_export.py::_preflight_media`（R05 导出预检）

**为什么必须动这个文件**：R05 要求“导出前发现素材缺失 fail-closed 阻断并指出缺哪些”。
现状在 `ffprobe` 缺失时 L200-202 提前返回空结果，把“文件不存在/过小”一并跳过，是唯一
的 fail-open 口子。改动最小化：

- 移除 `ffprobe` 缺失时的提前返回，改为打印“仅校验存在性/大小，跳过解码检查”。
- `check(p)` 中 `os.path.isfile` 与 `getsize < 1024` 恒执行；仅 ffprobe 解码段在
  `ffprobe is None` 时跳过。
- 异常文案保持含“素材预检不通过”“文件不存在”，`failure_class` 继续归为
  `asset_missing`（不可重试、终态），原有重试链路不变。

## 4. 测试

- `tests/test_publish_rollback.py` 新增 `test_cleanup_removes_all_restore_backup_sources`：
  - 应删：`draft_content.json.bak`、`draft_meta_info.json.bak`、`.bak_20260101_120000`、
    `.before_restore.bak`、`.repairbak_1700000000`、`.verifybak_1700000000`、
    `.splitbak_1700000000`、`_pipeline_backup.json`、`template.tmp`、`.backup/x.bak`（目录）；
  - 应留：`draft_content.json`、`draft_meta_info.json`、`draft_cover.jpg`、`.locked`、
    `draft_settings`、`Resources/cover.png`、`draft_content.json.pre_framefix.json`。
- 新增 `tests/test_media_refs_fail_closed.py`：
  1. 缺 `draft_content.json` + 有 `_pipeline_backup.json` → 引用被保护（含 videos/audios）。
  2. 缺 `draft_content.json` 且无任何备份源 → `referenced_media_paths()` 返回 `None`。
  3. `draft_content.json` 损坏 → `None`。
  4. 无草稿标记的普通目录 → 跳过且返回空集。
  5. `_preflight_media`：monkeypatch `_find_ffprobe -> None`，草稿引用一个不存在的文件
     → `problems` 非空且路径在列。
- 回归：`python -m pytest tests -q`，基线 275 passed / 0 failed，期望 275 + 新增用例全过。

## 5. 约束

- 不重启 watcher、不启停剪映、不跑导出。
- 除 3.5 外不改 `robust_auto_export.py`；`lib/cover_watch.py` 不动。
- 改动基于已提交的最新代码，不修改冻结基线 `data/freshness_legacy_baseline.json`。

## 6. 非目标

- 不重构 `prune_workdirs.py` 的 CLI。
- 不引入新的失败类别（沿用 `asset_missing`）。
- 不清理 `draft_content.json.pre_framefix.json` / `.pre_tailfix.json`（framefix 校验依赖）。
