# -*- coding: utf-8 -*-
"""
多状态自动 dump
================
自动操作剪映 → 点开关键面板 → 分别 dump 控件树 → 聚合输出。
"""

import json
import sys
import time
import os

import ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(1)

import uiautomation as uia

PROPERTY_ID_FULLDESC = 30159


def _resolve_prop(ctrl, prop_id):
    try:
        return ctrl.GetPropertyValue(prop_id)
    except Exception:
        return None


def _dump_to_list(ctrl, depth, max_depth, nodes_out):
    if depth > max_depth:
        return
    try:
        name = ctrl.Name or ""
    except Exception:
        name = ""
    try:
        cls = ctrl.ClassName or ""
    except Exception:
        cls = ""
    full_desc = _resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or ""
    try:
        ctrl_type = ctrl.ControlType
    except Exception:
        ctrl_type = None
    try:
        rect = ctrl.BoundingRectangle
        rect_tup = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        rect_tup = None

    nodes_out.append({
        "depth": depth,
        "type": ctrl_type,
        "class": cls,
        "name": name,
        "full_desc": full_desc,
        "rect": rect_tup,
    })

    try:
        for child in ctrl.GetChildren():
            _dump_to_list(child, depth + 1, max_depth, nodes_out)
    except Exception:
        pass


def dump_state(win, label, depth=12):
    print(f"\n{'='*60}")
    print(f"[状态] {label}")
    print(f"{'='*60}")
    nodes = []
    _dump_to_list(win, 0, depth, nodes)
    print(f"共 {len(nodes)} 节点")

    # 打印有 Desc 的节点（关键字过滤）
    keywords = (
        "btn", "button", "text", "subtitle", "caption", "separate", "vocal",
        "export", "track", "panel", "input", "progress", "play", "titlebar",
        "edit", "vemain", "vetree", "treemain", "collecttitle", "infoview",
        "timeline", "mtls", "mtime", "player", "scrollbar", "shellbutton",
        "qmltype_6", "textinfo", "window", "dialog", "popup", "menu",
    )
    for n in nodes:
        fd = (n["full_desc"] or "").lower()
        if fd and any(k in fd for k in keywords):
            rect = n["rect"]
            rect_s = f"{rect}" if rect else "N/A"
            print(f"  ★ [{n['type']}] Class={n['class']!r} Desc={n['full_desc']!r} Rect={rect_s}")

    return nodes


def find_jianying():
    for hint in ["剪映专业版", "剪映", "剪映5"]:
        try:
            win = uia.WindowControl(searchDepth=1, Name=hint)
            if win.Exists(0):
                return win
        except Exception:
            continue
    return None


def click_by_desc(win, desc_substr, label="", timeout=10):
    """按 FullDescription 子串找 QQuickText 节点并点击。返回是否成功。"""
    target = desc_substr.lower()
    end_t = time.time() + timeout
    while time.time() < end_t:
        try:
            # 遍历找 QQuickText
            for child in win.GetChildren():
                result = _click_recursive(child, target)
                if result:
                    print(f"  ✅ 点击成功: {label or desc_substr}")
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    print(f"  ❌ 未找到: {label or desc_substr}")
    return False


def _click_recursive(ctrl, target_desc_lower):
    """递归查找并点击第一个 Description 包含 target 的节点"""
    fd = (_resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or "").lower()
    if target_desc_lower in fd:
        try:
            ctrl.Click(simulateMove=False)
            return True
        except Exception:
            # 有时需要点父节点（按钮容器）
            parent = ctrl.GetParentControl()
            if parent:
                try:
                    parent.Click(simulateMove=False)
                    return True
                except Exception:
                    pass

    try:
        for child in ctrl.GetChildren():
            r = _click_recursive(child, target_desc_lower)
            if r:
                return True
    except Exception:
        pass
    return False


def main():
    win = find_jianying()
    if not win:
        print("❌ 未找到剪映窗口")
        sys.exit(1)
    win.SetActive()
    win.SetTopmost(True)
    time.sleep(0.3)
    win.SetTopmost(False)

    all_data = {}

    # === 状态 1: 初始状态 ===
    all_data["1_initial"] = dump_state(win, "初始编辑页（默认文本面板）")
    time.sleep(1)

    # === 状态 2: 点击"智能字幕" ===
    print("\n[操作] 点击 VETreeMainCellItem:智能字幕 ...")
    click_by_desc(win, "vetreemaincellitem:智能字幕", label="智能字幕")
    time.sleep(2)
    win = find_jianying()  # 重绑，面板切换可能重建控件树
    all_data["2_smart_subtitle_panel"] = dump_state(win, "点击智能字幕后")

    # === 状态 3: 点击时间线视频段 ===
    print("\n[操作] 点击时间线视频段 MTLSVideoP:第1集 ...")
    click_by_desc(win, "mtlsvideop:第1集", label="视频段 第1集")
    time.sleep(2)
    win = find_jianying()
    all_data["3_video_selected"] = dump_state(win, "选中视频段后（右侧属性面板应切换）")

    # === 状态 4: 点一个音频素材片段 ===
    print("\n[操作] 点击音频段 pipeline_sx ...")
    click_by_desc(win, "mtlsvideop:pipeline_sx", label="音频段")
    time.sleep(2)
    win = find_jianying()
    all_data["4_audio_selected"] = dump_state(win, "选中音频段后")

    # === 状态 5: 点击导出按钮看弹窗 ===
    print("\n[操作] 点击导出按钮 ...")
    click_by_desc(win, "mainwindowtitlebarexportbtn", label="导出")
    time.sleep(3)
    # 找导出弹窗（新窗口）
    export_win = None
    for w in uia.GetRootControl().GetChildren():
        try:
            if w.Name == "导出" or "Export" in (w.ClassName or ""):
                export_win = w
                break
        except Exception:
            pass
    if export_win:
        print(f"  找到导出弹窗: Title={export_win.Name!r} Class={export_win.ClassName!r}")
        all_data["5_export_dialog"] = dump_state(export_win, "导出弹窗")
    else:
        print("  未找到独立导出弹窗，可能是 MainWindow 内部弹出")
        all_data["5_export_dialog"] = dump_state(win, "点击导出后（主窗口）")

    # 关掉导出弹窗
    print("\n[操作] 关闭导出弹窗（按 Esc）...")
    uia.GetRootControl().SendKeys('{Esc}')
    time.sleep(1)

    # === 汇总 ===
    out_path = os.path.join(os.path.dirname(__file__), "dump_multi_state.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "states": {k: {"node_count": len(v), "nodes": v} for k, v in all_data.items()},
        }, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"✅ 多状态 dump 完成，共 {len(all_data)} 个状态")
    print(f"📄 输出: {out_path}")
    print(f"{'='*60}")

    # 关键发现汇总
    print("\n[关键标记汇总]")
    target_markers = [
        "智能字幕", "识别字幕", "识别歌词", "开始识别",
        "人声分离", "分离音频", "音频分离",
        "翻译", "translate", "subtitle", "caption",
        "track", "eye", "隐藏",
        "export", "导出", "progress",
    ]
    for state_label, nodes in all_data.items():
        found = set()
        for n in nodes:
            fd = (n["full_desc"] or "").lower()
            for tm in target_markers:
                if tm.lower() in fd:
                    found.add(n["full_desc"])
        if found:
            print(f"  [{state_label}] 发现:")
            for f in sorted(found):
                print(f"    - {f}")


if __name__ == "__main__":
    main()
