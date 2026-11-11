# -*- coding: utf-8 -*-
"""
模拟右键菜单操作并 dump
"""
import json, sys, time, os
import pyautogui
import ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(1)
import uiautomation as uia

PROPERTY_ID_FULLDESC = 30159


def _resolve_prop(ctrl, prop_id):
    try: return ctrl.GetPropertyValue(prop_id)
    except Exception: return None


def _dump_to_list(ctrl, depth, max_depth, nodes_out):
    if depth > max_depth: return
    try: name = ctrl.Name or ""
    except Exception: name = ""
    try: cls = ctrl.ClassName or ""
    except Exception: cls = ""
    full_desc = _resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or ""
    try: ctrl_type = ctrl.ControlType
    except Exception: ctrl_type = None
    try:
        r = ctrl.BoundingRectangle
        rect_tup = (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        rect_tup = None
    nodes_out.append({
        "depth": depth, "type": ctrl_type, "class": cls,
        "name": name, "full_desc": full_desc, "rect": rect_tup,
    })
    try:
        for child in ctrl.GetChildren():
            _dump_to_list(child, depth + 1, max_depth, nodes_out)
    except Exception: pass


def _find_rect(ctrl, target_desc_lower):
    fd = (_resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or "").lower()
    if target_desc_lower in fd:
        try:
            r = ctrl.BoundingRectangle
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception: return None
    try:
        for child in ctrl.GetChildren():
            r = _find_rect(child, target_desc_lower)
            if r: return r
    except Exception: pass
    return None


def find_jianying():
    for hint in ["剪映专业版", "剪映", "剪映5"]:
        try:
            win = uia.WindowControl(searchDepth=1, Name=hint)
            if win.Exists(0): return win
        except Exception: continue
    return None


def dump_all(label):
    results = {}
    win = find_jianying()
    if win:
        win.SetActive()
        nodes = []
        _dump_to_list(win, 0, 15, nodes)
        results["main"] = nodes
    for w in uia.GetRootControl().GetChildren():
        try:
            if w.Name == win.Name: continue
            cls = w.ClassName or ""
            if not cls: continue
            if any(kw in cls.lower() for kw in ["qml", "qt", "menu", "popup", "tool", "tip"]):
                nodes = []
                _dump_to_list(w, 0, 15, nodes)
                if nodes:
                    results[f"top:{w.Name or '?'}:{cls}"] = nodes
        except Exception: pass

    print(f"\n{'='*60}")
    print(f"[{label}]  唯一 Desc 列表:")
    all_descs = set()
    for k, nodes in results.items():
        for n in nodes:
            fd = n.get("full_desc")
            if fd: all_descs.add(fd)
    for d in sorted(all_descs):
        print(f"  {d}")
    print(f"  (共 {len(all_descs)} 个唯一 Desc)")
    return results


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
        except Exception:
            pass
    try:
        for child in ctrl.GetChildren():
            if _click_recursive(child, target): return True
    except Exception: pass
    return False


def main():
    win = find_jianying()
    if not win:
        print("❌ 未找到剪映")
        sys.exit(1)
    win.SetActive()
    win.SetTopmost(True)
    time.sleep(0.3)
    win.SetTopmost(False)

    print("\n[1] 选中第1集视频段 ...")
    click_by_desc(win, "mtlsvideop:第1集")
    time.sleep(1)

    print("[2] Ctrl+A 全选时间线 ...")
    win.SendKeys('^a')
    time.sleep(1)

    print("[3] 右键 MainTimeLineRoot ...")
    rect = _find_rect(win, "maintimeroot")
    if rect:
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        print(f"  时间线中心: ({cx}, {cy})")
        pyautogui.rightClick(cx, cy)
        time.sleep(2)
        dump_all("时间线右键后（菜单应打开）")
    else:
        print("❌ 找不到时间线矩形")

    print("\n[4] 视频段上右键 ...")
    rect2 = _find_rect(win, "mtlsvideop:第1集")
    if rect2:
        cx, cy = (rect2[0] + rect2[2]) // 2, (rect2[1] + rect2[3]) // 2
        pyautogui.rightClick(cx, cy)
        time.sleep(2)
        dump_all("视频段右键后")


if __name__ == "__main__":
    main()
