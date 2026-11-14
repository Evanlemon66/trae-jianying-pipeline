# tasks — R04 / R05 fail-closed

> 状态：全部完成（2026-11-14），整仓 282 passed / 1 warning。

前置：spec 见 [spec.md](file:///d:/剪映5.9/剪映5.9版+jianying-skill/国内用户下载这个（trae+skill）/trae-jianying-skill/specs/r04-r05-fail-closed/spec.md)

## T1 R04 白名单唯一实现
- [x] 新增 `pipeline/restore_backups.py`：`EXACT_BACKUP_NAMES`、`BACKUP_DIR_NAMES`、
      `_BACKUP_SUFFIX_RE`、`is_restore_backup_source()`、`prune_restore_backups()`。
- [x] `.backup/` 顶层目录整体删除；其余只删顶层命中白名单的文件。

## T2 R04 三个清理入口收敛
- [x] `pipeline/pipeline.py::cleanup_draft_temp_files` 改为委托 `prune_restore_backups`，
      签名不变。
- [x] `pipeline/pipeline.py::create_empty_draft` 两处内联 junk 循环改调
      `cleanup_draft_temp_files(new_dir)`。
- [x] `pipeline/merge_drafts.py::merge` 的内联 junk 循环改调 `prune_restore_backups(out_dir)`。

## T3 R05 引用扫描 fail-closed
- [x] `pipeline/pipeline.py` 新增 `_refs_from_draft()`，同时收集 videos / audios 本地路径。
- [x] `referenced_media_paths()`：缺 `draft_content.json` 的草稿目录先读
      `_pipeline_backup.json` 兜底；读不到返回 `None`；无草稿标记的目录跳过。

## T4 R05 导出预检 fail-closed
- [x] `robust_auto_export.py::_preflight_media`：去掉“无 ffprobe 直接返回”，
      存在性/大小检查恒执行，仅解码检查依赖 ffprobe。

## T5 测试
- [x] `tests/test_publish_rollback.py` 新增恢复源白名单用例（应删/应留两组）。
- [x] 新增 `tests/test_media_refs_fail_closed.py`（5 项：备份兜底、缺备份返回 None、
      损坏返回 None、非草稿目录跳过、无 ffprobe 仍报缺失）。

## T6 回归与收尾
- [x] `python -m pytest tests -q` 全过（282 passed / 1 warning）。
- [x] 不重启 watcher、不启停剪映、不跑导出。
- [x] 汇报改动文件、测试数字与 `robust_auto_export.py` 的改动原因。

## 完成定义
- 清理白名单只存在于 `pipeline/restore_backups.py`，三个入口共用。
- 缺 `draft_content.json` 时引用扫描不会静默返回“无引用”，最差返回 `None`。
- 无 ffprobe 时导出预检仍能阻断缺失素材并列出路径。
- 整仓测试通过，watcher 保持原状。

## R15 长片无断点续传（P2，已评估、暂不做）
- 结论：不引入续传/分片，代码保持现状。
- 理由：失败只会整段重来，不产出错成品；进度侧已有 `stage` / `start_time`
  与失败重试，可观测性满足当前需要。续传依赖剪映导出是否支持按段输出，
  属于新能力，改动面与验证成本远高于本批其余条目，收益仅是缩短重来时间。
- 后续若要做：先验证剪映分片导出能力，再在导出执行层加断点，不改现有判定链。

