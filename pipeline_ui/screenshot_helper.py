# -*- coding: utf-8 -*-
"""
剪映 UI 截图辅助：自动操作到目标状态 → 暂停 → 你截图 → 继续下一个
用法：py -3.12 pipeline_ui/screenshot_helper.py
"""
import sys, time
import ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(1)
import uiautomation as uia

PROPERTY_ID_FULLDESC = 30159


def _resolve_prop(ctrl, prop_id):
    try: return ctrl.GetPropertyValue(prop_id)
    except Exception: return None


def find_jianying():
    for hint in ["剪映专业版", "剪映", "剪映5"]:
        try:
            win = uia.WindowControl(searchDepth=1, Name=hint)
            if win.Exists(0): return win
        except Exception: continue
    return None


def click_by_desc(win, desc_substr, timeout=10):
    target = desc_substr.lower()
    end_t = time.time() + timeout
    while time.time() < end_t:
        try:
            if _click_recursive(win, target): return True
        except Exception: pass
        time.sleep(0.5)
    return False


def _click_recursive(ctrl, target):
    fd = (_resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or "").lower()
    if target in fd:
        try:
            ctrl.Click(simulateMove=False)
            return True
        except Exception: pass
    try:
        for child in ctrl.GetChildren():
            if _click_recursive(child, target): return True
    except Exception: pass
    return False


def pause(msg):
    print(f"\n⏸️  {msg}")
    input("   截完图按回车键继续...")


def main():
    win = find_jianying()
    if not win:
        print("❌ 未找到剪映窗口，请先打开剪映")
        sys.exit(1)
    win.SetActive()
    win.SetTopmost(True)
    time.sleep(0.3)
    win.SetTopmost(False)
    print(f"✅ 已连接剪映，当前窗口: {win.ClassName}")

    # === 截图点 1: 右键菜单 ===
    print("\n[场景1] 全选时间线 → 右键")
    click_by_desc(win, "mtlsvideop:第1集")
    time.sleep(0.5)
    win.SendKeys('^a')
    time.sleep(0.5)
    # 算时间线中心坐标
    target_desc = "maintimeroot"
    def find_rect(ctrl):
        fd = (_resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or "").lower()
        if target_desc in fd:
            try:
                r = ctrl.BoundingRectangle
                return (int(r.left) + int(r.right)) // 2, (int(r.top) + int(r.bottom)) // 2
            except Exception: return None
        try:
            for child in ctrl.GetChildren():
                r = find_rect(child)
                if r: return r
        except Exception: pass
        return None

    import pyautogui
    center = find_rect(win)
    if center:
        pyautogui.rightClick(*center)
        time.sleep(1)
        pause("右键菜单应该弹出了，请截图")

    # === 截图点 2: 选中视频段 ===
    print("\n[场景2] 选中视频段 → 看右侧面板")
    pyautogui.click(center[0], center[1])
    click_by_desc(win, "mtlsvideop:第1集")
    time.sleep(1.5)
    pause("右侧面板应显示视频属性，有哪些 Tab？请截图（全界面）")

    # === 截图点 3: 智能字幕面板 ===
    print("\n[场景3] 点击左栏智能字幕入口")
    click_by_desc(win, "vetreemaincellitem:智能字幕")
    time.sleep(1.5)
    pause("右侧应切换到智能字幕面板，请截图")


if __name__ == "__main__":
    main()
