# -*- coding: utf-8 -*-
"""skill 树上探仓库依赖失败时，必须给出布局说明而不是裸 ModuleNotFoundError。"""
import pytest

import jy_instance


def test_real_repo_layout_is_accepted():
    jy_instance._require_repo_layout()  # 当前仓库布局应通过


def test_missing_lib_raises_readable_import_error(tmp_path):
    original = jy_instance._REPO_ROOT
    try:
        jy_instance._REPO_ROOT = str(tmp_path)
        with pytest.raises(ImportError) as exc:
            jy_instance._require_repo_layout()
        message = str(exc.value)
        assert "<repo>/.trae/skills/" in message
        assert "lib/jianying_launch.py" in message
        assert "clone/解压" in message
    finally:
        jy_instance._REPO_ROOT = original
