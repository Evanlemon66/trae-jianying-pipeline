# 贡献指南

欢迎贡献。流程是标准的 Fork + PR：

## 流程

1. **Fork** 本仓库到你自己的账号
2. 克隆你的 Fork，基于 `dev` 建分支：`git checkout -b feat/你的改动 origin/dev`
3. 提交改动并推送到你的 Fork
4. 向本仓库的 **`dev` 分支**发 Pull Request（不要直接对 main）
5. CI 通过后由仓库主审查合并；dev 定期合入 main 并打版本 tag

## 本地验证（提交前必跑）

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
# 1. Python 语法
& $py -m py_compile pipeline\pipeline.py   # 以及你改动的其它 .py
# 2. 预设解析自检（如动了预设相关逻辑）
& $py pipeline\pipeline.py --list-presets
# 3. 安装器画像自检（如动了 install_deps.ps1）
powershell -File pipeline\install_deps.ps1 -DetectOnly
```

## 硬性规则

- **禁止提交个人配置/密钥**：`pipeline/presets.json`（已 gitignore，CI 会做泄漏检查）
- **禁止手搓剪映草稿 JSON**：草稿相关逻辑必须从模板复制生成（见使用指南「剪映草稿的坑」）
- commit message 前缀约定：`feat:` / `fix:` / `docs:` / `refactor:` / `test:`
- 改动依赖或安装逻辑时，同步更新 `使用指南.md` 对应章节

## 子进程一律隐藏窗口（硬约束）

**任何** `subprocess.Popen / run / check_output / check_call` 启动的外部进程，默认必须隐藏窗口
（带 `CREATE_NO_WINDOW`），不得在用户桌面弹出控制台黑框。watcher 由 `pythonw.exe` 托管、本身
没有控制台，子进程一旦不带 flag，Windows 就会为每个子进程新开一个可见控制台窗口。

### 导入约定

| 目录 | 写法 |
|---|---|
| `pipeline/` | `from no_window import NO_WINDOW`，调用处 `creationflags=NO_WINDOW` |
| `lib/` | 沿用现有常量（如 `cover_watch.py` 的 `NO_WINDOW`、`export_lock.py` 的本地常量） |
| `scripts/` | `getattr(subprocess, "CREATE_NO_WINDOW", 0)`（非 Windows 下为 0，天然兼容） |

### 兜底：`no_window.install_default_hidden()`

- **生效条件**：仅 Windows，且当前进程没有可用控制台（`pythonw.exe` 启动、`sys.stdout/stderr is None`、
  或 `GetConsoleWindow()==0`）。终端里手工 `python pipeline.py` 调试时不启用，行为完全不变。
- **改写范围**：只给完全没写 `creationflags` 的调用补 `CREATE_NO_WINDOW`；调用方显式传的值
  （含 `0`、`DETACHED_PROCESS`、`CREATE_NEW_CONSOLE`）一律原样尊重。
- **幂等**：重复安装无副作用；安装失败只打一行警告，不影响主流程。
- **安装点**：`lib/cover_watch.py`、`pipeline/batch_runner.py`、`pipeline/pipeline.py` 三个入口在 import 后各调用一次。
  新增会拉起子进程的独立入口时，同样应在入口处安装。

### 代码审查必查项

审查 PR 时把「新增/修改的子进程调用是否隐藏窗口」列为必查项：漏一个就可能在无人值守时反复弹黑框。

> 历史背景：曾因 `media_normalizer`（每集转码）、`local_materials`（建草稿时逐素材 ffprobe）、
> `tasklist` 等调用未带 flag，执行期间每 1–3 秒弹一次黑框，并把 watcher 拖到 900 秒心跳超时被杀。

## PR 描述写清楚三件事

改了什么 / 为什么改 / 怎么验证的（贴命令或输出）。
