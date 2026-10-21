import sys, os, time, json
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.abspath(".trae/skills/jianying-editor/scripts"))
sys.path.insert(0, os.path.abspath(".trae/skills/jianying-editor/examples"))

import pyautogui
import uiautomation as uia

COORDS_PATH = os.path.abspath("jianying_coords.json")
DRAFT_ROOT = os.path.join(os.environ["LOCALAPPDATA"], "JianyingPro", "User Data", "Projects", "com.lveditor.draft")

def load_coords():
    with open(COORDS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def get_jianying_window():
    w = uia.WindowControl(searchDepth=1, ClassName="MainWindow_QMLTYPE_6263")
    if not w.Exists(2):
        w = uia.WindowControl(searchDepth=1, Name="剪映专业版")
    if not w.Exists(0):
        return None
    return w

def window_rect():
    w = get_jianying_window()
    if not w:
        raise RuntimeError("剪映窗口不存在")
    r = w.BoundingRectangle
    return {"left": r.left, "top": r.top, "W": r.right - r.left, "H": r.bottom - r.top}

def click_by_ratio(ratioX, ratioY, offset_x=0, offset_y=0, delay_after=0.8):
    r = window_rect()
    sx = int(r["left"] + r["W"] * ratioX + offset_x)
    sy = int(r["top"] + r["H"] * ratioY + offset_y)
    print(f"    🖱️ 点击 ({sx}, {sy}) ratio=({ratioX:.3f}, {ratioY:.3f})")
    pyautogui.click(sx, sy)
    time.sleep(delay_after)

def click_mark(name, offset_x=0, offset_y=0, delay_after=0.8):
    coords = load_coords()
    if name not in coords:
        raise KeyError(f"标记 {name} 不存在, 可用: {list(coords.keys())}")
    c = coords[name]
    click_by_ratio(c["ratioX"], c["ratioY"], offset_x, offset_y, delay_after)

def ensure_foreground():
    import ctypes
    hwnd = ctypes.windll.user32.FindWindowW("MainWindow_QMLTYPE_6263", "剪映专业版")
    if not hwnd:
        hwnd = ctypes.windll.user32.FindWindowW(None, "剪映专业版")
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)

def switch_panel(panel_name):
    MARK_MAP = {
        "video": "sidebar_video",
        "audio": "sidebar_audio",
        "text": "sidebar_text",
    }
    mark = MARK_MAP.get(panel_name, panel_name)
    print(f"  📂 切换面板 → {panel_name} ({mark})")
    click_mark(mark, delay_after=1.0)

def vocal_separation():
    print(f"  🎤 点击「人声分离」")
    click_mark("vocal_sep_btn", delay_after=1.5)

def separate_audio():
    print(f"  🎵 点击「分离音频」")
    click_mark("audio_sep_btn", delay_after=1.5)

def recognize_subtitles():
    print(f"  ✍️ 点击「识别字幕」")
    click_mark("subtitle_recog", delay_after=2.0)

def open_draft_by_ui(draft_name):
    from robust_auto_export import Exporter
    exp = Exporter()
    exp.connect()
    if exp.is_edit_page():
        exp.switch_to_home()
        time.sleep(2)
        exp.connect()
    exp.open_draft(draft_name)
    time.sleep(3)
    exp.connect()
    return exp.is_edit_page()

def close_export_popup_if_any(exp):
    try:
        close_btn = exp.window.TextControl(searchDepth=8, Compare=lambda c,d: False)
        for desc in ["ExportSucceedCloseBtn", "ExportOkBtn", "关闭", "取消"]:
            try:
                from robust_auto_export import ControlFinder
                btn = exp.window.TextControl(searchDepth=8, Compare=ControlFinder.desc_matcher(desc))
                if btn.Exists(0):
                    btn.Click(simulateMove=False)
                    time.sleep(0.5)
            except: pass
    except: pass

def open_export_dialog(exp):
    from robust_auto_export import ControlFinder
    export_btn = exp.window.TextControl(searchDepth=5, Compare=ControlFinder.desc_matcher("MainWindowTitleBarExportBtn"))
    if export_btn.Exists(0):
        export_btn.Click(simulateMove=False)
        time.sleep(2)
        return True
    return False

def click_export_confirm(exp, resolution="1080P", framerate="30fps"):
    from robust_auto_export import ControlFinder
    try:
        res_btn = exp.window.TextControl(searchDepth=10, Compare=ControlFinder.desc_matcher("ExportSharpnessInput"))
        if res_btn.Exists(0):
            res_btn.Click(simulateMove=False)
            time.sleep(0.5)
            res_item = exp.window.TextControl(searchDepth=5, Compare=ControlFinder.desc_matcher(resolution))
            if res_item.Exists(0):
                res_item.Click(simulateMove=False)
                time.sleep(0.5)
    except: pass
    
    confirm_btn = exp.window.TextControl(searchDepth=8, Compare=ControlFinder.desc_matcher("ExportOkBtn"))
    if confirm_btn.Exists(0):
        confirm_btn.Click(simulateMove=False)
        return True
    return False

def wait_for_vocal_separation(draft_name, timeout=600, poll_interval=10):
    import json as _json
    dc_path = os.path.join(DRAFT_ROOT, draft_name, "draft_content.json")
    
    def _check():
        if not os.path.exists(dc_path):
            return False
        try:
            with open(dc_path, "r", encoding="utf-8") as f:
                d = _json.load(f)
            seps = d.get("materials", {}).get("vocal_separations", [])
            if not seps:
                return False
            for s in seps:
                pp = s.get("production_path", "")
                if pp and os.path.exists(pp if not pp.startswith("\\") else os.path.join(os.path.dirname(dc_path), pp[1:])):
                    return True
                if pp:
                    return True
            return False
        except:
            return False
    
    print(f"  ⏳ 等待人声分离完成 (超时 {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        if _check():
            print(f"  ✅ 人声分离完成! ({time.time()-start:.0f}s)")
            return True
        time.sleep(poll_interval)
    print(f"  ⚠️ 人声分离等待超时")
    return False

def wait_for_subtitles(draft_name, timeout=600, poll_interval=10):
    import json as _json
    dc_path = os.path.join(DRAFT_ROOT, draft_name, "draft_content.json")
    
    def _check():
        if not os.path.exists(dc_path):
            return False
        try:
            with open(dc_path, "r", encoding="utf-8") as f:
                d = _json.load(f)
            texts = d.get("materials", {}).get("texts", [])
            if len(texts) > 1:
                return True
            cg = d.get("canvas_config", {})
            if cg.get("subtitle_recognition_id") or cg.get("subtitle_taskinfo"):
                return True
            return False
        except:
            return False
    
    print(f"  ⏳ 等待字幕识别完成 (超时 {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        if _check():
            print(f"  ✅ 字幕识别完成! ({time.time()-start:.0f}s)")
            return True
        time.sleep(poll_interval)
    print(f"  ⚠️ 字幕识别等待超时")
    return False
