# services/bartender_project_creator_report_core.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Space miatt backtick kell
TABLE_NAME = "`etiket data`"

# Ennyi STR_TO_DATE ág van -> ennyi param kell ugyanarra az értékre
_DT_PATTERNS = 8


def ensure_bartender_project_activity_schema(conn) -> None:
    """
    Létrehozza a history + archive táblákat, ha még nem léteznek.
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bartender_project_history (
                id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                pn          VARCHAR(120) NOT NULL,
                rev         VARCHAR(40)  NOT NULL DEFAULT '',
                ecn         VARCHAR(40)  NOT NULL DEFAULT '',
                action      VARCHAR(40)  NOT NULL DEFAULT 'save',
                username    VARCHAR(120) NOT NULL DEFAULT 'unknown',
                saved_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_pn_rev_ecn (pn, rev, ecn),
                INDEX idx_saved_at  (saved_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()
    finally:
        cur.close()

    # archív tábla (időbélyeges snapshotok, soha nincs felülírás)
    ensure_bartender_archive_schema(conn)

    # history.archive_id oszlop (link az archív snapshotra) – ha már létezik, nem baj
    cur = conn.cursor()
    try:
        cur.execute("""
            ALTER TABLE bartender_project_history
            ADD COLUMN archive_id BIGINT UNSIGNED NULL DEFAULT NULL
        """)
        conn.commit()
    except Exception:
        # duplicate column / nincs jog -> nem gond
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        cur.close()


def ensure_bartender_archive_schema(conn) -> None:
    """
    bartender_archive: minden mentésről időbélyeges snapshot.
    Nincs UPDATE / felülírás – csak INSERT.
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bartender_archive (
                id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                pn          VARCHAR(120) NOT NULL,
                rev         VARCHAR(40)  NOT NULL DEFAULT '',
                ecn         VARCHAR(40)  NOT NULL DEFAULT '',
                action      VARCHAR(40)  NOT NULL DEFAULT 'save',
                username    VARCHAR(120) NOT NULL DEFAULT 'unknown',
                summary     LONGTEXT     NULL,
                id_template VARCHAR(255) NULL,
                printers    TEXT         NULL,
                saved_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_arch_pn_rev_ecn (pn, rev, ecn),
                INDEX idx_arch_saved_at   (saved_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()
    finally:
        cur.close()


def insert_archive_snapshot(
    conn,
    *,
    pn: str,
    rev: str,
    ecn: str,
    username: str,
    action: str = "save",
    summary_json: Optional[str] = None,
    id_template: Optional[str] = None,
    printers_json: Optional[str] = None,
) -> Optional[int]:
    """
    Időbélyeges snapshot felvétele a bartender_archive táblába.
    Hívás: minden save_label_config mentés után (a fő mentés MELLETT, nem helyette).
    Visszatér: az új archív sor id-ja, vagy None hiba esetén.
    Hiba esetén nem dob kivételt (a fő mentés már sikeres volt).
    """
    try:
        # best-effort: ha még nincs tábla, hozzuk létre
        try:
            ensure_bartender_archive_schema(conn)
        except Exception:
            pass

        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO bartender_archive
                    (pn, rev, ecn, action, username, summary, id_template, printers, saved_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                """,
                (
                    (pn or "").strip(),
                    (rev or "").strip(),
                    (ecn or "").strip(),
                    (action or "save").strip(),
                    (username or "unknown").strip(),
                    summary_json,
                    (id_template or "").strip() or None,
                    printers_json,
                ),
            )
            conn.commit()
            return int(cur.lastrowid) if cur.lastrowid else None
        finally:
            cur.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def prune_project_archives(
    conn,
    *,
    pn: str,
    rev: str,
    ecn: str,
    keep: int = 30,
) -> int:
    """
    Retention: egy projekthez (PN/REV/ECN) csak a legutóbbi `keep` snapshotot tartja meg,
    a régebbieket törli. A history sorok megmaradnak, csak az archive_id mutathat
    olyan snapshotra, amit töröltünk (a report ilyenkor "—"-t mutat).

    Best-effort: hiba esetén nem dob kivételt, 0-t ad vissza.
    Visszatér: a törölt sorok száma.
    """
    try:
        keep = max(1, min(int(keep or 30), 10000))
    except Exception:
        keep = 30

    pn  = (pn or "").strip()
    rev = (rev or "").strip()
    ecn = (ecn or "").strip()

    try:
        cur = conn.cursor()
        try:
            # a megtartandó legutóbbi `keep` id közül a legkisebb (= küszöb)
            cur.execute(
                """
                SELECT id
                FROM bartender_archive
                WHERE pn = %s AND rev = %s AND ecn = %s
                ORDER BY saved_at DESC, id DESC
                LIMIT 1 OFFSET %s
                """,
                (pn, rev, ecn, keep),
            )
            row = cur.fetchone()
            if not row:
                # nincs a keep-nél több snapshot -> nincs mit törölni
                return 0

            threshold_id = row[0]

            # mindent törlünk, ami a küszöbnél régebbi/egyenlő (a küszöb maga is a (keep+1)-edik)
            cur.execute(
                """
                DELETE FROM bartender_archive
                WHERE pn = %s AND rev = %s AND ecn = %s
                  AND id <= %s
                """,
                (pn, rev, ecn, threshold_id),
            )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            cur.close()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def fetch_archive_row(conn, archive_id: int) -> Optional[Dict[str, Any]]:
    """
    Egy archív snapshot teljes sora id alapján.
    """
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, pn, rev, ecn, action, username, summary, id_template, printers, saved_at
            FROM bartender_archive
            WHERE id = %s
            LIMIT 1
            """,
            (int(archive_id),),
        )
        row = cur.fetchone()
    finally:
        cur.close()

    if not row:
        return None

    ts = row.get("saved_at")
    return {
        "id":          row.get("id"),
        "pn":          row.get("pn") or "",
        "rev":         row.get("rev") or "",
        "ecn":         row.get("ecn") or "",
        "action":      row.get("action") or "save",
        "username":    row.get("username") or "unknown",
        "summary":     row.get("summary"),
        "id_template": row.get("id_template") or "",
        "printers":    row.get("printers"),
        "saved_at":    ts.isoformat(sep=" ") if ts else None,
    }


def list_project_archives(
    conn,
    *,
    pn: str,
    rev: str,
    ecn: str,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """
    Egy projekt archív snapshotjai (meta, summary nélkül), legújabbtól.
    """
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, pn, rev, ecn, action, username, saved_at
            FROM bartender_archive
            WHERE pn = %s AND rev = %s AND ecn = %s
            ORDER BY saved_at DESC, id DESC
            LIMIT %s
            """,
            (
                (pn or "").strip(),
                (rev or "").strip(),
                (ecn or "").strip(),
                max(1, min(int(limit), 2000)),
            ),
        )
        rows = cur.fetchall()
    finally:
        cur.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        ts = r.get("saved_at")
        out.append({
            "id":       r.get("id"),
            "action":   r.get("action") or "save",
            "username": r.get("username") or "unknown",
            "saved_at": ts.isoformat(sep=" ") if ts else None,
        })
    return out


def insert_project_history(
    conn,
    *,
    pn: str,
    rev: str,
    ecn: str,
    username: str,
    action: str = "save",
    archive_id: Optional[int] = None,
) -> None:
    """
    Egyetlen sor felvétele a history táblába.
    Hívás: minden save_label_config mentés után.
    archive_id: a hozzá tartozó bartender_archive snapshot id-ja (ha készült).
    Hiba esetén csak logol, nem dob kivételt (hogy a fő mentés ne törjön).
    """
    base_params = (
        (pn or "").strip(),
        (rev or "").strip(),
        (ecn or "").strip(),
        (action or "save").strip(),
        (username or "unknown").strip(),
    )
    try:
        cur = conn.cursor()
        try:
            try:
                cur.execute(
                    """
                    INSERT INTO bartender_project_history (pn, rev, ecn, action, username, archive_id, saved_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    """,
                    base_params + (int(archive_id) if archive_id else None,),
                )
            except Exception:
                # fallback: ha az archive_id oszlop még nem létezik (régi séma)
                try:
                    conn.rollback()
                except Exception:
                    pass
                cur.execute(
                    """
                    INSERT INTO bartender_project_history (pn, rev, ecn, action, username, saved_at)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    """,
                    base_params,
                )
            conn.commit()
        finally:
            cur.close()
    except Exception:
        # nem dobunk tovább – a fő mentés már sikeres volt
        try:
            conn.rollback()
        except Exception:
            pass


def query_project_history(
    conn,
    *,
    pn: str,
    rev: str,
    ecn: str,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """
    Egy adott projekt szerkesztési előzményei, legújabbtól a legrégebbiig.
    """
    cur = conn.cursor(dictionary=True)
    params = (
        (pn or "").strip(),
        (rev or "").strip(),
        (ecn or "").strip(),
        max(1, min(int(limit), 2000)),
    )
    try:
        try:
            cur.execute(
                """
                SELECT id, pn, rev, ecn, action, username, archive_id, saved_at
                FROM bartender_project_history
                WHERE pn = %s AND rev = %s AND ecn = %s
                ORDER BY saved_at DESC
                LIMIT %s
                """,
                params,
            )
        except Exception:
            # fallback: régi séma archive_id nélkül
            cur.execute(
                """
                SELECT id, pn, rev, ecn, action, username, saved_at
                FROM bartender_project_history
                WHERE pn = %s AND rev = %s AND ecn = %s
                ORDER BY saved_at DESC
                LIMIT %s
                """,
                params,
            )
        rows = cur.fetchall()
    finally:
        cur.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        ts = r.get("saved_at")
        out.append({
            "id":         r.get("id"),
            "action":     r.get("action") or "save",
            "username":   r.get("username") or "unknown",
            "archive_id": r.get("archive_id"),
            "saved_at":   ts.isoformat(sep=" ") if ts else None,
        })
    return out


def _dt_expr_sql_value_placeholder() -> str:
    c = "NULLIF(%s, '')"
    return (
        "COALESCE("
        f"STR_TO_DATE({c}, '%Y-%m-%d %H:%i:%S'),"
        f"STR_TO_DATE({c}, '%Y-%m-%d %H:%i'),"
        f"STR_TO_DATE({c}, '%Y.%m.%d %H:%i:%S'),"
        f"STR_TO_DATE({c}, '%Y.%m.%d %H:%i'),"
        f"STR_TO_DATE({c}, '%d.%m.%Y %H:%i:%S'),"
        f"STR_TO_DATE({c}, '%d.%m.%Y %H:%i'),"
        f"STR_TO_DATE({c}, '%d/%m/%Y %H:%i:%S'),"
        f"STR_TO_DATE({c}, '%d/%m/%Y %H:%i')"
        ")"
    )

def _dt_expr(col_sql: str) -> str:
    c = f"NULLIF({col_sql}, '')"
    return (
        "COALESCE("
        f"STR_TO_DATE({c}, '%Y-%m-%d %H:%i:%S'),"
        f"STR_TO_DATE({c}, '%Y-%m-%d %H:%i'),"
        f"STR_TO_DATE({c}, '%Y.%m.%d %H:%i:%S'),"
        f"STR_TO_DATE({c}, '%Y.%m.%d %H:%i'),"
        f"STR_TO_DATE({c}, '%d.%m.%Y %H:%i:%S'),"
        f"STR_TO_DATE({c}, '%d.%m.%Y %H:%i'),"
        f"STR_TO_DATE({c}, '%d/%m/%Y %H:%i:%S'),"
        f"STR_TO_DATE({c}, '%d/%m/%Y %H:%i')"
        ")"
    )



def _project_key_expr() -> str:
    # projekt = PN + REV + ECN
    return "CONCAT_WS(' | ', `PN`, `REV`, `ECN`)"


def _username_expr() -> str:
    # prefer: updated by -> edited by -> UNKNOWN
    return "COALESCE(NULLIF(`updated by`, ''), NULLIF(`edited by`, ''), 'UNKNOWN')"


def _add_dt_param(params: List[Any], value: str) -> None:
    """
    _dt_expr_sql_value_placeholder() _DT_PATTERNS darab %s-t használ,
    ezért ugyanazt az értéket _DT_PATTERNS-szor kell betenni.
    """
    params.extend([value] * _DT_PATTERNS)


def _build_where_and_params(
    *,
    start: Optional[str],
    end: Optional[str],
    username: Optional[str],
    project: Optional[str],
) -> Tuple[str, List[Any]]:
    """
    A frontből jön tipikusan:
      start = 'YYYY-MM-DD 00:00:00'
      end   = 'YYYY-MM-DD 23:59:59'

    username: exact match
    project: exact match: 'PN | REV | ECN'
    """
    dt_create = _dt_expr("`create date`")
    dt_update = _dt_expr("`updated date`")
    dt_last = f"COALESCE({dt_update}, {dt_create})"

    where = ["1=1"]
    params: List[Any] = []

    if start:
        where.append(f"{dt_last} >= {_dt_expr_sql_value_placeholder()}")
        _add_dt_param(params, start)

    if end:
        where.append(f"{dt_last} <= {_dt_expr_sql_value_placeholder()}")
        _add_dt_param(params, end)

    if username:
        where.append(f"{_username_expr()} = %s")
        params.append(username)

    if project:
        where.append(f"{_project_key_expr()} = %s")
        params.append(project)

    return " AND ".join(where), params


def list_bartender_activity_filters(
    conn,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 5000,
) -> Dict[str, List[str]]:
    """
    Dropdownokhoz distinct user/project lista (opcionálisan időszűréssel).
    """
    cur = conn.cursor(dictionary=True)

    where_sql, params = _build_where_and_params(
        start=start, end=end, username=None, project=None
    )

    cur.execute(
        f"""
        SELECT DISTINCT {_username_expr()} AS username
        FROM {TABLE_NAME}
        WHERE {where_sql}
        ORDER BY username
        LIMIT {int(limit)}
        """,
        params,
    )
    users = [r["username"] for r in cur.fetchall() if r.get("username")]

    cur.execute(
        f"""
        SELECT DISTINCT {_project_key_expr()} AS project_name
        FROM {TABLE_NAME}
        WHERE {where_sql}
        ORDER BY project_name
        LIMIT {int(limit)}
        """,
        params,
    )
    projects = [r["project_name"] for r in cur.fetchall() if r.get("project_name")]

    cur.close()
    return {"users": users, "projects": projects}


def query_bartender_project_activity_aggregated(
    conn,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    username: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    """
    Aggregált riport: user × (PN|REV|ECN)

    OUTPUT: a meglévő HTML-hez igazítva:
      - username
      - project_name
      - first_seen, last_seen
      - events_total
      - update_count
      - refresh_count (0)
      - last_action, last_action_ts
      - last_notes (None)
    """
    dt_create = _dt_expr("`create date`")
    dt_update = _dt_expr("`updated date`")
    dt_last = f"COALESCE({dt_update}, {dt_create})"

    where_sql, params = _build_where_and_params(
        start=start, end=end, username=username, project=project
    )

    cur = conn.cursor(dictionary=True)

    cur.execute(
        f"""
        SELECT
            {_username_expr()} AS username,
            {_project_key_expr()} AS project_name,

            MIN({dt_create}) AS first_seen_dt,
            MAX({dt_last}) AS last_seen_dt,

            COUNT(*) AS rows_total,
            SUM(CASE WHEN {dt_update} IS NOT NULL THEN 1 ELSE 0 END) AS updated_rows
        FROM {TABLE_NAME}
        WHERE {where_sql}
        GROUP BY username, project_name
        ORDER BY last_seen_dt DESC
        LIMIT {int(limit)}
        """,
        params,
    )

    rows = cur.fetchall()
    cur.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        first_dt = r.get("first_seen_dt")
        last_dt = r.get("last_seen_dt")

        rows_total = int(r.get("rows_total") or 0)
        updated_rows = int(r.get("updated_rows") or 0)

        out.append(
            {
                "username": r.get("username") or "UNKNOWN",
                "project_name": r.get("project_name") or "UNKNOWN_PROJECT",
                "first_seen": first_dt.isoformat(sep=" ") if first_dt else None,
                "last_seen": last_dt.isoformat(sep=" ") if last_dt else None,
                "events_total": rows_total,
                "update_count": updated_rows,
                "refresh_count": 0,
                "last_action": "create/update (etiket data)",
                "last_action_ts": last_dt.isoformat(sep=" ") if last_dt else None,
                "last_notes": None,
            }
        )

    return out


def query_active_project_locks(
    conn,
    *,
    max_age_sec: int = 180,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """
    Ki min dolgozik: bartender_project_locks + pn_config join.
    max_age_sec: csak a friss heartbeat / lock (NOW - max_age_sec) legyen.
    """
    cur = conn.cursor(dictionary=True)

    max_age_sec = max(10, min(int(max_age_sec or 180), 3600))
    limit = max(1, min(int(limit or 200), 2000))

    # heartbeat_at lehet NULL -> locked_at fallback
    cur.execute(
        f"""
        SELECT
            l.config_id,
            l.locked_by,
            l.lock_token,
            l.locked_at,
            l.heartbeat_at,
            p.PN  AS pn,
            p.REV AS rev,
            p.ECN AS ecn
        FROM bartender_project_locks l
        LEFT JOIN pn_config p ON p.id = l.config_id
        WHERE COALESCE(l.heartbeat_at, l.locked_at) >= (NOW() - INTERVAL %s SECOND)
        ORDER BY COALESCE(l.heartbeat_at, l.locked_at) DESC
        LIMIT {limit}
        """,
        (max_age_sec,),
    )

    rows = cur.fetchall()
    cur.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        pn = r.get("pn") or ""
        rev = r.get("rev") or ""
        ecn = r.get("ecn") or ""
        out.append({
            "config_id": r.get("config_id"),
            "locked_by": r.get("locked_by") or "",
            "locked_at": (r.get("locked_at").isoformat(sep=" ") if r.get("locked_at") else None),
            "heartbeat_at": (r.get("heartbeat_at").isoformat(sep=" ") if r.get("heartbeat_at") else None),
            "pn": pn,
            "rev": rev,
            "ecn": ecn,
            "project_name": " | ".join([x for x in [pn, rev, ecn] if x]),
        })

    return out


def force_delete_project_lock(
    conn,
    *,
    config_id: int,
) -> Dict[str, Any]:
    """
    Admin által kényszerített lock törlés.
    Visszatér: { 'deleted': True/False, 'rows_affected': int }
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM bartender_project_locks WHERE config_id = %s",
            (int(config_id),),
        )
        conn.commit()
        rows = cur.rowcount
    finally:
        cur.close()

    return {"deleted": rows > 0, "rows_affected": rows}


def query_project_size_batch(
    conn,
    project_keys: List[str],
) -> Dict[str, Dict[str, int]]:
    """
    Több projekt méretét adja vissza egyszerre.
    project_keys: ['PN | REV | ECN', ...]

    Visszatér: {
        'PN | REV | ECN': {
            'pages_count': 2,
            'cables_count': 4,   # groups összesen
            'connectors_count': 8,
        },
        ...
    }

    Az etiket data.summary JSON-ból számolja ki Python oldalon —
    nincs szükség JSON_TABLE-re (MySQL 5.7 kompatibilis).
    """
    import json

    if not project_keys:
        return {}

    # Minden unique PN/REV/ECN-t szétbontunk
    parsed_keys: List[tuple] = []
    for key in project_keys:
        parts = [p.strip() for p in str(key).split("|")]
        if len(parts) >= 3:
            parsed_keys.append((parts[0], parts[1], parts[2], key))

    if not parsed_keys:
        return {}

    # Egy lekérdezés az összes projekthez
    placeholders = ", ".join(["(%s, %s, %s)"] * len(parsed_keys))
    params = []
    for pn, rev, ecn, _ in parsed_keys:
        params.extend([pn, rev, ecn])

    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            f"""
            SELECT `PN`, `REV`, `ECN`, `summary`
            FROM `etiket data`
            WHERE (`PN`, `REV`, `ECN`) IN ({placeholders})
            """,
            params,
        )
        rows = cur.fetchall()
    finally:
        cur.close()

    result: Dict[str, Dict[str, int]] = {}

    for row in rows:
        pn  = (row.get("PN")  or "").strip()
        rev = (row.get("REV") or "").strip()
        ecn = (row.get("ECN") or "").strip()
        key = f"{pn} | {rev} | {ecn}"

        summary_raw = row.get("summary")
        try:
            parsed = json.loads(summary_raw) if summary_raw else []
        except Exception:
            parsed = []

        # új vs régi summary formátum
        if isinstance(parsed, dict):
            pages = parsed.get("pages") or parsed.get("summary") or []
        else:
            pages = parsed if isinstance(parsed, list) else []

        pages_count = len(pages)
        cables_count = 0
        connectors_count = 0

        for page in pages:
            if not isinstance(page, dict):
                continue
            groups = page.get("groups") or []
            cables_count += len(groups)
            for group in groups:
                if not isinstance(group, dict):
                    continue
                connectors_count += len(group.get("connectors") or [])

        result[key] = {
            "pages_count": pages_count,
            "cables_count": cables_count,
            "connectors_count": connectors_count,
        }

    return result