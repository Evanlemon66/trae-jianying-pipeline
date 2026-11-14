# 任务清单 · 草稿命名碰撞修复

> 前置：`specs/draft-naming-collision/spec.md` 经用户批准后执行。当前仅完成只读核查，未改代码。

## T1 统一组名小写口径（必须）
- [ ] `pipeline/export_config.py` L69-74 `preset_profile_name` 返回值加 `.lower()`
- [ ] 回归：`parse_preset_profiles("Presets_A, b")` → `["a","b"]`
- [ ] 确认无调用方依赖原大小写（`grep preset_profile_name`）

## T2 基础草稿查找加护栏（必须）
- [ ] `lib/cover_watch.py` L595-601 `find_stream_draft`：排除以 `_<小写版本名>` 结尾的目录；优先精确命中
- [ ] 同规则同步 `pipeline/series_runner.py` L76-84 `_find_draft`
- [ ] 验证：传基础名不返回 `_b`；`_b` 不返回基础名

## T3 基础草稿缺失时的判定（必须）
- [ ] `lib/cover_watch.py` L688-703 `_pending_account_outputs`：`draft is None` 时探测 `*串流_<组>`，返回克隆名并标记 `base_missing`
- [ ] `lib/cover_watch.py` `build_plan` 对应分支：`base_missing` 不再走「无草稿 → edit」
- [ ] 验证：用「八零猎王，风雪赶山养妻护女」「奶爸赶山：虎王认我当兄弟」两个真实剧名做只读判定

## T4 全量导出排除克隆（视 `--all-stream` 使用情况）
- [ ] `pipeline/series_runner.py` L87-92 `_all_stream_drafts` 过滤 `_<组>` 结尾

## T5 文档补充（必须）
- [ ] `handoff-contract.md` L124 / `SKILL.md` L245：`<组>` 明确写「一律小写」
- [ ] `pipeline/preset_ops.py` L328 注释与实现对齐（N4 落地后即成立）

## T6 静态验证（不跑导出、不动 job 数据）
- [ ] `python -m py_compile lib/cover_watch.py pipeline/series_runner.py pipeline/export_config.py pipeline/preset_ops.py`
- [ ] 只读跑 `build_plan` 对 5 种结构：A+B / 仅 A / A+B+C / B 组缺图 / 缺 `presets_b`
- [ ] 确认 `pipeline/presets.json` 与 `D:\短剧` 无写入
