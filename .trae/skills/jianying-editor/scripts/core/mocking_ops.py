import os
import json
import uuid
import pyJianYingDraft as draft
from utils.config import resolve_jianying_dir

# 草稿主文件候选顺序：5.9 原生读 draft_content.json，两者都在时以它为准；
# 只有 draft_info.json 的旧草稿就用它；都没有才新建 draft_content.json。
# 仓库 scripts/core 与本 skill 树各有一份同名常量/解析函数，
# tests/test_mocking_ops_draft_write.py 会断言两边顺序完全一致，防止再次漂开。
_DRAFT_CONTENT_CANDIDATES = ("draft_content.json", "draft_info.json")


def _resolve_draft_content_path(draft_dir):
    """按目录里实际存在的文件解析草稿主文件路径；都不存在时返回待新建的默认路径。"""
    for filename in _DRAFT_CONTENT_CANDIDATES:
        path = os.path.join(draft_dir, filename)
        if os.path.exists(path):
            return path
    return os.path.join(draft_dir, _DRAFT_CONTENT_CANDIDATES[0])


def _write_draft_json(data, content_path):
    """原子写草稿 JSON：tmp + os.replace，中断/失败都不留截断文件，异常一律上抛。

    本 skill 树要能独立分发，只依赖标准库，不 import 仓库 pipeline/。
    """
    tmp = content_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, content_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class MockVideoMaterial(draft.VideoMaterial):
    def __init__(self, material_id, duration, name, path):
        self.material_id = material_id
        self.duration = duration
        self.material_name = name
        self.path = path
    def serialize(self):
        return {
            "id": self.material_id, "type": "video", "name": self.material_name, "path": self.path,
            "duration": self.duration, "material_id": self.material_id
        }

class MockAudioMaterial(draft.AudioMaterial):
    def __init__(self, material_id, duration, name, path):
        self.material_id = material_id
        self.duration = duration
        self.material_name = name
        self.path = path
    def serialize(self):
        return {
            "id": self.material_id, "type": "audio", "name": self.material_name, "path": self.path,
            "duration": self.duration, "material_id": self.material_id
        }

class CompoundSegment:
    def __init__(self, material_id, target_timerange):
        self.material_id = material_id
        self.target_timerange = target_timerange
    def serialize(self):
        return {
             "id": str(uuid.uuid4()).upper(), "material_id": self.material_id,
             "target_timerange": self.target_timerange.serialize(),
             "render_index": 0, "type": "video"
        }

class MockingOpsMixin:
    """
    JyProject 的协议补丁与伪物料 Mixin。
    """
    def _force_activate_adjustments(self):
        content_path = _resolve_draft_content_path(os.path.join(self.root, self.name))
        if not os.path.exists(content_path): return

        try:
            with open(content_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            has_modified = False
            materials = data.setdefault("materials", {})
            all_effects = materials.setdefault("effects", [])

            PROP_MAP = {"KFTypeBrightness": "brightness", "KFTypeContrast": "contrast", "KFTypeSaturation": "saturation"}
            # 调色影子素材指向剪映自带的调色资源包：安装目录由配置/注册表推导，不写死
            jy_res_path = "Resources/DefaultAdjustBundle/combine_adjust"
            jy_dir = resolve_jianying_dir()
            if jy_dir:
                jy_res_path = os.path.join(jy_dir, jy_res_path).replace("\\", "/")

            for track in data.get("tracks", []):
                for seg in track.get("segments", []):
                    kfs = seg.get("common_keyframes", [])
                    active_props = [kf.get("property_type") for kf in kfs if kf.get("property_type") in PROP_MAP]

                    if active_props:
                        seg["enable_adjust"] = True
                        seg["enable_color_correct_adjust"] = True
                        refs = seg.setdefault("extra_material_refs", [])

                        for prop in active_props:
                            mat_type = PROP_MAP[prop]
                            if not any(m.get("type") == mat_type and m["id"] in refs for m in all_effects):
                                new_id = str(uuid.uuid4()).upper()
                                shadow_mat = {
                                    "type": mat_type, "value": 0.0, "path": jy_res_path, "id": new_id,
                                    "apply_target_type": 0, "platform": "all", "source_platform": 0, "version": "v2"
                                }
                                all_effects.append(shadow_mat)
                                refs.append(new_id)
                                has_modified = True

            if has_modified:
                _write_draft_json(data, content_path)
        except Exception as e:
            print(f"⚠️ 调色影子素材未写入 {content_path}: {e}")

    def _patch_cloud_material_ids(self):
        if not self._cloud_audio_patches and not self._cloud_text_patches: return
        content_path = _resolve_draft_content_path(os.path.join(self.root, self.name))
        if not os.path.exists(content_path): return

        try:
            with open(content_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            has_modified = False
            materials = data.get("materials", {})
            audios = materials.get("audios", [])
            for mat in audios:
                path = mat.get("path", "")
                for dummy_path, patch_info in self._cloud_audio_patches.items():
                    if dummy_path in path:
                        if patch_info["type"] == "music":
                            mat["music_id"] = patch_info["id"]
                            mat["type"] = "music"
                            has_modified = True
            
            if has_modified:
                _write_draft_json(data, content_path)
        except Exception as e:
            print(f"⚠️ 云端素材补丁未写入 {content_path}: {e}")
