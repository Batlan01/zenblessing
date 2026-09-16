# services/wo_done.py
# -*- coding: utf-8 -*-
"""
Munkarendelések "kész" jelzése.

Csak jelzés értékű: nem befolyásol semmilyen logikát, nem zár le semmit –
kizárólag vizuálisan jelöli a TeamLeader oldal tábláiban (OTD, WO kereső,
Összeszerelési állapotok, Beérkező), hogy az adott WO-val végeztek.

A jelzést a TL oldalt használó bárki átállíthatja, és MINDENKI látja.
Saját táblában él, hogy a meglévő workorder_priority tábla érintetlen maradjon.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

import mysql.connector

from services.dbpool import get_pooled_connection

log = logging.getLogger(__name__)

_RETRYABLE_ERRNOS = {2006, 2013, 2055}

_schema_ready = False
_schema_lock = threading.Lock()


def _execute(query: str, params: tuple | list = (), *,
             fetchone: bool = False, fetchall: bool = False):
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        conn = None
        cur = None
        try:
            conn = get_pooled_connection()
            cur = conn.cursor(dictionary=True, buffered=True)
            cur.execute(query, params or ())
            if cur.with_rows:
                data = cur.fetchone() if fetchone else cur.fetchall()
            else:
                data = cur.rowcount
            try:
                needs_commit = bool(conn.in_transaction)
            except Exception:
                needs_commit = True
            if needs_commit:
                conn.commit()
            return data
        except mysql.connector.Error as e:
            last_err = e
            if attempt == 1 and getattr(e, "errno", None) in _RETRYABLE_ERRNOS:
                continue
            raise
        finally:
            try:
                if cur is not None:
                    cur.close()
            except Exception:
                pass
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    raise last_err


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        _execute("""
            CREATE TABLE IF NOT EXISTS workorder_done (
                wo         VARCHAR(64)  NOT NULL PRIMARY KEY,
                done       TINYINT(1)   NOT NULL DEFAULT 0,
                updated_by VARCHAR(190) NOT NULL DEFAULT '',
                updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                           ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        _schema_ready = True


def norm_wo(wo: Any) -> str:
    return str(wo or "").strip()


def get_done_map() -> dict[str, dict]:
    """
    { "251796": {"by": "ntrencik", "at": "2026-09-08 10:12"} }
    Csak a késznek jelölt WO-k szerepelnek benne.
    """
    ensure_schema()
    rows = _execute("SELECT wo, updated_by, updated_at FROM workorder_done "
                    "WHERE done = 1", fetchall=True) or []
    out = {}
    for r in rows:
        at = r.get("updated_at")
        out[str(r["wo"])] = {
            "by": r.get("updated_by") or "",
            "at": at.strftime("%Y-%m-%d %H:%M") if hasattr(at, "strftime") else str(at or ""),
        }
    return out


def set_done(wo: Any, done: bool, actor: str = "") -> dict:
    """Kész jelzés be/ki. Visszaadja a WO friss állapotát."""
    ensure_schema()
    wo_s = norm_wo(wo)
    if not wo_s:
        raise ValueError("Hiányzó WO azonosító.")
    if len(wo_s) > 64:
        raise ValueError("Túl hosszú WO azonosító.")

    _execute(
        "INSERT INTO workorder_done (wo, done, updated_by) VALUES (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE done = VALUES(done), "
        "updated_by = VALUES(updated_by), updated_at = CURRENT_TIMESTAMP",
        (wo_s, 1 if done else 0, str(actor or "")[:190]),
    )
    log.info("wo_done: %s -> %s (%s)", wo_s, "KÉSZ" if done else "nem kész", actor)
    return {"wo": wo_s, "done": bool(done), "by": str(actor or "")}
