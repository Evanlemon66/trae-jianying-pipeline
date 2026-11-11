# -*- coding: utf-8 -*-
"""
剪映控件树 dump 工具
====================
递归遍历剪映窗口所有 UI 元素，导出 Name/ClassName/FullDescription/ControlType/BoundingRectangle。

用法:
    1. 先打开剪映，进入一个有视频素材的草稿（编辑页）
    2. py -3.12 pipeline_ui/dump_controls.py                    # 打印到控制台
    3. py -3.12 pipeline_ui/dump_controls.py -o dump.json       # 同时写 JSON 文件
    4. py -3.12 pipeline_ui/dump_controls.py -f -d 5            # 只看编辑页 + 深度 5

参数:
    -f, --filter-edit   只输出 MainWindow（编辑页）的子树
    -d, --depth N       最大递归深度 (默认 12)
    -o, --output FILE   额外写 JSON 文件
    -q, --quiet         不在控制台打印，只写文件
    --include-empty     包含 Name/Description 都为空的节点（默认跳过减少噪音）
    --window NAME       指定窗口标题（默认自动匹配"剪映专业版"或"剪映"）
"""

import argparse
import json
import sys
import time

import uiautomation as uia

# ================ 配置 ================
PROPERTY_ID_FULLDESC = 30159   # LegacyIAccessible.Description，剪映语义标记都在这


def _resolve_prop(ctrl, prop_id):
    """安全读取任意 UIA property，失败返回 None"""
    try:
        return ctrl.GetPropertyValue(prop_id)
    except Exception:
        return None


def _ctrl_type_name(t):
    """ControlType 枚举值 → 可读名"""
    try:
        return uia.ControlType.NameToControlType.get(t, str(t)) if isinstance(t, int) else str(t)
    except Exception:
        return str(t)


def _dump_node(ctrl, depth, max_depth, include_empty, lines, nodes_out):
    """递归 dump 单个节点及其子节点"""
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
        ctrl_type_val = ctrl.ControlType
    except Exception:
        ctrl_type_val = None
    ctrl_type_name = _ctrl_type_name(ctrl_type_val) if ctrl_type_val is not None else "Unknown"

    try:
        rect = ctrl.BoundingRectangle
        rect_str = f"({int(rect.left)},{int(rect.top)},{int(rect.right)},{int(rect.bottom)})"
    except Exception:
        rect_str = "N/A"

    # 噪音过滤：Name 和 FullDescription 都为空的叶子节点跳过
    if not include_empty and not name.strip() and not full_desc.strip():
        # 但父节点仍要处理，只是跳过这个节点
        pass
    else:
        indent = "  " * depth
        marker = ""
        # 高亮可能有语义的标记
        if full_desc and any(kw in full_desc.lower() for kw in (
            "btn", "button", "text", "subtitle", "caption", "separate", "vocal",
            "export", "track", "window", "panel", "input", "progress",
            "subtitle", "translate", "recogni", "ai", "smart"
        )):
            marker = " ★"

        line = f"{indent}[{ctrl_type_name}] Class={cls!r} Name={name!r} Desc={full_desc!r} Rect={rect_str}{marker}"
        lines.append(line)

        # JSON 节点
        nodes_out.append({
            "depth": depth,
            "type": ctrl_type_name,
            "class": cls,
            "name": name,
            "full_desc": full_desc,
            "rect": rect_str,
        })

    # 递归子节点
    try:
        children = ctrl.GetChildren()
        for child in children:
            try:
                _dump_node(child, depth + 1, max_depth, include_empty, lines, nodes_out)
            except Exception:
                pass
    except Exception:
        pass


def find_jianying_window(window_name_hint=None):
    """自动查找剪映窗口（编辑页优先，其次首页）"""
    hints = [window_name_hint] if window_name_hint else ["剪映专业版", "剪映", "剪映5"]
    for hint in hints:
        try:
            win = uia.WindowControl(searchDepth=1, Name=hint)
            if win.Exists(0):
                return win
        except Exception:
            continue

    # 兜底：遍历所有顶层窗口找包含"剪映"的
    try:
        for w in uia.GetRootControl().GetChildren():
            try:
                if "剪映" in (w.Name or ""):
                    return w
            except Exception:
                continue
    except Exception:
        pass

    return None


def main():
    parser = argparse.ArgumentParser(description="剪映 UI 控件树 dump")
    parser.add_argument("-f", "--filter-edit", action="store_true",
                        help="只输出 MainWindow（编辑页）子树，跳过首页元素")
    parser.add_argument("-d", "--depth", type=int, default=12,
                        help="最大递归深度 (默认 12)")
    parser.add_argument("-o", "--output", default=None, metavar="FILE",
                        help="额外写 JSON 文件")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="不在控制台打印，只写文件")
    parser.add_argument("--include-empty", action="store_true",
                        help="包含 Name/Description 都为空的节点")
    parser.add_argument("--window", default=None,
                        help="指定窗口标题")
    args = parser.parse_args()

    # 开启 DPI 感知
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    print("[*] 正在连接剪映窗口...")
    win = find_jianying_window(args.window)
    if not win:
        print("❌ 未找到剪映窗口，请先打开剪映并进入草稿编辑页")
        print("   可尝试: --window '剪映专业版' 手动指定窗口标题")
        sys.exit(1)

    win_class = win.ClassName or ""
    win_name = win.Name or ""
    print(f"✅ 已连接窗口: Title={win_name!r} Class={win_class!r}")

    if "HomePage" in win_class:
        print("⚠️  当前在首页，控件树会比较简单。建议先打开一个草稿进编辑页再 dump。")
    elif "MainWindow" in win_class:
        print("✅ 当前在编辑页（MainWindow），这正是我们需要的状态。")

    # 过滤：只取 MainWindow 子树
    root = win
    if args.filter_edit and "MainWindow" in win_class:
        root = win  # 已经是 MainWindow
    elif args.filter_edit and "HomePage" in win_class:
        print("⚠️  --filter-edit 已指定但当前在首页，将 dump 整个窗口")

    print(f"[*] 开始递归 dump（最大深度 {args.depth}，过滤空节点={not args.include_empty}）...")
    t0 = time.time()
    lines = []
    nodes = []
    _dump_node(root, 0, args.depth, args.include_empty, lines, nodes)
    elapsed = time.time() - t0

    summary = f"共 {len(nodes)} 个可见节点，耗时 {elapsed:.2f}s"

    if not args.quiet:
        print("\n" + "=" * 60)
        print(summary)
        print("=" * 60)
        for line in lines:
            print(line)

    if args.output:
        out = {
            "window": {"title": win_name, "class": win_class},
            "depth_max": args.depth,
            "include_empty": args.include_empty,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": round(elapsed, 2),
            "node_count": len(nodes),
            "nodes": nodes,
        }
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            print(f"\n✅ JSON 已写入: {args.output} ({len(nodes)} 节点)")
        except Exception as e:
            print(f"\n❌ 写文件失败: {e}")
            sys.exit(2)
    else:
        print(f"\n✅ {summary}")


if __name__ == "__main__":
    main()
