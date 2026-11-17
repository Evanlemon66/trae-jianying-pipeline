# -*- coding: utf-8 -*-
"""仓库环境自检的命令行入口（只读）。

用法：
  python pipeline/doctor.py

检查逻辑与 Console 共用 pipeline/doctor_core.py；本文件只保留文本报告与
退出码。逐项检查：仓库布局、Python 与依赖、ffmpeg/ffprobe、剪映安装与版本、
剪映进程/桌面会话、各配置路径、草稿根是否已被剪映索引、watcher 计划任务与
运行状态（后两项为本机运维信息，未注册/未运行只警告，不构成失败）。

只做读取与探测：不启动/关闭剪映、不启停 watcher、不触发导出、不写任何文件。
退出码：全部通过或有警告 → 0；存在阻断性"缺失"项 → 1（UI OCR 依赖缺失只报不阻断）。
"""
from __future__ import annotations

from doctor_core import (  # noqa: F401  re-export：保持 `import doctor` 的调用方不变
    AI_MODULES,
    CORE_MODULES,
    CheckResult,
    DoctorReport,
    check_config_paths,
    check_dependencies,
    check_draft_index,
    check_ffmpeg,
    check_jianying_install,
    check_jianying_session,
    check_python,
    check_repo_layout,
    check_test_deps,
    check_watcher_runtime,
    check_watcher_task,
    run_checks,
)

_LABEL = {"pass": "通过", "warn": "警告", "fail": "缺失"}
_LABEL_NON_BLOCKING = "缺失·非阻断"


def _label(r: CheckResult) -> str:
    """非阻断的 fail 用单独标签，避免与阻断项混淆。"""
    if r.status == "fail" and not r.blocking:
        return _LABEL_NON_BLOCKING
    return _LABEL.get(r.status, r.status)


def format_report(report: DoctorReport) -> str:
    lines = ["=" * 64, "剪映仓库环境自检（只读：不启停剪映 / watcher，不跑导出）",
             f"仓库: {report.repo_root}", "=" * 64]
    for r in report.results:
        lines.append(f"[{_label(r)}] {r.title}: {r.detail}")
        if r.fix and r.status != "pass":
            lines.append(f"       修复: {r.fix}")
    n_pass = sum(1 for r in report.results if r.status == "pass")
    lines.append("-" * 64)
    lines.append(f"结论: {n_pass} 项通过 / {len(report.warned)} 项警告 / {len(report.missing)} 项缺失")
    if report.failed:
        lines.append("存在缺失项：补齐后重新运行 python pipeline/doctor.py")
    elif report.missing:
        lines.append("缺失项均不阻断主链路（仅影响 UI 自动化）；建议按提示补齐。")
    elif report.warned:
        lines.append("没有阻塞项；警告项建议按提示确认。")
    else:
        lines.append("环境就绪。")
    return "\n".join(lines)


def main(argv=None) -> int:
    report = run_checks()
    print(format_report(report))
    return 0 if not report.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
