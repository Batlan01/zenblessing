# services/tables_core.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from services.db import get_db
from utils.roles import has_any_role, IT_ROLES, ALL_AUTHENTICATED


def _norm(s: str) -> str:
    return (s or "").strip().upper()


def _split_code_and_no(table_name: str) -> Tuple[str, Optional[int]]:
    """
    "EMI - 12" -> ("EMI", 12)
    "MDI-3" -> ("MDI", 3)
    egyéb -> ("", None)
    """
    raw = (table_name or "").strip()
    if "-" not in raw:
        return _norm(raw), None
    left, right = raw.split("-", 1)
    code = _norm(left)
    try:
        no = int(right.strip())
    except Exception:
        no = None
    return code, no


def _table_has_column(db, table: str, col: str) -> bool:
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table, col),
    )
    row = cur.fetchone() or {}
    cur.close()
    return int(row.get("c", 0)) > 0


def ensure_tables_meta_schema() -> None:
    """
    Meta táblák létrehozása (idempotens).
    """
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS table_groups (
            id INT NOT NULL AUTO_INCREMENT,
            code VARCHAR(50) NOT NULL,
            display_name VARCHAR(100) NOT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_table_groups_code (code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS table_group_permissions (
            id INT NOT NULL AUTO_INCREMENT,
            group_id INT NOT NULL,
            role_token VARCHAR(60) NOT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uq_group_role (group_id, role_token),
            CONSTRAINT fk_tgp_group
              FOREIGN KEY (group_id) REFERENCES table_groups(id)
              ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
        """
    )
    db.commit()
    cur.close()


def _user_is_it(user: dict | None) -> bool:
    jt = (user or {}).get("job_title", "")
    return has_any_role(jt, IT_ROLES)


def list_groups_for_user(user: dict | None) -> List[Dict[str, Any]]:
    """
    Csoportok listája (UI-hoz). IT mindent lát; nem-IT csak azokat,
    amelyeknek a permission tokenjei passzolnak a job_title-ra.
    """
    ensure_tables_meta_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)

    cur.execute(
        """
        SELECT g.id, g.code, g.display_name, g.sort_order, g.is_active,
               GROUP_CONCAT(p.role_token ORDER BY p.role_token SEPARATOR ',') AS tokens
        FROM table_groups g
        LEFT JOIN table_group_permissions p ON p.group_id = g.id
        GROUP BY g.id
        ORDER BY g.sort_order ASC, g.code ASC
        """
    )
    rows = cur.fetchall() or []
    cur.close()

    if not user:
        return []

    if _user_is_it(user):
        return [
            {
                "id": r["id"],
                "code": r["code"],
                "display_name": r["display_name"],
                "sort_order": r["sort_order"],
                "is_active": bool(r["is_active"]),
            }
            for r in rows
            if bool(r["is_active"])
        ]

    job_title = user.get("job_title", "")
    out = []
    for r in rows:
        if not bool(r["is_active"]):
            continue
        token_csv = r.get("tokens") or ""
        tokens = [_norm(x) for x in token_csv.split(",") if _norm(x)]
        if not tokens:
            continue

        # '*' -> minden auth user
        if "*" in tokens:
            out.append(
                {
                    "id": r["id"],
                    "code": r["code"],
                    "display_name": r["display_name"],
                    "sort_order": r["sort_order"],
                    "is_active": True,
                }
            )
            continue

        if has_any_role(job_title, set(tokens)):
            out.append(
                {
                    "id": r["id"],
                    "code": r["code"],
                    "display_name": r["display_name"],
                    "sort_order": r["sort_order"],
                    "is_active": True,
                }
            )
    return out


def create_group(code: str, display_name: str, sort_order: int = 0, is_active: bool = True) -> int:
    ensure_tables_meta_schema()
    db = get_db()
    cur = db.cursor()

    code_u = _norm(code)
    if not code_u:
        raise ValueError("Missing group code")
    if not display_name.strip():
        raise ValueError("Missing group name")

    try:
        cur.execute(
            """
            INSERT INTO table_groups (code, display_name, sort_order, is_active)
            VALUES (%s, %s, %s, %s)
            """,
            (code_u, display_name.strip(), int(sort_order or 0), 1 if is_active else 0),
        )
        db.commit()
        gid = cur.lastrowid
    except Exception as e:
        db.rollback()
        raise
    finally:
        cur.close()
    return int(gid)


def get_group_permissions(group_id: int) -> List[str]:
    ensure_tables_meta_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT role_token
        FROM table_group_permissions
        WHERE group_id = %s
        ORDER BY role_token ASC
        """,
        (int(group_id),),
    )
    rows = cur.fetchall() or []
    cur.close()
    return [_norm(r["role_token"]) for r in rows if _norm(r.get("role_token", ""))]


def set_group_permissions(group_id: int, role_tokens: List[str]) -> None:
    ensure_tables_meta_schema()
    db = get_db()
    cur = db.cursor()

    tokens = []
    for t in role_tokens or []:
        t2 = _norm(t)
        if t2:
            tokens.append(t2)
    # unique
    tokens = list(dict.fromkeys(tokens))

    try:
        cur.execute("DELETE FROM table_group_permissions WHERE group_id = %s", (int(group_id),))
        for t in tokens:
            cur.execute(
                "INSERT INTO table_group_permissions (group_id, role_token) VALUES (%s, %s)",
                (int(group_id), t),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()


def list_raspberry_devices() -> List[Dict[str, Any]]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, device_id, device_name
        FROM raspberrydevices
        ORDER BY device_name ASC
        """
    )
    rows = cur.fetchall() or []
    cur.close()
    return rows


def _allowed_group_codes_for_user(user: dict | None) -> List[str]:
    """
    Nem-IT esetén: mely group CODE-ok látszanak a usernek.
    """
    if not user:
        return []
    if _user_is_it(user):
        return []  # üres lista = "no filter"

    groups = list_groups_for_user(user)
    return [_norm(g["code"]) for g in groups if _norm(g.get("code", ""))]


def list_tables_for_user(user: dict | None, group_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    UI listához (creator oldal + dashboard).
    - Ha van tables.group_id -> join azon
    - Ha nincs -> table_name alapján parsol és table_groups.code alapján joinol
    PLUSZ:
    - visszaadja pos_x/pos_y-t
    - számol status/user_name/login_time/active_work mezőket (dashboard hover + színezés)
    """
    ensure_tables_meta_schema()
    ensure_tables_layout_columns()

    db = get_db()
    cur = db.cursor(dictionary=True)

    has_group_id = _table_has_column(db, "tables", "group_id")
    has_table_no = _table_has_column(db, "tables", "table_no")
    has_display = _table_has_column(db, "tables", "display_name")
    has_pos_x = _table_has_column(db, "tables", "pos_x")
    has_pos_y = _table_has_column(db, "tables", "pos_y")

    allowed_codes = _allowed_group_codes_for_user(user)  # [] = nincs szűrés (IT)

    # --- SQL: tables + groups + devices + layout ---
    if has_group_id:
        sql = """
            SELECT
              t.id,
              t.table_name,
              {disp} AS display_name,
              {tno}  AS table_no,
              {px}   AS pos_x,
              {py}   AS pos_y,
              t.raspberry_id,
              d.device_name,
              d.device_id,
              g.id   AS group_id,
              g.code AS group_code,
              g.display_name AS group_name,
              g.sort_order AS group_sort
            FROM tables t
            LEFT JOIN raspberrydevices d ON d.id = t.raspberry_id
            LEFT JOIN table_groups g ON g.id = t.group_id
            WHERE 1=1
        """.format(
            disp=("t.display_name" if has_display else "NULL"),
            tno=("t.table_no" if has_table_no else "NULL"),
            px=("t.pos_x" if has_pos_x else "NULL"),
            py=("t.pos_y" if has_pos_y else "NULL"),
        )

        params: List[Any] = []
        if group_id:
            sql += " AND t.group_id = %s"
            params.append(int(group_id))

        # permission szűrés nem-IT-nek group_code szerint
        if allowed_codes:
            sql += " AND g.code IN ({})".format(",".join(["%s"] * len(allowed_codes)))
            params.extend(allowed_codes)

        sql += " ORDER BY g.sort_order ASC, g.code ASC, COALESCE(t.table_no, 999999) ASC, t.id ASC"
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []

    else:
        # fallback: g.code = parsed prefix a table_name-ből
        sql = """
            SELECT
              t.id,
              t.table_name,
              {px} AS pos_x,
              {py} AS pos_y,
              t.raspberry_id,
              d.device_name,
              d.device_id,
              g.id AS group_id,
              g.code AS group_code,
              g.display_name AS group_name,
              g.sort_order AS group_sort
            FROM tables t
            LEFT JOIN raspberrydevices d ON d.id = t.raspberry_id
            LEFT JOIN table_groups g
              ON UPPER(TRIM(SUBSTRING_INDEX(t.table_name,'-',1))) = g.code
            WHERE 1=1
        """.format(
            px=("t.pos_x" if has_pos_x else "NULL"),
            py=("t.pos_y" if has_pos_y else "NULL"),
        )

        params: List[Any] = []
        if group_id:
            sql += " AND g.id = %s"
            params.append(int(group_id))

        if allowed_codes:
            sql += " AND g.code IN ({})".format(",".join(["%s"] * len(allowed_codes)))
            params.extend(allowed_codes)

        sql += """
            ORDER BY g.sort_order ASC,
                     g.code ASC,
                     CAST(TRIM(SUBSTRING_INDEX(t.table_name,'-',-1)) AS UNSIGNED) ASC,
                     t.id ASC
        """
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []

    cur.close()

    # --- Alap kimenet (pos_x/pos_y-val!) ---
    out: List[Dict[str, Any]] = []
    for r in rows:
        code, no = _split_code_and_no(r.get("table_name", ""))
        out.append(
            {
                "id": r["id"],
                "table_name": r.get("table_name"),
                "display_name": r.get("display_name") or r.get("table_name"),
                "table_no": r.get("table_no") if r.get("table_no") is not None else no,
                "pos_x": r.get("pos_x"),
                "pos_y": r.get("pos_y"),
                "raspberry_id": r.get("raspberry_id", 0) or 0,
                "device_name": r.get("device_name"),
                "device_id": r.get("device_id"),
                "group_id": r.get("group_id"),
                "group_code": r.get("group_code") or code,
                "group_name": r.get("group_name"),
                "group_sort": r.get("group_sort", 0),
                # dashboard mezők – kitöltjük lent:
                "status": "inactive",
                "user_name": None,
                "login_time": None,
                "active_work": None,
            }
        )

    # Ha nincs semmi, ne dolgozzunk feleslegesen
    if not out:
        return out

    # --- DASHBOARD státusz/hover adat számítás (mint a régi /api/get_tables) ---
    db2 = get_db()
    cur2 = db2.cursor(dictionary=True)

    def norm(s: Any) -> str:
        return (str(s) if s is not None else "").strip().upper()

    # 1) aktív loginok workerworkstationből (device -> legfrissebb login)
    cur2.execute(
        """
        SELECT 
            ww.RASPBERRY_DEVICE AS raspberry_device,
            w.id AS worker_id,
            w.name AS user_name,
            ww.LOGIN_DATE AS login_date
        FROM workerworkstation ww
        JOIN workers w ON ww.WORKER_ID = w.id
        WHERE ww.LOGOUT_DATE IS NULL
           OR ww.LOGOUT_DATE = ''
           OR ww.LOGOUT_DATE = '0000-00-00 00:00:00'
        """
    )
    raw_logins = cur2.fetchall() or []

    login_by_device: Dict[str, Dict[str, Any]] = {}
    for r in raw_logins:
        dev_key = norm(r.get("raspberry_device"))
        if not dev_key:
            continue
        prev = login_by_device.get(dev_key)
        if not prev:
            login_by_device[dev_key] = r
            continue
        # legfrissebb login_date
        prev_dt = prev.get("login_date")
        cur_dt = r.get("login_date")
        if cur_dt and prev_dt and cur_dt > prev_dt:
            login_by_device[dev_key] = r
        elif cur_dt and not prev_dt:
            login_by_device[dev_key] = r

    # 2) aktív WO user alapján (hover)
    #
    # FIGYELEM: itt korábban PROCESS_ID = 'ASSEMBLY' szűrő volt, ami SOHA nem
    # illeszkedett: a process_id ebben a rendszerben az ÁLLOMÁS kódját tárolja
    # (EMI, MTE, MDI, QC, TEST, SOLD, MOLD, CRIMP ...), nem az "ASSEMBLY"
    # szöveget – lásd /api/assembly_data és /api/incoming_workorders, amelyek
    # szintén állomásra szűrnek. Emiatt a work_by_worker mindig üres maradt,
    # és minden asztalnál "Nincs aktív munkarendelés" jelent meg.
    # Állomásra nem szűrünk: az asztalrács EMI, CRIMP és IT csoportokat is
    # tartalmaz, a dolgozóhoz kötés pedig a WORKER_ID-n keresztül már megvan.
    cur2.execute(
        """
        SELECT
            ww.WORKER_ID AS worker_id,
            wo.WO AS WO,
            wo.PN AS PN,
            ww.START_TIME AS start_time,
            ww.PROCESS_ID AS process_id
        FROM workstationworkorder ww
        JOIN workorders wo ON ww.WORK_ID = wo.id
        WHERE DATE(ww.START_TIME) = CURDATE()
          AND (
              UPPER(TRIM(ww.STATUS)) = 'ACTIVE'
              OR ww.END_TIME IS NULL
              OR ww.END_TIME = ''
              OR ww.END_TIME = '0000-00-00 00:00:00'
          )
        ORDER BY ww.START_TIME DESC
        """
    )
    active_work_rows = cur2.fetchall() or []

    work_by_worker: Dict[int, Dict[str, Any]] = {}
    for r in active_work_rows:
        wid = r.get("worker_id")
        if wid is None:
            continue
        prev = work_by_worker.get(wid)
        if not prev:
            work_by_worker[wid] = r
            continue
        prev_st = prev.get("start_time")
        cur_st = r.get("start_time")
        if cur_st and prev_st and cur_st > prev_st:
            work_by_worker[wid] = r
        elif cur_st and not prev_st:
            work_by_worker[wid] = r

    cur2.close()

    # 3) kitöltjük out mezőket
    for t in out:
        raspberry_id = t.get("raspberry_id") or 0
        device_name = t.get("device_name")

        # alap
        t["status"] = "inactive"
        t["user_name"] = None
        t["login_time"] = None
        t["active_work"] = None

        # ha van raspberry rendelve
        if raspberry_id != 0 and device_name:
            t["status"] = "no_user"

            dev_key = norm(device_name)
            login_rec = login_by_device.get(dev_key)

            if login_rec:
                t["status"] = "active"
                t["user_name"] = login_rec.get("user_name")

                lt = login_rec.get("login_date")
                if hasattr(lt, "strftime"):
                    t["login_time"] = lt.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    t["login_time"] = lt or None

                wid = login_rec.get("worker_id")
                wrec = work_by_worker.get(wid)
                if wrec:
                    st = wrec.get("start_time")
                    st_str = st.strftime("%Y-%m-%d %H:%M:%S") if hasattr(st, "strftime") else (st or None)
                    t["active_work"] = {
                        "WO": wrec.get("WO"),
                        "PN": wrec.get("PN"),
                        "start_time": st_str,
                        "process_id": (str(wrec.get("process_id") or "").strip() or None),
                    }

    return out


def bulk_create_tables(group_id: int, prefix: str, start_no: int, count: int) -> int:
    """
    Létrehoz: PREFIX - N sorokat.
    Ha vannak extra oszlopok (group_id/table_no/display_name), kitölti; ha nincsenek, csak table_name-t.
    """
    ensure_tables_meta_schema()
    db = get_db()
    cur = db.cursor()

    prefix_u = _norm(prefix)
    if not prefix_u:
        raise ValueError("Missing prefix")
    if count <= 0:
        raise ValueError("Bad count")

    has_group_id = _table_has_column(db, "tables", "group_id")
    has_table_no = _table_has_column(db, "tables", "table_no")
    has_display = _table_has_column(db, "tables", "display_name")

    inserted = 0
    try:
        for i in range(int(start_no), int(start_no) + int(count)):
            name = f"{prefix_u} - {i}"

            # ne duplikáljunk (table_name alapján)
            cur.execute("SELECT id FROM tables WHERE table_name = %s LIMIT 1", (name,))
            if cur.fetchone():
                continue

            if has_group_id and has_table_no and has_display:
                cur.execute(
                    """
                    INSERT INTO tables (table_name, raspberry_id, group_id, table_no, display_name)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (name, 0, int(group_id), int(i), name),
                )
            elif has_group_id and has_table_no:
                cur.execute(
                    """
                    INSERT INTO tables (table_name, raspberry_id, group_id, table_no)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (name, 0, int(group_id), int(i)),
                )
            elif has_group_id:
                cur.execute(
                    """
                    INSERT INTO tables (table_name, raspberry_id, group_id)
                    VALUES (%s, %s, %s)
                    """,
                    (name, 0, int(group_id)),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO tables (table_name, raspberry_id)
                    VALUES (%s, %s)
                    """,
                    (name, 0),
                )

            inserted += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()

    return inserted


def assign_device_to_table(table_id: int, raspberry_id: int) -> None:
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE tables SET raspberry_id = %s WHERE id = %s",
            (int(raspberry_id or 0), int(table_id)),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()


def ensure_tables_layout_columns() -> None:
    db = get_db()
    cur = db.cursor()
    try:
        for col, ddl in [
            ("group_id", "ALTER TABLE tables ADD COLUMN group_id INT NULL"),
            ("table_no", "ALTER TABLE tables ADD COLUMN table_no INT NULL"),
            ("pos_x",    "ALTER TABLE tables ADD COLUMN pos_x INT NULL"),
            ("pos_y",    "ALTER TABLE tables ADD COLUMN pos_y INT NULL"),
        ]:
            if not _table_has_column(db, "tables", col):
                cur.execute(ddl)
        db.commit()
    finally:
        cur.close()


def apply_grid_layout(
    group_id: int,
    prefix: str,
    start_no: int,
    rows: int,
    cols: int,
    x1: int = 1,
    y1: int = 1,
) -> dict:
    """
    Rács alapján:
    - létrehozza a táblákat (ha hiányoznak)
    - beírja a group_id, table_no, pos_x, pos_y mezőket
    - FONTOS: pos_x/pos_y globális koordináta, a kijelölés bal-felső sarkát (x1,y1) figyelembe veszi
    """
    ensure_tables_meta_schema()
    ensure_tables_layout_columns()

    db = get_db()
    cur = db.cursor()

    prefix_u = _norm(prefix)
    if not group_id:
        raise ValueError("Missing group_id")
    if not prefix_u:
        raise ValueError("Missing prefix")
    if rows <= 0 or cols <= 0:
        raise ValueError("Bad grid size")
    if rows * cols > 400:
        raise ValueError("Too many cells (max 400)")

    # kijelölés bal-felső sarka (fallback 1,1)
    try:
        x1i = int(x1) if x1 not in (None, "", False) else 1
    except Exception:
        x1i = 1
    try:
        y1i = int(y1) if y1 not in (None, "", False) else 1
    except Exception:
        y1i = 1

    if x1i <= 0: x1i = 1
    if y1i <= 0: y1i = 1

    created = 0
    updated = 0

    try:
        n = int(start_no)

        for y in range(1, rows + 1):
            for x in range(1, cols + 1):
                table_no = n
                table_name = f"{prefix_u} - {table_no}"

                # globális koordináta: kijelölés offsettel
                px = x1i + (x - 1)
                py = y1i + (y - 1)

                # 1) létezik-e már?
                cur.execute("SELECT id FROM tables WHERE table_name=%s LIMIT 1", (table_name,))
                row = cur.fetchone()

                if not row:
                    # create
                    cur.execute(
                        "INSERT INTO tables (table_name, raspberry_id, group_id, table_no, pos_x, pos_y) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (table_name, 0, int(group_id), int(table_no), int(px), int(py)),
                    )
                    created += 1
                else:
                    # update position + group + no
                    tid = row[0]
                    cur.execute(
                        "UPDATE tables SET group_id=%s, table_no=%s, pos_x=%s, pos_y=%s WHERE id=%s",
                        (int(group_id), int(table_no), int(px), int(py), int(tid)),
                    )
                    updated += 1

                n += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        cur.close()

    return {"created": created, "updated": updated, "total": rows * cols, "x1": x1i, "y1": y1i}
