"""产物新鲜度覆盖率自检：每个「写导出成功」的出口都必须接上 record_export。

为什么需要它：sidecar 只在 record_export 里写。如果某个导出出口调了
`es.succeed` / `jm.record_video` / `clear_export_stale` 却没接 record_export，
产物就会缺 sidecar；此时 job 台账已登记 exported，正好落进 evidence_missing
（线上会告警，但那是事后）。本测试把「漏接一个出口」提前到 CI 里变红。

做法：AST 静态扫描 lib/ 与 pipeline/，对每个含导出成功标记的函数，
要求它自身或其直接调用的同文件助手里有 record_export。

只读源码，不碰 <素材根>、不启动剪映、不跑导出。
直接运行：python tests/test_freshness_coverage.py
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("lib", "pipeline")

# 写「导出成功」的调用标记：(函数名, 接收者)。接收者 None 表示裸调用。
EXPORT_WRITE = {
    ("succeed", "es"),
    ("succeed", "export_state"),
    ("record_video", None),
    ("record_video", "jm"),
    ("record_video", "job_manifest"),
    ("clear_export_stale", None),
    ("clear_export_stale", "pl"),
    ("clear_export_stale", "jp"),
    ("clear_export_stale", "pipeline"),
}

# 显式豁免：必须精确命中（文件, 函数名），且该函数确实含导出成功标记。
# 多一条或少一条都会让测试红，防止豁免清单悄悄过期。
EXEMPT = {
    ("lib/cover_watch.py", "note_export_result"):
        "导出状态真源写入器本体（成功/失败共用），不产生新成品",
    ("lib/job_manifest.py", "_main"):
        "CLI record-video 人工补登记，不伪造 sidecar",
}

_MODULE = "<module>"


def _call_name(node):
    fn = node.func
    if isinstance(fn, ast.Name):
        return None, fn.id
    if isinstance(fn, ast.Attribute):
        recv = fn.value.id if isinstance(fn.value, ast.Name) else None
        return recv, fn.attr
    return None, None


def _enclosing_func(node, parents):
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
    return None


def _new_info(name):
    return {"name": name, "markers": [], "calls": set(),
            "has_record_export": False}


def scan_source(src, filename="<mem>"):
    """返回 {函数节点(或哨兵字符串): info}。

    info.markers        该函数内出现的导出成功标记
    info.has_record_export  自身或同文件助手（可传递）调用了 record_export
    """
    tree = ast.parse(src, filename=filename)
    parents = {}
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            parents[c] = p

    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node] = _new_info(node.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        recv, name = _call_name(node)
        owner = _enclosing_func(node, parents)
        info = funcs[owner] if owner is not None else funcs.setdefault(
            _MODULE, _new_info(_MODULE))
        if name:
            info["calls"].add(name)
        if (name, recv) in EXPORT_WRITE:
            info["markers"].append((name, recv))
        if name == "record_export":
            info["has_record_export"] = True

    by_name = {}
    for info in funcs.values():
        if info["name"] != _MODULE:
            by_name.setdefault(info["name"], []).append(info)

    # 单层 + 传递解析：导出函数调用的同文件助手若带 record_export，视为已接
    changed = True
    while changed:
        changed = False
        for info in funcs.values():
            if info["has_record_export"]:
                continue
            for callee in info["calls"]:
                if any(h["has_record_export"] for h in by_name.get(callee, [])):
                    info["has_record_export"] = True
                    changed = True
                    break
    return funcs


def _iter_py_files():
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def collect_violations():
    """返回 (violations, seen_exempt, marker_funcs)。

    violations:   [(relpath, func_name, markers)] 未接 record_export 的导出出口
    seen_exempt:  {(relpath, func_name)} 实际命中的豁免
    marker_funcs: {(relpath, func_name)} 所有含导出标记的函数（用于断言豁免有效）
    """
    violations, seen_exempt, marker_funcs = [], set(), set()
    for path in _iter_py_files():
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        with open(path, encoding="utf-8-sig") as fp:
            src = fp.read()
        for info in scan_source(src, rel).values():
            if not info["markers"]:
                continue
            ident = (rel, info["name"])
            marker_funcs.add(ident)
            if info["has_record_export"]:
                continue
            if ident in EXEMPT:
                seen_exempt.add(ident)
                continue
            violations.append((rel, info["name"], info["markers"]))
    return violations, seen_exempt, marker_funcs


# ━━━━━━━━━━━━━━━━ 测试 ━━━━━━━━━━━━━━━━

def test_scanner_detects_missing_record_export():
    """扫描器自检：有导出标记、无 record_export → 必须被抓到。"""
    src = (
        "import export_state as es\n"
        "def leaky(job_dir):\n"
        "    es.succeed(job_dir, profile='a')\n"
    )
    infos = list(scan_source(src).values())
    assert len(infos) == 1, infos
    assert infos[0]["markers"], infos
    assert not infos[0]["has_record_export"], infos


def test_scanner_accepts_same_file_helper():
    """扫描器自检：出口经同文件助手调 record_export → 不算漏。"""
    src = (
        "import export_state as es\n"
        "import freshness\n"
        "def _rec(job_dir, mp4):\n"
        "    freshness.record_export(job_dir, 'a', 'd', mp4)\n"
        "def ok(job_dir, mp4):\n"
        "    es.succeed(job_dir)\n"
        "    _rec(job_dir, mp4)\n"
    )
    infos = {i["name"]: i for i in scan_source(src).values()}
    assert infos["_rec"]["has_record_export"], infos
    assert infos["ok"]["markers"], infos
    assert infos["ok"]["has_record_export"], infos


def test_scanner_catches_bare_clear_export_stale():
    """扫描器自检：裸调用 clear_export_stale 也是导出出口。"""
    src = (
        "def f(draft_path):\n"
        "    clear_export_stale(draft_path)\n"
    )
    infos = list(scan_source(src).values())
    assert infos and infos[0]["markers"], infos


def test_repo_export_exits_are_covered():
    """仓库内所有导出成功出口都接了 record_export（或显式豁免）。"""
    violations, _seen, _markers = collect_violations()
    assert not violations, (
        "以下导出出口调了导出成功写入却没接 record_export（产物会缺 sidecar，"
        "线上将报 evidence_missing）：\n" +
        "\n".join(f"  {rel}::{fn}  {marks}" for rel, fn, marks in violations)
    )


def test_exempt_list_is_exact():
    """豁免清单必须与代码精确对应：不多、不少、且函数确实含标记。"""
    violations, seen, markers = collect_violations()
    assert not violations, violations
    missing = set(EXEMPT) - seen
    assert not missing, f"豁免清单里有失效条目（函数已无导出标记或改名）: {missing}"
    assert seen == set(EXEMPT), f"出现未登记的豁免: {seen - set(EXEMPT)}"
    assert set(EXEMPT).issubset(markers), "豁免函数未出现在标记函数集合中"


def main():
    tests = [test_scanner_detects_missing_record_export,
             test_scanner_accepts_same_file_helper,
             test_scanner_catches_bare_clear_export_stale,
             test_repo_export_exits_are_covered,
             test_exempt_list_is_exact]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"OK {len(tests)} tests")


if __name__ == "__main__":
    main()
