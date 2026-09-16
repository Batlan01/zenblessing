# services/search_db.py
# -*- coding: utf-8 -*-
"""
Thread-safe MariaDB kapcsolat threading.local() alapján.
Minden szál (Flask worker, indexelő, watcher) saját kapcsolatot kap.
"""
from __future__ import annotations
import hashlib, logging, re, threading, time
from typing import Optional
import mysql.connector

log = logging.getLogger(__name__)

_DB_CFG: dict = {}
_local = threading.local()          # minden szálnak saját _local.db
_RECONNECT_ERRNOS = {2006, 2013, 2055}


def init_app(app) -> None:
    global _DB_CFG
    _DB_CFG = {
        "host":               app.config.get("DB_HOST",     "10.10.2.15"),
        "user":               app.config.get("DB_USER",     "root"),
        "password":           app.config.get("DB_PASSWORD", "admin321"),
        "database":           app.config.get("DB_NAME",     "paperless"),
        "connection_timeout": 10,
        "charset":            "utf8",
        "autocommit":         False,
    }
    ensure_schema()


# ── Thread-local kapcsolat ────────────────────────────────────────────────────

def _conn() -> mysql.connector.MySQLConnection:
    """
    Visszaadja az aktuális szál saját kapcsolatát.
    Ha nincs, vagy megszakadt, újat nyit.
    """
    db = getattr(_local, "db", None)
    try:
        if db and db.is_connected():
            db.ping(reconnect=True, attempts=2, delay=1)
            return db
    except Exception:
        pass
    db = mysql.connector.connect(**_DB_CFG)
    _local.db = db
    return db


def _exec(sql: str, params=(), *, fetchone=False, fetchall=False):
    """Retry logika 3 kísérlettel reconnect-errorno-kra."""
    for attempt in range(3):
        try:
            conn = _conn()
            cur  = conn.cursor(dictionary=True, buffered=True)
            try:
                cur.execute(sql, params or ())
                if fetchone:  return cur.fetchone()
                if fetchall:  return cur.fetchall()
                conn.commit()
                return cur.lastrowid
            finally:
                try: cur.close()
                except: pass
        except mysql.connector.Error as e:
            if e.errno in _RECONNECT_ERRNOS and attempt < 2:
                log.warning("DB kapcsolat elveszett (%s) szál=%s, retry %d/3",
                            e.errno, threading.current_thread().name, attempt + 1)
                _local.db = None
                time.sleep(0.5 * (attempt + 1))
                continue
            raise


# ── Séma ─────────────────────────────────────────────────────────────────────

def ensure_schema() -> None:
    _exec("""
        CREATE TABLE IF NOT EXISTS search_files (
            id         BIGINT AUTO_INCREMENT PRIMARY KEY,
            name       VARCHAR(512) NOT NULL,
            path       TEXT         NOT NULL,
            path_hash  CHAR(64)     NOT NULL,
            ext        VARCHAR(32)  DEFAULT '',
            size       BIGINT       DEFAULT 0,
            mtime      DOUBLE       DEFAULT 0,
            drive      VARCHAR(64)  DEFAULT '',
            priority   TINYINT      DEFAULT 5,
            is_dir     TINYINT      DEFAULT 0,
            indexed_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_hash (path_hash),
            KEY idx_ext   (ext),
            KEY idx_drive (drive),
            KEY idx_isdir (is_dir),
            KEY idx_prio  (priority),
            KEY idx_name  (name(64))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # Meglévő sémára szükséges migráció
    for ddl in [
        "ALTER TABLE search_files ADD COLUMN is_dir TINYINT DEFAULT 0 AFTER priority",
        "ALTER TABLE search_files ADD KEY idx_isdir (is_dir)",
        "ALTER TABLE search_files ADD KEY idx_name  (name(64))",
        "ALTER TABLE search_files ADD FULLTEXT KEY ft_name (name)",
    ]:
        try:
            _exec(ddl)
        except mysql.connector.Error as e:
            if e.errno not in (1060, 1061, 1091):  # dup col / dup key / missing key
                log.warning("schema migration warning: %s", e)

    _exec(
        "CREATE TABLE IF NOT EXISTS search_roots ("
        "    id INT AUTO_INCREMENT PRIMARY KEY,"
        "    path VARCHAR(1024) NOT NULL,"
        "    priority TINYINT DEFAULT 5,"
        "    `recursive` TINYINT DEFAULT 1,"
        "    active TINYINT DEFAULT 1,"
        "    label VARCHAR(128) DEFAULT '',"
        "    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )
    _exec("""
        CREATE TABLE IF NOT EXISTS search_index_meta (
            k VARCHAR(64) PRIMARY KEY, v TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    log.info("search_db: schema OK (thread=%s)", threading.current_thread().name)


# ── Hash ─────────────────────────────────────────────────────────────────────

def path_hash(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()


# ── Írás ─────────────────────────────────────────────────────────────────────

def bulk_upsert(rows: list[tuple]) -> None:
    """rows: (name, path, path_hash, ext, size, mtime, drive, priority, is_dir)"""
    if not rows:
        return
    for attempt in range(3):
        try:
            conn = _conn()
            cur  = conn.cursor(buffered=True)
            cur.executemany(
                """
                INSERT INTO search_files
                    (name, path, path_hash, ext, size, mtime, drive, priority, is_dir)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name), size=VALUES(size), mtime=VALUES(mtime),
                    priority=VALUES(priority), is_dir=VALUES(is_dir), indexed_at=NOW()
                """,
                rows,
            )
            conn.commit()
            cur.close()
            return
        except mysql.connector.Error as e:
            if e.errno in _RECONNECT_ERRNOS and attempt < 2:
                log.warning("bulk_upsert retry %d/3 (errno=%s)", attempt + 1, e.errno)
                _local.db = None
                time.sleep(0.5 * (attempt + 1))
                continue
            raise


def delete_by_hash(path_hash_val: str) -> None:
    _exec("DELETE FROM search_files WHERE path_hash=%s", (path_hash_val,))


# ── Keresés ───────────────────────────────────────────────────────────────────

def _tokenize(q: str) -> list[str]:
    """Szóközök, kötőjelek, aláhúzások mentén tokenizál. Min 1 kar."""
    tokens = re.findall(r'[^\s\-_,;|]+', q.strip())
    return [t for t in tokens if len(t) >= 1]


def _bool_query(tokens: list[str]) -> Optional[str]:
    """
    FULLTEXT boolean query építés.
    Csak a >= 3 karakteres tokeneket veszi be (MariaDB ft_min_word_len=3 tipikus).
    """
    parts = []
    for t in tokens:
        c = re.sub(r'[^\w\-._]', '', t, flags=re.UNICODE)
        if len(c) >= 3:
            parts.append(f"+{c}*")
    return " ".join(parts) if parts else None


def search_files(
    q: str,
    ext_filter:   Optional[str] = None,
    drive_filter: Optional[str] = None,
    type_filter:  Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """
    Kombinált FULLTEXT + LIKE keresés:
    - Ha a query tartalmaz >= 3 karakteres tokent → FULLTEXT boolean + rendezés score szerint
    - LIKE fallback: minden tokent LIKE-kal keres (AND logika)
    - Rövid keresőknél (1-2 kar) csak LIKE
    - Eredmények deduplikálva, prioritás + relevancia szerint rendezve
    """
    q = (q or "").strip()
    if not q:
        return []

    tokens   = _tokenize(q)
    ft_query = _bool_query(tokens)

    def _filters(params: list) -> str:
        sql = ""
        if ext_filter:
            sql += " AND ext=%s"; params.append(ext_filter.lower().lstrip("."))
        if drive_filter:
            sql += " AND drive=%s"; params.append(drive_filter)
        if type_filter == "file":
            sql += " AND is_dir=0"
        elif type_filter == "dir":
            sql += " AND is_dir=1"
        return sql

    results_ft   = []
    results_like = []

    # ── FULLTEXT ág ──────────────────────────────────────────────
    if ft_query:
        try:
            params: list = [ft_query, ft_query]
            sql = (
                "SELECT id,name,path,ext,size,mtime,drive,priority,is_dir,"
                "MATCH(name) AGAINST(%s IN BOOLEAN MODE) AS score "
                "FROM search_files "
                "WHERE MATCH(name) AGAINST(%s IN BOOLEAN MODE)"
            )
            sql += _filters(params)
            sql += " ORDER BY priority DESC, score DESC LIMIT %s"
            params.append(limit)
            results_ft = _exec(sql, params, fetchall=True) or []
        except mysql.connector.Error as e:
            log.warning("FULLTEXT keresés hiba: %s", e)

    # ── LIKE ág (mindig fut, kiegészíti a FULLTEXT-et) ────────────
    # Minden tokenre LIKE feltétel — legfontosabb: az eredeti lekérdezés IS szerepel
    like_conditions = []
    like_params: list = []

    # Teljes q mint egy LIKE feltétel (pl. "WO-1234" egyben)
    like_conditions.append("name LIKE %s")
    like_params.append(f"%{q}%")

    # + minden egyes token külön (ha több szó volt)
    for t in tokens:
        if t.lower() not in q.lower().replace(q, ""):  # ne duplikáljuk ha token == q
            like_conditions.append("name LIKE %s")
            like_params.append(f"%{t}%")

    # OR logika a LIKE feltételek között (union-szerűen)
    where = "(" + " OR ".join(like_conditions) + ")"

    try:
        params2: list = like_params[:]
        sql2 = (
            f"SELECT id,name,path,ext,size,mtime,drive,priority,is_dir,0 AS score "
            f"FROM search_files WHERE {where}"
        )
        sql2 += _filters(params2)
        sql2 += " ORDER BY priority DESC, name LIMIT %s"
        params2.append(limit)
        results_like = _exec(sql2, params2, fetchall=True) or []
    except mysql.connector.Error as e:
        log.warning("LIKE keresés hiba: %s", e)

    # ── Deduplikáció + összefésülés ──────────────────────────────
    seen_ids: set = set()
    merged: list[dict] = []

    # FULLTEXT eredmények előre (relevánsabbak)
    for r in results_ft:
        d = dict(r)
        if d["id"] not in seen_ids:
            seen_ids.add(d["id"])
            merged.append(d)

    # LIKE kiegészítés (amit FULLTEXT nem talált)
    for r in results_like:
        d = dict(r)
        if d["id"] not in seen_ids:
            seen_ids.add(d["id"])
            merged.append(d)

    return merged[:limit]


# ── Statisztika ───────────────────────────────────────────────────────────────

def get_stats() -> dict:
    row  = _exec(
        "SELECT COUNT(*) AS total, SUM(is_dir=0) AS files, "
        "SUM(is_dir=1) AS dirs, MAX(indexed_at) AS last_index "
        "FROM search_files",
        fetchone=True,
    )
    meta = {r["k"]: r["v"] for r in (_exec("SELECT k,v FROM search_index_meta", fetchall=True) or [])}
    return {
        "total":       int((row or {}).get("total")  or 0),
        "files":       int((row or {}).get("files")  or 0),
        "dirs":        int((row or {}).get("dirs")   or 0),
        "last_index":  str((row or {}).get("last_index") or "-"),
        "status":      meta.get("status",      "idle"),
        "progress":    meta.get("progress",    ""),
        "last_error":  meta.get("last_error",  ""),
        "current_dir": meta.get("current_dir", ""),
    }


def get_ext_list() -> list[str]:
    rows = _exec(
        "SELECT DISTINCT ext FROM search_files WHERE ext!='' AND is_dir=0 ORDER BY ext",
        fetchall=True,
    ) or []
    return [r["ext"] for r in rows]


def get_drives() -> list[str]:
    rows = _exec(
        "SELECT DISTINCT drive FROM search_files WHERE drive!='' ORDER BY drive",
        fetchall=True,
    ) or []
    return [r["drive"] for r in rows]


def set_meta(k: str, v: str) -> None:
    _exec(
        "INSERT INTO search_index_meta (k,v) VALUES (%s,%s) "
        "ON DUPLICATE KEY UPDATE v=VALUES(v)",
        (k, v),
    )


def clear_index() -> int:
    for attempt in range(3):
        try:
            conn = _conn()
            cur  = conn.cursor(buffered=True)
            cur.execute("DELETE FROM search_files")
            deleted = cur.rowcount
            conn.commit()
            cur.close()
            set_meta("status",      "idle")
            set_meta("progress",    f"{deleted:,} bejegyzés törölve.")
            set_meta("last_error",  "")
            set_meta("current_dir", "")
            return deleted
        except mysql.connector.Error as e:
            if e.errno in _RECONNECT_ERRNOS and attempt < 2:
                _local.db = None
                time.sleep(1.0)
                continue
            raise


# ── Root CRUD ─────────────────────────────────────────────────────────────────

def get_roots() -> list[dict]:
    return [dict(r) for r in (
        _exec("SELECT * FROM search_roots ORDER BY priority DESC, id", fetchall=True) or []
    )]


def add_root(path: str, priority: int = 5, recursive: bool = True, label: str = "") -> int:
    return _exec(
        "INSERT INTO search_roots (path, priority, `recursive`, label) VALUES (%s,%s,%s,%s)",
        (path.strip(), priority, int(recursive), label),
    )


def toggle_root(root_id: int, active: bool) -> None:
    _exec("UPDATE search_roots SET active=%s WHERE id=%s", (int(active), root_id))


def delete_root(root_id: int) -> None:
    _exec("DELETE FROM search_roots WHERE id=%s", (root_id,))