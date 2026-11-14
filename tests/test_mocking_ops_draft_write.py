# -*- coding: utf-8 -*-
"""mocking_ops 两棵树的草稿写盘保护回归测试。

覆盖：
  ① 两树候选顺序一致（防漂移闸门）；
  ② 存在性优先解析：都在 → draft_content.json（5.9 原生），只有 info → info，都没有 → 待新建 content；
  ③ 原子写：成功不留 .tmp，失败上抛且不留 .tmp；
  ④ 写盘异常不再静默：mixin 会打印一条带路径的告警，而不是 except Exception: pass。
"""
import ast
import importlib.util
import json
import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_FILES = {
    "root": os.path.join(_ROOT, "scripts", "core", "mocking_ops.py"),
    "skill": os.path.join(_ROOT, ".trae", "skills", "jianying-editor", "scripts",
                          "core", "mocking_ops.py"),
}
_TOUCHED = ("pyJianYingDraft", "utils", "utils.config")


def _load(kind):
    """按文件路径加载 mocking_ops；pyJianYingDraft / utils.config 用 stub 顶掉并原样还原。"""
    saved = {k: sys.modules.get(k) for k in _TOUCHED}
    saved_cfg_attr = getattr(saved["utils"], "config", None) if saved["utils"] else None
    try:
        if "pyJianYingDraft" not in sys.modules:
            stub = types.ModuleType("pyJianYingDraft")
            stub.VideoMaterial = type("VideoMaterial", (), {})
            stub.AudioMaterial = type("AudioMaterial", (), {})
            sys.modules["pyJianYingDraft"] = stub
        if "utils" not in sys.modules:
            pkg = types.ModuleType("utils")
            pkg.__path__ = []
            sys.modules["utils"] = pkg
        if "utils.config" not in sys.modules:
            cfg = types.ModuleType("utils.config")
            cfg.resolve_jianying_dir = lambda: None
            sys.modules["utils.config"] = cfg
            sys.modules["utils"].config = cfg
        spec = importlib.util.spec_from_file_location(
            "mocking_ops_" + kind, _MOD_FILES[kind])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for name in _TOUCHED:
            if saved[name] is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved[name]
        if saved["utils"] is not None and saved["utils.config"] is None:
            saved["utils"].__dict__.pop("config", None)
        elif saved["utils"] is not None:
            saved["utils"].config = saved_cfg_attr


@pytest.fixture(scope="module", params=["root", "skill"])
def mod(request):
    return _load(request.param)


def _candidates_from_source(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_DRAFT_CONTENT_CANDIDATES"
                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path} 里没有 _DRAFT_CONTENT_CANDIDATES")


def _adjust_draft():
    return {"materials": {}, "tracks": [{"segments": [{
        "common_keyframes": [{"property_type": "KFTypeBrightness"}],
        "extra_material_refs": []}]}]}


def test_candidate_order_identical_between_trees():
    """两树候选顺序必须逐字一致，且 5.9 原生的 draft_content.json 排第一。"""
    root_order = _candidates_from_source(_MOD_FILES["root"])
    skill_order = _candidates_from_source(_MOD_FILES["skill"])
    assert root_order == skill_order
    assert root_order == ("draft_content.json", "draft_info.json")
    for path in _MOD_FILES.values():
        tree = ast.parse(open(path, encoding="utf-8").read())
        names = {n.name for n in tree.body
                 if isinstance(n, ast.FunctionDef) or isinstance(n, ast.AsyncFunctionDef)}
        assert "_resolve_draft_content_path" in names


def test_resolve_prefers_draft_content_when_both_exist(mod, tmp_path):
    (tmp_path / "draft_content.json").write_text("{}", encoding="utf-8")
    (tmp_path / "draft_info.json").write_text("{}", encoding="utf-8")
    assert os.path.basename(mod._resolve_draft_content_path(str(tmp_path))) \
        == "draft_content.json"


def test_resolve_uses_draft_info_when_it_is_the_only_one(mod, tmp_path):
    (tmp_path / "draft_info.json").write_text("{}", encoding="utf-8")
    assert os.path.basename(mod._resolve_draft_content_path(str(tmp_path))) \
        == "draft_info.json"


def test_resolve_defaults_to_draft_content_when_none_exists(mod, tmp_path):
    got = mod._resolve_draft_content_path(str(tmp_path))
    assert os.path.basename(got) == "draft_content.json"
    assert not os.path.exists(got)          # 只解析路径，不创建


def test_atomic_write_leaves_no_tmp(mod, tmp_path):
    target = tmp_path / "draft_content.json"
    mod._write_draft_json({"name": "中文草稿"}, str(target))
    assert json.loads(target.read_text(encoding="utf-8")) == {"name": "中文草稿"}
    assert not os.path.exists(str(target) + ".tmp")
    assert sorted(os.listdir(tmp_path)) == ["draft_content.json"]


def test_write_failure_is_visible_and_leaves_no_tmp(mod, tmp_path):
    """目标位置不可替换（这里是目录）：异常上抛，且 tmp 被 finally 清掉。"""
    target = tmp_path / "draft_content.json"
    target.mkdir()
    with pytest.raises(OSError):
        mod._write_draft_json({"a": 1}, str(target))
    assert not os.path.exists(str(target) + ".tmp")


def test_force_activate_writes_to_existing_draft_info(mod, tmp_path):
    """只有 draft_info.json 的旧草稿：补丁写进 info，且不新建 content。"""
    draft_dir = tmp_path / "old-ep"
    draft_dir.mkdir()
    (draft_dir / "draft_info.json").write_text(
        json.dumps(_adjust_draft()), encoding="utf-8")

    inst = mod.MockingOpsMixin()
    inst.root, inst.name = str(tmp_path), "old-ep"
    inst._cloud_audio_patches, inst._cloud_text_patches = {}, {}
    inst._force_activate_adjustments()

    saved = json.loads((draft_dir / "draft_info.json").read_text(encoding="utf-8"))
    seg = saved["tracks"][0]["segments"][0]
    assert seg["enable_adjust"] is True
    assert len(saved["materials"]["effects"]) == 1
    assert seg["extra_material_refs"] == [saved["materials"]["effects"][0]["id"]]
    assert not (draft_dir / "draft_content.json").exists()
    assert not os.path.exists(str(draft_dir / "draft_info.json") + ".tmp")


def test_force_activate_prefers_draft_content_when_both_exist(mod, tmp_path):
    draft_dir = tmp_path / "new-ep"
    draft_dir.mkdir()
    for fn in ("draft_content.json", "draft_info.json"):
        (draft_dir / fn).write_text(json.dumps(_adjust_draft()), encoding="utf-8")
    info_before = (draft_dir / "draft_info.json").read_text(encoding="utf-8")

    inst = mod.MockingOpsMixin()
    inst.root, inst.name = str(tmp_path), "new-ep"
    inst._cloud_audio_patches, inst._cloud_text_patches = {}, {}
    inst._force_activate_adjustments()

    saved = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert saved["tracks"][0]["segments"][0]["enable_adjust"] is True
    assert (draft_dir / "draft_info.json").read_text(encoding="utf-8") == info_before


def test_cloud_patch_failure_is_logged(mod, tmp_path, capsys):
    """读不出来时不再静默吞掉：打出带草稿路径的告警。"""
    draft_dir = tmp_path / "broken-ep"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").mkdir()   # 存在但不是文件 → 打开即失败

    inst = mod.MockingOpsMixin()
    inst.root, inst.name = str(tmp_path), "broken-ep"
    inst._cloud_audio_patches = {"dummy.mp3": {"type": "music", "id": "M1"}}
    inst._cloud_text_patches = {}
    inst._patch_cloud_material_ids()             # 不抛异常

    out = capsys.readouterr().out
    assert "未写入" in out
    assert "draft_content.json" in out
