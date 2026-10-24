# Changelog

本项目遵循语义化版本（SemVer）。每个 tag 为稳定快照，使用者建议 `git clone --branch v1.0.0` 取版本而非滚动 main。

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
