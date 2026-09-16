# services/search_watcher.py
# -*- coding: utf-8 -*-
"""
Valós idejű fájlrendszer figyelő watchdog alapján.
Minden esemény thread-local DB kapcsolaton keresztül kerül feldolgozásra.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_observer = None
_debounce_timers: dict[str, threading.Timer] = {}
_lock = threading.Lock()
DEBOUNCE_SEC = 2.0


# ── Lazy import watchdog (nem kötelező függőség) ──────────────────────────────

def _get_watchdog():
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
        return Observer, FileSystemEventHandler
    except ImportError:
        return None, None


def _is_drive_root(path: str) -> bool:
    """Igaz ha az útvonal egy meghajtó gyökere pl. C:\\"""
    p = path.strip().rstrip("/")
    # "C:" vagy "C:\\" alakok
    return len(p.rstrip("\\")) == 2 and p[1] == ':'


def _is_unc_path(path: str) -> bool:
    return path.startswith("\\\\")


def _is_watchable(path: str) -> bool:
    """
    Meghajtó gyökere (C:\\) és UNC share root esetén
    a watchdog snapshot-ot keszit ami millió fájlnál lefagy.
    Ezeket kihagyjuk — a scheduler inkrementális futása pótolja.
    """
    if _is_drive_root(path):
        return False
    if _is_unc_path(path):
        return False
    return True


# ── Event handler ─────────────────────────────────────────────────────────────

class _Handler:
    """Nem öröklünk FileSystemEventHandler-ből hogy a lazy import működjön."""

    def __init__(self, priority: int, drive: str):
        self.priority = priority
        self.drive    = drive

    def dispatch(self, event):
        """watchdog ezt hívja minden eseménynél."""
        self.on_any_event(event)

    def on_any_event(self, event):
        try:
            src = getattr(event, "src_path", None)
            dst = getattr(event, "dest_path", None)
            etype = getattr(event, "event_type", "")
            is_dir = getattr(event, "is_directory", False)

            if not src:
                return

            if etype == "deleted":
                _schedule_delete(src)
                return

            if etype == "moved":
                _schedule_delete(src)
                if dst:
                    _schedule_upsert(dst, is_dir, self.priority, self.drive)
                return

            if etype in ("created", "modified"):
                _schedule_upsert(src, is_dir, self.priority, self.drive)
                return

        except Exception as e:
            log.warning("watcher on_any_event hiba: %s", e)


# ── Debounce logika ───────────────────────────────────────────────────────────

def _schedule_upsert(path: str, is_dir: bool, priority: int, drive: str):
    key = f"u:{path}"
    with _lock:
        t = _debounce_timers.pop(key, None)
        if t:
            t.cancel()
        timer = threading.Timer(
            DEBOUNCE_SEC,
            _do_upsert,
            args=(path, is_dir, priority, drive),
        )
        _debounce_timers[key] = timer
        timer.start()


def _schedule_delete(path: str):
    key = f"d:{path}"
    with _lock:
        # Ha volt pending upsert ugyanerre a fájlra, töröljük azt
        upsert_key = f"u:{path}"
        t = _debounce_timers.pop(upsert_key, None)
        if t:
            t.cancel()

        t = _debounce_timers.pop(key, None)
        if t:
            t.cancel()
        timer = threading.Timer(DEBOUNCE_SEC, _do_delete, args=(path,))
        _debounce_timers[key] = timer
        timer.start()


# ── Tényleges DB műveletek ────────────────────────────────────────────────────

def _do_upsert(path: str, is_dir: bool, priority: int, drive: str):
    """Egy fájl/mappa indexbe írása. Saját thread-local DB kapcsolatot használ."""
    with _lock:
        _debounce_timers.pop(f"u:{path}", None)

    try:
        from services import search_db
        from services.search_indexer import DEFAULT_EXCLUDE_EXT

        if not os.path.exists(path):
            log.debug("watcher: fájl már nem létezik, kihagyva: %s", path)
            return

        name  = Path(path).name
        ph    = search_db.path_hash(path)

        if is_dir:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            row = (name, path, ph, "", 0, mtime, drive, priority, 1)
        else:
            try:
                st = os.stat(path)
            except OSError:
                return
            ext = Path(path).suffix.lstrip(".").lower()
            if ext in DEFAULT_EXCLUDE_EXT:
                return
            row = (name, path, ph, ext, st.st_size, st.st_mtime, drive, priority, 0)

        search_db.bulk_upsert([row])
        log.debug("watcher upsert OK: %s", path)

    except Exception as e:
        log.warning("watcher _do_upsert hiba (%s): %s", path, e)


def _do_delete(path: str):
    """Fájl/mappa törlése az indexből."""
    with _lock:
        _debounce_timers.pop(f"d:{path}", None)

    try:
        from services import search_db
        ph = search_db.path_hash(path)
        search_db.delete_by_hash(ph)
        log.debug("watcher delete OK: %s", path)
    except Exception as e:
        log.warning("watcher _do_delete hiba (%s): %s", path, e)


# ── Observer életciklus ───────────────────────────────────────────────────────

def start_watcher() -> bool:
    """
    Elindítja a fajlrendszer figyelo t minden aktiv root-ra.
    Meghajtok gyokere (C:\\, D:\\) es UNC share-ek ki vannak hagyva —
    ezeket a PollingObserver lefagyasztja (millios snapshot).
    Az inkrementalis scheduler (15 perc) potoloja a kimaradt valtozasokat.
    """
    global _observer

    Observer, FileSystemEventHandler = _get_watchdog()
    if Observer is None:
        log.warning("watchdog nincs telepitve — pip install watchdog")
        return False

    if _observer and _observer.is_alive():
        log.info("watcher mar fut")
        return True

    class _WatchHandler(FileSystemEventHandler):
        def __init__(self, priority, drive):
            super().__init__()
            self._h = _Handler(priority, drive)

        def on_any_event(self, event):
            self._h.on_any_event(event)

    try:
        from services import search_db
        roots  = search_db.get_roots()
        active = [r for r in roots if r.get("active", 1)]
    except Exception as e:
        log.warning("watcher: get_roots hiba: %s", e)
        return False

    if not active:
        log.info("watcher: nincs aktiv root")
        return False

    _observer = Observer()
    watched   = 0
    skipped   = 0

    for root in active:
        rp = root["path"]

        if not _is_watchable(rp):
            log.info(
                "watcher: '%s' meghajtogyor/UNC, kihagyva "
                "(az inkrementalis scheduler kezeli)", rp
            )
            skipped += 1
            continue

        if not os.path.exists(rp):
            log.warning("watcher: root nem elerheto, kihagyva: %s", rp)
            continue

        try:
            from services.search_indexer import _drive_label
            drive     = _drive_label(rp)
            handler   = _WatchHandler(priority=int(root["priority"]), drive=drive)
            recursive = bool(root.get("recursive", 1))
            _observer.schedule(handler, rp, recursive=recursive)
            watched += 1
            log.info("watcher figyel: %s (rekurziv=%s)", rp, recursive)
        except Exception as e:
            log.warning("watcher schedule hiba (%s): %s", rp, e)

    if watched == 0:
        msg = "meghajtogyor/UNC rootok ki vannak hagyva" if skipped else "egyetlen root sem indult el"
        log.info("watcher: %s — inkrementalis scheduler kezeli a valtozasokat", msg)
        _observer = None
        return False

    _observer.start()
    log.info("watcher elindult, %d root figyelve, %d kihagyva", watched, skipped)
    return True


def stop_watcher():
    global _observer
    # Függőben lévő timerek törlése
    with _lock:
        for t in _debounce_timers.values():
            try: t.cancel()
            except: pass
        _debounce_timers.clear()

    if _observer:
        try:
            _observer.stop()
            _observer.join(timeout=5)
        except Exception as e:
            log.warning("watcher stop hiba: %s", e)
        _observer = None
    log.info("watcher leállt")


def restart_watcher() -> bool:
    stop_watcher()
    time.sleep(0.3)
    return start_watcher()


def is_running() -> bool:
    return _observer is not None and _observer.is_alive()