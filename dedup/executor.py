"""把文件平铺复制到「保留 / 重复」两个文件夹，并生成 Markdown 报告。

源文件夹全程只读不动；keep 放所有唯一内容（无重复文件 + 每组保留者），
duplicates 放每组中的冗余副本。两个目标文件夹都不保留原目录层级：
同名文件按 Windows 习惯自动编号为「名称 (1).ext」「名称 (2).ext」…
如果目标里已存在内容相同的文件（上次运行复制过），则跳过而不是重复复制。
"""
from __future__ import annotations

import hashlib
import shutil
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .scanner import ScanResult


@dataclass
class CopyAction:
    src: Path
    dest_dir: Path   # 目标根文件夹（平铺）
    kind: str        # keep / duplicate
    size: int


@dataclass
class ExecuteResult:
    total: int = 0
    copied: int = 0
    copied_keep: int = 0
    copied_dup: int = 0
    bytes_copied: int = 0
    skipped_existing: int = 0                     # 目标已有相同内容，无需复制
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (原路径, 最终文件名)
    cancelled: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


def validate_paths(root: Path, keep: Path, dup: Path) -> list[str]:
    errors: list[str] = []

    def inside(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    root = root.resolve()
    keep = keep.resolve()
    dup = dup.resolve()
    if not root.is_dir():
        errors.append(f"源文件夹不存在或不是文件夹：{root}")
    for label, out in (("保留文件夹", keep), ("重复文件夹", dup)):
        if out.exists() and not out.is_dir():
            errors.append(f"{label}已存在但不是文件夹：{out}")
        if out == root or inside(out, root):
            errors.append(f"{label}不能是源文件夹本身或其子文件夹：{out}")
    if keep == dup:
        errors.append("保留文件夹和重复文件夹不能相同")
    if inside(keep, dup) or inside(dup, keep):
        errors.append("保留文件夹和重复文件夹不能互相嵌套")
    return errors


def plan_copies(result: ScanResult, keep: Path, dup: Path,
                keeper_indices: list[int]) -> list[CopyAction]:
    actions: list[CopyAction] = []
    for f in result.uniques:
        actions.append(CopyAction(f.path, keep, "keep", f.size))
    for gi, group in enumerate(result.groups):
        keeper = keeper_indices[gi]
        for i, f in enumerate(group):
            actions.append(CopyAction(f.path, dup if i != keeper else keep,
                                      "keep" if i == keeper else "duplicate", f.size))
    return actions


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_file(a: Path, b: Path) -> bool:
    """判断两个文件内容是否相同（大小相同且修改时间相同视为相同，否则比哈希）。"""
    try:
        sa, sb = a.stat(), b.stat()
        if sa.st_size != sb.st_size:
            return False
        if sa.st_mtime == sb.st_mtime:  # copy2 会保留修改时间
            return True
        return _md5(a) == _md5(b)
    except OSError:
        return False


def _pick_dest(dest_dir: Path, src: Path, used: set[str]) -> Path:
    """在 dest_dir 里为 src 挑一个不冲突的平铺文件名（内容相同的文件已在前面跳过，
    这里只需处理同名但内容不同的情况，按「名称 (1)、(2)…」编号）。"""
    stem, suffix = src.stem, src.suffix
    n = 0
    while True:
        candidate = src.name if n == 0 else f"{stem} ({n}){suffix}"
        n += 1
        if candidate in used:
            continue
        if (dest_dir / candidate).exists():
            continue
        used.add(candidate)
        return dest_dir / candidate


def execute(actions: list[CopyAction], progress=None,
            cancel: threading.Event | None = None) -> ExecuteResult:
    res = ExecuteResult(total=len(actions))
    used: dict[Path, set[str]] = defaultdict(set)
    dest_index: dict[Path, dict[int, list[Path]]] = {}  # 目标文件夹已有文件按大小索引（一次性）

    def content_in_dir(dest_dir: Path, src: Path, size: int) -> bool:
        """目标文件夹里是否已存在与 src 内容相同的文件（任意文件名）。"""
        if dest_dir not in dest_index:
            by_size: dict[int, list[Path]] = defaultdict(list)
            if dest_dir.is_dir():
                try:
                    for p in dest_dir.iterdir():
                        if p.is_file():
                            try:
                                by_size[p.stat().st_size].append(p)
                            except OSError:
                                pass
                except OSError:
                    pass
            dest_index[dest_dir] = by_size
        return any(_same_file(src, p) for p in dest_index[dest_dir].get(size, []))

    # 固定处理顺序，保证重名编号在重复运行时结果一致
    ordered = sorted(actions, key=lambda a: (str(a.dest_dir), str(a.src)))
    for i, act in enumerate(ordered):
        if cancel is not None and cancel.is_set():
            res.cancelled = True
            break
        try:
            if content_in_dir(act.dest_dir, act.src, act.size):
                res.skipped_existing += 1
            else:
                dest = _pick_dest(act.dest_dir, act.src, used[act.dest_dir])
                if dest.name != act.src.name:
                    res.renamed.append((str(act.src), str(dest.name)))
                act.dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(act.src, dest)
                if dest.stat().st_size != act.size:
                    raise OSError("复制后大小与源文件不一致")
                res.copied += 1
                res.bytes_copied += act.size
                if act.kind == "keep":
                    res.copied_keep += 1
                else:
                    res.copied_dup += 1
        except OSError as e:
            res.failures.append((str(act.src), str(e)))
        if progress is not None:
            progress(i + 1, len(ordered), str(act.src), act.kind)
    return res


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def write_report(dup_dir: Path, result: ScanResult, keeper_indices: list[int],
                 exec_res: ExecuteResult, keep_dir: Path) -> Path:
    dup_dir.mkdir(parents=True, exist_ok=True)
    rename_map = {src: name for src, name in exec_res.renamed}
    lines = [
        "# 照片 / 视频去重报告",
        "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 源文件夹：`{result.root}`",
        f"- 保留文件夹：`{keep_dir}`",
        f"- 重复文件夹：`{dup_dir}`",
        "",
        "两个目标文件夹均为平铺结构（不保留原目录层级），同名文件自动编号为「名称 (1)、(2)…」。",
        "",
        "## 总览",
        "",
        f"- 媒体文件总数：{len(result.files)}",
        f"- 非图片/视频文件：{result.non_media} 个（已自动跳过）",
        f"- 唯一内容数：{result.unique_content_count}",
        f"- 重复组数：{len(result.groups)}",
        f"- 冗余副本数：{result.redundant_count}",
        f"- 冗余占用空间：{fmt_size(result.redundant_bytes)}",
        "",
        "## 执行结果",
        "",
        f"- 复制到保留文件夹：{exec_res.copied_keep} 个文件",
        f"- 复制到重复文件夹：{exec_res.copied_dup} 个文件",
    ]
    if exec_res.skipped_existing:
        lines.append(f"- 目标已存在相同内容而跳过：{exec_res.skipped_existing} 个")
    lines.append(f"- 失败：{len(exec_res.failures)} 个" +
                 ("（源文件夹未被修改，可重新执行）" if exec_res.failures else ""))
    lines.append("")
    if result.groups:
        lines += ["## 重复组明细", ""]
        for gi, group in enumerate(result.groups, 1):
            keeper = keeper_indices[gi - 1]

            def where(p) -> str:
                s = str(p)
                return f"`{s}` → **`{rename_map[s]}`**" if s in rename_map else f"`{s}`"

            lines += [f"### 组 {gi}（{len(group)} 个副本，每个 {fmt_size(group[0].size)}）", "",
                      f"- **保留**：{where(group[keeper].path)}", ""]
            lines += [f"- 重复：{where(f.path)}"
                      for i, f in enumerate(group) if i != keeper]
            lines.append("")
    if result.skipped:
        lines += ["## 扫描时跳过的文件", ""]
        lines += [f"- `{p}`（{reason}）" for p, reason in result.skipped]
        lines.append("")
    if exec_res.failures:
        lines += ["## 复制失败的文件", ""]
        lines += [f"- `{p}`（{err}）" for p, err in exec_res.failures]
        lines.append("")
    report = dup_dir / "dedup_report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
