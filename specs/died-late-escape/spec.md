# died_late 止血层（第一层）：进程退出监听 + 防双开

> 状态：已落地（2026-11-14），整仓测试 269 passed。第二层（失败口径拆 `died_early` /
> `died_before_ready`）按已定决定暂缓，留待观察。

## 1. 背景与现状（代码事实）

- `lib/jianying_launch.py` 的 `ensure_jianying` 已区分 `died` / `died_late` / `died_after_ready`
  （L85、L665、L701、L877）。`died_late`（拉起成功后活约 118s 自退）不在“早退 ≤3s”的
  重试口径里，`jy_instance.JyInstance.launch` 直接返回 False（`jy_instance.py` L313-344）。
- `robust_auto_export.py` 的 `connect(retry=True)` 在 launch 失败时把 died/timeout 打成
  `[launch_failed:<outcome> alive=… attempt=… env_check=…]` 抛出（L676-685）。
  `lib/failure_class.py` L92 据此归类为 `launch_failed`，`lib/cover_watch.py` L2362+ 走
  跨轮重拉。
- 问题点：`Exporter.open_draft` 的“等编辑页”循环只按墙钟 `editor_s` 轮询 UIA，
  **期间不判剪映进程是否还活着**。剪映在 118s 自退后，这里仍会空等到墙钟（约 210s）
  耗尽才报“打开草稿超时”，然后才由 watcher 跨轮重拉，浪费一整轮等待。

## 2. 目标（本轮只做第一层）

1. **进程退出即失败**：等待期间监听剪映进程；进程消失立即抛
   `[launch_failed:died_during_open …]`，交给现有 `failure_class` → watcher 跨轮重拉。
2. **必须防双开**：任何重拉动作之前先枚举进程；已有 `JianyingPro` 存活时只做重连/重绑，
   绝不 spawn 第二个实例。
3. **不改**失败分类口径（`died_early` / `died_before_ready` 留待观察），**不改**重试上限策略。

## 3. 实际实现

### 3.1 `robust_auto_export.py`

- 新增 `_proc_alive(exporter)`（探测异常按 True，避免 psutil 抖动误杀）。
- 新增 `_wait_edit_page(exporter, editor_s, *, alive_fn, now_fn, sleep_fn, poll_fn)`：
  睡眠 1s → 存活检查 → UIA 轮询；进程消失抛
  `[launch_failed:died_during_open alive=Ns attempt=0 env_check=0] …`；
  墙钟耗尽仍抛原“打开草稿超时”。
- `Exporter.open_draft` 改为调用该函数；找卡片循环内也加存活检查。
- `connect(retry=True)` 在 launch 前判 `manager.backend.process_running()`：
  存活则 `_rebind_from_manager(required=False)`，窗口存在直接返回；否则继续走 launch
  （launch 内只等待/重绑，不 spawn）。

### 3.2 `jy_instance.py`

- `launch()` 开头做防双开唯一收口：`process_running()` 为 True 时只 probe/wait home，
  绝不调用 `ensure_jianying`/spawn；等待期间进程退出即返回 False。
- `restart()` 先 `kill()` 再 `launch()`，kill 成功后 `process_running()` 为 False，
  原“kill → spawn”语义不变。

### 3.3 不修改

- `lib/jianying_launch.py` 的早退/迟到分类与重试预算；
- `lib/cover_watch.py` 的跨轮重拉轮次与退避；
- 导出进度/通知逻辑。

## 4. 测试与验收（已完成）

新增 `tests/test_died_late_escape.py`（7 项）：

1. 存活序列 `[True, True, False]` → 快速抛 `[launch_failed:died_during_open`，消耗墙钟 < editor_s。
2. 恒存活 + 恒未就绪 → 抛“打开草稿超时”。
3. alive 探测抛异常 → 视为存活继续等。
4. launch 防双开：process_running=True + home probe=True → 返回 True，spawn 0 次。
5. process_running=True + probe 恒 False → 不 spawn，返回 False。
6. restart：kill 后 process_running=False → 正常 spawn。
7. `_proc_alive` 真/异常/假三种返回。

同步修正 `tests/test_jy_instance.py` ⑬：旧进程杀不掉时不再双开，改为“只重连不 spawn、
返回 False、launch_count==0”。

验收：`python -m pytest tests -q` → **269 passed, 1 warning**。

约束遵守：未启停剪映、未启停 watcher、未跑导出；未动 B 草稿与 B 导出产物。

## 5. 非目标（暂缓）

- 不把 `died_late` 纳入早退重试预算（第二层，留待观察）。
- 不改 `failure_class` 关键词集合与 `relaunch_max_rounds`。
