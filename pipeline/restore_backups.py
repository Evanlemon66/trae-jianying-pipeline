# -*- coding: utf-8 -*-
"""草稿目录「恢复备份源」白名单：唯一真源。

剪映的「恢复备份」会扫描草稿目录里的历史副本，异常退出/重新打开时把旧稿（往往是
坏稿）复活。清理必须覆盖全部已知备份源，又不能碰正常草稿文件。所有清理入口
（publish_staged_draft / create_empty_draft / merge_drafts / admission_test）
统一走这里，新增备份后缀只需改本文件。
"""
import os
import re
import shutil

# 精确匹配的备份文件名（pipeline 自己写的串流备份；发布后会被 _write_pipeline_backup 重写）
EXACT_BACKUP_NAMES = frozenset({"_pipeline_backup.json"})

# 已知备份目录：顶层目录名精确匹配，整个目录都是备份，不参与正常草稿
BACKUP_DIR_NAMES = frozenset({".backup"})

# 备份后缀族：.bak / .tmp 结尾（可带时间戳/用途后缀），以及 *bak_<数字> 形式。
# 覆盖：draft_content.json.bak、.bak_20260101_120000、.bak_vad_gate、
#       .before_restore.bak、template.tmp、
#       draft_content.json.repairbak_1700000000 / .splitbak_… / .verifybak_…
# 新增备份只要沿用 <原文件>.bak[_后缀] / .tmp / *bak_<数字> 命名即可自动命中。
_BACKUP_SUFFIX_RE = re.compile(r"(?:\.(?:bak|tmp)(?:[._-][^\\/]*)?|bak_\d+)$",
                               re.IGNORECASE)


def is_restore_backup_source(name):
    """文件名是否属于已知恢复备份源（大小写不敏感）。"""
    low = str(name).lower()
    return low in EXACT_BACKUP_NAMES or bool(_BACKUP_SUFFIX_RE.search(low))


def prune_restore_backups(draft_dir):
    """删除草稿目录顶层的已知恢复备份源，返回已删除路径列表。

    只扫顶层：命中白名单的文件才删，`.backup` 目录整体删除；不递归 Resources/
    等正常子目录。删除失败（被占用）静默跳过，与历史清理行为一致。
    """
    removed = []
    if not os.path.isdir(draft_dir):
        return removed
    try:
        names = os.listdir(draft_dir)
    except OSError:
        return removed
    for name in names:
        p = os.path.join(draft_dir, name)
        if os.path.isdir(p):
            if name.lower() in BACKUP_DIR_NAMES:
                try:
                    shutil.rmtree(p)
                    removed.append(p)
                except OSError:
                    continue
            continue
        if not os.path.isfile(p) or not is_restore_backup_source(name):
            continue
        try:
            os.remove(p)
            removed.append(p)
        except OSError:
            continue
    return removed
