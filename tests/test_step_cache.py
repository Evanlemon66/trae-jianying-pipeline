"""step_cache 单测：命中/失效/关闭开关（纯本地，秒级）。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))

import step_cache as sc  # noqa: E402


def _src(tmp_path):
    p = tmp_path / "ep01.mp4"
    p.write_bytes(b"x" * 10)
    return p


def test_miss_save_hit_then_size_change_miss(tmp_path):
    src = _src(tmp_path)
    wd = str(tmp_path / "wd")
    os.makedirs(wd)
    art = os.path.join(wd, "demucs_ep01", "vocals.wav")
    os.makedirs(os.path.dirname(art))
    with open(art, "wb") as f:
        f.write(b"a" * 5)

    key = sc.make_key("demucs", {"tag": "t"}, [sc.fingerprint(src)])
    assert sc.load(wd, "demucs", key) is None          # 首次 miss
    sc.save(wd, "demucs", key, {"vocal": sc.rel_path(wd, art),
                                "files": sc.dir_manifest(os.path.dirname(art), wd)})
    assert sc.load(wd, "demucs", key) is not None      # 写入后命中

    with open(art, "ab") as f:                          # 产物变化 → 失效
        f.write(b"b")
    assert sc.load(wd, "demucs", key) is None


def test_key_changes_with_source_and_version(tmp_path, monkeypatch):
    src = _src(tmp_path)
    wd = str(tmp_path / "wd")
    os.makedirs(wd)
    key1 = sc.make_key("asr", {"tag": "t"}, [sc.fingerprint(src)])
    src.write_bytes(b"x" * 11)                          # 源变化 → key 变
    key2 = sc.make_key("asr", {"tag": "t"}, [sc.fingerprint(src)])
    assert key1 != key2

    sc.save(wd, "asr", key2, {"subs": [{"text": "a"}]})
    assert sc.load(wd, "asr", key2) is not None
    monkeypatch.setattr(sc, "CACHE_VERSION", sc.CACHE_VERSION + 1)
    assert sc.load(wd, "asr", key2) is None             # 版本不同 → 失效


def test_disabled_never_reads_or_writes(tmp_path):
    src = _src(tmp_path)
    wd = str(tmp_path / "wd")
    os.makedirs(wd)
    key = sc.make_key("translate", {"src": ["a"]}, [sc.fingerprint(src)])
    try:
        sc.configure(False)
        assert sc.save(wd, "translate", key, {"en": ["A"]}) is None
        assert sc.load(wd, "translate", key) is None
        assert not os.path.exists(os.path.join(wd, sc.CACHE_DIRNAME))
    finally:
        sc.configure(True)
    assert sc.save(wd, "translate", key, {"en": ["A"]}) is not None
    assert sc.load(wd, "translate", key) is not None
