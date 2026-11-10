# -*- coding: utf-8 -*-
"""sitecustomize.py — 跨进程隐藏控制台窗口（仅在 PYTHONPATH 指向本目录时生效）。

背景：部分第三方包（videocaptioner→pydub）在自己的进程里用 `subprocess.Popen` 起
ffmpeg，不带任何 flag；我们的进程内补丁管不到它。父进程通过 hidden_child_env()
把本目录前置到 PYTHONPATH 并设置 JY_HIDE_WINDOW=1，解释器启动时 site.py 会自动
import 本模块，于是补丁在第三方包 import 之前就已就位。

时序要点（勿删）：本模块在解释器启动阶段被 import，早于 pydub 等第三方模块。
这里替换的是 `subprocess.Popen.__init__`，而 pydub.utils 里的
`from subprocess import Popen` 拿到的是同一个类对象，所以**不需要**再去单独
patch `pydub.utils.Popen`；后补那层既冗余又容易漏掉别的包。

为什么强制安装：`pythonw -m videocaptioner` 的 stdout/stderr 可能被接到管道，
`GetConsoleWindow()` 等启发式判断容易误判。既然父进程已用 JY_HIDE_WINDOW=1 显式
声明，这里就跳过启发式、直接安装（幂等）。
"""
import os

try:
    if os.environ.get("JY_HIDE_WINDOW") == "1":
        from no_window import install_default_hidden
        install_default_hidden(force=True)
except Exception:
    # 兜底绝不影响子进程正常启动
    pass
