---
name: task-notifier
description: 剪映 pipeline 任务的桌面通知模块与 Watcher 守护进程。批量草稿生成、导出、集间转场等长任务的完成/失败/进度事件，通过 Windows 现代 Toast API 实时推送到用户桌面，不依赖 Agent 轮次。同时提供 watcher 守护进程，脚本崩溃也能通知。触发词：通知我、监控任务、导出完告诉我、跑的时候起 watcher。
---

# Task Notifier

> 本 Skill 是 **"后台长任务通知"** 的统一入口。核心代码已在 `scripts/utils/notifier.py` 和 `scripts/utils/watcher.py`，本文件只描述 **Agent 如何路由到此 Skill、如何正确调用、以及与 batch_runner / robust_auto_export 的联合调用 SOP**。
>
> ⚠️ **Agent 侧行为约定**（如"收到 task_notification 后第一条输出必须是短通知"）**不在此维护**，已写入 project_memory Hard Constraints，由 Agent 自动继承。

---

## 🎯 Agent 路由触发词

当用户说以下内容时，**优先路由到此 Skill**：

| 用户意图 | 示例措辞 |
|---|---|
| 后台任务通知 | "跑的时候起 watcher" / "通知我" / "导出完告诉我一声" |
| 监控脚本状态 | "帮我盯着这个任务" / "脚本挂了也弹通知" |
| 批量 + 通知 | "批量跑一集加 watcher" / "--watch 跑整季" |
| 测试通知链路 | "测一下通知功能" / "再来一次 toast" |

## 📁 文件位置

```
scripts/utils/notifier.py          ← 通知库（toast()）
scripts/utils/watcher.py           ← Watcher 守护进程
.trae/skills/task-notifier/SKILL.md  ← 本文件
```

## 🛠️ 核心 API

### 代码内调用（notifier）

```python
from utils.notifier import toast
toast("任务完成", "剪映草稿已就绪", icon="success")
toast("导出失败", "超时 1200s", icon="error")
```

| icon 值 | emoji | 语义 |
|---|---|---|
| `info` / `success` / `done` | ✅ | 正常完成 |
| `warning` | ⚠️ | 非致命问题（可重试） |
| `error` / `fail` | ❌ | 致命失败（需人工介入） |

toast() 自动在标题前加 emoji，主流程立即返回（<50ms），**不阻塞、不抛异常**。

### Watcher 守护进程

```bash
# 常驻监控（后台）：batch_runner 启动后立即起一个 watcher
python scripts/utils/watcher.py --dir <workdir> --interval 3

# 单次检查（供 Agent 定时调用）：
python scripts/utils/watcher.py --dir <workdir> --once
```

| 参数 | 说明 | 默认 |
|---|---|---|
| `--dir` | **必填**，batch_runner 的 `--workdir` 路径 | — |
| `--interval` | 轮询间隔秒数 | 3.0 |
| `--once` | 只轮询一次就退出（Agent 定时调用用） | False |
| `--heartbeat` | 心跳通知间隔秒数（0=关闭，常驻模式才生效） | 60.0 |
| `--pid` | 主进程 PID：死亡立即告警；终态时其存活则静默退出（去重） | 0 |

**Watcher 行为规则**：
- 启动时读 `_batch_state.json` 快照，**初始 done 集不触发事件**（避免历史残留误报）
- 只报增量：`running → done/failed/skipped` 才弹通知
- **事件限速聚合**：同轮询 2s 窗口内多个状态事件合并为一条 toast（30 集连跑 → 1 条）
- **日志正则 + 误匹配排除**：`\b(error\|traceback\|exception\|fail(ed)?\|export_failed)\b` + 排除词表（error_rate / no error / error handling）
- **日志去重**：同一 log + 同一关键字 10s 内不重复弹
- **60s 心跳**：长时间无新事件时弹进度（`已完成 5/78 集, 运行中 73 集`）
- **主进程死亡检测**（`--pid` 时）：主进程中途退出立即弹 👁️ 主进程已退出
- **终态去重**：全部终态时主进程（--pid）仍存活 → watcher **静默退出**（收尾 🎬 由主进程发）；主进程已死 → watcher 弹 👁️ 全部终态 兜底
- **自愈**：单轮异常（文件锁/JSON 读一半）不退出，连续 5 轮异常才告警退出；未捕获异常前弹临终 toast
- 所有集进入终态后弹汇总通知并 **自动退出**
- 脚本崩溃也能通知（因为 watcher 是独立进程）

**互监机制（2026-10-25）**：batch_runner 默认 `--watch`（`--no-watch` 关闭），启动时拉起 watcher 并传入自身 PID；主循环每集开跑前反查 watcher 存活、死了立即重启——watcher 盯主进程、主进程盯 watcher，双向互保。series_runner 通过调用 batch_runner 自动获得同款守护。

## 🎬 Toast 标题前缀规范

| 前缀 | 来源 | 示例 |
|---|---|---|
| 🎬 | batch_runner 业务结果 | "🎬 串流草稿已就绪 · notify_v2 第02集（1 集）" |
| 🎬 | batch_runner 批量模式 | "🎬 批量制作完成 · 成功 78 集, 失败 2 集" |
| 📤 | robust_auto_export 导出 | "📤 导出进行中 / 📤 导出成功"（失败只写 progress json，不弹 toast） |
| 👁️ | watcher 状态变化（聚合） | "👁️ 状态: done · 第1-10集 → done (10)" |
| 👁️ | watcher 心跳进度 | "👁️ 进度（仍在跑）· 已完成 5/78 集" |
| 👁️ | watcher 汇总 | "👁️ 全部终态 · done=78 \| failed=0" |
| 👁️ | watcher 日志告警 | "👁️ 日志告警 · Export 10月21日" |

---

## 🚀 联合调用 SOP

### SOP 1：批量制作 + watcher（最常用）

```bash
python pipeline/batch_runner.py "D:\短剧\目录" --watch --only 20..30
```

`--watch` 自动后台起 watcher，无需手动再开。完成后弹：
1. 👁️ 心跳（60s 弹一次进度："已完成 5/78 集, 运行中 73 集"）
2. 👁️ 第 1-10 集 → done (10)（**2s 聚合**，30 集连跑不弹 30 条）
3. 🎬 串流草稿已就绪（batch_runner return）
4. 👁️ 全部终态（watcher 汇总后自动退出）

### SOP 2：单独跑 watcher

Agent 收到"帮我监控这个 workdir"请求：

```bash
python scripts/utils/watcher.py --dir <workdir> --interval 3
```

用户说"再查一次"时用 `--once`。

### SOP 3：robust_auto_export（已有 toast，无需 watcher）

```bash
python .trae/skills/jianying-editor/examples/robust_auto_export.py "草稿名" "output.mp4" --move
```

robust_auto_export 在导出中/成功时弹 toast；失败不弹（原因写进 `<剧名>.progress.json` 的 `errors`），导出场景不需要 watcher。
`scripts/auto_exporter.py` 是 98 行薄壳（仅调 `pyJianYingDraft.export_draft`），**没有 toast**，不要用它做通知。

---

## 📡 通知渠道

| 渠道 | 实现 | 状态 | 说明 |
|---|---|---|---|
| 🖥️ Windows 现代 Toast | `plyer` → `win32_helpers` → WinRT `Notifications` API | ✅ 主力 | 已验证 Win11 弹出。旧 `NotifyIcon.ShowBalloonTip` 在 Win11 静默废弃 |
| 📥 Windows 降级 | `Popen(powershell)` + `NotifyIcon` balloon | 🧩 备用 | Win7/8 或无 plyer 时才走 |
| 🍎 macOS | `osascript display notification` | 🧩 已实现未测试 | |
| 🐧 Linux | `notify-send` | 🧩 已实现未测试 | |
| 💬 Agent 对话内 | `<task_notification>` 系统回调 | ✅ 平台层 | 触发条件：后台任务退出（exit code 0/非0）。**不是脚本可控**——取决于 Trae 后台任务机制 |
| 📮 飞书/企业微信 | — | 🔮 预留 | 可作为 `toast()` 新增渠道，当前未实现 |
| 📧 邮件/webhook | — | 🔮 预留 | 同上 |

---

## 🔗 依赖链

```
plyer (pip install plyer)
  ↓ 导入时
notifier.py (优先 plyer，无 plyer → 降级 NotifyIcon)
  ↑ 被调用
  ├── batch_runner.py (串流/批量完成点)
  ├── robust_auto_export.py (导出进行中/成功)
  ├── watcher.py (状态变化/汇总)
  └── 其他未来接入脚本
```

**pip 安装命令**：
```bash
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m pip install plyer
```

---

## ⚠️ 已知限制 & Gap

| # | 限制 | 影响 | 计划 |
|---|---|---|---|
| 1 | **pipeline.py 单独直调无 toast** | 不经 batch_runner 直接跑单集时静默完成 | 低优先（当前都走 batch_runner） |
| ~~2~~ | ~~**无进度通知**~~ | ~~watcher 只在集结束时弹~~ | ✅ **已解决**：60s 心跳进度 |
| 3 | **对话内通知不可控** | 取决于 Trae task_notification 回调时序 + Agent turn 合并 | 平台层限制，脚本侧无法改 |
| 4 | **无渠道配置文件** | 所有脚本硬编码走桌面 toast | 可加 `toast_channels.json` 或环境变量 |
| 5 | **无失败重试自动通知** | 批量任务失败时只弹一次 done/failed | 低优先（已失败汇总里能看到） |
| 6 | **双拷贝维护** | `notifier.py` + `watcher.py` 在项目 scripts/ 和 skill 目录各一份 | 已接受（两套 env_setup + namespace package 冲突无法干净做 shim） |

---

## ✅ 已验证的集成点

| 脚本 | 调用方式 | 触发场景 | 验证 |
|---|---|---|---|
| [batch_runner.py](file:///d:\剪映5.9\剪映5.9版+jianying-skill\国内用户下载这个（trae+skill）\trae-jianying-skill\pipeline\batch_runner.py#L200-L201) | `_toast("🎬 串流草稿已就绪", ...)` | 单草稿串流模式完成 | ✅ 多次验证 |
| [batch_runner.py](file:///d:\剪映5.9\剪映5.9版+jianying-skill\国内用户下载这个（trae+skill）\trae-jianying-skill\pipeline\batch_runner.py#L273) | `_toast("🎬 批量制作完成", ...)` | 多草稿批量模式全部结束 | ✅ |
| [batch_runner.py](file:///d:\剪映5.9\剪映5.9版+jianying-skill\国内用户下载这个（trae+skill）\trae-jianying-skill\pipeline\batch_runner.py#L109-L126) | `--watch` 参数 | 自动后台启动 watcher 守护进程 | ✅ |
| [robust_auto_export.py（skill 版）](file:///d:\剪映5.9\剪映5.9版+jianying-skill\国内用户下载这个（trae+skill）\trae-jianying-skill\.trae\skills\jianying-editor\examples\robust_auto_export.py#L759) | `_toast_safe("📤 导出进行中", ...)` / `_toast_safe("📤 导出成功", ...)` | 导出中每 5 分钟一次进度、成功一次 | ✅ |
| [auto_exporter.py（项目版）](file:///d:\剪映5.9\剪映5.9版+jianying-skill\国内用户下载这个（trae+skill）\trae-jianying-skill\scripts\auto_exporter.py#L121-L124) | `toast("📤 导出成功/失败", ...)` | 项目根 scripts 下的独立副本（skill 版已回退为无 toast 薄壳） | ✅ |
| [watcher.py](file:///d:\剪映5.9\剪映5.9版+jianying-skill\国内用户下载这个（trae+skill）\trae-jianying-skill\scripts\utils\watcher.py#L155-L168) | `toast("👁️ 状态/全部终态", ...)` | 集结束 + 全部终态自动退出 | ✅ 生命周期测试通过 |
