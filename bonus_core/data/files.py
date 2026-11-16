"""一批材料里的文件按文件名识别（指纹、映射、智能体读取都用文件名），所以同一批里不能有同名文件。"""
from __future__ import annotations

from pathlib import Path


class DuplicateFileName(ValueError):
    pass


def require_unique_names(files) -> None:
    """不同文件夹里有同名文件时报错：按名字记的指纹只有一条，其中一个被改了也发现不了，读取时也可能读错文件。
    同一个路径重复给出不算。"""
    seen: dict[str, set[str]] = {}
    for f in files:
        p = Path(f)
        seen.setdefault(p.name, set()).add(str(p.resolve()))
    dup = {name: sorted(paths) for name, paths in seen.items() if len(paths) > 1}
    if dup:
        raise DuplicateFileName(
            "同一批材料里有同名文件，程序按文件名识别材料，分不清它们：" +
            "；".join(f"「{name}」：{'、'.join(paths)}" for name, paths in sorted(dup.items())) +
            "。请把其中一个改名（如加上来源或月份）后再处理")
