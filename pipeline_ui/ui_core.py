# -*- coding: utf-8 -*-
"""
剪映 UI 自动化核心工具库
=========================
封装 uiautomation / pyautogui，提供高层 API：
  - connect_jianying()   连接/唤醒剪映窗口
  - click_desc()         按 FullDescription 子串定位并点击（主武器）
  - click_ratio()        在指定矩形内按比例坐标点击（面板内开关兜底）
  - send_keys()          向窗口发送按键
  - wait_for_desc()      轮询等待某 Description 出现/消失
  - dismiss_dialogs()    清理干扰弹窗
  - get_panel_rect()     取右侧面板整体矩形（用于比例定位）
  - find_all_desc()      查找所有匹配 Description 的节点

所有坐标均以"比例坐标 × 窗口矩形"计算，自适应 DPI/分辨率/窗口大小。
"""

from __future__ import annotations

import os
import sys
import time
import ctypes
import json
import traceback
from typing import Callable, Optional, TypeVar
from functools import wraps
from contextlib import contextmanager

import uiautomation as uia

PROPERTY_ID_FULLDESC = 30159   # LegacyIAccessible.Description（剪映语义标记都在这）


# ================= e2e P0 框架：Deadline + poll_until + retryable =================

T = TypeVar('T')


class Deadline:
    """不可变 deadline 对象，支持 .min() 取子 budget（e2e AttemptBudget 简化版）。

    用法:
        d = Deadline(300)                    # 300s 总 budget
        sub = d.min(60)                      # 子 budget，不超过父 deadline
        sub2 = d.min(120)                    # 另一个子 budget
        poll_until(cond, deadline=sub)       # 用子 deadline 轮询
        if sub.expired(): ...                # 检查是否过期
        remaining = sub.remaining()           # 剩余秒数
    """
    __slots__ = ('_deadline',)

    def __init__(self, seconds: float):
        self._deadline = time.time() + max(0.0, seconds)

    @classmethod
    def min(cls, *deadlines: 'Deadline | float | None') -> 'Deadline':
        """从多个候选中取最早过期的那个；None/<=0 跳过。"""
        candidates = []
        for d in deadlines:
            if d is None:
                continue
            if isinstance(d, Deadline):
                candidates.append(d._deadline)
            else:
                candidates.append(time.time() + max(0.0, d))
        if not candidates:
            return cls(0)
        new = cls(0)
        new._deadline = min(candidates)
        return new

    @classmethod
    def never(cls) -> 'Deadline':
        """永不超时的 deadline（用于纯操作间隔场景）。"""
        new = cls(0)
        new._deadline = time.time() + 999_999
        return new

    def expired(self) -> bool:
        return time.time() >= self._deadline

    def remaining(self) -> float:
        return max(0.0, self._deadline - time.time())

    def __repr__(self) -> str:
        return f'Deadline(remaining={self.remaining():.1f}s)'


POLL_INTERVAL = 0.1        # 正向轮询 tick（秒）——剪映操作响应速度实测 ~50-100ms
NEGATION_GRACE = 1.0       # 反向轮询 grace（秒）——确认"确实没了"需要持续 hold


def poll_until(condition: Callable[[], bool | T],
               timeout_s: float | Deadline = 30.0,
               interval_s: float = POLL_INTERVAL,
               description: str = '',
               on_timeout: str | None = None) -> T | None:
    """正向轮询：直到 condition() 返回 truthy 或 deadline 过期。
    返回 condition() 的首次 truthy 返回值；None 表示超时。

    用法:
        win = poll_until(lambda: _find_jianying_native(), timeout_s=60,
                         description='剪映窗口出现')
        if win is None:
            raise RuntimeError('剪映启动超时')
    """
    dl = timeout_s if isinstance(timeout_s, Deadline) else Deadline(timeout_s)
    label = description or getattr(condition, '__name__', '<cond>')
    while not dl.expired():
        try:
            result = condition()
        except Exception as e:
            # retryable 异常在 retryable 装饰器层面处理，这里只打印降级
            # 让它继续 poll——UIA 树瞬态刷新导致的单次 null 不该打断整个轮询
            dprint(f"  [poll] {label}: 条件异常（继续轮询）: {str(e)[:80]}")
            time.sleep(interval_s)
            continue
        if result:
            return result
        remaining = dl.remaining()
        if remaining <= interval_s:
            break
        time.sleep(interval_s)
    if description:
        dprint(f"  ⏰ poll_until 超时: {label}（timeout={dl._deadline - (time.time() + dl.remaining()):.1f}s, "
               f"on_timeout={on_timeout or '无'}）")
    return None


def wait_until_absent(condition: Callable[[], bool],
                      timeout_s: float | Deadline = 30.0,
                      grace_s: float = NEGATION_GRACE,
                      description: str = '') -> bool:
    """反向轮询：condition() 连续 grace_s 返回 False 才算"确实没了"。
    返回 True = 成功消失；False = 超时。

    grace 的意义：剪映处理中状态文字可能瞬间闪过又回来，
    只看一次 False 会误判完成。持续 hold grace 时间才算真的消失。

    用法:
        ok = wait_until_absent(lambda: current_banner() == '人声分离处理中',
                                timeout_s=120, grace_s=1.0,
                                description='人声分离处理中 文字消失')
    """
    dl = timeout_s if isinstance(timeout_s, Deadline) else Deadline(timeout_s)
    holding_since: float | None = None
    label = description or getattr(condition, '__name__', '<absent>')
    while not dl.expired():
        try:
            result = condition()
        except Exception:
            result = True  # 异常保守处理：认为"还在"，继续等
        if not result:
            if holding_since is None:
                holding_since = time.time()
            elif time.time() - holding_since >= min(grace_s, dl.remaining()):
                return True
        else:
            holding_since = None
        remaining = dl.remaining()
        if remaining <= POLL_INTERVAL:
            break
        time.sleep(POLL_INTERVAL)
    if description:
        dprint(f"  ⏰ wait_until_absent 超时: {label}")
    return False


# ================= retryable 错误处理（e2e NODE_STALE 简化版） =================

class JianyingError(Exception):
    """剪映自动化错误基类：code（机器可读分类）+ retryable（是否可自动重试）。

    e2e 原则：错误分类比错误消息重要——上层用 code 精准处理，不解析字符串。
    """
    code: str = 'JY_UNKNOWN'
    retryable: bool = False

    def __init__(self, msg: str, cause: Exception | None = None, code: str = ''):
        super().__init__(msg)
        if code:
            self.code = code
        self.cause = cause

    def to_dict(self) -> dict:
        return {'code': self.code, 'retryable': self.retryable,
                'message': str(self), 'cause': repr(self.cause) if self.cause else None}


class StaleWindowError(JianyingError):
    """窗口瞬态不可用（UIA 树刷新 / OCR 截图失败 / 窗口重绘中）——可重试"""
    code = 'NODE_STALE'
    retryable = True


class LocatorError(JianyingError):
    """定位失败（OCR 找不到 / UIA desc 不匹配）——不可重试（策略本身错了）"""
    code = 'LOCATOR_FAILED'
    retryable = False


class WindowNotFoundError(JianyingError):
    """剪映窗口找不到（未启动/已退出）——不可重试，需冷启动"""
    code = 'WINDOW_NOT_FOUND'
    retryable = False


class ControlNotFoundError(JianyingError):
    """控件找不到（树已加载但目标可能被遮挡/未渲染）——可重试（重扫或滚动）"""
    code = 'CONTROL_NOT_FOUND'
    retryable = True


class DraftOpenError(JianyingError):
    """草稿打不开（卡片不存在/编辑页未进入）——可重试（刷新列表后重试）"""
    code = 'DRAFT_OPEN_FAILED'
    retryable = True


class VocalSeparationError(JianyingError):
    """人声分离失败（开关未生效/处理超时）——不可重试（需人工介入）"""
    code = 'VOCAL_SEPARATION_FAILED'
    retryable = False


class RecognizeError(JianyingError):
    """识别字幕/翻译失败（只出中文轨/英文 0 条）——不可重试"""
    code = 'RECOGNIZE_FAILED'
    retryable = False


class ExportFailedError(JianyingError):
    """导出失败（子进程退出码非 0 / 成品缺失）——可重试"""
    code = 'EXPORT_FAILED'
    retryable = True


class VerifyFailedError(JianyingError):
    """终检失败（轨道数/隐藏状态与预期不符）——不可重试"""
    code = 'VERIFY_FAILED'
    retryable = False


class CalibLayoutMismatchError(JianyingError):
    """布局自检失败（实测时间线与 calibration final_layout.tracks 不符）——不可重试"""
    code = 'CALIB_LAYOUT_MISMATCH'
    retryable = False


# code → 类别名（日志/报告里用人类可读分类，不解析消息）
ERROR_CATEGORY = {
    'NODE_STALE': '瞬态',
    'LOCATOR_FAILED': '定位',
    'WINDOW_NOT_FOUND': '窗口',
    'CONTROL_NOT_FOUND': '控件',
    'DRAFT_OPEN_FAILED': '草稿',
    'VOCAL_SEPARATION_FAILED': '人声分离',
    'RECOGNIZE_FAILED': '识别',
    'EXPORT_FAILED': '导出',
    'VERIFY_FAILED': '终检',
    'CALIB_LAYOUT_MISMATCH': '布局校准',
    'JY_UNKNOWN': '未知',
}


def describe_error(err: BaseException) -> str:
    """错误 → 一行可读描述（code/类别 + 是否可重试），统一日志格式。"""
    if isinstance(err, JianyingError):
        return (f"[{err.code}/{ERROR_CATEGORY.get(err.code, '未知')}] "
                f"{'可重试' if err.retryable else '不可重试'}: {err}")
    return f"[{type(err).__name__}] {err}"


def retryable(max_tries: int = 3,
              retry_delay: float = 0.2,
              retry_on: tuple[type, ...] = (StaleWindowError,),
              re_raise: bool = True):
    """装饰器：对 retryable 错误自动重试 N 次。

    只在异常类型匹配 retry_on 时重试；非 retryable 错误立即 re-raise。
    和 poll_until 配合使用：poll_until 负责 deadline 管理，retryable 负责单次调用的容错。

    用法:
        @retryable(max_tries=3)
        def _ocr_find_text(win, text):
            result = ocr.screenshot_and_find(win, text)
            if result is None:
                raise StaleWindowError(f'OCR 返回空: {text}')
            return result
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, max_tries + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    if not isinstance(e, retry_on):
                        raise  # 非 retryable 错误直接抛出
                    last_err = e
                    if attempt < max_tries:
                        dprint(f"  [retry] {fn.__name__} 第 {attempt}/{max_tries} 次"
                               f" 因 {type(e).__name__} 重试: {str(e)[:60]}")
                        time.sleep(retry_delay)
                    else:
                        if re_raise:
                            raise
                        return None
            return None
        return wrapper
    return decorator


# ================= PipelineBudget 层级预算（e2e AttemptBudget 简化版）=================

class PipelineBudget:
    """层级超时预算：pipeline 总预算 → step 子预算 → 动作子预算。

    核心规则（e2e AttemptBudget 语义）：
      - 任何子预算都比父预算早或相同过期（Deadline.min 自动取早的那个）
      - teardown 有**独立预算**：即使主体已超时，清理仍能跑自己的 N 秒
      - 任一子预算过期 → 后续步骤 expired() 为 True，可提前跳过而不是继续硬跑

    用法:
        budget = PipelineBudget(total_s=3600, teardown_s=10)
        for step in steps:
            with budget.capped(300) as dl:      # 该 step 最多 300s，且不超过总预算
                run_step(deadline=dl)
        # 即使上面超时，这里仍可用 teardown_deadline()
        with budget.teardown() as dl:
            cleanup(deadline=dl)
    """

    def __init__(self, total_s: float, teardown_s: float = 10.0):
        self._root = Deadline(total_s)
        self._teardown_s = teardown_s
        self._current = self._root
        self._t0 = time.time()

    # ---------- 查询 ----------
    def expired(self) -> bool:
        return self._current.expired()

    def remaining(self) -> float:
        return self._current.remaining()

    def elapsed(self) -> float:
        return time.time() - self._t0

    def step_deadline(self, max_s: float) -> Deadline:
        """取当前 step 的 deadline（不改变当前层）。"""
        return Deadline.min(self._current, max_s)

    # ---------- 作用域 ----------
    @contextmanager
    def capped(self, max_s: float):
        """进入子预算作用域（with 块内 budget.remaining() 反映子预算）。"""
        parent = self._current
        self._current = Deadline.min(parent, max_s)
        try:
            yield self._current
        finally:
            self._current = parent   # 退出恢复父预算（teardown 不受子预算影响）

    @contextmanager
    def teardown(self):
        """teardown 独立预算：不受主体超时影响，仍有 teardown_s 秒清理。"""
        parent = self._current
        self._current = Deadline(self._teardown_s)
        try:
            yield self._current
        finally:
            self._current = parent

    def __repr__(self) -> str:
        return f'PipelineBudget(elapsed={self.elapsed():.1f}s remaining={self.remaining():.1f}s)'


# ================= DPI 感知 =================
def setup_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return True
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return True
        except Exception:
            return False


setup_dpi()


# ================= 日志 =================
_DPRINT_T0 = None   # 设为 time.time() 时，dprint 每行加 [+耗时s] 前缀


def dprint(msg):
    if _DPRINT_T0 is not None:
        print(f"[*] [+{time.time() - _DPRINT_T0:6.2f}s] {msg}", flush=True)
    else:
        print(f"[*] {msg}", flush=True)


# 运行期可热设（准入/排障时 os.environ['DEBUG']='1'）
_DEBUG = os.environ.get('DEBUG') == '1'


def ddebug(msg):
    """仅 DEBUG=1 时输出的详细调试日志。

    用于高频、对正常跑流程无价值的噪声（窗口句柄复用、面板边缘计算等）。
    默认静默，排障时 `set DEBUG=1` 再跑即可看到。
    """
    if _DEBUG:
        dprint(msg)


class _StampStdout:
    """给每行 stdout 加 [HH:MM:SS.mmm] 墙钟前缀（已带时间戳的行不重复加）。"""

    def __init__(self, orig):
        self._orig = orig
        self._buf = ''

    def write(self, s):
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            self._emit(line)
        return len(s)

    def _emit(self, line):
        import re
        now = time.time()
        wall = time.strftime('%H:%M:%S', time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
        if line.strip() and not re.match(r'^\[\d{2}:\d{2}:\d{2}\.\d{3}\]', line):
            self._orig.write(f"[{wall}] {line}\n")
        else:
            self._orig.write(line + '\n')

    def flush(self):
        if self._buf:
            line, self._buf = self._buf, ''
            self._emit(line)
        self._orig.flush()

    def __getattr__(self, name):
        return getattr(self._orig, name)


def install_timestamp_stdout():
    """所有 print 输出统一加墙钟毫秒前缀（幂等）。在流程入口调用一次即可。"""
    import sys as _sys
    if isinstance(_sys.stdout, _StampStdout):
        return
    _sys.stdout = _StampStdout(_sys.stdout)


def enable_timing():
    """开启 dprint 计时前缀（每行显示相对流程起点的耗时）。"""
    global _DPRINT_T0
    _DPRINT_T0 = time.time()
    return _DPRINT_T0


# ================= 操作级打点（recognize/extract 等模块共用）=================
# 每个鼠标/键盘操作一条记录，结束后 op_timeline() 输出逐操作耗时表；
# 阶段分组（哪个操作属于哪个大阶段）由用户定义后映射，自动汇总每阶段耗时。
_OP_MARKS = []   # [(name, t, detail)]

# ---------- P3 trace-feed：每条操作实时落盘 JSONL（崩溃也不丢现场）----------
_OP_TRACE = {'path': None, 'f': None}


def start_op_trace(path=None):
    """开始 trace-feed：之后每次 op_mark / op_event 实时 append 到 JSONL。返回文件路径。"""
    if _OP_TRACE.get('f'):
        return _OP_TRACE['path']
    if path is None:
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        try:
            os.makedirs(logs_dir, exist_ok=True)
        except Exception:
            logs_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(logs_dir, time.strftime('op_trace_%Y%m%d_%H%M%S.jsonl'))
    try:
        _OP_TRACE['path'] = path
        _OP_TRACE['f'] = open(path, 'a', encoding='utf-8')
        dprint(f"  📝 trace-feed → {path}")
    except Exception:
        _OP_TRACE['path'] = None
        _OP_TRACE['f'] = None
    return path


def op_trace_write(event: dict):
    """写一条结构化 trace 事件（type/ts/name/status/duration_ms/...）。"""
    f = _OP_TRACE.get('f')
    if not f:
        return
    try:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')
        f.flush()
    except Exception:
        pass


def stop_op_trace():
    """关闭 trace 文件（幂等）。"""
    f = _OP_TRACE.get('f')
    if f:
        try:
            f.close()
        except Exception:
            pass
    _OP_TRACE['f'] = None


def op_event(kind, name, status='ok', duration_ms=None, **extra):
    """结构化事件：失败原因/重试次数/OCR 耗时等写入 trace-feed。

    用法:
        op_event('ocr', '定位音频Tab', status='ok', duration_ms=620, score=0.97)
        op_event('step', '导出', status='fail', duration_ms=732000, code='EXPORT_FAILED')
    """
    ev = {'type': kind, 'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
          't': round(time.time(), 3), 'name': name, 'status': status}
    if duration_ms is not None:
        ev['duration_ms'] = round(duration_ms, 1)
    ev.update(extra)
    op_trace_write(ev)


def op_mark(name, detail=''):
    """操作级打点：一次鼠标点击 / 一次按键 = 一条。独立毫秒墙钟行（全局时间戳器不重复盖）。"""
    if _OP_TRACE.get('path') is None:
        start_op_trace()   # 懒启动：任一链首次操作即开始记录
    t = time.time()
    _OP_MARKS.append((name, t, detail))
    wall = time.strftime('%H:%M:%S', time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
    print(f"[{wall}] ⚡ {name}" + (f"  {detail}" if detail else ''), flush=True)
    op_trace_write({'type': 'op', 't': round(t, 3),
                    'ts': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(t)),
                    'name': name, 'detail': detail})
    return t


def op_timeline(title='操作时间线', wall_t0=None):
    """输出操作时间线表：每操作 与上一步间隔(Δ) + 相对首个操作累计。返回打点列表。
    wall_t0：步骤起始墙钟时刻。单操作步骤「合计 0.0s」没有信息量，
    传入后额外打印步骤墙钟总耗时，避免误以为该步骤不耗时。"""
    if not _OP_MARKS:
        return []
    t0 = _OP_MARKS[0][1]
    print('-' * 64)
    print(f'⏱ {title}（{len(_OP_MARKS)} 个操作）')
    prev = t0
    for i, (name, t, detail) in enumerate(_OP_MARKS):
        print(f'  {i + 1:>2}. +{t - t0:6.1f}s (Δ{t - prev:5.1f}s) {name}'
              + (f'  {detail}' if detail else ''))
        prev = t
    print(f'  合计 {_OP_MARKS[-1][1] - t0:.1f}s（首个操作起）')
    if wall_t0:
        _wall = _OP_MARKS[-1][1] - wall_t0
        _before = t0 - wall_t0
        print(f'  步骤墙钟 {_wall:.1f}s（其中首个操作前 {_before:.1f}s 为非 UI 工作）')
    print('-' * 64)
    out = [(n, t, d) for n, t, d in _OP_MARKS]
    op_trace_write({'type': 'timeline', 'title': title,
                    'ops': [{'seq': i + 1, 'name': n,
                             't_rel': round(t - t0, 3), 'detail': d}
                            for i, (n, t, d) in enumerate(out)],
                    'total_s': round(out[-1][1] - t0, 1) if out else 0})
    return out


def take_op_marks():
    """取走并清空操作打点（供准入报告落盘 / 模块间分段）。"""
    out = list(_OP_MARKS)
    _OP_MARKS.clear()
    return out


# ================= 阶段耗时（用户定义 8 大阶段，按流水线执行顺序）=================
# 1 生成草稿（含预设+时间线裁剪） → 2 UI初始化 → 3 人声分离 → 4 识别中文字幕+翻译
# → 5 分离音频 → 6 草稿最终形态(只隐藏中文轨,其余解除隐藏) → 7 终检（UI 自动化检查） → 8 导出
_OP_STAGES = []   # [name, t_start, t_end|None]


def op_stage(name):
    """阶段边界标记（幂等：当前段同名则跳过）。admission 与 ui_pipeline 共用，
    模块入口和编排层都可打标，重复打同名标只生效第一个。"""
    if _OP_STAGES and _OP_STAGES[-1][0] == name and _OP_STAGES[-1][2] is None:
        return
    op_stage_end()
    t = time.time()
    _OP_STAGES.append([name, t, None])
    wall = time.strftime('%H:%M:%S', time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
    print(f"\n[{wall}] ⏱ ——— 阶段 {name} 开始 ———", flush=True)


def op_stage_end():
    """手动闭合当前阶段段（此后的时间不计入该段）。"""
    if _OP_STAGES and _OP_STAGES[-1][2] is None:
        _OP_STAGES[-1][2] = time.time()


def stage_timeline():
    """输出阶段耗时汇总表。返回 [(name, seconds)]。"""
    t = time.time()
    if _OP_STAGES and _OP_STAGES[-1][2] is None:
        _OP_STAGES[-1][2] = t
    rows = [(n, e - s) for n, s, e in _OP_STAGES if e and e > s]
    if rows:
        print('\n⏱ 阶段耗时汇总', flush=True)
        for n, sec in rows:
            print(f"  {n:<26} {sec:8.1f}s", flush=True)
        op_trace_write({'type': 'stages',
                        'stages': [{'name': n, 'duration_s': round(sec, 1)}
                                   for n, sec in rows]})
    stop_op_trace()   # 阶段汇总即流程收尾 → 关闭 trace 文件
    return rows


# ================= 属性读取 =================
def _resolve_prop(ctrl, prop_id):
    """安全读取 UIA 属性，失败返回 None"""
    try:
        return ctrl.GetPropertyValue(prop_id)
    except Exception:
        return None


def _ctrl_desc(ctrl):
    return (_resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or "").lower()


def _ctrl_name(ctrl):
    try:
        return (ctrl.Name or "").strip()
    except Exception:
        return ""


def _ctrl_class(ctrl):
    try:
        return ctrl.ClassName or ""
    except Exception:
        return ""


def _ctrl_rect(ctrl):
    try:
        r = ctrl.BoundingRectangle
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


# ================= 窗口连接 =================
# Win32 原生查找（不走 UIA COM 树）：UIA 枚举在本机会被卡住 2min+（编辑页加载/僵尸窗口），
# 而窗口"是否出现"用 EnumWindows 毫秒级即可判定——命中后按句柄直绑 UIA 控件，绕开枚举卡顿。
def _enum_top_windows():
    """EnumWindows 枚举可见顶层窗口 → [(hwnd, class_name, title)]。纯 Win32，毫秒级。"""
    import ctypes
    from ctypes import wintypes
    out = []
    user32 = ctypes.windll.user32
    _Proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    buf = ctypes.create_unicode_buffer(512)
    # 64 位句柄截断防御：不设 argtypes 时 hwnd 默认按 32 位 c_int 传
    user32.EnumWindows.argtypes = [_Proc, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def _cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            buf.value = ''
            user32.GetClassNameW(hwnd, buf, 512)
            cls = buf.value or ''
            buf.value = ''
            user32.GetWindowTextW(hwnd, buf, 512)
            out.append((int(hwnd), cls, buf.value or ''))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_Proc(_cb), 0)
    except Exception:
        pass
    return out


def _good_class_native(cls):
    c = (cls or '').lower()
    if c.startswith('homepage_qmltype') or c.startswith('mainwindow_qmltype'):
        return True
    if c.startswith('jianying'):
        return True
    # 实测（2026-10）：冷启动后剪映主窗口类名是 Qt622QWindowIcon（Qt 6 通用顶层类，
    # 而非历史观察的 HomePage_QMLTYPE）。title 含「剪映」前提已保证不误抓其他 Qt 应用。
    if c.endswith('qwindowicon'):
        return True
    return False


def find_jianying_native():
    """Win32 找剪映真窗口（按类名过滤假窗口）。返回 (hwnd, cls, title) 或 None。"""
    hits = enum_jianying_windows()
    return hits[0] if hits else None


def enum_jianying_windows():
    """所有剪映真窗口（首页+编辑页可能同时存在）→ [(hwnd, cls, title)]。Win32 毫秒级。"""
    try:
        return [(h, c, t) for h, c, t in _enum_top_windows()
                if '剪映' in t and _good_class_native(c)]
    except Exception:
        return []


# ================= HWND 持久化恢复（e2e cdp-recovery 模式）=================
# 第一次绑定成功后缓存 HWND；下次优先 attach 到这个句柄（省掉 Win32 枚举 + UIA 搜索）。
# 恢复前必须验证 identity（标题含剪映 + 类名合法）——连到错误的窗口比不连更糟。
_LAST_HWND = {'hwnd': 0, 'cls': '', 'title': '', 'page': None}


def hwnd_identity_ok(hwnd):
    """校验句柄仍是**可见**的剪映窗口。返回 (title, cls) 或 None。

    实测（2026-11）：打开草稿会新建顶层窗口，旧首页窗口随即销毁/隐藏；
    不可见窗口的 UIA 树为空，若误复用会一直判不出页（42s 空转）。
    """
    if not hwnd:
        return None
    try:
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return None
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return None
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cls = cls_buf.value
        if '剪映' not in title or not _good_class_native(cls):
            return None
        return title, cls
    except Exception:
        return None


def _try_reattach_cached_hwnd():
    """优先复用上次成功绑定的 HWND。
    复用条件：句柄可见 + 标题/类名合法 + 能判出页类型。
    实测（2026-11）：打开草稿会新建顶层窗口（旧首页窗口随即消失），
    只校验 IsWindow 会一直 attach 到已空的旧窗口，导致判不出页、后续全失败。"""
    hwnd = _LAST_HWND.get('hwnd') or 0
    if not hwnd:
        return None
    ident = hwnd_identity_ok(hwnd)
    if not ident:
        ddebug(f"  [hwnd] 缓存句柄 {hwnd} 已失效/不可见 → 重新查找")
        invalidate_hwnd_cache()
        return None
    title, cls = ident
    bound = _bind_uia_by_hwnd(hwnd, cls)
    if not bound or bound[0] is None:
        return None
    win, pt = bound
    if pt is None:
        # 树未建好（编辑页加载中）或旧窗口残留 → 交给原生全量扫描
        ddebug(f"  [hwnd] 缓存句柄 {hwnd} 判不出页 → 重新查找")
        invalidate_hwnd_cache()
        return None
    # 缓存的是首页窗口，但同时还有别的剪映窗口 → 编辑页可能已在新窗口打开
    if pt != 'edit' and len(enum_jianying_windows()) > 1:
        ddebug(f"  [hwnd] 存在多个剪映窗口（缓存为 {pt}）→ 重新判定")
        return None
    ddebug(f"  [hwnd] 复用缓存窗口 hwnd={hwnd} cls={cls} page={pt}")
    _LAST_HWND['page'] = pt
    return win, pt


def _bind_uia_by_hwnd(hwnd, known_cls=None, cache=True, activate=True):
    """按句柄直绑 UIA 控件（单元素绑定，不走树枚举）。
    cache=False 用于候选窗口探测（只有最终选中的窗口才写缓存）。
    activate=False 用于探测（避免反复抢焦点/置顶）。"""
    try:
        win = uia.ControlFromHandle(hwnd)
        if win is not None:
            bound = _activate_and_classify(win, known_cls, activate=activate)
            if bound and bound[0] is not None and cache:
                _LAST_HWND['hwnd'] = hwnd
                _LAST_HWND['cls'] = known_cls or ''
                _LAST_HWND['page'] = bound[1]
            return bound
    except Exception:
        pass
    return None


def invalidate_hwnd_cache():
    """剪映退出/重启/窗口重建后清空 HWND 缓存（避免 attach 到已死句柄）。"""
    _LAST_HWND['hwnd'] = 0
    _LAST_HWND['cls'] = ''
    _LAST_HWND['title'] = ''
    _LAST_HWND['page'] = None


def _select_best_native():
    """原生枚举所有剪映窗口 → 逐个判页 → edit 优先，其次 home。
    打开草稿会在**新窗口**打开编辑页，所以必须全量扫描而不能只认一个句柄。"""
    hits = enum_jianying_windows()
    if not hits:
        return None, None
    best_edit = best_home = None
    for hwnd, cls, title in hits:
        bound = _bind_uia_by_hwnd(hwnd, cls, cache=False, activate=False)
        if not bound or bound[0] is None:
            continue
        win, pt = bound
        if pt == 'edit' and best_edit is None:
            best_edit = (hwnd, cls, win, pt)
        elif pt == 'home' and best_home is None:
            best_home = (hwnd, cls, win, pt)
    chosen = best_edit or best_home
    if not chosen:
        # 所有窗口都判不出页（编辑页加载中）：先绑第一个可见窗口，page=None
        hwnd, cls, title = hits[0]
        ddebug(f"  [hwnd] {len(hits)} 个剪映窗口均判不出页（加载中）→ 绑定 hwnd={hwnd}")
        return _bind_uia_by_hwnd(hwnd, cls) or (None, None)
    hwnd, cls, win, pt = chosen
    try:
        win.SetActive()
        win.SetTopmost(True)
        time.sleep(0.2)
        win.SetTopmost(False)
    except Exception:
        pass
    _LAST_HWND['hwnd'] = hwnd
    _LAST_HWND['cls'] = cls
    _LAST_HWND['page'] = pt
    ddebug(f"  [hwnd] 选定窗口 hwnd={hwnd} cls={cls} page={pt}（候选 {len(hits)} 个）")
    return win, pt


def connect_jianying(retry=True, max_wait=60):
    """
    连接剪映窗口。返回 (window, page_type)
    page_type: 'edit' / 'home' / None

    过滤规则：只认 ClassName 以 HomePage_QMLTYPE / MainWindow_QMLTYPE / Jianying /
    Qt*QWindowIcon 开头的窗口（标题必须含「剪映」），排除 Chrome_WidgetWin_1 假窗口。
    """
    # ⓪ HWND 持久化恢复：句柄有效且能判页才复用（最快路径）
    cached = _try_reattach_cached_hwnd()
    if cached:
        return cached

    # ① Win32 原生全量扫描（毫秒级）：枚举所有剪映窗口 → 判页 → edit 优先
    win, pt = _select_best_native()
    if win is not None:
        return win, pt

    # ② 直接找（UIA 兜底，兼容 Qt6 通用顶层类）
    for hint in ["剪映专业版"]:
        try:
            win = uia.WindowControl(searchDepth=1, Name=hint)
            if win.Exists(0):
                cls = win.ClassName or ""
                if _good_class_native(cls):
                    return _activate_and_classify(win)
                # 假窗口，继续找
                dprint(f"跳过假窗口 ClassName={cls!r}")
        except Exception:
            continue

    # 兜底：遍历顶层窗口
    try:
        for w in uia.GetRootControl().GetChildren():
            try:
                if "剪映" in (w.Name or "") and _good_class_native(w.ClassName or ""):
                    return _activate_and_classify(w)
            except Exception:
                continue
    except Exception:
        pass

    if retry:
        dprint("未找到剪映窗口，尝试启动...")
        return launch_and_connect(max_wait=max_wait)
    return None, None


def launch_and_connect(exe_path=None, max_wait=60):
    import subprocess, winreg

    if not exe_path:
        # 5.9 硬编码优先
        p59 = r"D:\剪映5.9\JianyingPro\5.9.0.11632\JianyingPro.exe"
        if os.path.exists(p59):
            exe_path = p59
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r"Software\Microsoft\Windows\CurrentVersion\Uninstall") as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        sk_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sk_name) as sk:
                            try:
                                dn = winreg.QueryValueEx(sk, "DisplayName")[0]
                                if "剪映专业版" in dn:
                                    ico = winreg.QueryValueEx(sk, "DisplayIcon")[0]
                                    exe_path = os.path.join(os.path.dirname(ico), "JianyingPro.exe")
                                    break
                            except Exception:
                                continue
            except Exception:
                pass

    if not exe_path or not os.path.exists(exe_path):
        raise RuntimeError("未找到剪映可执行文件，请手动启动后再试")

    dprint(f"启动: {exe_path}")
    subprocess.Popen([exe_path], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    t_launch = time.time()
    last_log = 0.0
    last_diag = 0.0
    while time.time() - t_launch < max_wait:
        time.sleep(2)
        el = time.time() - t_launch
        # Win32 原生检测（毫秒级，不受 UIA 枚举卡顿影响）。
        # all_jy = 标题含剪映的所有可见窗口（含 Chrome_WidgetWin_1 假窗口/初始化中窗口）
        t_chk = time.time()
        try:
            all_jy = [(h, c, t) for h, c, t in _enum_top_windows() if '剪映' in t]
        except Exception:
            all_jy = []
        chk_s = time.time() - t_chk
        if chk_s > 3:
            dprint(f"  [diag] 本轮 Win32 窗口枚举耗时 {chk_s:.1f}s（异常，正常应 <0.1s）")
        if el - last_log >= 15:
            last_log = el
            if all_jy:
                cls_set = ', '.join(sorted({c for _, c, _ in all_jy}))
                dprint(f"  ...剪映启动等待中（{el:.0f}s）已检测到窗口: {cls_set}")
            else:
                dprint(f"  ...剪映启动等待中（{el:.0f}s）（尚未检测到剪映任何窗口）")
        # 真 QML 窗口（HomePage/MainWindow）出现 → 立即返回
        real = [(h, c, t) for h, c, t in all_jy if _good_class_native(c)]
        if real:
            hwnd, cls, title = real[0]
            dprint(f"  剪映窗口已出现（{el:.0f}s）cls={cls}")
            bound = _bind_uia_by_hwnd(hwnd, cls)
            if bound and bound[0] is not None:
                return bound
            # 句柄绑定失败（极少见）→ UIA 搜索兜底
            try:
                win = uia.WindowControl(searchDepth=1, Name='剪映专业版')
                if win.Exists(2):
                    return _activate_and_classify(win)
            except Exception:
                pass
        elif all_jy and el - last_diag >= 30:
            # 只有假窗口/初始化中窗口：进程已拉起，QML 主窗口还没创建（肉眼看到的
            # 任务栏图标/闪屏属于这种）→ 每 30s 提示一次，说明检测没有瞎等
            last_diag = el
            dprint(f"  [diag] 剪映进程已拉起，主窗口初始化中（{el:.0f}s）: "
                   f"{sorted({c for _, c, _ in all_jy})}")
    raise RuntimeError(f"启动后 {max_wait}s 内未找到剪映窗口")


_PAGE_FEATS = (
    ('edit', 'maintimelineroot'),    # 实测：编辑页时间线根节点，深度仅 2
    ('edit', 'vesettingpanel'),      # 右侧设置面板展开时才有（辅助特征）
    ('home', 'homepagedrafttitle'),  # 首页草稿卡片
)


def _detect_page(win):
    """按 FullDescription 特征判页（Qt 通用窗口类名判不出页时唯一依据）。
    实测（2026-11 剪映 5.9/Qt6）：首页与编辑页标题都叫「剪映专业版」，
    编辑页必有 MainTimeLineRoot（深度 2）；旧的 VESettingPanel 在未展开设置面板时不存在。
    单次遍历同时检测所有特征、命中 edit 即提前返回，避免首页重复全树扫描。"""
    found = {'edit': False, 'home': False}

    def walk(ctrl, depth):
        if depth > 10 or found['edit']:
            return
        desc = _ctrl_desc(ctrl)
        for pt, feat in _PAGE_FEATS:
            if feat in desc:
                found[pt] = True
        if found['edit']:
            return
        try:
            for c in ctrl.GetChildren():
                walk(c, depth + 1)
        except Exception:
            pass

    walk(win, 0)
    if found['edit']:
        return 'edit'
    if found['home']:
        return 'home'
    return None


# ================= S3-A2：窗口放置策略（参数化） =================
# primary（默认，向后兼容= S0 放置补丁行为）/ keep（不挪动）/
# match-calibration（未在"已有 calibration 的屏"上则挪过去）/
# target（挪到指定屏，配合 set_screen_policy 的 target：primary|secondary|<index>）
_SCREEN_POLICY = {'policy': 'primary', 'target': None}


def set_screen_policy(policy='primary', target=None):
    """设置窗口放置策略。必须在 connect_jianying 之前调用（每次窗口绑定都会校验）。"""
    _SCREEN_POLICY['policy'] = policy
    _SCREEN_POLICY['target'] = target
    ddebug(f'  [窗口放置] 策略={policy} target={target}')


def get_screen_policy():
    return dict(_SCREEN_POLICY)


def _enum_monitors():
    """枚举显示器 → [{'hmon','rect','primary','size'}]，primary 优先、再按 x 排序。"""
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    class _RECT(ctypes.Structure):
        _fields_ = [('left', wintypes.LONG), ('top', wintypes.LONG),
                    ('right', wintypes.LONG), ('bottom', wintypes.LONG)]

    class _MI(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', _RECT),
                    ('rcWork', _RECT), ('dwFlags', wintypes.DWORD)]

    out = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(_RECT), wintypes.LPARAM)

    def _cb(hmon, hdc, lprect, lp):
        mi = _MI()
        mi.cbSize = ctypes.sizeof(_MI)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            out.append({'hmon': int(hmon),
                        'rect': (r.left, r.top, r.right, r.bottom),
                        'primary': bool(mi.dwFlags & 1),
                        'size': (r.right - r.left, r.bottom - r.top)})
        return True

    cb = MONITORENUMPROC(_cb)
    user32.EnumDisplayMonitors(0, 0, cb, 0)
    out.sort(key=lambda m: (not m['primary'], m['rect'][0]))
    return out


def _resolve_target_monitor(target, monitors):
    """target: 'primary' | 'secondary' | <int index（primary-first, x 排序）>"""
    if not monitors:
        return None
    if target in (None, 'primary'):
        return next((m for m in monitors if m['primary']), monitors[0])
    if target == 'secondary':
        return next((m for m in monitors if not m['primary']), None)
    try:
        return monitors[int(target)]
    except Exception:
        return None


def _calibration_screens():
    """已有 calibration 的屏分辨率 [(w,h)]（直接读 JSON，避免 ui_core↔ui_calibration 循环依赖）。"""
    import glob
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'calibrations')
    sizes = []
    for p in glob.glob(os.path.join(d, '*.json')):
        try:
            with open(p, encoding='utf-8') as f:
                sc = (json.load(f).get('env') or {}).get('screen')
            if sc and len(sc) == 2:
                sizes.append((int(sc[0]), int(sc[1])))
        except Exception:
            continue
    return sizes


def _window_monitor(win, monitors):
    """窗口当前所在显示器（与 env_fingerprint 同口径：MONITOR_DEFAULTTONEAREST）。"""
    hwnd = win.NativeWindowHandle
    if not hwnd:
        return None
    hmon = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
    for m in monitors:
        if m['hmon'] == int(hmon):
            return m
    return None


def _move_window_to_monitor(win, mon, label):
    """整窗挪到目标显示器原点并最大化（SWP_NOSIZE|SWP_NOZORDER|SWP_SHOWWINDOW + SW_MAXIMIZE）。"""
    hwnd = win.NativeWindowHandle
    user32 = ctypes.windll.user32
    user32.SetWindowPos(hwnd, 0, mon['rect'][0], mon['rect'][1], 0, 0,
                        0x0001 | 0x0004 | 0x0040)
    user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE（pipeline 全屏基准态）
    time.sleep(0.5)
    msg = f"[窗口放置] 窗口已移至{label}({mon['rect'][0]},{mon['rect'][1]}) 并最大化"
    print(f'[*]   {msg}', flush=True)
    op_event('placement', f'窗口移至{label}',
             to=f"{mon['rect'][0]},{mon['rect'][1]}")


def _ensure_window_placement(win):
    """按 _SCREEN_POLICY 保证剪映窗口位于目标屏（calibration 绝对坐标为屏坐标系）。

    实测（2026-11）：剪映不持久化窗口位置，新窗口跟随光标所在屏；
    且打开草稿会新建顶层窗口（首页窗口销毁）——因此必须在每次窗口绑定时校验，
    而非启动后挪一次。判定口径与 ui_calibration.env_fingerprint 一致：
    MonitorFromWindow(MONITOR_DEFAULTTONEAREST) + GetMonitorInfoW。
    注意：不用 Win+Shift+←（实测三次全部无效）。"""
    try:
        policy = _SCREEN_POLICY['policy']
        if policy == 'keep':
            ddebug('  [窗口放置] 策略=keep，不挪动')
            return
        monitors = _enum_monitors()
        cur = _window_monitor(win, monitors)
        if cur is None:
            return

        if policy == 'target':
            mon = _resolve_target_monitor(_SCREEN_POLICY['target'], monitors)
            label = f"目标屏[{_SCREEN_POLICY['target']}]"
        elif policy == 'match-calibration':
            sizes = _calibration_screens()
            if cur['size'] in sizes:
                ddebug(f'  [窗口放置] 已在校准屏 {cur["size"]}')
                return
            mon = next((m for m in monitors if m['size'] in sizes), None)
            if mon is None:
                ddebug(f'  [窗口放置] 无校准屏可匹配 {sizes}，回落主屏')
                mon = next((m for m in monitors if m['primary']), None)
            label = f'校准屏{mon["size"] if mon else "?"}'
        else:  # primary（默认）
            mon = next((m for m in monitors if m['primary']), None)
            label = '主屏'

        if mon is None or mon is cur:
            ddebug(f'  [窗口放置] 已在{label}')
            return
        from_rect = cur['rect']
        _move_window_to_monitor(win, mon, label)
        ddebug(f'  [窗口放置] 从({from_rect[0]},{from_rect[1]})移出')
    except Exception as e:
        dprint(f'  [窗口放置] 校验/移动失败: {e}')   # 失败才保留 dprint


def _activate_and_classify(win, known_cls=None, activate=True):
    if activate:
        try:
            win.SetActive()
            win.SetTopmost(True)
            time.sleep(0.2)
            win.SetTopmost(False)
        except Exception:
            pass
        _ensure_window_placement(win)

    # 优先用 Win32 已测得的类名（避免 UIA 属性读取再被卡）
    cls = known_cls or (win.ClassName or "")
    if "MainWindow" in cls:
        return win, "edit"
    elif "HomePage" in cls:
        return win, "home"
    # Qt 通用类名（Qt622QWindowIcon）：desc 特征判页
    pt = _detect_page(win)
    if pt:
        return win, pt
    return win, None


def ensure_maximized(win):
    """
    确保剪映窗口最大化（比例坐标定位的前提，每次进编辑页都应调用）。
    最大化后窗口矩形变化，返回重绑后的新窗口对象。
    注意：不做跨屏移动——所有坐标均已按窗口矩形相对计算，副屏负坐标也能正常工作。
    """
    try:
        if win.IsMaximized():
            return win
    except Exception:
        pass
    try:
        win.Maximize()
        time.sleep(0.8)
    except Exception:
        try:
            import pyautogui
            win.SetActive()
            time.sleep(0.3)
            pyautogui.hotkey('win', 'up')
            time.sleep(0.8)
        except Exception:
            pass
    new_win, _ = connect_jianying(retry=False)
    return new_win if new_win else win


# ================= PopupRouter 弹窗路由（e2e DialogRouter 简化版）=================

class PopupRouter:
    """弹窗路由器：按注册顺序匹配 → 处理 → 重新扫描，直到没有可处理弹窗。

    handler = (name, match_fn, handle_fn)：
      match_fn(dlg) -> bool   该窗口是否属于这个弹窗
      handle_fn(dlg) -> bool  处理动作（True=已处理，继续扫描）
    """

    def __init__(self, handlers=None):
        self._handlers = list(handlers or [])

    def register(self, name, match, handle):
        self._handlers.append((name, match, handle))
        return self

    def _find_dialog(self, title):
        try:
            dlg = uia.WindowControl(searchDepth=1, Name=title)
            return dlg if dlg.Exists(0) else None
        except Exception:
            return None

    def handle_all(self, max_rounds=5):
        """扫描并处理所有已注册弹窗，返回已处理名称列表。"""
        handled = []
        for _ in range(max_rounds):
            hit = None
            for name, match, handle in self._handlers:
                dlg = self._find_dialog(name)
                if dlg and match(dlg):
                    hit = (name, dlg, handle)
                    break
            if not hit:
                break
            name, dlg, handle = hit
            try:
                if not handle(dlg):
                    break
                handled.append(name)
                dprint(f"  弹窗已处理: {name}")
                time.sleep(0.3)
            except Exception as e:
                dprint(f"  弹窗处理失败 {name}: {e}")
                break
        return handled


def _make_button_handler(text):
    """生成「点某个按钮，找不到就 Esc」的 handler。"""
    def _handle(dlg):
        for finder in (lambda: dlg.ButtonControl(searchDepth=3, Name=text),
                       lambda: dlg.TextControl(searchDepth=3, Name=text)):
            try:
                c = finder()
                if c.Exists(0):
                    c.Click(simulateMove=False)
                    return True
            except Exception:
                continue
        try:
            dlg.SendKeys('{Esc}')
            return True
        except Exception:
            return False
    return _handle


def default_popup_router() -> PopupRouter:
    """剪映常见干扰弹窗默认路由表。"""
    r = PopupRouter()
    for title, btn in (('环境检测', '确定'), ('链接媒体', '取消'),
                       ('提示', '确定'), ('更新', '以后再说')):
        r.register(title, lambda dlg: True, _make_button_handler(btn))
    return r


def dismiss_blocking_dialogs(win=None):
    """关闭剪映顶部常见干扰弹窗（PopupRouter 驱动，路由表可扩展）。"""
    return default_popup_router().handle_all()


# ================= Description 定位 =================
def _find_nodes_by_desc(win, desc_substr, max_depth=15, results=None):
    """递归收集所有 FullDescription 包含子串的节点"""
    if results is None:
        results = []
    target = desc_substr.lower()

    def recurse(ctrl, depth):
        if depth > max_depth:
            return
        if target in _ctrl_desc(ctrl):
            results.append(ctrl)
        try:
            for child in ctrl.GetChildren():
                recurse(child, depth + 1)
        except Exception:
            pass

    recurse(win, 0)
    return results


def click_desc(win, desc_substr, timeout=10, simulate_move=False):
    """
    按 FullDescription 子串定位并点击。
    返回 True=成功，False=失败。
    优先点目标节点本身；失败时尝试点其父容器（UI 经常把事件绑在父层）。
    """
    end_t = time.time() + timeout
    while time.time() < end_t:
        nodes = _find_nodes_by_desc(win, desc_substr)
        if nodes:
            for node in nodes:
                # 方案 A: 点自己
                try:
                    node.Click(simulateMove=simulate_move)
                    return True
                except Exception:
                    pass
                # 方案 B: 点父容器
                try:
                    parent = node.GetParentControl()
                    if parent:
                        parent.Click(simulateMove=simulate_move)
                        return True
                except Exception:
                    pass
        time.sleep(0.3)
    return False


def wait_for_desc(win, desc_substr, appear=True, timeout=30, poll=0.5):
    """轮询等待 Description 出现/消失（用 poll_until / wait_until_absent 实现）。
    appear=True: 等出现 → 找到节点返回
    appear=False: 等消失 → 返回 True
    """
    def _check():
        nodes = _find_nodes_by_desc(win, desc_substr)
        if appear:
            return nodes[0] if nodes else None
        return not nodes  # True = 已经消失

    if appear:
        return poll_until(_check, timeout_s=timeout, interval_s=poll,
                          description=f'Desc 出现: {desc_substr}')
    else:
        ok = wait_until_absent(lambda: bool(_find_nodes_by_desc(win, desc_substr)),
                               timeout_s=timeout, description=f'Desc 消失: {desc_substr}')
        return ok or None  # True → True, False → None 保持原有返回值语义


# ================= 比例坐标点击 =================
def click_ratio(rect, rx, ry, simulate_move=True):
    """
    在给定矩形内按比例坐标点击 (0~1)。
    rect = (left, top, right, bottom)
    rx, ry = 0.5 表示中心
    """
    if not rect:
        raise ValueError("rect 为空")
    x = rect[0] + int((rect[2] - rect[0]) * rx)
    y = rect[1] + int((rect[3] - rect[1]) * ry)
    return click_abs(x, y, simulate_move)


def click_abs(x, y, simulate_move=True):
    try:
        if simulate_move:
            import pyautogui
            pyautogui.click(x, y)
        else:
            # uiautomation 的 Click(simulateMove=False) 是 SendInput
            pass
        return True
    except Exception:
        # 兜底：uiautomation Click
        return False


def get_panel_rect(win, desc_substr=None, fallback_ratio=(0.60, 0.55, 0.98, 0.98)):
    """
    取剪映右侧属性面板的矩形（绝对坐标）。
    优先按 Description 找面板容器；找不到则用窗口内比例估算。
    """
    # 方案 A: 直接找面板 Tab 所在控件取矩形
    if desc_substr:
        nodes = _find_nodes_by_desc(win, desc_substr)
        for n in nodes:
            rect = _ctrl_rect(n)
            if rect:
                return rect

    # 方案 B: 估算——右侧 38% 区域
    try:
        wr = win.BoundingRectangle
        # S5.5：四元组必须解到四个变量（旧代码 `ww, wh = 四元组` 必抛 ValueError，
        # 被本层 except 吞掉 → 兜底恒为 None，调用方只能吃 None 再自行兜底）
        wl, wt, wright, wbottom = int(wr.left), int(wr.top), int(wr.right), int(wr.bottom)
        wr_w = wright - wl
        wr_h = wbottom - wt
        left = wl + int(wr_w * fallback_ratio[0])
        top = wt + int(wr_h * fallback_ratio[1])
        right = wl + int(wr_w * fallback_ratio[2])
        bottom = wt + int(wr_h * fallback_ratio[3])
        return (left, top, right, bottom)
    except Exception:
        return None


def get_timeline_rect(win):
    """取时间线区域矩形"""
    nodes = _find_nodes_by_desc(win, "maintimelineroot")
    for n in nodes:
        rect = _ctrl_rect(n)
        if rect:
            return rect
    return None


def get_window_rect(win):
    try:
        r = win.BoundingRectangle
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


# DPI_AWARENESS_CONTEXT_*（Win10 1703+）
_DPI_CTX_UNAWARE = -1
_DPI_CTX_PER_MONITOR_V2 = -4


def ui_content_scale(win=None):
    """窗口内容缩放比 = 物理尺寸 / 逻辑尺寸（实测，不依赖 monitor dpi_scale）。

    做法：同一 hwnd 在「PerMonitorV2」与「Unaware」两个**线程** DPI 上下文里各读一次
    GetWindowRect——前者是物理像素，后者是系统虚拟化后的逻辑像素，比值即窗口的实际
    渲染缩放（100%=1.0，150%=1.5）。只改当前线程，读完立刻还原。

    为什么不用 monitor dpi_scale：DPI 虚拟化下屏幕尺寸会被读成逻辑值（历史上出现过
    2560×1440 被当成 3840×2160），而窗口自身两读的比值不受这条链影响。

    返回 (scale, source)；测不到或两次读数自相矛盾 → (None, 原因)。
    调用方拿到 None 必须显式处理（报"不可测"或显式标记降级），不许用猜测系数。
    """
    if win is None:
        return None, 'no_window'
    try:
        hwnd = win.NativeWindowHandle
    except Exception:
        return None, 'no_hwnd'
    if not hwnd:
        return None, 'hwnd=0'

    u32 = ctypes.windll.user32
    if not hasattr(u32, 'SetThreadDpiAwarenessContext'):
        return None, 'no_SetThreadDpiAwarenessContext'

    class _R(ctypes.Structure):
        _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                    ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

    def _read():
        r = _R()
        if not u32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
            return None
        return (r.right - r.left, r.bottom - r.top)

    def _in_ctx(ctx):
        prev = u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(ctx))
        try:
            return _read()
        finally:
            if prev:
                u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(prev))

    phys = _in_ctx(_DPI_CTX_PER_MONITOR_V2)
    logi = _in_ctx(_DPI_CTX_UNAWARE)
    if not phys or not logi or logi[0] <= 0 or logi[1] <= 0:
        return None, f'GetWindowRect_failed phys={phys} logical={logi}'
    sx = phys[0] / float(logi[0])
    sy = phys[1] / float(logi[1])
    if abs(sx - sy) > 0.02:
        return None, f'w/h_inconsistent {sx:.3f}/{sy:.3f}'
    if not (0.5 <= sx <= 4.0):
        return None, f'out_of_range {sx:.3f}'
    return sx, f'win_ratio phys={phys} logical={logi}'


# ================= 键盘 =================
def send_keys(win, keys):
    """向窗口发送按键（SendKeys 格式：^=Ctrl, +=Shift, %=Alt）"""
    try:
        win.SendKeys(keys)
        return True
    except Exception:
        return False


def select_main_track(settle=0.5):
    """
    Home 回时间线首 + Ctrl+A 全选主轨片段。
    高频操作（识别字幕/分离音频/导出前都要用），统一封装。
    用 pyautogui 物理按键（剪映时间线只认真实输入焦点，SendKeys 对 Home 不可靠）。
    """
    import pyautogui
    pyautogui.FAILSAFE = False
    op_mark('按键', 'Home（回时间线首）')
    pyautogui.press('home')
    time.sleep(0.3)
    op_mark('组合键', 'Ctrl+A（全选主轨片段）')
    pyautogui.hotkey('ctrl', 'a')
    time.sleep(settle)
    dprint("  ✅ Home + Ctrl+A 已选中主轨")


# ================= 截图 =================
def capture_region(x, y, w, h):
    """
    截取虚拟屏任意区域（支持多显示器/负坐标/副屏），返回 PIL Image。
    用 BitBlt(GetDC(NULL))，pyautogui.screenshot 在副屏会截出全黑。
    """
    import ctypes
    from ctypes import wintypes
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    # 64 位句柄修正
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]

    src_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(src_dc)
    bmp = gdi32.CreateCompatibleBitmap(src_dc, w, h)
    gdi32.SelectObject(mem_dc, bmp)
    SRCCOPY = 0x00CC0020
    gdi32.BitBlt(mem_dc, 0, 0, w, h, src_dc, x, y, SRCCOPY)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # 负数 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    buf = (ctypes.c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, src_dc)

    img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA")
    return img.convert("RGB")


def save_screenshot(win, path):
    """截取剪映窗口区域保存（多显示器安全）"""
    try:
        rect = get_window_rect(win)
        if rect:
            img = capture_region(rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1])
        else:
            # 兜底：主屏物理分辨率（不再硬编码 1920×1080，副屏/4K 会截错区域）
            _u = ctypes.windll.user32
            img = capture_region(0, 0, _u.GetSystemMetrics(0), _u.GetSystemMetrics(1))
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        img.save(path)
        return path
    except Exception as e:
        dprint(f"截图失败: {e}")
        return None


# ================= 轨道操作 =================
def select_segment(win, desc_substr, timeout=10):
    """选中时间线上片段（按 Description 匹配）。选中后右侧面板会切换。"""
    return click_desc(win, desc_substr, timeout=timeout)


def select_all_timeline(win):
    """全选时间线 Ctrl+A（警告：会选所有轨道）"""
    return send_keys(win, '^a')


def deselect_all(win):
    """Esc 取消选中"""
    return send_keys(win, '{Esc}')


# ================= 面板渲染关键 =================
def ensure_panel_rendered(win, max_retries=5):
    """
    剪映 5.9 UIA 树懒加载：右侧面板控件（VESwitchButton / VESpinBox 等）
    只有在"左键选片段 → Ctrl+A 全选 → 重绑"序列后才完整出现在 UIA 树里。
    返回 (new_win, rendered_bool)。rendered=False 表示未能确保完整渲染。
    """
    for attempt in range(max_retries):
        # 找任何一个视频片段
        nodes = _find_nodes_by_desc(win, 'mtlsvideop')
        if not nodes:
            dprint(f"ensure_panel_rendered: 没找到 mtlsvideop 片段 (尝试 {attempt+1})")
            return win, False

        target = nodes[0]
        try:
            r = target.BoundingRectangle
            import pyautogui
            cx, cy = (int(r.left) + int(r.right)) // 2, (int(r.top) + int(r.bottom)) // 2
            pyautogui.click(cx, cy)
        except Exception:
            pass
        time.sleep(0.5)
        win.SendKeys('^a')
        time.sleep(0.6)

        # 重绑 + 检查面板 Tab 是否出现
        win2, _ = connect_jianying()
        tabs = _find_nodes_by_desc(win2, 'vesettingpanelsubtabcontrol:')
        if tabs:
            return win2, True

        win = win2
    dprint(f"ensure_panel_rendered: 重试 {max_retries} 次仍未能确保完整渲染")
    return win, False


def get_timeLINE_track_header_rect(win):
    """
    取时间线轨道头区域（包含眼睛图标、锁定图标、轨道标题的左侧竖条）矩形。
    基于 MainTimeLineRoot 左边界向左延伸估算。
    返回绝对坐标 (left, top, right, bottom) 或 None。
    """
    nodes = _find_nodes_by_desc(win, 'maintimelineroot')
    for n in nodes:
        try:
            r = n.BoundingRectangle
            tr_left = int(r.left)
            tr_top = int(r.top) - 80    # 向上延伸包含播放控制栏
            tr_right = tr_left + 180    # 轨道头大约 180px 宽
            tr_bottom = int(r.bottom)
            return (tr_left - 20, tr_top, tr_right, tr_bottom)
        except Exception:
            pass
    # 兜底：估算窗口右侧 40%-45% 是轨道头
    wr = get_window_rect(win)
    if wr:
        ww = wr[2] - wr[0]
        wh = wr[3] - wr[1]
        return (wr[0] + int(ww * 0.40), wr[1] + int(wh * 0.55),
                wr[0] + int(ww * 0.46), wr[3] - int(wh * 0.05))
    return None


def get_right_panel_rect(win):
    """取右侧属性面板区域矩形（估算，用于面板内控件比例定位）"""
    wr = get_window_rect(win)
    if not wr:
        return None
    ww = wr[2] - wr[0]
    wh = wr[3] - wr[1]
    return (wr[0] + int(ww * 0.58), wr[1] + int(wh * 0.28),
            wr[2] - 20, wr[3] - int(wh * 0.08))


# ===== 面板边缘（4K 多栏布局的 x 锚定基准） =====
# 1920 逻辑基准（不折叠字幕面板）：
#   左面板左缘=0，左面板右缘=816（= VEComboBox.right），右面板内容左缘=1394
_PANEL_L_LEFT_LOGICAL = 0
_PANEL_COMBO_LEFT_LOGICAL = 118      # 左面板内 VEComboBox 左缘（1920 实测）
_PANEL_RIGHT_RIGHT_LOGICAL = 816     # 左面板右缘 = VEComboBox 右缘（1920 实测）
_PANEL_R_CONTENT_LEFT_LOGICAL = 1394  # 右面板内容左缘（1920「草稿参数」OCR 框左缘）
_PANEL_L3_RATIO = (0.0, 0.425, 0.726) # 三级兜底比例（WW 归一）：左缘/左面板右缘/右面板内容左缘


def panel_edges(win=None, s=None):
    """左/右面板边缘（屏幕物理 px）。**x 锚定唯一基准，不许其它地方再按窗口原点推。**

    优先级（必须显式记录用了哪一级，禁止静默降级）：
      一级 UIA：左面板右缘 = VEComboBox.right；左缘 = combo.left − 118×s；
                右面板内容左缘 = 右面板内容组 rect.left（宽>200 且贴窗口右缘的最左者）
      二级 OCR：UIA 缺项时不可用（本函数不跑 OCR，由调用方按自己的 OCR 命中框补）
      三级 比例：仅前两级拿不到时用，返回 src='ratio' 且 warn 非空，调用方必须 WARN

    返回 dict：left_left / left_right / right_left / src{...} / warn[str]
             left_combos（左面板 VEComboBox 物理 rect 列表，供"组合框右缘 − k×s"绑定）
             degraded（True = 至少一边来自三级比例，调用方用它/其它级别交叉校验）
    任一边拿不到 → 对应值为 None（调用方按"不可测"处理，不许猜）。
    """
    out = {'left_left': None, 'left_right': None, 'right_left': None,
           'left_combos': [], 'degraded': False, 'src': {}, 'warn': []}
    wr = get_window_rect(win)
    if not wr:
        out['warn'].append('窗口矩形不可用 → 面板边缘不可测')
        return out
    WL, WT, WW, WH = wr[0], wr[1], wr[2] - wr[0], wr[3] - wr[1]
    if not s:
        try:
            s, _why = ui_content_scale(win)
        except Exception:
            s = None
    s = float(s or 1.0)

    combos, groups = [], []
    from ui_calibration import get_param as _gp

    def walk(ctrl, depth=0):
        if depth > 15:
            return
        try:
            cls = ctrl.ClassName or ''
        except Exception:
            cls = ''
        r = _ctrl_rect(ctrl)
        if r:
            if 'VEComboBox' in cls:
                combos.append(r)
            elif r[2] - r[0] >= 200 and r[2] >= WL + WW - 40 \
                    and r[0] >= WL + WW * 0.6 and r[1] < WT + WH * 0.5:
                groups.append(r)
        try:
            for ch in ctrl.GetChildren():
                walk(ch, depth + 1)
        except Exception:
            pass

    try:
        walk(win)
    except Exception as e:
        out['warn'].append(f'UIA 遍历失败: {e}')

    # 左面板：右缘 = 左半屏 VEComboBox 的右缘最大值
    left_combos = [r for r in combos if r[2] <= WL + WW * 0.6]
    combo_left_logical = int(_gp('panel_edges', 'combo_left_logical'))
    if left_combos:
        out['left_right'] = max(r[2] for r in left_combos)
        out['left_left'] = (min(r[0] for r in left_combos)
                            - int(round(combo_left_logical * s)))
        out['left_combos'] = sorted(left_combos, key=lambda r: (r[1], r[0]))
        out['src']['left_right'] = 'uia:VEComboBox.right'
        out['src']['left_left'] = f'uia:VEComboBox.left-{combo_left_logical}×s'
    else:
        out['warn'].append('左面板 VEComboBox 未暴露 → 左边缘降级')

    # 右面板内容左缘：贴右缘的最左内容组
    if groups:
        out['right_left'] = min(r[0] for r in groups)
        out['src']['right_left'] = 'uia:Group.left'
    else:
        out['warn'].append('右面板内容组未暴露 → 右面板边缘降级')

    # 三级：比例兜底（显式 WARN，绝不静默）
    fallback_ratio = list(_gp('panel_edges', 'fallback_ratio'))
    if out['left_right'] is None:
        out['left_right'] = WL + int(round(fallback_ratio[1] * WW))
        out['left_left'] = WL + int(round(fallback_ratio[0] * WW))
        out['src']['left_right'] = out['src']['left_left'] = 'ratio(1920比例)'
        out['warn'].append(
            f"三级降级：左面板边缘用 1920 比例估算 → 右缘 {out['left_right']} "
            f"（4K 实测 2101，偏差约 -469；1920 下比例=实测无偏差）")
    if out['right_left'] is None:
        out['right_left'] = WL + int(round(fallback_ratio[2] * WW))
        out['src']['right_left'] = 'ratio(1920比例)'
        out['warn'].append(
            f"三级降级：右面板内容左缘用 1920 比例估算 → {out['right_left']} "
            f"（4K 实测 3019，偏差约 -231；1920 下=1394 无偏差）")
    out['degraded'] = any(str(v).startswith('ratio') for v in out['src'].values())
    return out
