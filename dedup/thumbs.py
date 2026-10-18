"""图片缩略图生成（内存缓存）。视频和暂不支持的格式返回 None，由前端显示占位图标。"""
from __future__ import annotations

import io
import threading
from pathlib import Path

from PIL import Image, ImageOps

THUMB_SIZE = (320, 320)
MAX_CACHE = 400

_cache: dict[tuple[str, int, float], bytes | None] = {}
_lock = threading.Lock()


def get_thumbnail(path: str | Path, size: int, mtime: float) -> bytes | None:
    key = (str(path), size, mtime)
    with _lock:
        if key in _cache:
            return _cache[key]
    data = _generate(Path(path))
    with _lock:
        if len(_cache) >= MAX_CACHE:
            _cache.pop(next(iter(_cache)))
        _cache[key] = data
    return data


def _generate(path: Path) -> bytes | None:
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail(THUMB_SIZE)
            has_alpha = img.mode in ("RGBA", "LA", "PA", "P")
            buf = io.BytesIO()
            if has_alpha:
                img.save(buf, format="PNG")
            else:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return None
