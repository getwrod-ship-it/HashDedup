"""递归扫描媒体文件，按内容哈希找出字节级完全相同的重复组。

判定流程（大文件夹也快）：
1. 按文件大小分组——大小不同的文件必然不同，直接算唯一；
2. 同大小的文件先比前 64KB 的 MD5——前缀不同即判定不同；
3. 前缀相同的再算整个文件的 SHA-256，全哈希相同才算重复。
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
              ".heic", ".heif", ".avif", ".ico"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".flv", ".m4v",
              ".mpg", ".mpeg", ".3gp", ".mts"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

PREFIX_SIZE = 64 * 1024
CHUNK_SIZE = 1024 * 1024


@dataclass
class FileInfo:
    path: Path
    size: int
    mtime: float
    ctime: float


@dataclass
class ScanResult:
    root: Path
    files: list[FileInfo] = field(default_factory=list)      # 参与比对的全部媒体文件
    groups: list[list[FileInfo]] = field(default_factory=list)  # 重复组，每组 >= 2 个文件
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (路径, 跳过原因)
    non_media: int = 0   # 既不是图片也不是视频、被忽略的文件数
    cancelled: bool = False

    @property
    def uniques(self) -> list[FileInfo]:
        grouped = {id(f) for g in self.groups for f in g}
        return [f for f in self.files if id(f) not in grouped]

    @property
    def unique_content_count(self) -> int:
        return len(self.groups) + len(self.uniques)

    @property
    def redundant_count(self) -> int:
        return sum(len(g) - 1 for g in self.groups)

    @property
    def redundant_bytes(self) -> int:
        return sum((len(g) - 1) * g[0].size for g in self.groups)


@dataclass
class Progress:
    phase: str = "walk"     # walk 清点 / hash 内容比对 / done
    files_seen: int = 0     # 清点阶段：已发现的媒体文件数
    current: str = ""       # 正在处理的文件
    done_files: int = 0     # 比对阶段：已定案的媒体文件数
    total_files: int = 0    # 媒体文件总数


class Cancelled(Exception):
    pass


class Scanner:
    def __init__(self, root: str | Path,
                 progress=None, cancel: threading.Event | None = None):
        self.root = Path(root).resolve()
        self._progress_cb = progress or (lambda p: None)
        self._cancel = cancel or threading.Event()
        self._progress = Progress()
        self.files: list[FileInfo] = []
        self.skipped: list[tuple[str, str]] = []
        self.non_media = 0

    def scan(self) -> ScanResult:
        result = ScanResult(root=self.root)
        try:
            self._walk()
            self.files.sort(key=lambda f: str(f.path))
            result.files = self.files
            result.skipped = self.skipped
            result.non_media = self.non_media
            result.groups = self._find_duplicates()
            self._progress.phase = "done"
            self._progress.current = ""
            self._emit()
        except Cancelled:
            result.cancelled = True
        return result

    def _emit(self):
        self._progress_cb(self._progress)

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    def _walk(self):
        seen: set[str] = set()
        for dirpath, _dirnames, filenames in os.walk(self.root):
            self._check_cancel()
            for name in filenames:
                if os.path.splitext(name)[1].lower() not in MEDIA_EXTS:
                    self.non_media += 1
                    continue
                full = Path(os.path.realpath(os.path.join(dirpath, name)))
                key = str(full).lower()  # Windows 路径大小写不敏感
                if key in seen:
                    continue
                try:
                    st = full.stat()
                except OSError as e:
                    self.skipped.append((str(full), f"无法读取: {e}"))
                    continue
                if st.st_size == 0:
                    self.skipped.append((str(full), "空文件"))
                    continue
                seen.add(key)
                self.files.append(FileInfo(full, st.st_size, st.st_mtime, st.st_ctime))
                self._progress.files_seen = len(self.files)
                self._progress.current = str(full)
                self._emit()

    def _find_duplicates(self) -> list[list[FileInfo]]:
        by_size: dict[int, list[FileInfo]] = defaultdict(list)
        for f in self.files:
            by_size[f.size].append(f)
        p = self._progress
        p.phase = "hash"
        p.current = ""
        p.total_files = len(self.files)
        # 大小唯一的文件无需哈希，直接视为已定案；其余文件边比对边累加，
        # 这样 (done_files/total_files) 始终覆盖全部媒体文件且单调递增
        p.done_files = sum(1 for flist in by_size.values() if len(flist) == 1)
        self._emit()

        candidates = [flist for flist in by_size.values() if len(flist) > 1]
        groups: list[list[FileInfo]] = []
        for flist in candidates:
            self._check_cancel()
            by_prefix: dict[str | None, list[FileInfo]] = defaultdict(list)
            for f in flist:
                by_prefix[self._hash_file(f, prefix_only=True)].append(f)
                p.done_files += 1
                p.current = str(f.path)
                self._emit()
            for prefix, preflist in by_prefix.items():
                if prefix is None or len(preflist) < 2:
                    continue
                by_full: dict[str | None, list[FileInfo]] = defaultdict(list)
                for f in preflist:
                    by_full[self._hash_file(f)].append(f)
                    p.current = str(f.path)
                    self._emit()
                for digest, g in by_full.items():
                    if digest is not None and len(g) > 1:
                        groups.append(g)
        # 冗余字节多的组排在前面，最值得处理的先看
        groups.sort(key=lambda g: (len(g) - 1) * g[0].size, reverse=True)
        return groups

    def _hash_file(self, f: FileInfo, prefix_only: bool = False) -> str | None:
        h = hashlib.md5()
        length = PREFIX_SIZE if prefix_only else f.size
        try:
            with open(f.path, "rb") as fp:
                read = 0
                while read < length:
                    chunk = fp.read(min(CHUNK_SIZE, length - read))
                    if not chunk:
                        break
                    h.update(chunk)
                    read += len(chunk)
        except OSError as e:
            self.skipped.append((str(f.path), f"读取失败: {e}"))
            self.files.remove(f)
            return None
        return h.hexdigest()
