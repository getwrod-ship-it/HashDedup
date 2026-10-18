"""从每组重复中自动选出保留者。

规则（依次比较，先满足者胜出）：
1. 文件更大（理论上质量更高；同组文件大小相同，此条为兜底）；
2. 修改时间更早（更接近原始拍摄/保存时间）；
3. 创建时间更早（Windows 复制会保留修改时间，但创建时间能区分原件与副本）；
4. 路径更短、字典序更小（保证结果稳定可复现）。
"""
from __future__ import annotations

from .scanner import FileInfo


def _better(a: FileInfo, b: FileInfo) -> bool:
    """a 是否应替代 b 成为保留者。"""
    if a.size != b.size:
        return a.size > b.size
    if a.mtime != b.mtime:
        return a.mtime < b.mtime
    if a.ctime != b.ctime:
        return a.ctime < b.ctime
    sa, sb = str(a.path), str(b.path)
    if len(sa) != len(sb):
        return len(sa) < len(sb)
    return sa < sb


def keeper_index(group: list[FileInfo]) -> int:
    best = 0
    for i in range(1, len(group)):
        if _better(group[i], group[best]):
            best = i
    return best
