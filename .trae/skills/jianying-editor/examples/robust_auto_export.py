# -*- coding: utf-8 -*-
import os
import re
import sys
import json
import time
import shutil
import argparse
import traceback
import ctypes
import psutil
import subprocess
import winreg

# 输出流统一 UTF-8：重定向到文件/GBK 控制台时，打印 emoji 不再抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ========== 1. 环境与依赖修复 (打包关键) ==========
def _init_frozen_env():
    """修复 PyInstaller 打包后的环境问题"""
    if getattr(sys, 'frozen', False):
        os.chdir(os.path.dirname(sys.executable))
        try:
            import comtypes.client
            _ = comtypes.client.CreateObject
        except ImportError: pass
        if hasattr(sys, '_MEIPASS'):
            os.environ['PATH'] = sys._MEIPASS + os.pathsep + os.environ.get('PATH', '')

_init_frozen_env()

import uiautomation as uia

# ========== 2. 核心辅助类 ==========

def dprint(msg):
    print(f"[*] {msg}")


def _presets_jianying_dir():
    """向上查找 pipeline/presets.json 的 jianying_dir（可选配置，读不到返回空串）。"""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        d = os.path.dirname(d)
        if not d:
            break
        preset = os.path.join(d, "pipeline", "presets.json")
        if os.path.isfile(preset):
            try:
                with open(preset, encoding="utf-8") as f:
                    return (json.load(f).get("jianying_dir") or "").strip()
            except (OSError, ValueError):
                return ""
    return ""


def _presets_deps_root():
    """向上查找 pipeline/presets.json 的 deps_root（可选配置，读不到返回空串）。"""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        d = os.path.dirname(d)
        if not d:
            break
        preset = os.path.join(d, "pipeline", "presets.json")
        if os.path.isfile(preset):
            try:
                with open(preset, encoding="utf-8") as f:
                    return (json.load(f).get("deps_root") or "").strip()
            except (OSError, ValueError):
                return ""
    return ""


def _find_ffprobe():
    """定位 ffprobe：JY_DEPS_ROOT → presets.json deps_root → PATH；找不到返回 None。"""
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    for base in (os.environ.get("JY_DEPS_ROOT", "").strip(), _presets_deps_root()):
        if not base:
            continue
        for rel in (os.path.join("tools", "ffmpeg", "bin", exe), os.path.join("bin", exe), exe):
            candidate = os.path.join(base, rel)
            if os.path.isfile(candidate):
                return candidate
    return shutil.which("ffprobe")


def _ffprobe_duration(path):
    """用 ffprobe 读视频真实时长（秒）；失败返回 None。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True)
        return float((r.stdout or "").strip())
    except Exception:
        return None


def _ffprobe_has_audio(path):
    """是否含音轨。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True)
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _preflight_media(draft_name):
    """导出预检：把草稿引用的音视频源文件逐个 ffprobe 一遍，提前暴露损坏/缺失素材。

    剪映遇到坏素材会弹「导出文件缺失/损坏 10011」并卡在 0%，肉眼难查；
    这里在动剪映之前就校验完，返回 (可解码数量, [(路径, 原因), ...])。
    """
    import json
    import concurrent.futures as futures

    root = os.environ.get("JY_PROJECTS_ROOT", "").strip() or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "JianyingPro", "User Data",
        "Projects", "com.lveditor.draft")
    content = os.path.join(root, draft_name, "draft_content.json")
    if not os.path.isfile(content):
        raise Exception(f"素材预检无法读取内容文件: {content}")

    ffprobe = _find_ffprobe()
    if not ffprobe:
        dprint("⚠️ 未找到 ffprobe，跳过素材预检（损坏素材仍可能导致导出卡死）")
        return 0, []

    with open(content, encoding="utf-8") as f:
        data = json.load(f)
    materials = data.get("materials") or {}
    paths = set()
    for key in ("videos", "audios"):
        for m in materials.get(key) or []:
            p = (m.get("path") or "").strip()
            if not p or p.startswith("http"):
                continue  # 网络素材/空占位不参与本地校验
            paths.add(os.path.normpath(p.replace("/", os.sep)))
    if not paths:
        return 0, []

    def check(p):
        if not os.path.isfile(p):
            return (p, "文件不存在")
        if os.path.getsize(p) < 1024:
            return (p, "文件过小/空文件")
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", p],
                capture_output=True, text=True, timeout=90)
        except Exception as e:
            return (p, f"ffprobe 调用失败: {e}")
        if r.returncode != 0 or not (r.stdout or "").strip():
            errs = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
            last = errs[-1] if errs else "无法解码"
            if last.startswith(p + ":"):
                last = last[len(p) + 1:]  # ffprobe 的错误行自带路径，去掉避免重复
            return (p, last.strip()[:120])
        return None

    problems = []
    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        for res in pool.map(check, sorted(paths)):
            if res:
                problems.append(res)
    return len(paths) - len(problems), problems


# ========== 进度可见性（进度文件 + 定时 toast + 任务流程记录） ==========
_last_progress_write = [0.0]
_last_progress_toast = [0.0]

# 本次任务上下文 + 主流程记录：全部写进 <output>.progress.json
# 查进度时先看这个 json：status/stage/attempt/flow/errors 就能还原整个任务
_TASK = {"draft": None, "output": None, "move": False, "attempt": 1, "attempts_max": 1,
         "status": "running", "stage": None, "start_time": None, "expected_duration": None,
         "real_file": None, "errors": []}
_FLOW = []
_PREFLIGHT_DONE = [False]


def _flow(step):
    """记录一条主流程节点并立刻落盘（不打印，调用处自行 dprint）。"""
    _FLOW.append({"t": time.strftime("%H:%M:%S"), "step": str(step)})
    _flush_task()


def _flush_task(done=False, final_file=None, duration=None, cover=None):
    """按当前上下文刷新进度 json（失败静默，不影响导出）。"""
    if not _TASK.get("output"):
        return
    _write_progress(_TASK["output"], _TASK.get("real_file"),
                    _TASK.get("expected_duration"),
                    _TASK.get("start_time") or time.time(),
                    done=done, final_file=final_file, duration=duration, cover=cover)


def _estimate_percent(real_export_file, expected_duration):
    """体积估算百分比（封顶 99%）；无法估算时返回 None。"""
    if not expected_duration or expected_duration <= 0:
        return None
    est_total = expected_duration * _EST_BYTES_PER_SEC
    if est_total <= 0:
        return None
    cur = _export_progress_size(real_export_file)
    return f"约 {min(99, int(cur / est_total * 100))}%"


def _progress_paths(output_path):
    """进度 json 的两个落点（都返回绝对路径）。

    运行中 → `L\\<剧名>.progress.json`（与剧名目录同级）
        不预建剧名目录，剪映才会把成品直接写进 `L\\<剧名>\\`，而不是 `剧名(1)`。
    完成   → `L\\<剧名>\\<剧名>.mp4.progress.json`（与 mp4、封面同目录）
    """
    out = os.path.abspath(output_path)
    bundle = os.path.dirname(out)
    side = os.path.join(os.path.dirname(bundle), os.path.basename(bundle) + ".progress.json")
    return out + ".progress.json", side


def _write_progress(output_path, real_export_file, expected_duration, start_time,
                    done=False, final_file=None, duration=None, cover=None):
    """任务进度 + 主流程 json，关键节点即时刷新。

    运行中写同级 `<剧名>.progress.json`，完成时改写到剧名目录内并与 mp4/封面同目录。
    """
    import json
    if not output_path:
        return
    rec = {
        "task": "剪映导出",
        "draft": _TASK.get("draft"),
        "output": output_path,
        "move": bool(_TASK.get("move")),
        "status": "success" if done else (_TASK.get("status") or "running"),
        "stage": _TASK.get("stage"),
        "attempt": _TASK.get("attempt"),
        "attempts_max": _TASK.get("attempts_max"),
        "done": done,
        "exported_mb": round(_export_progress_size(real_export_file) / 1048576, 1),
        "elapsed_s": int(time.time() - start_time) if start_time else 0,
        "percent": _estimate_percent(real_export_file, expected_duration),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "flow": list(_FLOW),
        "errors": list(_TASK.get("errors") or []),
    }
    if done and final_file:
        try:
            rec["final_mb"] = round(os.path.getsize(final_file) / 1048576, 1)
        except OSError:
            pass
        rec["percent"] = "100%"
        if duration:
            rec["duration_s"] = round(duration, 1)
        if cover:
            rec["cover"] = cover
    final_path, side_path = _progress_paths(output_path)
    target = final_path if (done and final_file) else side_path
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        if target == final_path and os.path.isfile(side_path):
            os.remove(side_path)  # 收尾后只留成品目录里那一份
    except Exception:
        pass


def _toast_safe(title, msg):
    """toast 通知（失败静默，不影响导出主流程）。"""
    try:
        _scripts = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
        if _scripts not in sys.path:
            sys.path.insert(0, os.path.abspath(_scripts))
        from utils.notifier import toast
        toast(title, msg)
    except Exception:
        pass


def _export_progress_size(path):
    """导出中产物体积（字节）：文件=自身大小；文件夹=内含文件总大小；不存在=0。
    供看门狗判断导出是否仍在推进。path 可能无 .mp4 后缀，两种形态都试。
    """
    if not path:
        return 0
    cands = [path] if path.lower().endswith(".mp4") else [path, path + ".mp4"]
    total = 0
    for c in cands:
        if os.path.isfile(c):
            try: total += os.path.getsize(c)
            except OSError: pass
        elif os.path.isdir(c):
            for root_dir, _dirs, files in os.walk(c):
                for f in files:
                    try: total += os.path.getsize(os.path.join(root_dir, f))
                    except OSError: pass
    return total


def _find_export_in_dir(search_dir, safe_name, min_mtime=None):
    """在目录中定位导出成品，兼容剪映「同名文件夹包裹 mp4」的落地方式。
    优先级：同名 .mp4 文件 → <name>.mp4 文件夹内的 <name>.mp4 → 最新 .mp4。
    min_mtime: 提供时，候选文件修改时间必须 ≥ 该值（防拿到上次导出的残留）。
    """
    def fresh(fp):
        return min_mtime is None or _mtime(fp) >= min_mtime

    if not os.path.isdir(search_dir):
        return None
    direct = os.path.join(search_dir, safe_name + ".mp4")
    if os.path.isfile(direct) and fresh(direct):
        return direct
    folder = os.path.join(search_dir, safe_name + ".mp4")
    if os.path.isdir(folder):
        inner = os.path.join(folder, safe_name + ".mp4")
        if os.path.isfile(inner) and fresh(inner):
            return inner
        for f in os.listdir(folder):
            fp = os.path.join(folder, f)
            if f.lower().endswith(".mp4") and fresh(fp):
                return fp
    newest, newest_t = None, 0.0
    cutoff = min_mtime if min_mtime is not None else time.time() - 300
    for f in os.listdir(search_dir):
        if not f.lower().endswith(".mp4"):
            continue
        fp = os.path.join(search_dir, f)
        mt = _mtime(fp)
        if mt > cutoff and mt > newest_t:
            newest, newest_t = fp, mt
    return newest


class ControlFinder:
    """控件查找逻辑 (移植自 pyJianYingDraft)"""
    
    @staticmethod
    def desc_matcher(target_desc: str, exact: bool = False):
        """根据 LegacyIAccessible.Description (ID:30159) 查找"""
        target_desc = target_desc.lower()
        def matcher(control: uia.Control, depth: int) -> bool:
            try:
                # 30159 = UIA_FullDescriptionPropertyId
                full_desc = control.GetPropertyValue(30159)
                if not full_desc: return False
                full_desc = full_desc.lower()
                return (target_desc == full_desc) if exact else (target_desc in full_desc)
            except:
                return False
        return matcher

    @staticmethod
    def class_matcher(class_name: str, exact: bool = False):
        """根据 ClassName 查找"""
        class_name = class_name.lower()
        def matcher(control: uia.Control, depth: int) -> bool:
            try:
                curr = control.ClassName.lower()
                return (class_name == curr) if exact else (class_name in curr)
            except:
                return False
        return matcher

# ========== 3. 导出控制器 ==========

class Exporter:
    def __init__(self):
        self.window = None
        self._setup_dpi()
        self.connect()

    def _setup_dpi(self):
        """强制开启高 DPI 感知"""
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            dprint("DPI: 系统级感知已开启")
        except:
            ctypes.windll.user32.SetProcessDPIAware()
            dprint("DPI: 基础感知已开启")

    def _find_jianying_path(self):
        """定位剪映执行文件：JY_JIANYING_DIR → presets.json → 注册表 → 常用路径。"""
        # 0. 显式配置优先（环境变量 / pipeline/presets.json 的 jianying_dir）
        for base in (os.environ.get("JY_JIANYING_DIR", "").strip(), _presets_jianying_dir()):
            if not base:
                continue
            for rel in ("JianyingPro.exe", os.path.join("Apps", "JianyingPro.exe")):
                candidate = os.path.join(base, rel)
                if os.path.exists(candidate):
                    return candidate
        # 1. 尝试从注册表查找卸载项
        try:
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name) as subkey:
                        try:
                            name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                            if "剪映专业版" in name:
                                ico = winreg.QueryValueEx(subkey, "DisplayIcon")[0]
                                apps_dir = os.path.dirname(ico)
                                # 优先尝试根目录的入口程序
                                main_exe = os.path.join(apps_dir, "JianyingPro.exe")
                                if os.path.exists(main_exe):
                                    return main_exe
                        except: continue
        except: pass

        # 2. 常用路径兜底
        paths = [
            os.path.join(os.environ.get('USERPROFILE', ''), r"AppData\Local\JianyingPro\Apps\JianyingPro.exe"),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), r"JianyingPro\Apps\JianyingPro.exe"),
        ]
        for p in paths:
            if p and os.path.exists(p): return p

        return None

    def connect(self, retry=True):
        """连接或唤醒剪映窗口"""
        # 1. 查找窗口
        self.window = uia.WindowControl(searchDepth=1, Name='剪映专业版')
        if not self.window.Exists(0):
            # 尝试通过导出窗口定位
            export_win = uia.WindowControl(searchDepth=1, Name='导出')
            if export_win.Exists(0):
                self.window = export_win
            else:
                if retry:
                    dprint("未找到【剪映专业版】，尝试启动程序...")
                    exe_path = self._find_jianying_path()
                    if exe_path:
                        dprint(f"启动: {exe_path}")
                        subprocess.Popen(exe_path)
                        # 等待程序启动完成
                        for _ in range(30):
                            time.sleep(2)
                            if uia.WindowControl(searchDepth=1, Name='剪映专业版').Exists(0):
                                dprint("程序已启动。")
                                return self.connect(retry=False)
                    raise Exception("无法自动启动【剪映专业版】，请手动启动后重试。")
                else:
                    raise Exception("未找到【剪映专业版】窗口，请确保它已启动。")
        
        # 2. 激活窗口
        try:
            if self.window.Exists(0):
                self.window.SetActive()
                self.window.SetTopmost(True) # 临时置顶确保不被遮挡
                time.sleep(0.2)
                self.window.SetTopmost(False)
        except Exception as e:
            dprint(f"窗口激活警告: {e}")

    def dismiss_blocking_dialogs(self):
        """尝试关闭可能阻挡界面的弹窗 (如环境检测、媒体丢失、版本更新等)"""
        dialogs = [
            {"Name": "环境检测", "CloseBtn": "确定"},
            {"Name": "链接媒体", "CloseBtn": "取消"},
            {"Name": "提示", "CloseBtn": "确定"},
            {"Name": "更新", "CloseBtn": "以后再说"}
        ]
        for dlg in dialogs:
            try:
                # 检查是否存在名为该名称的窗口
                win = uia.WindowControl(searchDepth=1, Name=dlg["Name"])
                if win.Exists(0):
                    dprint(f"检测到干扰弹窗【{dlg['Name']}】，正在尝试关闭...")
                    # 尝试定位按钮 (可能是 Button 或 Text 类型)
                    close_btn = win.ButtonControl(Name=dlg["CloseBtn"])
                    if not close_btn.Exists(0):
                        close_btn = win.TextControl(Name=dlg["CloseBtn"])
                    
                    if close_btn.Exists(0):
                        close_btn.Click(simulateMove=False)
                    else:
                        win.SendKeys('{Esc}')
                    time.sleep(1)
            except: pass

    def is_home_page(self):
        return "HomePage" in self.window.ClassName

    def is_edit_page(self):
        return "MainWindow" in self.window.ClassName

    def switch_to_home(self):
        """从编辑页返回首页。

        2026-10-25 实测（剪映 5.9）：编辑页右上角"关闭"按钮（TitleBarButton foundIndex=3）
        = 退出编辑页回首页——点击后窗口先 GONE、约 1s 内重建为 HomePage，进程保留不杀。
        优选点关闭；失败（按钮不存在/超时未回首页）则重启剪映兜底。
        """
        dprint("正在切换回首页...")
        if self.is_home_page():
            dprint("已在首页.")
            return

        close_btn = self.window.GroupControl(searchDepth=1, ClassName="TitleBarButton", foundIndex=3)
        if close_btn.Exists(1):
            close_btn.Click(simulateMove=False)
            # 点击后窗口先消失再重建为首页，轮询等重建完成（最多 30s）
            for _ in range(15):
                time.sleep(2)
                try:
                    w = uia.WindowControl(searchDepth=1, SubName="剪映")
                    if w.Exists(1) and "HomePage" in (w.ClassName or ""):
                        self.connect()
                        if self.is_home_page():
                            dprint("✅ 已关闭编辑页回到首页")
                            return
                except Exception:
                    pass
            dprint("⚠️ 关闭编辑页后 30s 未回首页，转重启兜底...")
        else:
            dprint("⚠️ 未找到关闭按钮，转重启兜底...")

        _restart_jianying_to_home(self)
        self.connect()  # 重启后窗口句柄已变，必须重新绑定
        if not self.is_home_page():
            raise Exception("重启后仍未回到首页，请手动检查剪映状态。")

    def open_draft(self, draft_name):
        """在首页查找并打开草稿"""
        # 1. 自动处理首页干扰弹窗
        self.dismiss_blocking_dialogs()
        
        dprint(f"正在查找草稿: {draft_name}...")
        if not self.is_home_page():
            self.switch_to_home()

        # 使用 Description 查找草稿标题
        target_desc = f"HomePageDraftTitle:{draft_name}"
        
        # 2. 增强查找循环
        draft_card = None
        dprint("开始进入草稿查找轮询...")
        for attempt in range(12): # 延长到 24 秒等待首页渲染
            # 策略 A: 精确 Description 匹配
            draft_text = self.window.TextControl(searchDepth=6, Compare=ControlFinder.desc_matcher(target_desc, exact=True))
            if draft_text.Exists(0):
                draft_card = draft_text.GetParentControl()
                dprint(f"✅ 匹配成功 (Desc): {draft_name}")
                break
            
            # 策略 B: Name 模糊匹配
            draft_text = self.window.TextControl(searchDepth=6, Name=draft_name)
            if draft_text.Exists(0):
                draft_card = draft_text.GetParentControl()
                dprint(f"✅ 匹配成功 (Name): {draft_name}")
                break
                
            # 策略 C: 部分匹配 (包含)
            def partial_matcher(control, depth):
                try: return draft_name in control.Name
                except: return False
            
            draft_text = self.window.TextControl(searchDepth=6, Compare=partial_matcher)
            if draft_text.Exists(0):
                draft_card = draft_text.GetParentControl()
                dprint(f"✅ 匹配成功 (模糊): {draft_text.Name}")
                break
            
            time.sleep(2)
            if attempt == 5:
                # 尝试点击一下首页空白处，激活列表滚动
                dprint("尝试刷新首页状态...")
                self.window.Click(ratioX=0.5, ratioY=0.5, simulateMove=False)

        if not draft_card:
             # 打印首页前 10 个文本块帮助调试
             try:
                 dprint("未找到草稿，首页可见文本如下:")
                 all_texts = [child.Name for child in self.window.GetChildren() if child.ControlType == uia.ControlType.TextControl][:10]
                 dprint(f"DEBUG: {all_texts}")
             except: pass
             raise Exception(f"未找到名为【{draft_name}】的草稿。")

        # 点击草稿卡片
        dprint(f"正在打开草稿...")
        # 剪映首页草稿通常在点击后会有短暂的“正在打开”状态
        draft_card.Click(simulateMove=False)
        
        # 等待加载编辑页 (延长到 60s，并增加异常容错)
        dprint("正在加载编辑器 (可能需要较长时间)...")
        for i in range(60):
            time.sleep(1)
            try:
                # 在窗口切换过程中，可能会有短暂的窗口丢失，忽略连接错误
                self.connect(retry=False) 
                if self.is_edit_page():
                    dprint(f"成功进入编辑页 (耗时 {i+1}s)。")
                    # 额外等待渲染完成
                    time.sleep(3)
                    return
            except:
                # 忽略加载过程中的连接失败，继续轮询
                continue
        raise Exception("打开草稿超时，未能进入编辑界面。请确认是否手动干预或程序卡死。")

    def export(self, output_path, draft_name, resolution="1080P", framerate=None,
               expected_duration=None, move_output=False):
        """执行导出流程。

        resolution/framerate: 提供时才在导出弹窗中修改对应设置；None 表示沿用剪映当前值。
        expected_duration: 草稿预期时长（秒），提供时会校验成品时长与草稿差 < 2s，
                          否则抛异常（由调用方判定是否重试）。
        move_output: 是否把成品和封面搬运到 output_path（安全搬运）。
                          默认 False：保留剪映原生目录结构（同名文件夹内含 mp4+封面）。
        """
        dprint("=== 开始导出流程 ===")
        
        if not self.is_edit_page():
            raise Exception("当前不在编辑页，无法导出。")

        safe_name = "".join([c for c in draft_name if c not in r'\/:*?"<>|']).strip()
        output_dir = os.path.dirname(os.path.abspath(output_path))

        # 2. 点击顶部【导出】按钮
        export_btn = self.window.TextControl(searchDepth=5, Compare=ControlFinder.desc_matcher("MainWindowTitleBarExportBtn"))
        if not export_btn.Exists(1):
            export_btn = self.window.TextControl(searchDepth=5, Name="导出")
        
        if not export_btn.Exists(0):
            raise Exception("找不到【导出】按钮。")
            
        export_btn.Click(simulateMove=False)
        time.sleep(2) 

        # 3. 定位导出弹窗并抓取导出路径
        self.connect(retry=False)
        real_export_file = None
        
        try:
            # 参考源代码逻辑：通过 ExportPath 抓取真实导出位置
            export_path_sib = self.window.TextControl(searchDepth=8, Compare=ControlFinder.desc_matcher("ExportPath"))
            if export_path_sib.Exists(2):
                # 兄弟节点往往包含真实的绝对路径文本
                path_ctrl = export_path_sib.GetSiblingControl(lambda ctrl: True)
                if path_ctrl:
                    real_export_file = path_ctrl.GetPropertyValue(30159) # 抓取 FullDescription
                    _TASK["real_file"] = real_export_file
                    dprint(f"检测到剪映真实导出位置: {real_export_file}")
                    _flow(f"剪映实际导出位置: {real_export_file}")
        except Exception as e:
            dprint(f"路径抓取失败 (尝试保底): {e}")

        # 4. 设置分辨率 (参考源代码逻辑)
        if resolution:
            try:
                res_map = {"480P": "480", "720P": "720", "1080P": "1080", "2K": "2K", "4K": "4K"}
                target_val = res_map.get(resolution, resolution)
                
                # 点击分辨率下拉框
                res_btn = self.window.TextControl(searchDepth=10, Compare=ControlFinder.desc_matcher("ExportSharpnessInput"))
                if res_btn.Exists(1):
                    res_btn.Click(simulateMove=False)
                    time.sleep(0.5)
                    # 在弹出的列表中查找目标分辨率
                    res_item = self.window.TextControl(searchDepth=5, Name=target_val)
                    if not res_item.Exists(0.5):
                        # 尝试通过 FullDescription 查找
                        res_item = self.window.TextControl(searchDepth=5, Compare=ControlFinder.desc_matcher(target_val))
                    
                    if res_item.Exists(0.5):
                        res_item.Click(simulateMove=False)
                        dprint(f"已设置分辨率为: {resolution}")
                        time.sleep(0.5)
            except Exception as e:
                dprint(f"分辨率设置失败: {e}")

        # 4.5 设置帧率（提供时才改动，否则沿用剪映当前值）
        if framerate:
            try:
                fr_map = {"24fps": "24", "25fps": "25", "30fps": "30",
                          "50fps": "50", "60fps": "60"}
                target_fr = fr_map.get(str(framerate).lower(), str(framerate))
                fr_btn = self.window.TextControl(searchDepth=10, Compare=ControlFinder.desc_matcher("FrameRateInput"))
                if fr_btn.Exists(1):
                    fr_btn.Click(simulateMove=False)
                    time.sleep(0.5)
                    fr_item = self.window.TextControl(searchDepth=5, Name=target_fr)
                    if not fr_item.Exists(0.5):
                        fr_item = self.window.TextControl(searchDepth=5, Compare=ControlFinder.desc_matcher(target_fr))
                    if fr_item.Exists(0.5):
                        fr_item.Click(simulateMove=False)
                        dprint(f"已设置帧率为: {framerate}")
                        time.sleep(0.5)
            except Exception as e:
                dprint(f"帧率设置失败: {e}")

        # 5. 点击【导出】确认按钮
        confirm_btn = self.window.TextControl(searchDepth=8, Compare=ControlFinder.desc_matcher("ExportOkBtn"))
        if not confirm_btn.Exists(0):
            confirm_btn = self.window.ButtonControl(Name="导出")
        
        if not confirm_btn.Exists(1):
             raise Exception("无法在导出弹窗中找到确认按钮。")

        dprint("确认导出...")
        confirm_btn.Click(simulateMove=False)
        _TASK["stage"] = "导出中"
        _flow(f"确认导出，开始编码（分辨率 {resolution or '沿用当前设置'}）")

        # 6. 等待导出完成（成功检测 + 失败弹窗检测 + 体积停滞看门狗 + 窗口丢失检测）
        dprint("正在导出中，请勿操作剪映...")
        start_time = time.time()
        completed = False
        last_size, last_growth = -1, time.time()
        win_lost = 0

        while time.time() - start_time < 43200: # 最长 12 小时（长剧集导出可达数小时）
            try:
                close_btn = self.window.TextControl(searchDepth=8, Compare=ControlFinder.desc_matcher("ExportSucceedCloseBtn"))
                if close_btn.Exists(0):
                    dprint("✅ 导出完成检测成功！")
                    close_btn.Click(simulateMove=False)
                    completed = True
                    break
                # 失败弹窗检测
                if (self.window.TextControl(searchDepth=8, Name="导出失败").Exists(0)
                        or self.window.TextControl(searchDepth=8, Compare=ControlFinder.desc_matcher("ExportFailed")).Exists(0)
                        or self.window.TextControl(searchDepth=8, Compare=ControlFinder.desc_matcher("ExportFail")).Exists(0)):
                    raise Exception("剪映提示导出失败（检测到失败弹窗）。")
                win_lost = 0
            except Exception as e:
                if "导出失败" in str(e):
                    raise
                win_lost += 1
                if win_lost >= 15:  # 连续约30秒无法访问窗口，判定剪映已崩溃/被关闭
                    raise Exception("导出过程中剪映窗口丢失（疑似崩溃或被关闭）。")

            # 体积看门狗：优先盯 UI 抓到的路径；剪映会把同名目录去重成「剧名(1)/(2)」，
            # 此时 UI 路径可能指向不存在的旧位置，改用实际在增长的 mp4 接管，避免误判卡死
            now = time.time()
            cur = _watchdog_size(real_export_file, output_dir, safe_name)
            if cur > last_size:
                last_size, last_growth = cur, now
            else:
                adopted = _adopt_growing_mp4(output_dir, safe_name, start_time - 5,
                                             current=real_export_file)
                if adopted:
                    adopted_size = _export_progress_size(adopted)
                    if adopted_size > last_size:
                        dprint(f"看门狗接管实际产物: {adopted}（{adopted_size/1048576:.1f}MB）")
                        real_export_file = adopted
                        _TASK["real_file"] = adopted
                        last_size, last_growth = adopted_size, now
            # 首字节宽限：无预期时长时 420s；有时按 60% 时长动态（420~1800s）。
            # 已开始写盘后 180s 无增长才判卡死
            if last_size > 0:
                stall_limit = 180
            elif expected_duration:
                stall_limit = max(420, min(1800, int(expected_duration * 0.6)))
            else:
                stall_limit = 420
            if now - last_growth > stall_limit:
                shot = None
                try:
                    shot = os.path.join(output_dir, safe_name + ".stall.png")
                    self.window.CaptureToImage(shot)
                    dprint(f"已保存卡死现场截图: {shot}")
                except Exception:
                    shot = None
                _TASK["stage"] = "卡死"
                _flow(f"看门狗判定卡死：{stall_limit}s 产物无增长（{last_size/1048576:.1f}MB）"
                      + (f"，现场截图 {os.path.basename(shot)}" if shot else ""))
                raise Exception(
                    f"导出卡死：产物 {stall_limit}s 无增长（{last_size/1048576:.1f}MB）: {real_export_file}")
            time.sleep(2)

            # 进度可见性：每 30s 写进度文件（output_path.progress.json），每 5 分钟 toast 一次
            now = time.time()
            if now - _last_progress_write[0] >= 30:
                _last_progress_write[0] = now
                _write_progress(output_path, real_export_file, expected_duration,
                                start_time, done=False)
            if now - _last_progress_toast[0] >= 300:
                _last_progress_toast[0] = now
                pct = _estimate_percent(real_export_file, expected_duration)
                _toast_safe("📤 导出进行中",
                            f"{draft_name}  {pct}" if pct else f"{draft_name}（体积估算不可用）")

        if not completed:
            raise Exception("导出超时或未检测到完成状态。")
        time.sleep(2)  # 等剪映收尾写盘、释放文件句柄

        # 7. 定位导出成品（UI 真实路径优先 + 文件夹感知 + 兜底扫描 + ffprobe 校验）
        #    时效校验：候选文件 mtime 必须晚于本次导出开始时间，防止命中上次导出的残留
        dprint("定位导出成品...")
        min_mtime = start_time - 5  # 5s 容差，抵御文件系统时间粒度
        found_file = None

        # A. UI 抓到的真实路径（可能无 .mp4 后缀、可能是文件夹）
        if real_export_file:
            p = real_export_file
            if os.path.isfile(p):
                found_file = p
            elif os.path.isdir(p):
                inner = os.path.join(p, safe_name + ".mp4")
                found_file = inner if os.path.isfile(inner) else None
                if not found_file:
                    for f in os.listdir(p):
                        if f.lower().endswith(".mp4"):
                            found_file = os.path.join(p, f)
                            break
            elif not p.lower().endswith(".mp4"):
                p2 = p + ".mp4"
                if os.path.isfile(p2):
                    found_file = p2
                elif os.path.isdir(p2):
                    inner = os.path.join(p2, safe_name + ".mp4")
                    found_file = inner if os.path.isfile(inner) else None

        # A 时效校验：UI 路径命中的文件若早于本次导出开始，判定为上次残留，弃用转兜底
        if found_file and _mtime(found_file) < min_mtime:
            dprint(f"⚠️ UI路径命中的文件早于本次导出开始，疑似上次残留，弃用: {found_file}")
            found_file = None

        # B. 兜底：在目标输出目录和常见目录扫描
        if not found_file:
            up = os.environ.get("USERPROFILE", "")
            for d in set([output_dir, os.path.join(up, "Videos"),
                          os.path.join(up, "Desktop"),
                          os.path.join(up, "Videos", "Jianying")]):
                found_file = _find_export_in_dir(d, safe_name, min_mtime=min_mtime)
                if found_file:
                    break

        # B2. 剪映去重目录（剧名(1)/(2)…）：成品名带 (n) 后缀，按「任意新 mp4」匹配
        if not found_file:
            found_file = _adopt_growing_mp4(output_dir, safe_name, min_mtime)
            if found_file:
                dprint(f"⚠️ 在剪映去重目录中找到成品: {found_file}")

        if not found_file:
            raise Exception(f"未找到导出成品（UI路径={real_export_file}，扫描={output_dir}）")

        dprint(f"找到导出原始文件: {found_file}")
        _flow(f"定位到成品: {found_file}（{os.path.getsize(found_file)/1048576:.0f}MB）")
        _write_progress(output_path, real_export_file, expected_duration,
                        start_time, done=True, final_file=found_file)
        _toast_safe("📤 导出成功", f"{draft_name}（{os.path.getsize(found_file)/1073741824:.2f}GB）")

        # 8. 校验：可读 + 有音轨 + 时长与草稿一致
        dur = _ffprobe_duration(found_file)
        if dur is None:
            raise Exception(f"成品无法读取（ffprobe 失败）: {found_file}")
        if not _ffprobe_has_audio(found_file):
            raise Exception(f"成品无音轨: {found_file}")
        if expected_duration is not None:
            # 截断判定只认「短」：成品比草稿短 >2s 才算截断；
            # 偏长是编码器 GOP 对齐填充（常见 +2~5s），属正常成品
            if dur < expected_duration - 2.0:
                raise Exception(f"成品时长 {dur:.1f}s 短于草稿 {expected_duration:.1f}s，疑似截断")
            if dur > expected_duration + 10.0:
                raise Exception(f"成品时长 {dur:.1f}s 异常偏长（草稿 {expected_duration:.1f}s），"
                                f"疑似草稿/导出内容不符")
        _flow(f"成品校验通过：时长 {dur:.1f}s，音轨=有")

        # 9. 搬运开关：默认保留剪映原生结构（同名文件夹内含 mp4+封面），开启后才搬到目标路径
        if move_output:
            os.makedirs(output_dir, exist_ok=True)
            if os.path.abspath(found_file) != os.path.abspath(output_path):
                if os.path.isfile(output_path):
                    os.remove(output_path)
                shutil.move(found_file, output_path)
            # 封面归位：封面名跟随剪映去重后的目录名（剧名(2)-封面.jpg），需模糊匹配
            src_dir = os.path.dirname(found_file)
            if os.path.abspath(src_dir) != os.path.abspath(output_dir):
                covers = _cover_candidates(src_dir, found_file, safe_name)
                dst_cover = os.path.join(output_dir, safe_name + "-封面.jpg")
                if covers:
                    if os.path.isfile(dst_cover):
                        os.remove(dst_cover)
                    shutil.move(covers[0], dst_cover)
                    dprint(f"封面已归位: {dst_cover}")
                else:
                    dprint("⚠️ 未找到封面（剪映未生成或命名异常）")
                # 搬空后清理剪映生成的同名空壳文件夹（仅当确实已空）
                if os.path.isdir(src_dir) and not os.listdir(src_dir):
                    os.rmdir(src_dir)
            # 清理剪映去重产生的空壳目录，保证只留唯一剧名目录
            _prune_bundle_dups(output_dir, safe_name)
            cover_path = os.path.join(output_dir, safe_name + "-封面.jpg")
            cover_path = cover_path if os.path.isfile(cover_path) else None
            dprint(f"🎉 导出完成（已搬运）: {output_path} 时长 {dur:.1f}s 音轨=有")
            _record_export_manifest(draft_name, output_path)
            _TASK.update(status="success", stage="完成")
            _flow(f"搬运归位完成：mp4、封面、进度文件均在 {output_dir}")
            _flush_task(done=True, final_file=output_path, duration=dur, cover=cover_path)
        else:
            src_dir = os.path.dirname(found_file)
            cover = os.path.join(src_dir, safe_name + "-封面.jpg")
            cover_note = f"，封面: {cover}" if os.path.isfile(cover) else ""
            dprint(f"🎉 导出完成（未搬运，保留剪映原生结构）: {found_file}{cover_note} 时长 {dur:.1f}s 音轨=有")
            _record_export_manifest(draft_name, found_file)
            _TASK.update(status="success", stage="完成")
            _flow(f"导出完成（未搬运）: {found_file}")
            _flush_task(done=True, final_file=found_file, duration=dur,
                        cover=cover if os.path.isfile(cover) else None)
        return True


# 移植自老脚本 auto_exporter.py（2026-10-25）
_UI_RETRY_KEYWORDS = ("剪映窗口未找到", "导出按钮", "编辑窗口", "草稿", "未找到", "卡死", "超时")
_EST_BYTES_PER_SEC = 1.05 * 1024 * 1024  # 1080P 导出码率经验估值（字节/秒）


def _record_export_manifest(draft_name, export_file):
    """导出成功后把成品路径补进串流产物清单（失败不影响导出流程）。"""
    try:
        _pipeline_dir = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "..", "pipeline"))
        if os.path.isdir(_pipeline_dir):
            if _pipeline_dir not in sys.path:
                sys.path.insert(0, _pipeline_dir)
            import stream_manifest
            stream_manifest.record(draft_name, export_file=export_file)
    except Exception as _e:
        dprint(f"[warn] 记录成品到串流清单失败: {_e}")


def _check_disk_space(output_path, expected_duration):
    """磁盘余量预检：目标盘剩余不足预估成品 1.2 倍时直接拒绝启动（不占用重试）。"""
    if not expected_duration or expected_duration <= 0:
        return
    est_bytes = expected_duration * _EST_BYTES_PER_SEC * 1.2
    drive = os.path.splitdrive(os.path.abspath(output_path))[0] + "\\"
    free = shutil.disk_usage(drive).free
    if free < est_bytes:
        raise Exception(
            f"磁盘空间不足: {drive} 剩余 {free / 2**30:.1f}GB，预估需 {est_bytes / 2**30:.1f}GB"
        )
    dprint(f"磁盘预检通过: {drive} 剩余 {free / 2**30:.1f}GB，预估需 {est_bytes / 2**30:.1f}GB")


def _cover_candidates(src_dir, found_file, safe_name):
    """封面候选名：剪映封面跟随「实际文件夹名」，目录被去重成 剧名(2) 时封面也叫 剧名(2)-封面.jpg。"""
    stem = os.path.splitext(os.path.basename(found_file))[0]
    cands = [os.path.join(src_dir, safe_name + "-封面.jpg"),
             os.path.join(src_dir, stem + "-封面.jpg")]
    try:
        for f in os.listdir(src_dir):
            if f.lower().endswith((".jpg", ".jpeg", ".png")) and "封面" in f:
                p = os.path.join(src_dir, f)
                if p not in cands:
                    cands.append(p)
    except OSError:
        pass
    return [p for p in cands if os.path.isfile(p)]


def _sibling_bundle_dirs(output_dir, safe_name):
    """输出目录 + 剪映去重产生的同级「剧名(1)/(2)…」目录。"""
    output_dir = os.path.abspath(output_dir)
    dirs = [output_dir]
    parent = os.path.dirname(output_dir)
    base = os.path.basename(output_dir)
    # 只认「剧名(n)」形式，避免剧名本身带括号时误伤其它目录
    dup_pattern = re.compile(r"^" + re.escape(base) + r"\(\d+\)$")
    try:
        for name in os.listdir(parent):
            p = os.path.join(parent, name)
            if os.path.isdir(p) and name != base and dup_pattern.match(name):
                dirs.append(p)
    except OSError:
        pass
    return dirs


def _temp_export_dirs(output_dir):
    """剪映导出临时目录：导出中的字节先落在 .__jianying_export_temp_folder__ 里，
    导出结束才落到最终路径。返回输出目录及其父目录下存在的临时目录。"""
    out = os.path.abspath(output_dir)
    dirs = []
    for base in (out, os.path.dirname(out)):
        if not base:
            continue
        p = os.path.join(base, ".__jianying_export_temp_folder__")
        if os.path.isdir(p) and p not in dirs:
            dirs.append(p)
    return dirs


def _watchdog_size(real_export_file, output_dir, safe_name):
    """看门狗读取的总体积：UI 路径 + 输出目录（递归，含目录内临时目录）
    + 剪映去重兄弟目录 + 导出根目录下的临时目录。任一处在长即视为有进度。"""
    out = os.path.abspath(output_dir)
    targets = []
    if real_export_file:
        targets.append(real_export_file)
    targets.append(out)
    targets.extend(_sibling_bundle_dirs(out, safe_name)[1:])
    targets.extend(d for d in _temp_export_dirs(out) if os.path.abspath(d) != out)
    seen, total = set(), 0
    for p in targets:
        ap = os.path.abspath(p)
        if ap in seen:
            continue
        seen.add(ap)
        total += _export_progress_size(ap)
    return total


def _adopt_growing_mp4(output_dir, safe_name, min_mtime, current=None):
    """UI ExportPath 失效时接管真实产物：在输出目录、去重兄弟目录与导出临时目录里
    找本次新生成的 mp4，取最大者。"""
    best, best_size = None, 0
    for d in _sibling_bundle_dirs(output_dir, safe_name) + _temp_export_dirs(output_dir):
        if not os.path.isdir(d):
            continue
        for root, _subdirs, files in os.walk(d):
            for name in files:
                if not name.lower().endswith(".mp4"):
                    continue
                p = os.path.join(root, name)
                if current and os.path.abspath(p) == os.path.abspath(current):
                    continue
                if _mtime(p) < min_mtime:
                    continue
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size > best_size:
                    best, best_size = p, size
    return best


def _prune_bundle_dups(output_dir, safe_name):
    """删除剪映去重留下的空壳目录（无 mp4、无封面）；含成品/封面的一律保留并提醒。"""
    output_dir = os.path.abspath(output_dir)
    for d in _sibling_bundle_dirs(output_dir, safe_name):
        if os.path.abspath(d) == output_dir:
            continue
        keep = False
        for _root, _dirs, files in os.walk(d):
            for name in files:
                if name.lower().endswith((".mp4", ".jpg", ".jpeg", ".png", ".webp")) or "封面" in name:
                    keep = True
        if keep:
            dprint(f"⚠️ 保留非空去重目录（含成品/封面）: {d}")
            continue
        shutil.rmtree(d, ignore_errors=True)
        dprint(f"🧹 已清理空壳去重目录: {d}")


def _salvage_existing_output(draft_name, output_path, expected_duration, min_mtime=None, extra_dirs=None):
    """导出报错后的成品抢救：剪映可能在报错前已写出成品（含同名文件夹形态）。
    搜索范围：目标目录、目标目录的父目录（兼容「导出根目录/剧名子目录」与平铺两种布局）、
    以及调用方通过 extra_dirs 指定的额外目录；校验（时长+音轨）通过则归位为最终文件并视为成功。"""
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if not expected_duration:
        dprint("⚠️ 无预期时长，跳过成品抢救（无法校验完整性，避免搬错上次残留）")
        return False
    search_dirs = [out_dir, os.path.dirname(out_dir)]
    for d in (extra_dirs or []):
        search_dirs.append(os.path.abspath(d))
    seen, uniq_dirs = set(), []
    for d in search_dirs:
        if d not in seen:
            seen.add(d)
            uniq_dirs.append(d)
    for search_dir in uniq_dirs:
        cand = _find_export_in_dir(search_dir, draft_name, min_mtime=min_mtime)
        if not cand:
            continue
        # 校验：时长 + 音轨
        dur = _ffprobe_duration(cand)
        if dur is None or abs(dur - expected_duration) > 2:
            continue
        if not _ffprobe_has_audio(cand):
            continue
        try:
            stem = os.path.splitext(os.path.basename(output_path))[0]
            if os.path.abspath(cand) != os.path.abspath(output_path):
                shutil.move(cand, output_path)
            for cover_src in _cover_candidates(os.path.dirname(cand), cand, draft_name):
                dst_cover = os.path.join(out_dir, stem + "-封面.jpg")
                if os.path.abspath(cover_src) != os.path.abspath(dst_cover):
                    shutil.move(cover_src, dst_cover)
                break
            leftover = os.path.dirname(cand)
            if leftover != out_dir and os.path.isdir(leftover) and not os.listdir(leftover):
                os.rmdir(leftover)
        except OSError:
            continue
        dprint(f"✅ 抢救到已完成成品: {cand}（校验通过）")
        return True
    return False


def _restart_jianying_to_home(exporter):
    """导出成功后重启剪映回首页（兜底手段，优选是 switch_to_home 点关闭按钮）。

    仅在关闭按钮失效时使用：导出已完成落盘或导出任务未开始，杀进程安全。
    """
    dprint("重启剪映以回到首页...")
    jy_path = exporter._find_jianying_path()
    subprocess.run(["taskkill", "/F", "/IM", "JianyingPro.exe"], capture_output=True)
    time.sleep(2)
    subprocess.Popen([jy_path])
    for _ in range(30):  # 最多等 60s 首页出现
        time.sleep(2)
        try:
            win = uia.WindowControl(searchDepth=1, SubName="剪映")
            if win.Exists(1) and "HomePage" in (win.ClassName or ""):
                dprint("✅ 剪映已重启并回到首页")
                return
        except Exception:
            pass
    dprint("⚠️ 剪映已重启，但未检测到首页（可能仍在启动中，不影响导出结果）")


def _find_draft_dir(draft_name):
    """定位剪映草稿目录（JY_PROJECTS_ROOT 优先，其次 %LOCALAPPDATA% 默认位置）。"""
    root = os.environ.get("JY_PROJECTS_ROOT", "").strip() or os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "JianyingPro", "User Data", "Projects", "com.lveditor.draft")
    candidate = os.path.join(root, draft_name)
    if os.path.isdir(candidate):
        return candidate
    return None


def _restore_streams_from_backup(draft_name):
    """导出前检查草稿是否被剪映合并了串流 segments，有的话用 pipeline 备份恢复。

    剪映 5.9 打开草稿时会自动把同轨首尾相连的 video segments 合并成 1 个大段
    （"串流视频"优化），合并时丢失 crop 后的 source_timerange.duration，
    导致音画不同步。pipeline 发布时会在草稿目录写一份 _pipeline_backup.json，
    本函数在打开草稿前比对，如果发现 video/audio 轨段数骤降则恢复。
    """
    import json
    draft_dir = _find_draft_dir(draft_name)
    if not draft_dir:
        return False
    content_path = os.path.join(draft_dir, "draft_content.json")
    backup_path = os.path.join(draft_dir, "_pipeline_backup.json")
    if not os.path.exists(backup_path):
        return False

    try:
        with open(content_path, "r", encoding="utf-8") as f:
            cur = json.load(f)
        with open(backup_path, "r", encoding="utf-8") as f:
            bak = json.load(f)
    except Exception as e:
        dprint(f"⚠️ 读取草稿/备份失败: {e}")
        return False

    def _seg_counts(d):
        cnts = {}
        for t in d.get("tracks", []):
            if t.get("type") in ("video", "audio"):
                cnts[t["type"]] = cnts.get(t["type"], 0) + len(t.get("segments") or [])
        return cnts

    cur_cnt = _seg_counts(cur)
    bak_cnt = _seg_counts(bak)
    needs_restore = False
    for k in bak_cnt:
        if cur_cnt.get(k, 0) < bak_cnt[k] / 2:  # 骤降 >50% 认为被合并
            needs_restore = True
            break

    if not needs_restore:
        dprint(f"🛡️ 草稿轨道正常: 当前 {cur_cnt} vs 备份 {bak_cnt}，无需恢复")
        return False

    # 恢复 video + audio tracks（字幕轨/贴纸轨/材质等保持当前版本）
    # 把当前 draft 里所有 video/audio tracks 整体替换成备份里的对应 tracks
    restore_types = {"video", "audio"}
    bak_tracks_by_type = {t.get("type", ""): [] for t in bak.get("tracks", [])
                          if t.get("type") in restore_types}
    for t in bak.get("tracks", []):
        if t.get("type") in restore_types:
            bak_tracks_by_type[t["type"]].append(t)

    cur_tracks = cur.get("tracks", [])
    new_tracks = []
    restored = 0
    bak_idx = {t: 0 for t in bak_tracks_by_type}
    for t in cur_tracks:
        ttype = t.get("type", "")
        if ttype in restore_types and bak_tracks_by_type.get(ttype):
            bi = bak_idx[ttype]
            if bi < len(bak_tracks_by_type[ttype]):
                cur_n = len(t.get("segments") or [])
                bak_n = len(bak_tracks_by_type[ttype][bi].get("segments") or [])
                if cur_n < bak_n / 2:
                    new_tracks.append(bak_tracks_by_type[ttype][bi])
                    restored += 1
                else:
                    new_tracks.append(t)
                bak_idx[ttype] = bi + 1
            else:
                new_tracks.append(t)
        else:
            new_tracks.append(t)
    cur["tracks"] = new_tracks

    # 写前备份当前草稿（保留一份 .before_restore.bak），备份失败则不恢复，避免改坏草稿
    try:
        shutil.copy2(content_path, content_path + ".before_restore.bak")
    except OSError as e:
        dprint(f"⚠️ 恢复前备份失败，保守起见跳过恢复: {e}")
        return False

    with open(content_path, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False)
    dprint(f"🛡️ 草稿轨道已恢复: {restored} 条轨 从备份还原，"
           f"当前 video/audio segments: {_seg_counts(cur)}")
    return True


def run(draft_name, output_path, move_output=False, expected_duration=None,
        keep_open=False, fallback_dir=None):
    _TASK.update(draft=draft_name, output=output_path, move=move_output,
                 status="running", stage="准备", start_time=time.time(),
                 expected_duration=expected_duration, real_file=None)
    dprint(f"任务: 导出【{draft_name}】 -> {output_path}（搬运={'开' if move_output else '关'}）")
    _flow(f"任务启动：导出【{draft_name}】→ {output_path}（搬运={'开' if move_output else '关'}，"
          f"最多 {_TASK['attempts_max']} 次尝试）")

    # 1. 素材预检：源文件逐个 ffprobe，坏素材在启动剪映之前就报出来（不进重试）
    if not _PREFLIGHT_DONE[0]:
        _PREFLIGHT_DONE[0] = True
        _TASK["stage"] = "素材预检"
        ok_n, problems = _preflight_media(draft_name)
        if problems:
            brief = "、".join(os.path.splitext(os.path.basename(p))[0]
                             for p, _why in problems[:12])
            tail = f" 等 {len(problems)} 个" if len(problems) > 12 else ""
            _TASK["errors"].append(
                "素材预检不通过：\n" + "\n".join(
                    f"  - {p}  {why}" for p, why in problems))
            _TASK["stage"] = "素材预检失败"
            _flow(f"素材预检不通过：{len(problems)} 个源文件损坏（{brief}{tail}）")
            raise Exception(f"素材预检不通过：{len(problems)} 个源文件损坏或缺失（{brief}{tail}）")
        dprint(f"✅ 素材预检通过: {ok_n} 个源文件均可解码")
        _flow(f"素材预检通过：{ok_n} 个源文件均可正常解码")

    exporter = Exporter()

    # 2. 确保在首页
    if exporter.is_edit_page():
        dprint("检测到在编辑页，将尝试先导出当前草稿（如果名字匹配）或切回首页")
        # 这里简单处理：切回首页重来，确保状态一致
        exporter.switch_to_home()

    # 3. 导出前修复：剪映可能在 pipeline 发布后打开过草稿，
    # 自动合并串流 video segments 导致 crop 元数据丢失 → 音画不同步
    try:
        restored = _restore_streams_from_backup(draft_name)
        _flow("导出前轨道检查：已从备份恢复串流片段" if restored else "导出前轨道检查：轨道正常，无需恢复")
    except Exception as e:
        dprint(f"⚠️ 恢复检查失败（继续导出）: {e}")
        _flow(f"导出前轨道检查失败（继续导出）: {e}")

    # 2. 打开草稿
    _TASK["stage"] = "打开草稿"
    exporter.open_draft(draft_name)
    _flow("已进入编辑页")

    # 2.5 磁盘预检（移植自老脚本）：目标盘余量不足预估成品 1.2 倍时直接拒绝启动
    _check_disk_space(output_path, expected_duration)

    # 3. 导出（剪映导出到它自己记住的路径，脚本负责定位与校验；
    #    move_output=True 时再把成品+封面搬到 output_path）
    export_started = time.time()
    try:
        exporter.export(output_path, draft_name,
                        expected_duration=expected_duration, move_output=move_output)
    except Exception as e:
        # 异常抢救（移植自老脚本）：剪映可能在报错前已写出完整成品，校验通过则视为成功
        dprint(f"⚠️ 导出流程异常: {e}，尝试抢救成品...")
        _TASK["errors"].append(f"导出流程异常: {e}")
        if _salvage_existing_output(draft_name, output_path, expected_duration,
                                    min_mtime=export_started - 5,
                                    extra_dirs=[fallback_dir] if fallback_dir else None):
            dprint("✅ 成品已抢救归位，本次导出视为成功")
            _TASK.update(status="success", stage="抢救成功")
            _flow("异常抢救成功：成品已归位，本次导出视为成功")
            _flush_task(done=True, final_file=output_path)
        else:
            raise

    # 4. 收尾：回首页（优选关闭编辑页，失败自动重启兜底）。导出成功后界面停在编辑页/导出视图，
    #    看起来像"卡死在导出界面"；失败不影响导出结果（成品已落盘校验）。
    if not keep_open:
        try:
            exporter.switch_to_home()
        except Exception as e:
            dprint(f"⚠️ 导出后回首页失败（不影响导出结果）: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="剪映自动化导出工具")
    parser.add_argument("draft_name", help="草稿名称")
    parser.add_argument("output_path", help="目标文件路径")
    parser.add_argument("--move", action="store_true",
                        help="导出后把成品+封面搬运到目标路径（默认关闭，保留剪映原生目录结构）")
    parser.add_argument("--expect-duration", type=float, default=None, metavar="秒",
                        help="草稿预期时长（秒），提供时校验成品时长误差<2s，不符则判失败")
    parser.add_argument("--keep-open", action="store_true",
                        help="导出成功后保持剪映当前页面（默认重启剪映回首页）")
    parser.add_argument("--fallback-dir", default=None, metavar="目录",
                        help="成品抢救时的额外搜索目录（默认自动包含目标目录的父目录）")
    parser.add_argument("--retries", type=int, default=2,
                        help="UI 类失败重试次数（默认 2，即共 3 次尝试；非 UI 错误不重试）")

    args = parser.parse_args()

    # 重试循环（移植自老脚本）：UI 类失败 → 回首页（内含关闭按钮/重启兜底）→ 重试
    attempts = 1 + max(0, args.retries)
    _TASK.update(draft=args.draft_name, output=args.output_path, move=args.move,
                 attempts_max=attempts, start_time=time.time())
    for attempt in range(1, attempts + 1):
        _TASK["attempt"] = attempt
        try:
            if attempt > 1:
                _flow(f"开始第 {attempt}/{attempts} 次尝试")
            run(args.draft_name, args.output_path,
                move_output=args.move, expected_duration=args.expect_duration,
                keep_open=args.keep_open, fallback_dir=args.fallback_dir)
            sys.exit(0)
        except Exception as e:
            reason = str(e)
            ui_fail = any(k in reason for k in _UI_RETRY_KEYWORDS)
            if not ui_fail or attempt >= attempts:
                _TASK["errors"].append(f"[尝试 {attempt}] {reason}")
                _TASK.update(status="failed", stage="失败")
                _flow(f"任务失败（第 {attempt}/{attempts} 次尝试后停止）：{reason[:160]}")
                dprint(f"❌ 错误: {reason}")
                dprint("-" * 40)
                dprint("💡 建议：如果脚本无法定位窗口或草稿，请尝试【手动重启剪映程序】后再运行。")
                dprint("-" * 40)
                dprint(traceback.format_exc())
                _flush_task()
                if sys.stdin.isatty():
                    input("按回车键退出...")
                sys.exit(1)
            _TASK["errors"].append(f"[尝试 {attempt}] {reason}")
            _flow(f"第 {attempt} 次尝试失败：{reason[:160]}，回首页后重试")
            dprint(f"⚠️ 导出 UI 失败（{reason}），回首页后重试（{attempt}/{attempts - 1}）...")
            try:
                Exporter().switch_to_home()  # 新实例重绑窗口；内部失败自动重启剪映兜底
            except Exception as e2:
                dprint(f"⚠️ 回首页恢复失败（仍将继续重试）: {e2}")
            time.sleep(5)
