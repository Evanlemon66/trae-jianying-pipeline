# 任务清单：文档一致性修订

前置：用户确认本 spec 后再执行；执行前先 `git status` 确认无其他未提交改动干扰。

## T1 补齐断链步骤（P0）

- [ ] `快速上手.md` §0：恢复"首次手动启动剪映并登录一次"的说明
- [ ] `快速上手.md` §2 与 §5 之间：新增"准备 `default` 空草稿模板"步骤，说明缺失时报错形态
- [ ] `快速上手.md` §3.2：新增"先在剪映创建组合预设"步骤
- [ ] `使用指南.md` §1/§5：同步登录、`default` 模板、预设前置三条
- [ ] `使用指南.md` §5.7：补 `TraeDramaWatcher` 注册示例 + "不注册的影响"说明

验收：从 clone 开始按顺序执行，不出现"文档没写但必须做"的动作。

## T2 修正与实现不一致（P1）

- [ ] 统一 `_export_stale.json`（4 处）
- [ ] 修正 `export_console.log` 落点（§6.2/§8/§9）
- [ ] 删除 `.verify.json` 描述
- [ ] 重写 doctor 退出码规则，补齐 3 个检查项（两份文档）
- [ ] 修正 batch_runner "新鲜度记录" 措辞
- [ ] `presets.example.json` `_说明` 的 `--profile` 逗号表述
- [x] ~~`ui_pylibs` 补第一候选路径（两份文档）~~（该目录已整体移除，条目作废）
- [ ] 修正 `✅ 导出完成!` 日志文本

验收：每条改动都能在代码里指到对应实现（函数名/行号）。

## T3 消除自相矛盾与细节（P2/P3）

- [ ] `快速上手.md` §3.1 拆"最低可出片"与"完整交付"两档
- [ ] `快速上手.md` §4.2 补 `--list-presets` 退出码 2 的例外
- [ ] `使用指南.md` §2 补 `.edit_state.json`
- [ ] 无 D 盘提示、`requirements.txt` 入口、`pipeline_ui` 入口表述
- [ ] 超时行措辞、JSON 示例中 `jianying_dir` 注释

验收：两份文档对同一事实只保留一种说法。

## T4 文档自检

- [ ] `grep -n "export_stale\|verify.json\|export_console\|WATCHER"` 两份文档，逐条比对
- [ ] 实跑 `python pipeline\doctor.py` 与 `python pipeline\pipeline.py --list-presets`，把输出与文档描述对照
- [ ] 汇报：改了哪些文件、每条对应哪个 ID
