# -*- coding: utf-8 -*-
import os
import sys
import time
import shutil
import argparse
import traceback
import ctypes
import psutil
import subprocess
import winreg

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


# ========== 进度可见性（进度文件 + 定时 toast） ==========
_last_progress_write = [0.0]
_last_progress_toast = [0.0]


def _estimate_percent(real_export_file, expected_duration):
    """体积估算百分比（封顶 99%）；无法估算时返回 None。"""
    if not expected_duration or expected_duration <= 0:
        return None
    est_total = expected_duration * _EST_BYTES_PER_SEC
    if est_total <= 0:
        return None
    cur = _export_progress_size(real_export_file)
    return f"约 {min(99, int(cur / est_total * 100))}%"


def _write_progress(output_path, real_export_file, expected_duration, start_time,
                    done=False, final_file=None):
    """进度文件：<output_path>.progress.json，每 30s 刷新；完成时 done:true。"""
    import json
    rec = {
        "done": done,
        "exported_mb": round(_export_progress_size(real_export_file) / 1048576, 1),
        "elapsed_s": int(time.time() - start_time),
        "percent": _estimate_percent(real_export_file, expected_duration),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if done and final_file:
        rec["final_mb"] = round(os.path.getsize(final_file) / 1048576, 1)
        rec["percent"] = "100%"
    try:
        with open(os.path.abspath(output_path) + ".progress.json", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _toast_safe(title, msg):
    """toast 通知（失败静默，不影响导出主流程）。"""
    try:
        _scripts = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "..", "scripts")
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
            for f in os.listdir(c):
                fp = os.path.join(c, f)
                if os.path.isfile(fp):
                    try: total += os.path.getsize(fp)
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
        """通过注册表或常用路径查找剪映执行文件（必须优先 5.9 版）"""
        # 0. 硬编码 5.9 版路径优先（注册表指向的是另一套 11.x 安装，会找不到草稿）
        exe_59 = r"D:\剪映5.9\剪映5.9版+jianying-skill\剪映5.9(win+mac)\剪映5.9Windows\JianyingPro\5.9.0.11632\JianyingPro.exe"
        if os.path.exists(exe_59):
            return exe_59
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
        user_profile = os.environ.get('USERPROFILE')
        paths = [
            os.path.join(user_profile, r"AppData\Local\JianyingPro\Apps\JianyingPro.exe"),
            r"C:\Users\Administrator\AppData\Local\JianyingPro\Apps\JianyingPro.exe"
        ]
        for p in paths:
            if os.path.exists(p): return p
            
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
                    dprint(f"检测到剪映真实导出位置: {real_export_file}")
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

            # 体积看门狗：产物 180 秒无增长判定卡死（含迟迟不落盘的情况）
            if real_export_file:
                cur = _export_progress_size(real_export_file)
                if cur > last_size:
                    last_size, last_growth = cur, time.time()
                elif time.time() - last_growth > 180:
                    raise Exception(f"导出卡死：产物体积 180s 无增长（{last_size/1048576:.1f}MB）: {real_export_file}")
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
        safe_name = "".join([c for c in draft_name if c not in r'\/:*?"<>|']).strip()
        output_dir = os.path.dirname(os.path.abspath(output_path))
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

        if not found_file:
            raise Exception(f"未找到导出成品（UI路径={real_export_file}，扫描={output_dir}）")

        dprint(f"找到导出原始文件: {found_file}")
        _write_progress(output_path, real_export_file, expected_duration,
                        start_time, done=True, final_file=found_file)
        _toast_safe("📤 导出成功", f"{draft_name}（{os.path.getsize(found_file)/1073741824:.2f}GB）")

        # 8. 校验：可读 + 有音轨 + 时长与草稿一致
        dur = _ffprobe_duration(found_file)
        if dur is None:
            raise Exception(f"成品无法读取（ffprobe 失败）: {found_file}")
        if not _ffprobe_has_audio(found_file):
            raise Exception(f"成品无音轨: {found_file}")
        if expected_duration is not None and abs(dur - expected_duration) > 2.0:
            raise Exception(f"成品时长 {dur:.1f}s 与草稿 {expected_duration:.1f}s 不符，疑似截断")

        # 9. 搬运开关：默认保留剪映原生结构（同名文件夹内含 mp4+封面），开启后才搬到目标路径
        if move_output:
            os.makedirs(output_dir, exist_ok=True)
            if os.path.abspath(found_file) != os.path.abspath(output_path):
                if os.path.isfile(output_path):
                    os.remove(output_path)
                shutil.move(found_file, output_path)
                # 顺带把封面搬到输出目录
                src_dir = os.path.dirname(found_file)
                cover = os.path.join(src_dir, safe_name + "-封面.jpg")
                if os.path.isfile(cover):
                    dst_cover = os.path.join(output_dir, safe_name + "-封面.jpg")
                    if os.path.isfile(dst_cover):
                        os.remove(dst_cover)
                    shutil.move(cover, dst_cover)
                # 搬空后清理剪映生成的同名空壳文件夹（仅当确实已空）
                if (os.path.isdir(src_dir)
                        and os.path.abspath(src_dir) != os.path.abspath(output_dir)
                        and not os.listdir(src_dir)):
                    os.rmdir(src_dir)
            dprint(f"🎉 导出完成（已搬运）: {output_path} 时长 {dur:.1f}s 音轨=有")
        else:
            src_dir = os.path.dirname(found_file)
            cover = os.path.join(src_dir, safe_name + "-封面.jpg")
            cover_note = f"，封面: {cover}" if os.path.isfile(cover) else ""
            dprint(f"🎉 导出完成（未搬运，保留剪映原生结构）: {found_file}{cover_note} 时长 {dur:.1f}s 音轨=有")
        return True


# 移植自老脚本 auto_exporter.py（2026-10-25）
_UI_RETRY_KEYWORDS = ("剪映窗口未找到", "导出按钮", "编辑窗口", "草稿", "未找到", "卡死", "超时")
_EST_BYTES_PER_SEC = 1.05 * 1024 * 1024  # 1080P 导出码率经验估值（字节/秒）


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


def _salvage_existing_output(draft_name, output_path, expected_duration, min_mtime=None):
    """导出报错后的成品抢救：剪映可能在报错前已写出成品（含同名文件夹形态）。
    在目标目录与剪映默认导出目录中搜索，校验（时长+音轨）通过则归位为最终文件并视为成功。"""
    out_dir = os.path.dirname(os.path.abspath(output_path))
    search_dirs = [out_dir, r"D:\油管打猎\L"]
    for search_dir in search_dirs:
        cand = _find_export_in_dir(search_dir, draft_name, min_mtime=min_mtime)
        if not cand:
            continue
        # 校验：时长（提供时）+ 音轨
        if expected_duration:
            dur = _ffprobe_duration(cand)
            if dur is None or abs(dur - expected_duration) > 2:
                continue
        if not _ffprobe_has_audio(cand):
            continue
        try:
            stem = os.path.splitext(os.path.basename(output_path))[0]
            if os.path.abspath(cand) != os.path.abspath(output_path):
                shutil.move(cand, output_path)
            cover_src = os.path.join(
                os.path.dirname(output_path) if os.path.dirname(cand) == out_dir else os.path.dirname(cand),
                draft_name + "-封面.jpg")
            if os.path.isfile(cover_src):
                shutil.move(cover_src, os.path.join(out_dir, stem + "-封面.jpg"))
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


def run(draft_name, output_path, move_output=False, expected_duration=None,
        keep_open=False):
    dprint(f"任务: 导出【{draft_name}】 -> {output_path}（搬运={'开' if move_output else '关'}）")
    exporter = Exporter()

    # 1. 确保在首页
    if exporter.is_edit_page():
        dprint("检测到在编辑页，将尝试先导出当前草稿（如果名字匹配）或切回首页")
        # 这里简单处理：切回首页重来，确保状态一致
        exporter.switch_to_home()

    # 2. 打开草稿
    exporter.open_draft(draft_name)

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
        if _salvage_existing_output(draft_name, output_path, expected_duration,
                                    min_mtime=export_started - 5):
            dprint("✅ 成品已抢救归位，本次导出视为成功")
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
    parser.add_argument("--retries", type=int, default=2,
                        help="UI 类失败重试次数（默认 2，即共 3 次尝试；非 UI 错误不重试）")

    args = parser.parse_args()

    # 重试循环（移植自老脚本）：UI 类失败 → 回首页（内含关闭按钮/重启兜底）→ 重试
    attempts = 1 + max(0, args.retries)
    for attempt in range(1, attempts + 1):
        try:
            run(args.draft_name, args.output_path,
                move_output=args.move, expected_duration=args.expect_duration,
                keep_open=args.keep_open)
            sys.exit(0)
        except Exception as e:
            reason = str(e)
            ui_fail = any(k in reason for k in _UI_RETRY_KEYWORDS)
            if not ui_fail or attempt >= attempts:
                dprint(f"❌ 错误: {reason}")
                dprint("-" * 40)
                dprint("💡 建议：如果脚本无法定位窗口或草稿，请尝试【手动重启剪映程序】后再运行。")
                dprint("-" * 40)
                dprint(traceback.format_exc())
                if sys.stdin.isatty():
                    input("按回车键退出...")
                sys.exit(1)
            dprint(f"⚠️ 导出 UI 失败（{reason}），回首页后重试（{attempt}/{attempts - 1}）...")
            try:
                Exporter().switch_to_home()  # 新实例重绑窗口；内部失败自动重启剪映兜底
            except Exception as e2:
                dprint(f"⚠️ 回首页恢复失败（仍将继续重试）: {e2}")
            time.sleep(5)
