# -*- coding: utf-8 -*-
"""tests/bundle_fixture.py — 成品三件套的统一构造。

clean / 归档现在按导出侧同口径校验：「mp4 非空 + progress.done=true + 竖版
封面（短边 >= 720）」。各测试文件原先各自 handcrafted 假产物，形状一旦陈旧
就会被新校验整片拦红。这里集中一处：

  - 默认参数产出**合法**三件套，正常用例直接拿来用；
  - 异常用例用显式参数制造残缺（skip= / progress_done= / cover_size=），
    「故意缺件」与「默认合法」一眼可辨，不靠每个文件自己补字段。

用法：
    from bundle_fixture import make_bundle, png_cover, progress_json
    make_bundle(dir_path, "剧A")                          # 合法三件套
    make_bundle(dir_path, "剧B", skip={"cover"})          # 故意缺封面
    make_bundle(dir_path, "剧C", progress_done=False)     # done=false
    make_bundle(dir_path, "剧D", cover_size=(1080, 1080)) # 方形封面（比例不合格）
"""
import json
import os
import struct

DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_MP4_BYTES = 10240


def png_cover(w=DEFAULT_WIDTH, h=DEFAULT_HEIGHT):
    """最小 PNG 头：校验只读魔数与 IHDR 宽高，不需要真实像素数据。"""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00")


def progress_json(done=True):
    return {"progress": 100, "done": done}


def make_bundle(dir_path, name, *, skip=(), mp4_bytes=DEFAULT_MP4_BYTES,
                progress_done=True, cover_size=(DEFAULT_WIDTH, DEFAULT_HEIGHT)):
    """在 dir_path 下造 `<name>.mp4` / `.mp4.progress.json` / `-封面.jpg`。

    skip 里列出的件不写（制造缺件）；progress_done=False 制造未完成；
    cover_size 可传非竖版/过小尺寸。返回三件套路径，缺件对应值为 None。
    """
    os.makedirs(dir_path, exist_ok=True)
    mp4 = os.path.join(dir_path, f"{name}.mp4")
    progress = mp4 + ".progress.json"
    cover = os.path.join(dir_path, f"{name}-封面.jpg")
    if "mp4" not in skip:
        with open(mp4, "wb") as f:
            f.write(b"m" * mp4_bytes)
    if "progress" not in skip:
        with open(progress, "w", encoding="utf-8") as f:
            json.dump(progress_json(progress_done), f)
    if "cover" not in skip:
        with open(cover, "wb") as f:
            f.write(png_cover(*cover_size))
    return {"mp4": None if "mp4" in skip else mp4,
            "progress": None if "progress" in skip else progress,
            "cover": None if "cover" in skip else cover}
