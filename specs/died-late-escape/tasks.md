# tasks — died_late 止血层（第一层）

> 状态：全部完成（2026-11-14），整仓 269 passed。第二层暂缓。

前置：spec 见 [spec.md](spec.md)

## T1 进程存活辅助与等待循环抽取
- [x] `robust_auto_export.py` 新增 `_proc_alive(exporter)`（异常按 True）。
- [x] 新增 `_wait_edit_page(...)`：睡眠 1s → 存活检查 → UIA 轮询；进程消失抛
  `[launch_failed:died_during_open alive=Ns attempt=0 env_check=0]`；墙钟耗尽抛原“打开草稿超时”。
- [x] `Exporter.open_draft` 改为调用该函数；找卡片循环内也加存活检查。

## T2 connect 防双开（第一道）
- [x] `Exporter.connect(retry=True)` 在 launch 前判 `manager.backend.process_running()`：
  存活则 `_rebind_from_manager(required=False)`，窗口存在直接返回；否则继续走 launch
  （launch 内只等待不 spawn）。

## T3 manager 防双开唯一收口
- [x] `jy_instance.JyInstance.launch` 开头：`process_running()` 为 True 时只 probe/wait home，
  绝不 spawn；超时或等待期间进程退出返回 False。
- [x] 确认 `restart()` 的 kill→launch 语义不变（kill 后 process_running=False）。

## T4 单测
- [x] 新建 `tests/test_died_late_escape.py`（7 项）：
  1. 存活序列 [True, True, False] → 快速抛 `[launch_failed:died_during_open`。
  2. 恒存活 + 恒未就绪 → 抛“打开草稿超时”。
  3. alive 探测抛异常 → 视为存活继续等。
  4. launch 防双开：process_running=True + home probe=True → 返回 True，spawn 0 次。
  5. process_running=True + probe 恒 False → 不 spawn，返回 False。
  6. restart：kill 后 process_running=False → 正常 spawn。
  7. `_proc_alive` 真/异常/假三种返回。

## T5 回归
- [x] `python -m pytest tests -q` → 269 passed, 1 warning。
- [x] `tests/test_jy_instance.py` ⑬ 同步改为“旧进程杀不掉时只重连不 spawn”。
- [x] 未启停剪映/watcher，未跑导出。

## 完成定义
- 代码改动仅限：`robust_auto_export.py`、`jy_instance.py`、新增测试文件、`test_jy_instance.py` ⑬ 预期。
- `died_late` 场景的等待时间从“墙钟 ~210s”降为“进程退出后 ≤2s 判定失败”。
- 防双开：任何路径下存活实例存在时 spawn 次数为 0。
