"""手工路径的公共守卫 + 锁兜底（只服务 `--manual`，默认路径不引用）。

两条硬约束：
- 零 watcher 依赖：job.json / cover_job/** / .watcher.lock / .export_state.json
  一律不读；所有文件访问过 `guard()`，误读即 AssertionError。
- 不改共享锁：`lib/export_lock.py` 一个字节都不动。正常取锁仍用它；取锁失败
  时才由手工侧诊断锁文件，确认陈旧才接管（删锁 + 重试一次）。

判活三态：alive / dead / unknown。tasklist 跑不通、输出无法识别一律 unknown，
unknown 绝不接管（宁停不抢）。
"""
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (os.path.join(_REPO, "lib"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import export_lock  # noqa: E402  # 只调用它的 acquire/release，不改它

try:  # pythonw 托管下 tasklist 子进程必须隐藏，否则每次探活弹窗
    from no_window import NO_WINDOW as _NO_WINDOW
except Exception:  # pragma: no cover
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MEDIA_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".m4a", ".mp3",
             ".wav", ".aac", ".flac", ".ogg")
PROTECTED_FILES = ("s2.mp4", "s3.mp4", "ss.mp4")
WATCHER_NAMES = {"job.json", ".watcher.lock", ".export_state.json"}

STALE_AFTER = 60  # 秒：不可解析锁的接管阈值（与旧的 mtime 兜底口径一致）


class WatcherReadError(AssertionError):
    """手工判定层试图读取 watcher 侧状态。"""


# ── 运行期防呆 ───────────────────────────────────────────────────────
def guard(path):
    """手工层绝不允许触碰 job / cover 侧文件。"""
    if not path:
        return
    s = str(path).replace("/", "\\")
    parts = [x.lower() for x in s.split("\\") if x]
    if "_stream_manifests" in parts:
        return  # 清单目录本身是中立数据
    if "cover_job" in parts or (parts and parts[-1] in WATCHER_NAMES):
        raise WatcherReadError(f"[manual] 手工清理禁止读取 watcher 侧文件: {path}")


def exists(p):
    guard(p)
    return bool(p) and os.path.exists(p)


def isdir(p):
    guard(p)
    return bool(p) and os.path.isdir(p)


def isfile(p):
    guard(p)
    return bool(p) and os.path.isfile(p)


def real(p):
    try:
        return os.path.normcase(os.path.realpath(p))
    except OSError:
        return os.path.normcase(os.path.abspath(p))


# ── 保护区断言（手工侧自带，不用默认路径的 _blocked_reason）──────────
def protected(path, source_dir):
    """返回不可删/不可搬原因；空串=可以动。"""
    a = os.path.abspath(path)
    if source_dir:
        s = os.path.abspath(source_dir)
        if a == s:
            return "等于源视频目录"
        if s.startswith(a + os.sep):
            return "包含源视频目录"
    parts = [x.lower() for x in a.split(os.sep)]
    if "_broken" in parts:
        return "位于 _broken 保护区"
    if "cover_job" in parts:
        return "位于 cover_job 保护区"
    if os.path.basename(a).lower() in PROTECTED_FILES:
        return "散落成品保护文件"
    if os.path.basename(a).lower() == "_work":
        return "pipeline/_work 旧状态目录"
    return ""


# ── 草稿引用扫描（中间产物是否还能删）──────────────────────────────
def media_paths(data):
    """草稿 JSON 里所有被引用的媒体绝对路径（保守扫描，只多不少）。"""
    out = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            if os.path.isabs(node) and node.lower().endswith(MEDIA_EXT):
                out.add(os.path.normcase(os.path.abspath(node)))

    walk(data)
    return out


def referenced_owners(projects_root):
    """{草稿名: 引用路径集合}；无法确认时返回 None（调用方必须保守保留）。"""
    if not projects_root or not os.path.isdir(projects_root):
        return {} if projects_root else None
    owners = {}
    for name in os.listdir(projects_root):
        d = os.path.join(projects_root, name)
        if not os.path.isdir(d):
            continue
        jp = os.path.join(d, "draft_content.json")
        if not os.path.isfile(jp):
            if not os.path.isfile(os.path.join(d, "draft_meta_info.json")):
                continue  # 非草稿目录
            return None  # 草稿缺内容文件 → 无法确认
        try:
            with open(jp, encoding="utf-8") as f:
                owners[name] = media_paths(json.load(f))
        except (OSError, ValueError):
            return None
    return owners


def referencing_drafts(path, owners):
    key = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    prefix = key + os.sep
    return sorted(n for n, refs in owners.items()
                  if any(r == key or r.startswith(prefix) for r in refs))


# ── 锁兜底（不碰 export_lock.py）────────────────────────────────────
def lock_path():
    return getattr(export_lock, "_LOCK_PATH",
                   os.path.join(_REPO, ".export.lock"))


def pid_state(pid):
    """alive / dead / unknown。任何不确定都归 unknown。"""
    if pid <= 0:
        return "dead"
    try:
        res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=10, creationflags=_NO_WINDOW)
    except Exception:
        return "unknown"
    if res.returncode != 0:
        return "unknown"
    raw = res.stdout or b""
    if not raw:
        return "unknown"
    text = ""
    for enc in ("utf-8-sig", "gbk", "mbcs", "latin-1"):  # 中文 Windows 常见 GBK 输出
        try:
            text = raw.decode(enc).strip()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        return "unknown"
    for line in text.splitlines():
        cols = [c.strip().strip('"') for c in line.split('","')]
        if len(cols) > 1 and cols[1] == str(pid):
            return "alive"
    low = text.lower()
    if any(k in low for k in ("no tasks", "not found", "没有", "不存在")):
        return "dead"
    return "unknown"


def diagnose_lock():
    """只读诊断锁文件：pid / mtime 龄期 / 进程三态；不写任何东西。"""
    p = lock_path()
    if not os.path.exists(p):
        return {"path": p, "exists": False}
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = 0.0
    age = max(0.0, time.time() - mtime) if mtime else -1.0
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError:
        raw = b""
    data = None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        data = None
    pid = 0
    try:
        pid = int((data or {}).get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return {"path": p, "exists": True, "age": age, "size": len(raw),
            "data": data if isinstance(data, dict) else None,
            "pid": pid, "pid_state": pid_state(pid)}


def _stale_reason(diag):
    """确认陈旧才返回原因；否则空串（=不许接管）。"""
    if not diag.get("exists"):
        return ""
    data = diag.get("data")
    if not data or diag.get("pid", 0) <= 0:
        if diag.get("age", -1) > STALE_AFTER:
            return (f"锁文件不可解析或无有效 pid（{diag.get('size', 0)} 字节，"
                    f"mtime {diag.get('age', 0):.0f}s 前）")
        return ""
    if diag.get("pid_state") == "dead":
        return f"持有进程 pid={diag.get('pid')} 已不存在"
    return ""


def _print_diag(diag):
    if not diag.get("exists"):
        print(f"  [锁] 锁文件不存在: {diag.get('path')}")
        return
    data = diag.get("data") or {}
    age = diag.get("age", -1)
    age_txt = "未知" if age < 0 else f"{age:.0f}s"
    print(f"  [锁] 诊断: path={diag.get('path')} size={diag.get('size', 0)}B "
          f"mtime={age_txt}前 pid={diag.get('pid') or '-'} "
          f"owner={data.get('owner') or '-'} started={data.get('started') or '-'} "
          f"进程判定={diag.get('pid_state') or 'unknown'}")


def acquire_export_lock(owner):
    """取锁；失败才诊断，确认陈旧才接管。返回 (ok, 说明)。"""
    p = lock_path()
    if not os.path.exists(p):
        if export_lock.acquire(owner=owner):
            return True, ""
        diag = diagnose_lock()
        _print_diag(diag)
        return False, ("全局导出锁竞争失败（无锁文件却抢不到，可能有并发在写），"
                       "本次不动文件")

    diag = diagnose_lock()
    _print_diag(diag)
    why = _stale_reason(diag)
    if not why:
        st = diag.get("pid_state") or "unknown"
        hint = ("持有进程仍在运行，绝不能抢" if st == "alive"
                else "无法确认持有进程状态（tasklist 不可用/输出不可识别），按宁停不抢处理"
                if st == "unknown" else "持有信息不足")
        return False, (f"全局导出锁被占用且未确认陈旧（{hint}）；如确认无人在导出，"
                       "可人工删除该锁文件后重试")

    print(f"  [锁] 确认陈旧：{why} → 由手工侧接管（先删锁，重试一次）")
    print(f"  [锁] 建议动作：已确认无导出进程；若你不同意，请立刻 Ctrl+C 并检查 {p}")
    try:
        os.remove(p)
    except OSError as e:
        return False, f"陈旧锁删除失败（{e}），本次不动文件"
    if export_lock.acquire(owner=owner):
        return True, "（已接管陈旧锁）"
    return False, "接管陈旧锁后重新取锁仍失败，本次不动文件"


def release_export_lock():
    export_lock.release()
