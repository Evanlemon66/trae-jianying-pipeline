# Changelog

本项目遵循语义化版本（SemVer）。每个 tag 为稳定快照，使用者建议 `git clone --branch v1.0.0` 取版本而非滚动 main。

> 说明：本文件此前仅登记 v1.0.0，自 v1.1.0 起恢复记录。

## [v1.1.0] - 2026-10-28

`drama-cover-builder` 技能升级，聚焦 Stage 1 标题生成与交付门禁。

### 新增
- **版4 机制版**与八族技法池（M1 数字资产反差 / M2 关键物件 / M3 结局倒叙 / M4 身份体面反差 / M5 价值反转 / M6 群体社会反应 / M7 悬念缺口 / M8 处境痛点），产出由 3×3=9 条扩为 **3×4=12 条**
- `references/scorecard.md`：七维评分、硬门槛、每版本 1 条推荐
- `references/failure-cases.md`：F1—F10 失败模式、真实翻车记录、标题台账、规范升级触发条件
- `references/platform-rules.md`：A 层审核红线、B 层平台规格（待核实）、C 层烧字字数档位与压缩规则
- `references/ab-testing.md`：发布侧 A/B 设计（预留，默认不读）

### 变更
- 标题规范升 v4.1（`references/title-spec.md`），v3 归档到 `references/archive/`
- 交付门禁改为 **T1—T12 平铺编号、每次选一条**，构图由封面位决定，不再按版本锁定
- `SKILL.md` 单源化：标题细则只留 spec，冲突以 spec 为准
- 素材卡片前置为可见核对项；超出能力边界给一句说明 + 至多两个转化选项
- `使用手册` 同步 3×4 结构、编号门禁与版4 定义

### 修复
- `drama_cover.py title` 帮助文案与参数：不再强制 `--synopsis`，保留兼容
- 台账 T 编号与 12 条矩阵对齐；版4 绝对化用语按 F10 改写

## [v1.0.0] - 2026-10-25

首个稳定版本。

### 核心链路
- pipeline 全流程：视频注入（画布自适应横竖屏）→ demucs 人声分离（GPU，独立音轨方案 A）→ ASR（剪映云→Paraformer→whisper 降级链）→ 翻译（GLM→讯飞→Ollama 降级链）→ 双语字幕轨 → 智能封面 → 自动导出
- 批量：`batch_runner.py`（串流/逐集、区间选集、断点续跑、--cut 裁剪、--presets）、`series_runner.py` 多剧编排
- 草稿工程：模板复制生成、暂存区隔离发布、merge_drafts 段级合并、verify_draft 终检修复、prune_workdirs 引用感知清理

### 工程化
- `install_deps.ps1` 环境自适应一键安装（GPU 驱动→torch 通道、显存/内存→whisper/ollama 档位、ModelScope 国内 CDN）
- `_env_profile.json` 硬件画像，pipeline 运行时读取选型
- 组合预设四层解析（--presets > presets.json > pipeline_ 前缀发现 > 无），个人配置 gitignored
- 桌面通知双保险（toast + watcher 守护）
- 使用指南.md（11 类踩坑清单 + 故障速查）

### 已知约束
- 仅 Windows；导出 UI 自动化绑定剪映专业版 5.9
- 翻译免费档服务端排队延迟为主要瓶颈，批量建议低峰时段
