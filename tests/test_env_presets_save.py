# -*- coding: utf-8 -*-
"""presets.json 保存链路测试：真实落盘 + 备份 + 原子替换 + 备份保留上限。

用临时目录替换 envinit.PRESETS_PATH，不触碰生产 pipeline/presets.json。
"""
import json
import os
import sys

import pytest

_CONSOLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "pipeline_ui", "console")
sys.path.insert(0, _CONSOLE)
try:
    import envinit  # noqa: E402
finally:
    sys.path.remove(_CONSOLE)
    # 避免 console/doctor.py 与 paths/status 污染其他测试（tests/test_doctor.py 需要 pipeline/doctor.py）
    for _m in ("doctor", "paths", "status"):
        sys.modules.pop(_m, None)


def _cfg(root):
    return {"series_root": str(root), "export_dir": str(root), "deps_root": str(root)}


def test_save_writes_file_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(_cfg(tmp_path)), encoding="utf-8")
    monkeypatch.setattr(envinit, "PRESETS_PATH", str(path))

    cfg = _cfg(tmp_path)
    cfg["custom_flag"] = {"a": 1}
    res = envinit.save_presets(cfg)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["custom_flag"] == {"a": 1}
    assert res["path"] == str(path)
    assert res["backup"] and os.path.isfile(res["backup"])
    assert len(list(tmp_path.glob("presets.json.bak-*"))) == 1
    # 原子替换后不残留临时文件
    assert not list(tmp_path.glob("presets.json.tmp-*"))


def test_invalid_config_is_not_written(tmp_path, monkeypatch):
    path = tmp_path / "presets.json"
    original = json.dumps(_cfg(tmp_path))
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(envinit, "PRESETS_PATH", str(path))

    with pytest.raises(ValueError):
        envinit.save_presets({"export_dir": "", "deps_root": ""})

    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("presets.json.bak-*"))


def test_prune_keeps_latest_ten(tmp_path, monkeypatch):
    names = []
    for i in range(12):
        p = tmp_path / ("presets.json.bak-20260101-0000%02d-abcd" % i)
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))
        names.append(p.name)

    monkeypatch.setattr(envinit, "PRESETS_PATH", str(tmp_path / "presets.json"))
    envinit._prune_backups(keep=10)

    left = list(tmp_path.glob("presets.json.bak-*"))
    assert len(left) == 10
    assert names[-1] in {p.name for p in left}
    assert names[0] not in {p.name for p in left}
