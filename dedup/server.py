"""FastAPI 应用：托管前端页面，管理扫描 / 执行的后台任务。

单用户本地工具，全局一个 Job（一次扫描-执行流程），状态机：
idle -> scanning -> scanned -> executing -> done，任意阶段可 cancelled / error。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import thumbs
from .executor import execute, plan_copies, validate_paths, write_report
from .planner import keeper_index
from .scanner import IMAGE_EXTS, Scanner


class Job:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        self.state = "idle"
        self.root: Path | None = None
        self.keep: Path | None = None
        self.dup: Path | None = None
        self.progress: dict[str, Any] = {}
        self.result = None           # ScanResult
        self.keepers: list[int] = []  # 每组保留者下标（自动或用户覆盖）
        self.error: str | None = None
        self.summary: dict[str, Any] | None = None
        self.cancel_event = threading.Event()

    def busy(self) -> bool:
        return self.state in ("scanning", "executing")


job = Job()


def _run_scan(root: Path, keep: Path, dup: Path) -> None:
    def on_progress(p) -> None:
        with job.lock:
            job.progress = {"phase": p.phase, "files_seen": p.files_seen,
                            "current": p.current, "done_files": p.done_files,
                            "total_files": p.total_files}
    try:
        scanner = Scanner(root, progress=on_progress, cancel=job.cancel_event)
        result = scanner.scan()
        with job.lock:
            job.result = result
            if result.cancelled:
                job.state = "cancelled"
            else:
                job.keepers = [keeper_index(g) for g in result.groups]
                job.state = "scanned"
    except Exception as e:  # 后台线程兜底，任何异常都要反映到界面上
        with job.lock:
            job.error = f"{type(e).__name__}: {e}"
            job.state = "error"


def _run_execute(keepers: list[int]) -> None:
    try:
        with job.lock:
            result, keep, dup = job.result, job.keep, job.dup
        actions = plan_copies(result, keep, dup, keepers)

        def on_progress(done: int, total: int, current: str, kind: str) -> None:
            with job.lock:
                job.progress = {"phase": "copy", "done": done, "total": total,
                                "current": current, "kind": kind}

        exec_res = execute(actions, progress=on_progress, cancel=job.cancel_event)
        report = write_report(dup, result, keepers, exec_res, keep)
        with job.lock:
            job.summary = {
                "total": exec_res.total, "copied": exec_res.copied,
                "copied_keep": exec_res.copied_keep, "copied_dup": exec_res.copied_dup,
                "bytes_copied": exec_res.bytes_copied,
                "skipped_existing": exec_res.skipped_existing,
                "renamed": len(exec_res.renamed),
                "cancelled": exec_res.cancelled,
                "failures": [{"src": s, "error": e} for s, e in exec_res.failures],
                "report": str(report),
            }
            job.state = "done" if not exec_res.cancelled else "cancelled"
    except Exception as e:
        with job.lock:
            job.error = f"{type(e).__name__}: {e}"
            job.state = "error"


class ScanRequest(BaseModel):
    root: str
    keep_dir: str
    dup_dir: str


class ExecuteRequest(BaseModel):
    keepers: list[int] | None = None


class OpenRequest(BaseModel):
    path: str


def _file_json(root: Path, f) -> dict[str, Any]:
    return {"path": str(f.path), "rel": f.path.relative_to(root).as_posix(),
            "name": f.path.name, "size": f.size, "mtime": f.mtime, "ctime": f.ctime,
            "is_image": f.path.suffix.lower() in IMAGE_EXTS}


def create_app() -> FastAPI:
    app = FastAPI(title="照片/视频去重工具", docs_url=None, redoc_url=None)

    @app.post("/api/scan")
    def start_scan(req: ScanRequest):
        with job.lock:
            if job.busy():
                raise HTTPException(409, "已有任务在进行中，请先取消或等待完成")
            root, keep, dup = Path(req.root), Path(req.keep_dir), Path(req.dup_dir)
            errors = validate_paths(root, keep, dup)
            if errors:
                raise HTTPException(400, "；".join(errors))
            job.reset()
            job.root, job.keep, job.dup = root.resolve(), keep.resolve(), dup.resolve()
            job.state = "scanning"
            threading.Thread(target=_run_scan, args=(job.root, job.keep, job.dup),
                             daemon=True).start()
        return {"ok": True}

    @app.get("/api/status")
    def status():
        with job.lock:
            data: dict[str, Any] = {"state": job.state, "progress": job.progress,
                                    "error": job.error,
                                    "root": str(job.root) if job.root else None,
                                    "keep": str(job.keep) if job.keep else None,
                                    "dup": str(job.dup) if job.dup else None}
            if job.result is not None:
                r = job.result
                data["stats"] = {"files": len(r.files),
                                 "unique_content": r.unique_content_count,
                                 "groups": len(r.groups),
                                 "redundant": r.redundant_count,
                                 "redundant_bytes": r.redundant_bytes,
                                 "skipped": len(r.skipped),
                                 "non_media": r.non_media}
            if job.summary is not None:
                data["summary"] = job.summary
            return data

    @app.get("/api/results")
    def results():
        with job.lock:
            if job.result is None:
                raise HTTPException(404, "还没有扫描结果")
            r = job.result
            groups = [{"index": gi,
                       "size": g[0].size,
                       "count": len(g),
                       "files": [_file_json(r.root, f) for f in g],
                       "keeper": job.keepers[gi]}
                      for gi, g in enumerate(r.groups)]
            uniques = [_file_json(r.root, f) for f in r.uniques]
        return JSONResponse({"root": str(r.root), "groups": groups, "uniques": uniques})

    @app.post("/api/execute")
    def start_execute(req: ExecuteRequest):
        with job.lock:
            if job.busy():
                raise HTTPException(409, "已有任务在进行中")
            if job.state != "scanned" or job.result is None:
                raise HTTPException(400, "请先完成一次扫描")
            keepers = req.keepers if req.keepers is not None else job.keepers
            if len(keepers) != len(job.result.groups) or any(
                    not 0 <= k < len(g) for k, g in zip(keepers, job.result.groups)):
                raise HTTPException(400, "保留者列表与扫描结果不匹配")
            job.keepers = keepers
            job.state = "executing"
            threading.Thread(target=_run_execute, args=(list(keepers),),
                             daemon=True).start()
        return {"ok": True}

    @app.post("/api/cancel")
    def cancel():
        job.cancel_event.set()
        return {"ok": True}

    @app.get("/api/thumb")
    def thumb(path: str):
        with job.lock:
            root = job.root
        if root is None:
            raise HTTPException(404, "尚未配置扫描")
        real = Path(os.path.realpath(path))
        inside = str(real).lower().startswith(str(root.resolve()).lower() + os.sep)
        if not inside:
            raise HTTPException(403, "只能查看源文件夹内的文件")
        try:
            st = real.stat()
        except OSError:
            raise HTTPException(404, "文件不存在")
        data = thumbs.get_thumbnail(real, st.st_size, st.st_mtime)
        if data is None:
            raise HTTPException(404, "无法生成缩略图")
        return Response(content=data,
                        media_type="image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg")

    @app.get("/api/browse")
    def browse(path: str = ""):
        with job.lock:
            base = job.root
        p = Path(path) if path else (base or Path.cwd())
        if not p.exists():
            raise HTTPException(400, f"路径不存在：{p}")
        if p.is_file():
            p = p.parent
        p = p.resolve()
        dirs = []
        try:
            for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
                if child.is_dir():
                    dirs.append(child.name)
        except OSError as e:
            raise HTTPException(400, f"无法读取文件夹：{e}")
        parent = p.parent
        return {"path": str(p), "parent": None if parent == p else str(parent), "dirs": dirs}

    @app.post("/api/open")
    def open_folder(req: OpenRequest):
        p = Path(req.path)
        if not p.is_dir():
            raise HTTPException(400, f"文件夹不存在：{p}")
        try:
            import subprocess
            import sys
            if sys.platform == "win32":
                os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except OSError as e:
            raise HTTPException(500, f"打开失败：{e}")
        return {"ok": True}

    static_dir = Path(__file__).parent.parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app
