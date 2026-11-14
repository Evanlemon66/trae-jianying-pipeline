# -*- coding: utf-8 -*-
"""jianying_launch.py — 剪映启动共用层（唯一入口 ensure_jianying）。

背景：仓库里"拉起剪映"存在两套实现——
  - 准入链 pipeline_ui/ui_core.launch_and_connect：版本强校验、cwd=exe 目录、
    CREATE_BREAKAWAY_FROM_JOB、WMI 兜底、窗口级就绪判定；
  - 导出链 jy_instance.UIABackend.launch：裸 Popen、等首页、进程存活轮询、
    启动即死重试。
本模块把这套能力收敛到一个入口，两条链路都调它；差异只通过参数表达：
  readiness='home'   等首页渲染完成（导出链默认）
  readiness='window' 等任意真 QML 窗口出现（准入链沿用，后续自己处理编辑页）
  retries / budget   启动即死重拉预算（按"每次运行"计，不按 attempt 放大）

只读依赖：不 import uiautomation / ui_core，避免循环依赖；UIA 探测由调用方以
ready_fn 注入。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

# 台账是旁路：导入失败/写盘失败都不允许影响启动流程。
try:
    from launch_ledger import append_record as _ledger_append
except Exception:  # pragma: no cover - 仅在 sys.path 异常时命中
    _ledger_append = None

# ── 常量（唯一口径，调用方不要再各自定义）────────────────────────────
REQUIRED_JY_VERSION = '5.9.0.11632'
EXE_NAME = 'JianyingPro.exe'

# 让剪映脱离调用方所在的作业对象：终端/父进程结束时不会把剪映连带回收。
BREAKAWAY_FROM_JOB = 0x01000000

# 就绪墙钟：冷启动时窗口出现但 HomePage 未渲染完仍算"未就绪"，继续轮询。
# 上限覆盖"环境检测窗"排他期：实测检测窗持续 8.3s（C1 attempt2，2026-11-14），
# 慢机留 14 倍余量；检测窗未消失前一律不判 ready（见 ensure_jianying）。
HOME_READY_TIMEOUT_S = 120.0

# ── 启动失败分类（两类问题必须分开，不能共用一份处理动作）──────────────
# 1) 启动即退（instant death）：进程起来后几秒内自己退出。实测签名 1.2–2.1s：
#    有「环境检测」窗、主窗口 0 B、exit 0；属确定性崩溃，重拉必须换条件
#    （清残留 + 间隔），不能原样重复。
# 2) 软失败（soft）：进程活着但等不到窗口 / 窗口激活超时 / 进不了编辑页。
INSTANT_DEATH_MAX_S = 10.0
# 启动即死：拉起后 EARLY_DEATH_WINDOW_S 内进程消失 = 启动即死，立即重拉。
# EARLY_DEATH_RETRIES 是**每次运行**的重试上限（不是无限重试）：
#   0 次重试 = 一次就放弃；1 次 = 两次尝试；这里是 2 次 = 最多 3 次尝试。
# 预算不与导出层 attempt 退避叠加，也不与崩溃框预算（CRASH_RETRIES）互相挤占。
EARLY_DEATH_WINDOW_S = 20.0
EARLY_DEATH_RETRIES = 2
# 重拉间隔：固定值，不做指数退避。
# 作用不是"等资源释放"（早退实例没进应用层），而是配合 clean_launch_residuals
# 确保 VEDetector/CrashSender/crashpad 完全退出后再拉起，避免新实例立刻撞同一个
# 检测器窗口。实测（2026-11-14，NAT_RETRY 六轮）+0.2s 与 +2.0s 成功率同为 6/6，
# 取 2s 只是给进程表稳定留余量，不代表成功率依赖这段等待。
EARLY_DEATH_SETTLE_S = 2.0
# 清残留后等待进程表稳定的墙钟上限（clean_launch_residuals 内部再判一次）。
RESIDUAL_CLEAN_WAIT_S = 5.0

# restart() 的 kill 与新进程之间的静默间隔。
# 原值 3.0 的依据是"旧进程可能还占着单实例互斥体"；B4 对照观测（2026-11-14，6 轮）
# 未复现该机理：进程确认消失后立即拉起 6/6 成功，含 3 次"崩溃残留检测器仍在"的
# 立即重拉（3/3 ready）。残留 VeDetector 只会让新实例多等一轮检测窗（~9-11s），
# 不会导致二次早退，3s 静置既不去掉这个检测窗、也不提高成功率，故归零。
# 若日后在 restart 路径实测到互斥失败，再按证据回调，不要凭感觉加回。
RESTART_SETTLE_S = 0.0

# VEDetector 崩溃框（nvapi 崩溃）单独一份预算：崩溃不是"早退"，本机实测连续崩 3 次，
# 与早死共用 1 次预算会让第 2 次崩溃就直接放弃。崩溃重拉前固定静置，避免立刻再撞，
# 这段等待不进早死路径（早死路径仍为 0，见上）。
CRASH_RETRIES = 2
CRASH_SETTLE_S = 3.0

# ready 后观察窗：窗口就绪 ≠ 稳定。07:10:23 那次实例 ready 后 8s 内进程消失，
# 但 ensure_jianying 在 ready 分支直接 return，台账里完全没有这条形态。
# ready 后 POST_READY_WATCH_S 内进程消失 → 判 died_after_ready，落台账并进重试。
# 每次成功拉起都要付这段等待，故按实测收敛到 3s：ready 后立死的形态都在 1-2s 内
# 暴露（11-14 观测），3s 足够覆盖，又不再白等 8s。
POST_READY_WATCH_S = 3.0

# 检测器停滞判死：主日志 0 字节 + 检测器（VeDetector）日志已写过但连续 5s 零增长
# + 从未建窗 + 主进程存活 = 这条实例已走死路。
# 依据（2026-11-15）：成功样本检测器日志最大行间隔 1.0s；失败 A1 的检测器日志
# 停在 +6s（21,426 B / 137 行，末尾 GetNvSettingDetectorSwitch，随后 nvapi64
# BEX64 崩溃），主进程却还要空等到 +32s 才退出。5s 阈值可在 +11s 判死重拉。
# 额外要求「主日志 0 字节」：健康实例在检测器收尾后到建窗之间也有 ~7s 静默
#（04:33:29 检测器日志停在 +15s，窗口 +21.8s 才出现），只靠"零增长"会误杀。
DETECTOR_STALL_S = 5.0

_PIPE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline")
if os.path.isfile(os.path.join(_PIPE_DIR, "export_config.py")) and _PIPE_DIR not in sys.path:
    sys.path.insert(0, _PIPE_DIR)
try:
    from export_config import get_jianying_log_dir
    _JY_LOG_DIR = get_jianying_log_dir()
except Exception:  # 独立拷贝 lib 时退回约定位置
    _JY_LOG_DIR = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "JianyingPro", "User Data", "Log")


class JianyingLaunchError(RuntimeError):
    """启动层错误（继承 RuntimeError，兼容既有 except RuntimeError 的调用方）。"""


@dataclass
class LaunchOutcome:
    """一次启动尝试（可能含内部重拉）的结果。"""
    ok: bool = False
    outcome: str = ''            # ready / timeout / died / died_late / died_after_ready
    value: Any = None            # ready_fn 的返回值（如已绑定窗口）
    exe: str = ''
    pid: Optional[int] = None
    attempt: int = 0
    alive_s: float = 0.0
    ready_at_s: Optional[float] = None          # 首次判定就绪的时刻（相对本次拉起）
    post_ready_died_at_s: Optional[float] = None  # 就绪后存活了多久才消失（ready 后死）
    exit_at: str = ''            # 判定进程消失的时刻（ISO，本次尝试的退出时刻）
    death_kind: str = ''         # instant / early / late（进程退出类失败）/ no_window
    # 主窗口首次出现的时刻（相对本次拉起，秒）；进程活着但一直是 None = 从未建窗
    window_seen_at_s: Optional[float] = None
    # 进程首次被判定存活的时刻（相对本次拉起，秒）；仅观测，不参与判定
    process_seen_at_s: Optional[float] = None
    # 本次尝试 spawn 的 epoch 秒（仅观测：把上面的相对时刻换算成绝对时刻）
    spawn_epoch: Optional[float] = None
    how: str = ''                # createprocess / wmi / unknown（按实际父进程判）
    launched: bool = False
    app_log: Optional[str] = None
    app_log_bytes: Optional[int] = None
    detector_log: Optional[str] = None
    detector_log_bytes: Optional[int] = None
    detector_tail: str = ''
    # 环境检测窗（VEDetector 转圈窗）/ 崩溃框取证
    env_check_seen: bool = False        # 本次尝试内是否出现过"环境检测"窗口
    env_check_span: float = 0.0         # 检测窗从出现到消失的耗时（秒）
    env_check_pid: Optional[int] = None
    crash_title: str = ''               # 崩溃框标题（VEDetector has stopped working 等）
    crash_text: str = ''                # 崩溃框正文（子窗口文本拼接）
    crash_pid: Optional[int] = None
    crash_at: str = ''                  # 发现崩溃框的时刻（ISO）
    app_gpu_probes: Optional[int] = None      # 主日志 GetGPUInfoEx 轮数
    app_not_find_device: Optional[int] = None  # 主日志 not find device 行数
    crash_retry: bool = False    # 本次失败是否因崩溃框而重拉（与早死预算分开）
    error: str = ''


class LaunchBudget:
    """启动重拉预算。每次运行创建一个，耗尽后不再重拉。

    两份独立预算：早死（died/died_after_ready）与崩溃框（VEDetector 崩溃），
    互不挤占——否则本机已知的 nvapi 连续崩溃会把早死预算一次用光。
    """

    def __init__(self, retries: int = EARLY_DEATH_RETRIES,
                 crash_retries: int = CRASH_RETRIES):
        self.remaining = max(0, int(retries))
        self.crash_remaining = max(0, int(crash_retries))

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def take_crash(self) -> bool:
        if self.crash_remaining <= 0:
            return False
        self.crash_remaining -= 1
        return True


# ── 基础工具 ────────────────────────────────────────────────────────

# 最近若干次探针异常（排障用：超时到底是 UIA 阻塞还是线程环境问题）
PROBE_ERRORS: list[str] = []


def poll_with_timeout(fn: Callable[[], Any], timeout: float) -> tuple[bool, Any]:
    """在 daemon 线程里执行一次探测，超时判失败（防 UIA 无限阻塞）。

    线程入口做 STA 初始化：UIA 的 COM 对象只能在已初始化的套间里创建/调用，
    未初始化线程会抛 OSError(-2147221008)，被吞掉后表现为"永远探不到"。
    异常仍按原逻辑返回 False，但类型与消息记到 PROBE_ERRORS 便于回溯超时原因。
    """
    result: dict[str, Any] = {}

    def _runner():
        _t_com = time.time()
        try:
            # COINIT_APARTMENTTHREADED = 0x2（UIA 要求 STA，不能用默认 MTA）
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        except Exception:
            pass
        if os.environ.get('JY_PROBE_TRACE') == '1':
            print(f"[probe-trace] com-init start={_t_com:.3f} "
                  f"dur={time.time() - _t_com:.3f}s", flush=True)
        try:
            result["value"] = fn()
        except Exception as e:
            PROBE_ERRORS.append(f"{type(e).__name__}: {e}")
            del PROBE_ERRORS[:-20]
        finally:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or "value" not in result:
        return False, None
    return True, result["value"]


def launch_flags(breakaway: bool = True) -> int:
    """隐藏控制台窗口；默认附带脱离作业。breakaway=False 用于作业禁止脱离时的回退。"""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if breakaway:
        flags |= BREAKAWAY_FROM_JOB
    return flags


def process_running(name: str = EXE_NAME) -> bool:
    """剪映进程是否存活。优先 psutil，缺失时退 tasklist。"""
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == name.lower():
                return True
        return False
    except Exception:
        pass
    try:
        r = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {name}'],
                           capture_output=True, text=True, encoding='mbcs',
                           errors='replace', timeout=10,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return name in (r.stdout or '')
    except Exception:
        return False


# ── exe 定位 + 版本强校验 ───────────────────────────────────────────

class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ('dwSignature', ctypes.c_uint32), ('dwStrucVersion', ctypes.c_uint32),
        ('dwFileVersionMS', ctypes.c_uint32), ('dwFileVersionLS', ctypes.c_uint32),
        ('dwProductVersionMS', ctypes.c_uint32), ('dwProductVersionLS', ctypes.c_uint32),
        ('dwFileFlagsMask', ctypes.c_uint32), ('dwFileFlags', ctypes.c_uint32),
        ('dwFileOS', ctypes.c_uint32), ('dwFileType', ctypes.c_uint32),
        ('dwFileSubtype', ctypes.c_uint32), ('dwFileDateMS', ctypes.c_uint32),
        ('dwFileDateLS', ctypes.c_uint32),
    ]


def exe_file_version(path: str) -> str:
    """读 exe 的 VS_FIXEDFILEINFO 文件版本（如 5.9.0.11632）。失败返回空串。"""
    try:
        from ctypes import wintypes
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return ''
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return ''
        ptr, ln = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, '\\', ctypes.byref(ptr), ctypes.byref(ln)):
            return ''
        ffi = ctypes.cast(ptr, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
        ms, ls = ffi.dwFileVersionMS, ffi.dwFileVersionLS
        return f'{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}'
    except Exception:
        return ''


def jianying_version_of(exe_path: str) -> str:
    """版本口径：优先 exe 文件版本，读不到再退回安装目录末级目录名。"""
    return (exe_file_version(exe_path)
            or os.path.basename(os.path.dirname(os.path.abspath(exe_path))))


def registry_jianying_exes() -> list[tuple[str, str]]:
    """从卸载注册表收集剪映候选 exe（HKCU/HKLM × 32/64 位视图，可能多版本共存）。"""
    import winreg
    out: list[tuple[str, str]] = []
    sub = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sk:
                                dn = winreg.QueryValueEx(sk, "DisplayName")[0]
                                if "剪映专业版" not in dn:
                                    continue
                                try:
                                    ico = (winreg.QueryValueEx(sk, "DisplayIcon")[0] or '').strip('"')
                                except Exception:
                                    ico = ''
                                if ico:
                                    out.append((f'注册表 DisplayIcon({dn})',
                                                os.path.join(os.path.dirname(ico), EXE_NAME)))
                                try:
                                    loc = (winreg.QueryValueEx(sk, "InstallLocation")[0] or '').strip()
                                except Exception:
                                    loc = ''
                                if loc:
                                    out.append((f'注册表 InstallLocation({dn})',
                                                os.path.join(loc, EXE_NAME)))
                        except Exception:
                            continue
            except Exception:
                continue
    return out


def _presets_jianying_dir(log_fn: Callable[[str], None]) -> str:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pipe = os.path.join(repo, 'pipeline')
    if pipe not in sys.path:
        sys.path.insert(0, pipe)
    from export_config import get_jianying_dir
    return (get_jianying_dir() or '').strip()


def jianying_candidates(log_fn: Optional[Callable[[str], None]] = None) -> list[tuple[str, str]]:
    """只读收集剪映候选 exe（presets.json > 环境变量 > 卸载注册表）。不会拉起进程。"""
    log = log_fn or (lambda _m: None)
    cands: list[tuple[str, str]] = []
    try:
        jd = _presets_jianying_dir(log)
        if jd:
            cands.append((f'presets.json jianying_dir={jd}', os.path.join(jd, EXE_NAME)))
    except Exception as e:
        log(f'  [jy-path] 读取 presets.json 失败: {e}')
    env_dir = (os.environ.get('JY_JIANYING_DIR') or '').strip()
    if env_dir:
        cands.append((f'JY_JIANYING_DIR={env_dir}', os.path.join(env_dir, EXE_NAME)))
    try:
        cands.extend(registry_jianying_exes())
    except Exception as e:
        log(f'  [jy-path] 读取注册表失败: {e}')
    return cands


def inspect_jianying_install(required: str = REQUIRED_JY_VERSION) -> dict:
    """只读检查剪映安装与版本（doctor 与版本预检共用，不拉起剪映）。

    返回 {ok, exe, version, source, checked, problems, message}。
    """
    cands = jianying_candidates()
    checked: list[tuple[str, str, str]] = []
    problems: list[str] = []
    for src, path in cands:
        if not path or not os.path.isfile(path):
            problems.append(f'{src} → 不存在: {path}')
            continue
        version = jianying_version_of(path)
        checked.append((src, path, version))
        if version and version == required:
            return {"ok": True, "exe": path, "version": version, "source": src,
                    "checked": checked, "problems": problems, "message": ""}
    if not checked:
        return {"ok": False, "exe": "", "version": "", "source": "", "checked": checked,
                "problems": problems,
                "message": f'未找到剪映专业版安装（仅支持 {required}），请安装或设置 JY_JIANYING_DIR'}
    versions = sorted({v for _s, _p, v in checked if v})
    return {"ok": False, "exe": "", "version": "", "source": "", "checked": checked,
            "problems": problems,
            "message": f'仅支持剪映 {required}，检测到: {", ".join(versions) or "无法读取版本"}'}


def resolve_jianying_exe(required: str = REQUIRED_JY_VERSION,
                         log_fn: Optional[Callable[[str], None]] = None) -> str:
    """解析剪映可执行文件并校验版本（带来源清单）。

    版本门分两档：
      - 明确读到文件版本且 ≠ required → 跳过该候选；
      - 读不到版本信息 → 打告警后接受该候选（附目录名佐证），不因此放宽拒绝条件。
    全部候选都不可用时抛 JianyingLaunchError，绝不静默返回其它大版本。
    """
    log = log_fn or (lambda _m: None)
    cands = jianying_candidates(log)

    problems = []
    for src, p in cands:
        if not p or not os.path.exists(p):
            problems.append(f'{src} → 不存在: {p}')
            continue
        ver = exe_file_version(p)
        if ver and ver != required:
            problems.append(f'{src} → 版本 {ver} ≠ {required}: {p}')
            continue
        if not ver:
            log(f'  [warn] 读不到版本信息，继续启动（目录名佐证: '
                f'{os.path.basename(os.path.dirname(os.path.abspath(p)))}）: {p}')
        else:
            log(f'剪映可执行文件: {p}（版本 {ver}，来源: {src}）')
        return p
    raise JianyingLaunchError(
        f'未找到剪映 {required}（不会静默拉起其它版本）：\n  ' + '\n  '.join(problems) +
        '\n请在 pipeline/presets.json 的 jianying_dir 指向 5.9.0.11632 安装目录')


# ── 进程拉起 ────────────────────────────────────────────────────────

def wmi_launch(exe_path: str, log_fn: Optional[Callable[[str], None]] = None) -> bool:
    """用 WMI 在终端作业之外拉起剪映（作业禁止 breakaway 时的兜底）。"""
    log = log_fn or (lambda _m: None)
    exe_dir = os.path.dirname(os.path.abspath(exe_path))
    cmdline = '"{}"'.format(exe_path).replace("'", "''")
    cwd = exe_dir.replace("'", "''")
    script = (f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              f"-Arguments @{{CommandLine='{cmdline}'; CurrentDirectory='{cwd}'}}; "
              f"Write-Output $r.ReturnValue")
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception as e:
        log(f'  [warn] WMI 作业外拉起失败: {e}')
        return False
    out = [ln.strip() for ln in (r.stdout or '').splitlines() if ln.strip()]
    if out and out[-1] == '0':
        log('  已通过 WMI 在终端作业之外拉起剪映（命令结束后不被连带回收）')
        return True
    log(f'  [warn] WMI 作业外拉起失败: rc={out[-1] if out else "?"} '
        f'{(r.stderr or "").strip()[:200]}')
    return False


def _find_new_jianying_pid(since: float) -> Optional[int]:
    """找 since 之后新出现的剪映主进程（WMI 拉起不回传 pid，用它补）。

    WMI 的 Popen 等价物不暴露 pid；按创建时间窗口挑最新一个，避免误认旧实例。
    """
    try:
        import psutil
        best: Optional[tuple[float, int]] = None
        for p in psutil.process_iter(['name', 'create_time']):
            if (p.info.get('name') or '').lower() != EXE_NAME.lower():
                continue
            ct = p.info.get('create_time') or 0.0
            if ct + 0.5 < since:
                continue
            if best is None or ct > best[0]:
                best = (ct, p.pid)
        return best[1] if best else None
    except Exception:
        return None


def detect_launch_how(pid: Optional[int], hint: str = '',
                      log_fn: Optional[Callable[[str], None]] = None) -> str:
    """按实际父进程判真实启动方式：CreateProcess / WMI。

    - subprocess.Popen（breakaway / plain）→ 父进程就是本进程；breakaway 只脱离
      作业对象，不改变父子关系；
    - WMI Win32_Process.Create → 父进程是 WmiPrvSE.exe。
    读不到父进程时按请求方式兜底并告警，绝不写死"看起来对"的值。
    """
    log = log_fn or (lambda _m: None)
    ppid: Optional[int] = None
    pname = ''
    if pid:
        try:
            import psutil
            parent = psutil.Process(pid).parent()
            if parent is not None:
                ppid, pname = parent.pid, (parent.name() or '')
        except Exception:
            pass
    elif hint == 'wmi':
        return 'wmi'  # WMI 路径本来就不回传 pid，属预期而非实测失败
    if pname and 'wmiprvse' in pname.lower():
        return 'wmi'
    if ppid is not None and ppid == os.getpid():
        return 'createprocess'
    fallback = {'breakaway': 'createprocess', 'plain': 'createprocess',
                'wmi': 'wmi'}.get(hint, 'unknown')
    log(f'  [warn] 启动方式实测失败（pid={pid or "-"} 父进程={pname or "-"}），'
        f'按请求方式记 {fallback}')
    return fallback


def spawn_jianying(exe_path: str,
                   log_fn: Optional[Callable[[str], None]] = None) -> tuple[Optional[int], str]:
    """拉起剪映：breakaway → WMI → 普通 Popen。返回 (pid, 真实启动方式)。

    必须显式 cwd=exe 目录：JianyingPro.exe 是启动器，按相对路径加载同级
    Resources/ 插件，cwd 落在调用方目录时会秒退或迟迟不建主窗口。
    返回的 how 是按实际父进程判出来的（createprocess / wmi），不是请求方式。
    """
    log = log_fn or (lambda _m: None)
    exe_dir = os.path.dirname(os.path.abspath(exe_path))
    log(f'启动: {exe_path}')
    how = 'breakaway'
    while True:
        try:
            started = time.time()
            p = subprocess.Popen([exe_path], cwd=exe_dir,
                                 creationflags=launch_flags(breakaway=(how == 'breakaway')))
            real = detect_launch_how(p.pid, how, log)
            log(f'  启动方式: {real}（请求 {how}，cwd={exe_dir}） pid={p.pid}')
            return p.pid, real
        except OSError as e:
            if how != 'breakaway':
                raise JianyingLaunchError(f'剪映启动失败（{how}）: {e}')
            log(f'  [warn] 脱离作业启动失败（{e}），改用 WMI 在终端作业之外拉起…')
            started = time.time()
            if wmi_launch(exe_path, log_fn=log):
                pid = _find_new_jianying_pid(started)
                real = detect_launch_how(pid, 'wmi', log)
                log(f'  启动方式: {real}（请求 wmi，cwd={exe_dir}） pid={pid or "-"}')
                return pid, real
            how = 'plain'


# ── 启动即死取证 ────────────────────────────────────────────────────

def jianying_log_snapshot(started_epoch: float) -> tuple[Optional[str], Optional[int]]:
    """找该次启动产生/更新的最新 JianyingPro_*.log，返回 (文件名, 字节数)。"""
    try:
        names = [n for n in os.listdir(_JY_LOG_DIR)
                 if n.startswith("JianyingPro_") and n.endswith(".log")]
    except OSError:
        return None, None
    best, best_mtime = None, 0.0
    for n in names:
        p = os.path.join(_JY_LOG_DIR, n)
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m + 5 < started_epoch:   # 启动前 5s 之前写的日志不属于这次启动
            continue
        if m > best_mtime:
            best, best_mtime = p, m
    if not best:
        return None, None
    try:
        return os.path.basename(best), os.path.getsize(best)
    except OSError:
        return os.path.basename(best), None


def _latest_detector_path(started_epoch: float) -> Optional[str]:
    """剪映日志目录下「本次启动之后写过」的最新非主日志路径（拿不到返回 None）。"""
    try:
        names = [n for n in os.listdir(_JY_LOG_DIR)
                 if n.lower().endswith('.log')
                 and not n.startswith('JianyingPro_')]   # 排除主日志
    except OSError:
        return None
    best, best_mtime = None, 0.0
    for n in names:
        p = os.path.join(_JY_LOG_DIR, n)
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m + 5 < started_epoch:   # 启动前 5s 之前写的日志不属于这次启动
            continue
        if m > best_mtime:
            best, best_mtime = p, m
    return best


def latest_detector_log_stat(started_epoch: float) -> tuple[Optional[str], Optional[int]]:
    """检测器日志的轻量状态：(文件名, 字节数)。只 stat，不读内容。"""
    p = _latest_detector_path(started_epoch)
    if not p:
        return None, None
    try:
        return os.path.basename(p), os.path.getsize(p)
    except OSError:
        return os.path.basename(p), None


def latest_detector_log(started_epoch: float, max_lines: int = 8,
                        max_chars: int = 400) -> tuple[Optional[str], Optional[int], str]:
    """取最近一次启动的检测器日志（按 mtime 取最新，不靠文件名猜）。

    检测器日志文件名各版本不固定（vedetector*.log / *.log 等），因此不匹配文件名，
    而是取剪映日志目录下「启动时刻之后写过的、非主日志（JianyingPro_*）」中 mtime 最新
    的那份；找不到时返回 (None, None, '')，不影响主流程。

    返回 (文件名, 字节数, 尾部若干行文本)。
    """
    best = _latest_detector_path(started_epoch)
    if not best:
        return None, None, ''
    try:
        name = os.path.basename(best)
        size = os.path.getsize(best)
    except OSError:
        return None, None, ''
    try:
        with open(best, 'r', encoding='utf-8', errors='replace') as f:
            lines = [ln.strip() for ln in f.readlines() if ln.strip()]
    except OSError:
        return name, size, ''
    return name, size, ' | '.join(lines[-max_lines:])[:max_chars]


# ── 环境检测窗 / 崩溃框（启动等待期的排他与处置）────────────────────

_WM_CLOSE = 0x0010
_ENV_CHECK_MARK = '环境检测'          # 转圈窗标题（实测含该字样）
_CRASH_MARKS = ('VEDetector', 'CrashSender')


def enum_visible_windows() -> list[tuple[int, str, str]]:
    """EnumWindows 枚举可见顶层窗口 → [(hwnd, class, title)]。纯 Win32，毫秒级。"""
    import ctypes
    from ctypes import wintypes
    out: list[tuple[int, str, str]] = []
    try:
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

        user32.EnumWindows(_Proc(_cb), 0)
    except Exception:
        pass
    return out


def window_pid(hwnd: int) -> int:
    """窗口所属进程 pid（0 表示取不到）。"""
    import ctypes
    from ctypes import wintypes
    pid = wintypes.DWORD(0)
    try:
        ctypes.windll.user32.GetWindowThreadProcessId(
            wintypes.HWND(hwnd), ctypes.byref(pid))
    except Exception:
        return 0
    return int(pid.value)


def window_child_texts(hwnd: int, max_items: int = 6) -> str:
    """收集子窗口文本（对话框正文），拼成可读串，供崩溃框取证。"""
    import ctypes
    from ctypes import wintypes
    texts: list[str] = []
    try:
        user32 = ctypes.windll.user32
        _Proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        buf = ctypes.create_unicode_buffer(512)

        def _cb(h, _lp):
            try:
                buf.value = ''
                user32.GetWindowTextW(h, buf, 512)
                t = (buf.value or '').strip()
                if t and t not in texts:
                    texts.append(t)
            except Exception:
                pass
            return True

        user32.EnumChildWindows(wintypes.HWND(hwnd), _Proc(_cb), 0)
    except Exception:
        pass
    return ' / '.join(texts[:max_items])


def env_check_windows() -> list[tuple[int, str, int]]:
    """当前可见的"环境检测"窗口 → [(hwnd, title, pid)]。

    检测窗存在期间主窗口即便已创建也不代表可用：readiness 判定必须先排他，
    否则 readiness='window' 会把检测窗（或检测期的半成品窗口）当成就绪。
    """
    return [(h, t, window_pid(h)) for h, _c, t in enum_visible_windows()
            if _ENV_CHECK_MARK in t]


def crash_dialogs() -> list[dict]:
    """VEDetector/CrashSender 崩溃框 → [{hwnd,title,text,pid}]。"""
    out: list[dict] = []
    for h, _c, t in enum_visible_windows():
        if any(m in t for m in _CRASH_MARKS):
            out.append({'hwnd': h, 'title': t,
                        'text': window_child_texts(h), 'pid': window_pid(h)})
    return out


def dismiss_window(hwnd: int) -> bool:
    """发 WM_CLOSE 优雅关闭窗口（不强杀进程）。"""
    import ctypes
    from ctypes import wintypes
    try:
        return bool(ctypes.windll.user32.PostMessageW(
            wintypes.HWND(hwnd), _WM_CLOSE,
            wintypes.WPARAM(0), wintypes.LPARAM(0)))
    except Exception:
        return False


def app_log_gpu_stats(log_name: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """统计某次主日志里 GetGPUInfoEx 轮数与 not find device 行数。"""
    if not log_name:
        return None, None
    p = os.path.join(_JY_LOG_DIR, log_name)
    n_gpu = n_nf = 0
    try:
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if 'GetGPUInfoEx' in line:
                    n_gpu += 1
                if 'not find device' in line:
                    n_nf += 1
    except OSError:
        return None, None
    return n_gpu, n_nf


def _fill_log_stats(outcome: LaunchOutcome, started: float) -> None:
    """把本次尝试的日志与 GPU 探测统计填进 outcome（成功/失败都记）。"""
    log_name, log_bytes = jianying_log_snapshot(started)
    det_name, det_bytes, det_tail = latest_detector_log(started)
    outcome.app_log = log_name
    outcome.app_log_bytes = log_bytes
    outcome.detector_log = det_name
    outcome.detector_log_bytes = det_bytes
    outcome.detector_tail = det_tail
    outcome.app_gpu_probes, outcome.app_not_find_device = app_log_gpu_stats(log_name)


# ── 共享启动入口 ────────────────────────────────────────────────────

def classify_death(alive_s: float) -> str:
    """进程退出类失败分类：instant（启动即退）/ early（早死）/ late（晚死）。"""
    if alive_s <= INSTANT_DEATH_MAX_S:
        return 'instant'
    if alive_s <= EARLY_DEATH_WINDOW_S:
        return 'early'
    return 'late'


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def cleanup_residuals(log_fn: Optional[Callable[[str], None]] = None) -> list[str]:
    """重拉前清残留（委托 lib/jianying_proc.clean_launch_residuals，kill 仍走唯一入口）。

    清理 JianyingPro / VEDetector / CrashSender 与剪映自己的 crashpad_handler；
    失败不影响重拉，只记一行日志。
    """
    log = log_fn or (lambda _m: None)
    try:
        import jianying_proc as jp
    except Exception as e:  # pragma: no cover - 仅在 sys.path 异常时命中
        log(f'  [cleanup] 跳过（无法加载 jianying_proc: {e}）')
        return []
    try:
        return jp.clean_launch_residuals(wait_s=RESIDUAL_CLEAN_WAIT_S, log_fn=log)
    except Exception as e:  # pragma: no cover
        log(f'  [cleanup] 失败（不影响重拉）: {e}')
        return []


def _validate_exe(exe_path: str, required: str, require_exists: bool,
                  validate_version: bool,
                  log_fn: Optional[Callable[[str], None]] = None) -> None:
    """校验调用方显式指定的 exe（版本门两档，与 resolve_jianying_exe 一致）。

    - 明确读到文件版本且 ≠ required → 抛错拒绝启动；
    - 读不到版本信息 → 告警后继续（附目录名佐证），不因此放宽拒绝条件。
    """
    log = log_fn or (lambda _m: None)
    exists = bool(exe_path) and os.path.exists(exe_path)
    if not exists:
        if require_exists:
            raise JianyingLaunchError(f'指定的剪映可执行文件不存在: {exe_path}')
        return
    if not validate_version:
        return
    ver = exe_file_version(exe_path)
    if ver and ver != required:
        raise JianyingLaunchError(
            f'指定的剪映版本 {ver} ≠ {required}，拒绝启动: {exe_path}')
    if not ver:
        log(f'  [warn] 读不到版本信息，继续启动（目录名佐证: '
            f'{os.path.basename(os.path.dirname(os.path.abspath(exe_path)))}）: {exe_path}')
    else:
        log(f'剪映可执行文件: {exe_path}（版本 {ver}，调用方指定）')


def _record_launch(outcome: LaunchOutcome, *, launch_id: str, entry: str,
                   readiness: str, started_iso: str) -> None:
    """把一次拉起写进台账（旁路：失败静默，不影响启动）。"""
    if _ledger_append is None:
        return
    died = outcome.outcome in ('died', 'died_late', 'died_after_ready')
    try:
        job_round = int(os.environ.get('JY_LEDGER_JOB_ROUND') or 0)
    except ValueError:
        job_round = 0
    _ledger_append({
        'ts': started_iso,
        'launch_id': launch_id,
        # job / job_round 由 cover_watch 通过环境变量透传给导出子进程，
        # 用来把同一部剧的跨轮重拉串起来（job_round=0 是首次正常轮）。
        'job': os.environ.get('JY_LEDGER_JOB') or None,
        'job_round': job_round,
        'entry': entry or 'unknown',
        'attempt': outcome.attempt,
        'retry_count': max(0, outcome.attempt - 1),
        'result': ('ready' if outcome.outcome == 'ready'
                   else 'timeout' if outcome.outcome == 'timeout'
                   else 'window_not_created' if outcome.outcome == 'window_not_created'
                   else 'died_after_ready' if outcome.outcome == 'died_after_ready'
                   else 'died'),
        'died_at_s': round(outcome.alive_s, 3) if died else None,
        'alive_s': round(outcome.alive_s, 3),
        # 窗口首次出现的时刻（相对拉起）；null = 本次尝试进程活着但从未建窗
        'window_seen_at_s': (round(outcome.window_seen_at_s, 3)
                             if outcome.window_seen_at_s is not None else None),
        'process_seen_at_s': (round(outcome.process_seen_at_s, 3)
                              if outcome.process_seen_at_s is not None else None),
        # spawn 的 epoch 秒：把「相对拉起」的观测时刻换算成绝对时刻（仅观测）
        'spawn_epoch': (round(outcome.spawn_epoch, 3)
                        if outcome.spawn_epoch is not None else None),
        # 秒退单独留痕：存活时长 + 退出时刻 + 类型（instant/early/late/crash/post_ready）
        'exit_at': outcome.exit_at or None,
        'death_kind': outcome.death_kind or None,
        'instant_death': outcome.death_kind == 'instant',
        # ready 后死单独留痕：alive_s 不能用早退口径（died_at_s ≤ 3s）去解释它。
        'ready_at_s': (round(outcome.ready_at_s, 3)
                       if outcome.ready_at_s is not None else None),
        'post_ready_died_at_s': (round(outcome.post_ready_died_at_s, 3)
                                 if outcome.post_ready_died_at_s is not None else None),
        'env_check_seen': bool(outcome.env_check_seen),
        'env_check_span': round(outcome.env_check_span, 2) if outcome.env_check_span else None,
        'env_check_pid': outcome.env_check_pid,
        'gpu_probes': outcome.app_gpu_probes,
        'notfind': outcome.app_not_find_device,
        'crash': bool(outcome.crash_title),
        'crash_title': outcome.crash_title,
        'crash_retry': bool(outcome.crash_retry),
        'pid': outcome.pid,
        'how': outcome.how,
        'readiness': readiness,
        'late': outcome.outcome == 'died_late',
        'error': outcome.error,
    })


def _post_ready_watch(alive: Callable[[], bool], ready_at: float, window: float,
                      *, time_fn: Callable[[], float],
                      sleep_fn: Callable[[float], None],
                      poll_interval: float) -> Optional[float]:
    """ready 后观察窗：窗口内进程消失则返回"就绪后存活秒数"，否则 None。"""
    step = poll_interval if poll_interval > 0 else 0.2
    while True:
        if not alive():
            return max(0.0, time_fn() - ready_at)
        remaining = window - (time_fn() - ready_at)
        if remaining <= 0:
            return None
        sleep_fn(min(step, remaining))


def _stop_stalled_instance(log: Callable[[str], None],
                           grace_s: float = 1.5) -> bool:
    """关掉已判死路的实例：先 WM_CLOSE 给 grace_s 自然退出的机会，不退再按进程树强杀。

    死路实例通常没有可响应 WM_CLOSE 的窗口（主日志 0 字节、从未建窗），所以强杀
    兜底必须有；这里不走 stop_jianying 的 15s 收尾等待——那会把刚省下的 20s 等回去。
    """
    try:
        import jianying_proc as jp
    except Exception:
        return False
    try:
        if not jp.running():
            return True
        n = jp._post_wm_close()
        if n:
            log(f"  [detector-stall] 已发 WM_CLOSE（{n} 个窗口），等 {grace_s:g}s 自然退出")
        _t = time.time()
        while time.time() - _t < grace_s:
            if not jp.running():
                log("  [detector-stall] 已优雅退出")
                return True
            time.sleep(0.2)
        log("  [detector-stall] 未自然退出 → 结束进程树")
        jp._taskkill(force=True)
        _t = time.time()
        while time.time() - _t < 10.0:
            if not jp.running():
                return True
            time.sleep(0.2)
    except Exception as e:  # noqa: BLE001
        log(f"  [detector-stall] 关闭失败（仅告警）：{type(e).__name__}: {e}")
    try:
        return not jp.running()
    except Exception:
        return False


def ensure_jianying(
    exe_path: Optional[str] = None,
    *,
    timeout: float = HOME_READY_TIMEOUT_S,
    readiness: str = 'home',
    retries: int = EARLY_DEATH_RETRIES,
    required_version: str = REQUIRED_JY_VERSION,
    validate_version: bool = True,
    require_exists: bool = True,
    budget: Optional[LaunchBudget] = None,
    ready_fn: Optional[Callable[[], Any]] = None,
    alive_fn: Optional[Callable[[], bool]] = None,
    spawn_fn: Optional[Callable[[str], Any]] = None,
    cleanup_fn: Optional[Callable[[Optional[Callable[[str], None]]], list]] = None,
    window_seen_fn: Optional[Callable[[], bool]] = None,
    time_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval: float = 1.0,
    probe_timeout: float = 8.0,
    early_death_window: float = EARLY_DEATH_WINDOW_S,
    settle_s: float = EARLY_DEATH_SETTLE_S,
    post_ready_watch_s: float = POST_READY_WATCH_S,
    detect_detector_stall: bool = True,
    detector_stall_s: float = DETECTOR_STALL_S,
    entry: str = 'unknown',
    log_fn: Optional[Callable[[str], None]] = None,
    tick_fn: Optional[Callable[[float, int], None]] = None,
) -> LaunchOutcome:
    """启动剪映并等到就绪（唯一入口）。

    readiness='home'   等首页（HomePage）渲染完成，导出链默认；
    readiness='window' 等任意真 QML 窗口出现，准入链沿用（后续自行处理编辑页）。
    entry              台账归类：'admit'（准入链）/ 'export'（导出链）。

    等待期间每轮判进程存活：进程消失立即判本次启动失败，不等满 timeout。
    启动即死（EARLY_DEATH_WINDOW_S 内消失）按 budget 重拉，预算按每次运行计。
    """
    log = log_fn or (lambda _m: None)

    exe = exe_path or resolve_jianying_exe(required_version, log_fn=log)
    _validate_exe(exe, required_version, require_exists, validate_version,
                  log_fn=log)

    budget = budget if budget is not None else LaunchBudget(retries)
    alive = alive_fn or process_running
    spawn = spawn_fn or (lambda p: spawn_jianying(p, log_fn=log))
    # 同一次调用（含内部重拉）共用一个 id，重试是独立一条记录但能串起来。
    launch_id = uuid.uuid4().hex[:12]

    if ready_fn is None:
        if readiness != 'home':
            raise JianyingLaunchError(f'未知就绪口径: {readiness}（缺 ready_fn）')
        ready_fn = _default_home_ready
    # 窗口是否出现过：只用于区分「进程活着但没建窗」与「窗口在但页面没渲染好」，
    # 不参与就绪判定、不改变重试行为（超时仍按原逻辑收尾）。
    window_seen = window_seen_fn or _default_window_seen
    stall_stat = latest_detector_log_stat if detect_detector_stall else None

    deadline = time_fn() + max(0.0, float(timeout))

    def _mark_window_seen() -> None:
        """就绪判定间隙补记「主窗口已建出」时刻（只观测，不影响判定）。

        轮询里第一次探测就可能阻塞到 ready（首探 ~22s），原实现在循环顶部检查，
        成功分支直接 break 会漏记；这里在每次探测返回后也补一次，口径与判页层同源
        （Win32 枚举到该 pid 的可见顶层窗口即算）。
        """
        if outcome.window_seen_at_s is not None:
            return
        try:
            _ws = window_seen
            try:
                _seen = bool(_ws(pid))
            except TypeError:
                _seen = bool(_ws())
            if _seen:
                outcome.window_seen_at_s = time_fn() - started
                log(f"[window-seen] attempt={attempt} pid={pid} "
                    f"at=+{outcome.window_seen_at_s:.1f}s")
        except Exception:
            pass

    attempt = 1
    while True:
        started = time_fn()
        started_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        spawn_result = spawn(exe)
        spawn_hint = ''
        if isinstance(spawn_result, tuple):
            pid = spawn_result[0]
            spawn_hint = spawn_result[1] if len(spawn_result) > 1 else ''
        else:
            pid = spawn_result
        # 真实启动方式按实际父进程判；自定义 spawn_fn 只给 pid 时也在这里补判。
        how = detect_launch_how(pid, spawn_hint, log)
        log(f'  launch-how: {how} pid={pid or "-"}')
        outcome = LaunchOutcome(exe=exe, pid=pid, attempt=attempt, how=how,
                                launched=True, outcome='timeout',
                                spawn_epoch=started)
        env_first = None          # 本次尝试内检测窗首次出现时刻
        det_size = None           # 本次尝试内检测器日志观测到的字节数
        det_since = None          # 该字节数最后一次变化的时刻（停滞起点）
        det_name = None
        crashed = False           # 本次失败是否来自崩溃框（走独立预算）
        while True:
            # 0) 记下进程出现 / 主窗口首次出现的时刻（仅观测，不参与判定）
            if outcome.process_seen_at_s is None:
                try:
                    if alive():
                        outcome.process_seen_at_s = time_fn() - started
                        log(f"[process-seen] attempt={attempt} pid={pid} "
                            f"at=+{outcome.process_seen_at_s:.1f}s")
                except Exception:
                    pass
            if outcome.window_seen_at_s is None:
                _mark_window_seen()
            # 1) 崩溃框优先：记录（标题/正文/pid/时刻）→ WM_CLOSE → 按启动即死走重试
            crash = crash_dialogs()
            if crash:
                c0 = crash[0]
                outcome.crash_title = c0['title']
                outcome.crash_text = c0['text']
                outcome.crash_pid = c0['pid']
                outcome.crash_at = datetime.now().astimezone().isoformat(timespec='seconds')
                for c in crash:
                    log(f"[detector-crash] at={outcome.crash_at} pid={c['pid']} "
                        f"title={c['title']!r} text={c['text']!r} "
                        f"closed={dismiss_window(c['hwnd'])}")
                outcome.alive_s = time_fn() - started
                outcome.outcome = 'died'
                outcome.exit_at = outcome.crash_at
                outcome.death_kind = 'crash'
                crashed = True
                outcome.error = (f'环境检测崩溃框（{c0["title"]}，pid={c0["pid"]}），'
                                 f'已关闭并按崩溃重试')
                break

            # 2) 环境检测窗排他：检测窗还在（转圈/崩溃）就不许判 ready
            envs = env_check_windows()
            if envs:
                if env_first is None:
                    env_first = time_fn()
                    outcome.env_check_seen = True
                    outcome.env_check_pid = envs[0][2]
                    log(f"[env-check] 出现 at=+{env_first - started:.1f}s "
                        f"title={envs[0][1]!r} pid={envs[0][2]}"
                        f"（排他：检测窗未消失前不判 ready）")
                ok, val = False, None
            else:
                if env_first is not None and not outcome.env_check_span:
                    outcome.env_check_span = time_fn() - env_first
                    log(f"[env-check] 消失 at=+{time_fn() - started:.1f}s "
                        f"用时 {outcome.env_check_span:.1f}s")
                ok, val = poll_with_timeout(ready_fn, probe_timeout)
                # 探测返回后补记窗口时刻：首探可能阻塞到 ready，避免成功分支漏记
                if outcome.window_seen_at_s is None:
                    _mark_window_seen()

            if ok and val:
                ready_at = time_fn()
                outcome.value = val
                outcome.ready_at_s = ready_at - started
                watched = None
                if post_ready_watch_s > 0:
                    watched = _post_ready_watch(alive, ready_at, post_ready_watch_s,
                                                time_fn=time_fn, sleep_fn=sleep_fn,
                                                poll_interval=poll_interval)
                if watched is not None:
                    # 就绪后又消失：不能当成功返回，否则这次形态在台账里完全不可见。
                    outcome.ok = False
                    outcome.outcome = 'died_after_ready'
                    outcome.post_ready_died_at_s = watched
                    outcome.alive_s = time_fn() - started
                    outcome.exit_at = _now_iso()
                    outcome.death_kind = 'post_ready'
                    if env_first is not None and not outcome.env_check_span:
                        outcome.env_check_span = time_fn() - env_first
                    break
                outcome.ok = True
                outcome.outcome = 'ready'
                outcome.alive_s = time_fn() - started
                if env_first is not None and not outcome.env_check_span:
                    outcome.env_check_span = time_fn() - env_first
                _fill_log_stats(outcome, started)
                log(f"[startup-ready] attempt={attempt} pid={pid} how={outcome.how} "
                    f"alive_s={outcome.alive_s:.1f} "
                    f"env_check={1 if outcome.env_check_seen else 0} "
                    f"env_span={outcome.env_check_span:.1f}s "
                    f"gpu_probes={outcome.app_gpu_probes} "
                    f"notfind={outcome.app_not_find_device}")
                _record_launch(outcome, launch_id=launch_id, entry=entry,
                               readiness=readiness, started_iso=started_iso)
                return outcome
            elapsed = time_fn() - started
            if time_fn() >= deadline:
                # 进程活着但全程没建出任何窗口：这是独立一类失败（window_not_created），
                # 与「窗口在但页面没渲染好」（timeout）分开报，便于判断是不是卡在启动前期。
                if outcome.window_seen_at_s is None:
                    outcome.outcome = 'window_not_created'
                    outcome.death_kind = 'no_window'
                else:
                    outcome.outcome = 'timeout'
                outcome.alive_s = elapsed
                outcome.error = (
                    f'进程存活 {elapsed:.1f}s 但从未创建任何剪映窗口（window_not_created）'
                    if outcome.outcome == 'window_not_created'
                    else f'启动后 {timeout:g}s 内未达到就绪（{readiness}）')
                if env_first is not None and not outcome.env_check_span:
                    outcome.env_check_span = time_fn() - env_first
                _fill_log_stats(outcome, started)
                tag = ('startup-window-not-created'
                       if outcome.outcome == 'window_not_created' else 'startup-timeout')
                log(f"[{tag}] attempt={attempt} pid={pid} how={outcome.how} "
                    f"alive_s={outcome.alive_s:.1f} "
                    f"window_seen_at={'-' if outcome.window_seen_at_s is None else f'+{outcome.window_seen_at_s:.1f}s'} "
                    f"env_check={1 if outcome.env_check_seen else 0} "
                    f"env_span={outcome.env_check_span:.1f}s "
                    f"gpu_probes={outcome.app_gpu_probes} "
                    f"notfind={outcome.app_not_find_device}")
                _record_launch(outcome, launch_id=launch_id, entry=entry,
                               readiness=readiness, started_iso=started_iso)
                return outcome
            if not alive():
                outcome.alive_s = elapsed
                outcome.exit_at = _now_iso()
                outcome.outcome = ('died' if elapsed <= early_death_window
                                   else 'died_late')
                outcome.death_kind = classify_death(elapsed)
                break

            # 3) 检测器停滞判死：检测器日志已写过、连续 DETECTOR_STALL_S 零增长、
            #    从未建窗、主进程存活、且主日志 0 字节（应用层毫无进展）→ 死路。
            #    正常实例在检测器收尾后也会静默 ~7s，但那时主日志已有内容，不会命中。
            if stall_stat is not None and outcome.window_seen_at_s is None:
                _dn, _ds = stall_stat(started)
                _now = time_fn()
                if _ds:
                    if _ds != det_size:
                        det_size, det_since, det_name = _ds, _now, _dn
                    elif det_since is not None and (_now - det_since) >= detector_stall_s:
                        _app_bytes = jianying_log_snapshot(started)[1]
                        if not _app_bytes:
                            _stall_s = _now - det_since
                            log(f"[detector-stalled] attempt={attempt} pid={pid} "
                                f"at=+{_now - started:.1f}s detector_log={det_name} "
                                f"bytes={det_size} 零增长={_stall_s:.1f}s "
                                f"app_log_bytes={_app_bytes if _app_bytes is not None else 0} "
                                f"window_seen=- → 判死重拉")
                            outcome.alive_s = _now - started
                            outcome.outcome = 'died'
                            outcome.death_kind = 'detector_stalled'
                            outcome.detector_log = det_name
                            outcome.detector_log_bytes = det_size
                            outcome.error = (f'检测器日志 {det_size}B 连续 {_stall_s:.0f}s '
                                             f'零增长、主日志 0 字节且从未建窗'
                                             f'（detector_stalled）')
                            _stop_stalled_instance(log)
                            outcome.exit_at = _now_iso()
                            break
            if tick_fn:
                tick_fn(elapsed, attempt)
            sleep_fn(poll_interval)

        if crashed:
            retry = budget.take_crash()
        else:
            # 重拉口径：退出时"还没建出过任何窗口"→ 无论存活多久都可重拉
            # （这类死亡是启动链没走完，与窗口建立后崩掉是两回事）；
            # 已建出窗口后再死，沿用原口径（≤EARLY_DEATH_WINDOW_S 的 died / ready 后死）。
            no_window = outcome.window_seen_at_s is None
            retry = (no_window or outcome.outcome in ('died', 'died_after_ready')) and budget.take()
        outcome.crash_retry = bool(crashed and retry)
        if env_first is not None and not outcome.env_check_span:
            outcome.env_check_span = time_fn() - env_first
        _fill_log_stats(outcome, started)
        if outcome.outcome == 'died_after_ready':
            log(f"[startup-died-after-ready] attempt={attempt} pid={pid} how={outcome.how} "
                f"started={started_iso} ready_at=+{outcome.ready_at_s:.1f}s "
                f"alive_s={outcome.alive_s:.1f} "
                f"post_ready_s={outcome.post_ready_died_at_s:.1f} "
                f"app_log={outcome.app_log or '-'} "
                f"app_log_bytes={'-' if outcome.app_log_bytes is None else outcome.app_log_bytes} "
                f"detector_log={outcome.detector_log or '-'} "
                f"detector_log_bytes={'-' if outcome.detector_log_bytes is None else outcome.detector_log_bytes} "
                f"env_check={1 if outcome.env_check_seen else 0} "
                f"retry={1 if retry else 0}")
        else:
            log(f"[startup-died] attempt={attempt} pid={pid} how={outcome.how} "
                f"started={started_iso} exit_at={outcome.exit_at or '-'} "
                f"kind={outcome.death_kind or 'died'} "
                f"alive_s={outcome.alive_s:.1f} "
                f"window_seen_at={'-' if outcome.window_seen_at_s is None else f'+{outcome.window_seen_at_s:.1f}s'} "
                f"app_log={outcome.app_log or '-'} "
                f"app_log_bytes={'-' if outcome.app_log_bytes is None else outcome.app_log_bytes} "
                f"detector_log={outcome.detector_log or '-'} "
                f"detector_log_bytes={'-' if outcome.detector_log_bytes is None else outcome.detector_log_bytes} "
                f"detector_tail={outcome.detector_tail or '-'} "
                f"env_check={1 if outcome.env_check_seen else 0} "
                f"env_span={outcome.env_check_span:.1f}s "
                f"gpu_probes={outcome.app_gpu_probes} "
                f"notfind={outcome.app_not_find_device} "
                f"crash={outcome.crash_title or '-'} retry={1 if retry else 0}"
                + (" late=1" if outcome.outcome == 'died_late' else ""))
        if not outcome.error:
            if outcome.outcome == 'died_after_ready':
                outcome.error = (f'就绪后 {outcome.post_ready_died_at_s:.0f}s 剪映进程退出'
                                 f'（ready 后死，不计入启动即死口径）')
            else:
                why = '启动即退' if outcome.death_kind == 'instant' else '晚死'
                if outcome.window_seen_at_s is None:
                    why += '，退出时从未建出窗口'
                outcome.error = (f'启动后 {outcome.alive_s:.1f}s 剪映进程退出'
                                 f'（{why}）')
        if not retry and outcome.outcome != 'ready':
            # 预算耗尽：把"这台机器现在起不来"讲清楚（尝试次数 / 最后退出时刻 / 失败类型），
            # 上层据此直接报错，不再无限重试。
            outcome.error = (f'[launch_exhausted:{outcome.death_kind or outcome.outcome}] '
                             f'共尝试 {outcome.attempt} 次仍未就绪；最后一次退出时刻 '
                             f'{outcome.exit_at or "-"}，失败类型 {outcome.death_kind or outcome.outcome}。'
                             f'{outcome.error}')
            log(f"[startup-exhausted] attempts={outcome.attempt} "
                f"last_exit_at={outcome.exit_at or '-'} "
                f"kind={outcome.death_kind or outcome.outcome} "
                f"error={outcome.error}")
        _record_launch(outcome, launch_id=launch_id, entry=entry,
                       readiness=readiness, started_iso=started_iso)
        if not retry:
            return outcome
        # 重拉前先清残留（VEDetector / CrashSender / crashpad / 残留剪映），再按固定间隔拉起：
        # 秒退是确定性崩溃，原样重复同样的动作解决不了。
        log(f"[startup-retry] attempt={attempt}→{attempt + 1} "
            f"kind={outcome.death_kind or outcome.outcome} "
            f"alive_s={outcome.alive_s:.1f} "
            f"window_seen_at={'-' if outcome.window_seen_at_s is None else f'+{outcome.window_seen_at_s:.1f}s'} "
            f"exit_at={outcome.exit_at or '-'} "
            f"→ 清理残留后重拉")
        if cleanup_fn is None:
            cleanup_residuals(log)
        else:
            cleanup_fn(log)
        # 崩溃重拉固定静置，避免新实例立刻再撞同一个 nvapi 崩溃；早死路径用显式间隔。
        sleep_fn(max(settle_s, CRASH_SETTLE_S) if crashed else settle_s)
        attempt += 1


def _default_home_ready() -> bool:
    """默认首页判定：UIA 主窗口 ClassName 含 HomePage。"""
    try:
        import uiautomation as uia
        w = uia.WindowControl(searchDepth=1, Name="剪映专业版")
        if not w.Exists(0):
            w = uia.WindowControl(searchDepth=1, Name="导出")
        return "HomePage" in (w.ClassName or "")
    except Exception:
        return False


def _default_window_seen(pid: Optional[int] = None) -> bool:
    """窗口出现判定：Win32 枚举到该 pid 的可见顶层窗口即算（不看标题/类名）。

    与判页层同源（都以 Win32 枚举 hwnd 为准）。原实现要求标题已含「剪映」，
    建窗初期标题还没就绪时会漏检，把「建过窗口后早死」错归成「未建窗」。
    """
    try:
        import jy_page_detect as jpd
        wins = jpd.enum_top_windows()
    except Exception:
        return False
    if not wins:
        return False
    if pid:
        return any(window_pid(h) == pid for h, _c, _t in wins)
    # 没有 pid（个别 spawn 实现不回传）：回退到「当前所有剪映主进程的窗口」
    try:
        import psutil
        pids = {p.pid for p in psutil.process_iter(['name'])
                if (p.info.get('name') or '').lower() == os.path.basename(EXE_NAME).lower()}
    except Exception:
        pids = set()
    if pids:
        return any(window_pid(h) in pids for h, _c, _t in wins)
    return any('剪映' in (t or '') for _h, _c, t in wins)


# ── 收尾策略 ────────────────────────────────────────────────────────

def finish_run(keep_window: bool,
               launched_this_run: bool,
               *,
               close_fn: Optional[Callable[[], bool]] = None,
               is_running_fn: Optional[Callable[[], bool]] = None,
               log_fn: Optional[Callable[[str], None]] = None) -> bool:
    """收尾策略：留窗 / 关闭 / 只关本轮拉起的实例。

    返回 True 表示"当前已是预期状态"（留窗或已确认退出）。
    """
    log = log_fn or (lambda _m: None)
    if keep_window:
        log('[剪映] 收尾：按配置留窗（不关闭）')
        return True
    if not launched_this_run:
        log('[剪映] 收尾：非本轮拉起，保持现状（不关闭）')
        return True
    if is_running_fn is not None and not is_running_fn():
        return True
    if close_fn is None:
        return True
    return bool(close_fn())
