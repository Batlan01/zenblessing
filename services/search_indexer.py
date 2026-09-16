# services/search_indexer.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from services import search_db

log = logging.getLogger(__name__)

DEFAULT_INCLUDE_EXT: set[str] = set()
DEFAULT_EXCLUDE_EXT: set[str] = {
    "tmp", "log", "bak", "cache", "lock",
    "pyc", "pyo", "pyd", "class", "db-shm", "db-wal",
}
SKIP_DIR_PATTERNS: list[str] = [
    "node_modules", "__pycache__", ".git", ".svn",
    "$RECYCLE.BIN", "System Volume Information",
    "Windows", "Program Files", "ProgramData",
]

BATCH_SIZE     = 500
PROGRESS_EVERY = 150
MAX_FILE_SIZE  = 5 * 1024 ** 3

_lock   = threading.Lock()
_thread: Optional[threading.Thread] = None


def is_running() -> bool:
    """Valódi szálreferencia alapján — nem ragad bent ha a szál elszáll."""
    return _thread is not None and _thread.is_alive()


def _safe_meta(k: str, v: str) -> None:
    """set_meta ami nem dob kivételt ha a DB pillanatnyilag nem elérhető."""
    try:
        search_db.set_meta(k, v)
    except Exception as e:
        log.warning("set_meta hiba (%s): %s", k, e)


def _drive_label(path: str) -> str:
    p = path.replace("/", "\\")
    if p.startswith("\\\\"):
        parts = p.lstrip("\\").split("\\", 2)
        return f"\\\\{parts[0]}\\{parts[1]}" if len(parts) >= 2 else p[:32]
    if len(p) >= 2 and p[1] == ":":
        return p[:2].upper()
    return ""


def _should_skip_dir(name: str) -> bool:
    nl = name.lower()
    return any(pat.lower() in nl for pat in SKIP_DIR_PATTERNS)


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1:].lower() if dot >= 1 else ""


def _build_file_row(entry: os.DirEntry, priority: int, drive: str) -> Optional[tuple]:
    try:
        stat = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    if stat.st_size > MAX_FILE_SIZE:
        return None
    ext = _ext(entry.name)
    if ext in DEFAULT_EXCLUDE_EXT:
        return None
    if DEFAULT_INCLUDE_EXT and ext not in DEFAULT_INCLUDE_EXT:
        return None
    return (entry.name, entry.path, search_db.path_hash(entry.path),
            ext, stat.st_size, stat.st_mtime, drive, priority, 0)


def _build_dir_row(path: str, name: str, priority: int, drive: str) -> tuple:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return (name, path, search_db.path_hash(path), "", 0, mtime, drive, priority, 1)


def _flush(batch: list, counters: dict) -> None:
    if not batch:
        return
    try:
        search_db.bulk_upsert(batch)
    except Exception as e:
        log.error("bulk_upsert hiba: %s", e)
        counters["errors"] += 1
    batch.clear()
    _safe_meta("progress",
        f"{counters['indexed']:,} fájl | "
        f"{counters['dirs']:,} mappa | "
        f"{counters['skipped']:,} kihagyva | "
        f"{counters['errors']} hiba"
    )


def _scan_root(root_path: str, priority: int, recursive: bool,
               seen: set, batch: list, counters: dict) -> None:
    drive = _drive_label(root_path)
    stack = [root_path]
    last_prog = 0

    while stack:
        current = stack.pop()
        _safe_meta("current_dir", current[:140])
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if _should_skip_dir(entry.name):
                            continue
                        ph = search_db.path_hash(entry.path)
                        if ph not in seen:
                            seen.add(ph)
                            batch.append(_build_dir_row(entry.path, entry.name, priority, drive))
                            counters["dirs"] += 1
                        if recursive:
                            stack.append(entry.path)
                        continue

                    row = _build_file_row(entry, priority, drive)
                    if row is None:
                        counters["skipped"] += 1
                        continue
                    ph = row[2]
                    if ph in seen:
                        counters["skipped"] += 1
                        continue
                    seen.add(ph)
                    batch.append(row)
                    counters["indexed"] += 1

                    total = counters["indexed"] + counters["dirs"]
                    if len(batch) >= BATCH_SIZE or total - last_prog >= PROGRESS_EVERY:
                        _flush(batch, counters)
                        last_prog = total

        except PermissionError:
            counters["errors"] += 1
        except OSError as e:
            log.warning("scan hiba (%s): %s", current, e)
            counters["errors"] += 1


def run_full_index(roots: Optional[list[dict]] = None) -> dict:
    counters = {"indexed": 0, "dirs": 0, "skipped": 0, "errors": 0}
    t0 = time.monotonic()
    try:
        _safe_meta("status",      "running")
        _safe_meta("progress",    "Indítás…")
        _safe_meta("last_error",  "")
        _safe_meta("current_dir", "")

        if roots is None:
            roots = search_db.get_roots()
        active = [r for r in roots if r.get("active", 1)]
        if not active:
            _safe_meta("status",   "idle")
            _safe_meta("progress", "Nincs aktív root.")
            return {"ok": True, "msg": "Nincs aktív root.", **counters}

        seen:  set[str]    = set()
        batch: list[tuple] = []

        for root in active:
            rp = root["path"]
            if not os.path.exists(rp):
                log.warning("Root nem elérhető: %s", rp)
                counters["errors"] += 1
                continue
            log.info("Indexelés: %s (prio=%s)", rp, root["priority"])
            _safe_meta("progress", f"Scan: {rp}")
            _scan_root(rp, int(root["priority"]), bool(root.get("recursive", 1)),
                       seen, batch, counters)

        _flush(batch, counters)

        elapsed = time.monotonic() - t0
        msg = (f"{counters['indexed']:,} fájl | "
               f"{counters['dirs']:,} mappa | "
               f"{counters['skipped']:,} kihagyva | "
               f"{counters['errors']} hiba | "
               f"{elapsed:.1f}s")
        log.info("Indexelés kész: %s", msg)
        _safe_meta("status",      "idle")
        _safe_meta("progress",    msg)
        _safe_meta("current_dir", "")
        return {"ok": True, "msg": msg, **counters}

    except Exception as e:
        log.exception("Indexelés kritikus hiba")
        _safe_meta("status",      "error")
        _safe_meta("last_error",  str(e))
        _safe_meta("current_dir", "")
        return {"ok": False, "msg": str(e), **counters}


def run_full_index_async(roots: Optional[list[dict]] = None) -> bool:
    global _thread
    with _lock:
        if is_running():
            return False
        _thread = threading.Thread(target=run_full_index, args=(roots,), daemon=True)
        _thread.start()
    return True


def run_incremental(roots: Optional[list[dict]] = None, since_seconds: int = 900) -> dict:
    global _thread
    with _lock:
        if is_running():
            return {"ok": False, "msg": "Indexelés már fut."}
        _thread = threading.Thread(
            target=_run_incremental_inner,
            args=(roots, since_seconds),
            daemon=True,
        )
        _thread.start()
    return {"ok": True}


def _run_incremental_inner(roots, since_seconds):
    counters = {"indexed": 0, "dirs": 0, "skipped": 0, "errors": 0}
    cutoff = time.time() - since_seconds
    try:
        _safe_meta("status",    "running")
        _safe_meta("last_error","")
        _safe_meta("progress",  "Inkrementális frissítés…")
        if roots is None:
            roots = search_db.get_roots()
        active = [r for r in roots if r.get("active", 1)]
        seen:  set[str]    = set()
        batch: list[tuple] = []
        for root in active:
            rp = root["path"]
            if not os.path.exists(rp):
                continue
            drive = _drive_label(rp)
            stack = [rp]
            while stack:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            if entry.is_dir(follow_symlinks=False):
                                if not _should_skip_dir(entry.name) and root.get("recursive", 1):
                                    stack.append(entry.path)
                                continue
                            try:
                                if entry.stat().st_mtime < cutoff:
                                    continue
                            except OSError:
                                continue
                            row = _build_file_row(entry, int(root["priority"]), drive)
                            if row and row[2] not in seen:
                                seen.add(row[2])
                                batch.append(row)
                                counters["indexed"] += 1
                                if len(batch) >= BATCH_SIZE:
                                    _flush(batch, counters)
                except (PermissionError, OSError):
                    counters["errors"] += 1
        _flush(batch, counters)
        _safe_meta("status", "idle")
    except Exception as e:
        log.exception("Inkrementális indexelés hiba")
        _safe_meta("status",     "error")
        _safe_meta("last_error", str(e))