# Tasks：路径配置收口 + watcher 暂停 + 运行记录

> 状态：已实施完成（验证见 `checklist.md`）
> 对应 Spec：`spec.md`（本轮）

## A. Q3 路径配置收口

- [x] A1 `pipeline/export_config.py`：新增 `ConfigError`、`resolve_paths()`、`validate_config()`、`get_watch_roots()`
- [x] A2 `pipeline/export_config.py`：删除业务根字面量，解析改为“无效即抛错”
- [x] A3 `pipeline/export_config.py`：`series_root` 不再读 `watch.roots`；有效扫描根 = `[series_root] + watch.roots` 去重，额外根校验“存在 + 有 `*/cover_job`”
- [x] A4 `pipeline_ui/admission_draft.py`：`_root_of` 改为 `video.root` 显式优先、否则 `series_root`；`root_index>0` 报错
- [x] A5 `pipeline_ui/init_draft.py`：不再直接传 `watch.roots`
- [x] A6 `pipeline_ui/console/paths.py`、`envinit.py`、`pipeline/doctor_core.py`：统一走解析快照
- [x] A7 `pipeline/install_deps.ps1`：删除 `D:\AI_Deps` 与 C 盘兜底
- [x] A8 `pipeline/presets.example.json`：业务根置空、`watch.roots` 置空、移除字面量
- [x] A9 新增 `pipeline/migrate_config.py`（dry-run/write + 备份）
- [x] A10 Console 新增 `config.migrate` 动作
- [x] A11 `scripts/freshness_baseline.py` 帮助文案去字面量
- [x] A12 `tests/test_path_config.py` 重写、新增 `tests/test_migrate_config.py`
- [x] A13 Skill 同步：`.trae/skills/jianying-editor/SKILL.md` 与 `examples/robust_auto_export.py` 的 roots 默认值
- [x] A14 Skill 同步：`.trae/skills/drama-cover-builder/SKILL.md` 与 `references/handoff-contract.md` 的归档/导出根语义
- [x] A15 契约同步：根 `SKILL.md` 增加“路径唯一入口 resolve_paths()”
- [x] A16 用户手册同步：`使用指南.md`、`快速上手.md` 的必填项、回退描述、示例路径
- [x] A17 文档同步：`docs/manual-entry-real-run.md`、`docs/web-console-plan.md`、`docs/project_status.md`
- [x] A18 历史 Spec 同步：`specs/web-console`、`docs-consistency`、`export-parity`、`multi-account-todo` 加“路径契约已收口”说明
- [x] A19 收尾检查：全仓库字面量只允许出现在历史数据/占位/迁移说明中
- [x] A20 `pipeline/archive_job.py` + `tests/test_archive_job.py`：改用 `get_watch_roots()`/`get_archive_root()`，解析失败 fail-closed

## B. Q6 环境预设分组与文档

- [x] B1 `pipeline_ui/console/static/env.js`：`jianying` 组渲染到环境预设页（组名改“剪映前置”）
- [x] B2 文档：删除登录步骤；计划任务标可选；素材目录标暂不要求
- [x] B3 文档：新增“环境预设 / 环境依赖 / 剪映前置”三分结构
- [x] B4 文档：按实际使用重写五项“缺口”复核
- [x] B5 新增 `pipeline/install_deps_admin.ps1`（长路径 / VC++ / 缺失 ffmpeg，`-DetectOnly` 输出 `ADMIN_NEEDED`）
- [x] B6 `pipeline/install_deps.ps1`：非管理员不再写 HKLM，改为提示走管理员脚本；终检补 VC++ / MediaInfo / numpy 一致性
- [x] B7 Console 一键安装改三段式；日志合并 detect/admin/stdout/stderr 尾部
- [x] B8 `envinit.deps_report()` 新增 VC++、MediaInfo、numpy 一致性三项

## C. Q7 P0：暂停哨兵

- [x] C1 `pipeline/watcher_supervisor.ps1`：`$pauseFile` + `Invoke-Supervisor` 开头检查
- [x] C2 `pipeline/delayed_start.ps1`：`$pauseFile` + 启动前检查

## D. Q7 P1：Console 暂停/恢复

- [x] D1 `pipeline_ui/console/envinit.py`：`PAUSE_PATH`、`watcher_paused()`、`pause_watcher()`、`resume_watcher()`
- [x] D2 `pipeline_ui/console/envinit.py`：`_chk_watcher_runtime()` 暂停态与对应按钮
- [x] D3 `pipeline_ui/console/app.py`：`task.pause` / `task.resume`
- [x] D4 `pipeline_ui/console/static/env.js`：确认文案与分支
- [x] D5 `pipeline_ui/console/status.py`：`watcher_info().paused` / `pause_path`

## E. Q8 P0：batch_runner 运行记录

- [x] E1 `pipeline/batch_runner.py`：`_Tee`、`_write_run`（临时文件 + `os.replace` 原子写）、`_start_run`、`_finish_run`
- [x] E2 `main()` 接入运行记录；`__main__` 统一收尾（SystemExit / 异常 / 中断）
- [x] E3 串流模式：写 `started_at` / `attempts` / `error` / `error_class`
- [x] E4 逐集模式：成功清除错误字段，失败落 `error` + `error_class`
- [x] E5 导出成功后回填 `_artifacts`（草稿 → 成品路径，非集条目不计入进度）

## F. Q8 P1：只读聚合

- [x] F1 新增 `pipeline/run_status.py`
- [x] F2 `pipeline_ui/console/status.py` 读 `_run.json`（缺失回退 `_batch_state.run`），新增 `latest_runs()`；`/api/runs` 增 `pipeline_runs`
- [x] F3 `tests/test_external_tasks.py` 新增用例（往返、tee、run 不计入 total）

## G. 文档

- [x] G1 Q3：写入“完全去硬编码”的方案与迁移说明
- [x] G2 Q7/Q8：追加实施记录
- [x] G3 更新总改动面、验证清单、证据索引（`docs/console-doctor-followup-implementation.md` §11）

## H. 验证

- [x] H0 `python -m pytest tests -q` 全量（452 passed，全绿）
- [x] H1 `pytest tests/test_path_config.py tests/test_migrate_config.py tests/test_external_tasks.py`（32 passed）
- [x] H2 PowerShell 语法检查四个 `.ps1`（均为 UTF-8 BOM，0 错误）
- [x] H3 哨兵手工验证（supervisor 日志“已暂停”，未拉起）
- [x] H4 `migrate_config.py --dry-run` 验证（输出计划、不写盘）
- [x] H5 `run_status.py --json` 验证
- [x] H6 Console 暂停/恢复逻辑联调（`pause_watcher`/`resume_watcher`/`watcher_info().paused`）
