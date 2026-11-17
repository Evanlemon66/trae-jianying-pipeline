# Checklist：路径配置收口 + watcher 暂停 + 运行记录

> 对应 `spec.md` / `tasks.md`。每条均为可核对的事实，已逐项验证。

## Q3 路径三根统一

- [x] `pipeline/export_config.py` 提供 `ConfigError`、`resolve_paths()`、`validate_config()`、`get_watch_roots()`
- [x] 业务根（`series_root`/`export_dir`/`deps_root`）无字面量默认值；未配置或显式无效 → 抛 `ConfigError`
- [x] `get_series_root()` 只读 `JY_SERIES_ROOT` / presets.json，不再从 `watch.roots` 回退
- [x] `watch.roots` 为空 → 有效扫描根仅 `[series_root]`；非空 → 每项须存在、是目录、含 `*/cover_job`（不要求在 `series_root` 下）
- [x] 有效扫描根去重合并，唯一入口 `get_watch_roots()`
- [x] `archive_job.py` 用 `get_watch_roots()` 与 `get_archive_root()`；解析失败/空根 → 拒绝执行（fail-closed）
- [x] `cover_watch.py`、`series_runner.py`、`admission_draft.py`、`init_draft.py`、`admission_test.py` 不再直接读 `watch.roots`
- [x] `doctor_core.py`、`console/paths.py`、`envinit.py` 统一走 `resolve_paths("console")`
- [x] `install_deps.ps1` 无 `D:\AI_Deps` / C 盘兜底，缺失时直接报错
- [x] `presets.example.json` 业务根为空、`watch.roots=[]`，说明文案同步
- [x] `pipeline/migrate_config.py` 支持 `--dry-run` / `--write`（写前备份、写后校验）
- [x] Console 支持 `config.migrate` 动作，环境页有对应检查项与按钮
- [x] `.trae/skills/jianying-editor`（SKILL.md + `robust_auto_export.py`）改用 `get_watch_roots()`
- [x] `.trae/skills/drama-cover-builder`（SKILL.md + `handoff-contract.md`）归档/导出根语义同步
- [x] 根 `SKILL.md` 增加“路径唯一入口 `resolve_paths()`”契约
- [x] `使用指南.md`、`快速上手.md`、`docs/manual-entry-real-run.md`、`docs/web-console-plan.md`、`docs/project_status.md` 同步
- [x] `specs/**` 五个历史 Spec 增加“路径契约已收口”说明
- [x] 全仓库 `D:\短剧`、`D:\成片导出`、`D:\短剧_存档`、`D:\AI_Deps` 仅剩测试夹具/历史输出/迁移说明
- [x] Console 界面文案去掉写死盘符（`view.js` 剧集目录、`env.js` series_root / watch.roots 标签）

## Q6 环境预设 / 环境依赖

- [x] `config` + `jianying`（“剪映前置”）分组渲染到「环境预设」页
- [x] 删除“首次登录并同意协议”步骤
- [x] 空草稿模板 `default` 与组合预设 `presets_a/b` 作为环境预设提醒项
- [x] `TraeDramaWatcher` 计划任务标记为可选（warn，不强制）
- [x] 新增 `pipeline/install_deps_admin.ps1`（`-DetectOnly` 输出 `ADMIN_NEEDED=0|1`）
- [x] Console 一键安装改为：普通权限探测 → 按需一次 UAC → 普通权限主安装
- [x] 依赖自检新增 VC++ Redistributable、MediaInfo `can_parse()`（当时另有 ui_pylibs/全局 numpy 一致性项，该目录移除后已删除）
- [x] `imageio-ffmpeg` 经检索无调用，已从缺口清单移除

## Q7 watcher 暂停

- [x] `watcher_supervisor.ps1` 启动前检查 `pipeline/_work/watcher.pause`，命中则不拉起
- [x] `delayed_start.ps1` 开始与拉起前各检查一次，命中则退出
- [x] Console `task.pause` / `task.resume`，`envinit.watcher_paused()/pause_watcher()/resume_watcher()`
- [x] `status.watcher_info()` 返回 `paused` / `pause_path`
- [x] 暂停不杀正在运行的任务

## Q8 运行记录

- [x] `batch_runner.py` 写 `<workdir>/_run.json`（11 个规定字段）与 `run-<id>.log`
- [x] stdout/stderr tee 到运行日志；`__main__` 统一收尾（含 SystemExit / 异常）
- [x] `_batch_state.json` 顶层 `run` 快照；单集 `started_at`/`attempts`/`error`/`error_class`
- [x] 导出成功回填 `_artifacts`（草稿 → 成品路径）
- [x] 新增只读 `pipeline/run_status.py`（`--workdir`/`--series`/`--json`/`--tail`）
- [x] Console `status.batch_state_summary()` 附带 `run` 且不计入集数；`latest_runs()`；`/api/runs` 含 `pipeline_runs`

## 验证

- [x] `python -m pytest tests -q` → **452 passed**（全绿；含脚本式子测试）
- [x] `pytest tests/test_path_config.py tests/test_migrate_config.py tests/test_external_tasks.py` → 32 passed
- [x] `python tests/test_archive_job.py` → 全部通过（含配置解析失败用例）
- [x] 语义变更用例同步：`test_resolve_series_root_index`（root_index>0 拒绝、video.root 优先）、`test_env_presets_save`（必填 series_root）
- [x] 脚本用例显式注入 `get_watch_roots`（`test_chain_exception.py` / `test_plan_nowrite.py`），不再依赖本机 presets
- [x] `test_manual_clean_gates.py` 闸3 重定基线 `95a4dc9` + `BATCH_CHANGED` 白名单
- [x] 四个 `.ps1` 语法检查通过（`Parser::ParseFile`，UTF-8 BOM）
- [x] 哨兵手工验证：写入暂停文件后 supervisor 日志输出“已暂停”，未拉起新进程
- [x] `migrate_config.py --dry-run` 输出迁移计划且不写盘
- [x] `run_status.py --json` 可运行；`_run.json` 往返测试通过
- [x] `pause_watcher()` / `resume_watcher()` 与 `status.watcher_info().paused` 联动正确
