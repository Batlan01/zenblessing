# routes/tables_injector.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from flask import Blueprint, render_template, request, session, jsonify, g
from routes.auth import login_required, require_roles
from utils.roles import IT_ROLES, MANAGER_ROLES  # ha kell bővítsd
from flask import Blueprint, render_template, request, session, jsonify, g
from routes.auth import login_required, require_roles, IT_ROLES, MANAGER_ROLES, TEAMLEADER_ROLES
from services.db import get_db

from services.tables_core import (
    ensure_tables_meta_schema,
    list_groups_for_user,
    create_group,
    get_group_permissions,
    set_group_permissions,
    list_raspberry_devices,
    list_tables_for_user,
    bulk_create_tables,
    assign_device_to_table,
)
from services.tables_core import apply_grid_layout


bp_tables = Blueprint("tables", __name__, url_prefix="/<lang>/tables")


@bp_tables.url_value_preprocessor
def pull_lang(endpoint, values):
    g.lang = values.pop("lang", "hu")
    
def _norm(s: str) -> str:
    return (s or "").strip().upper()

def _tokens_from_db(val):
    if not val:
        return []
    if isinstance(val, list):
        return [_norm(x) for x in val if _norm(x)]
    # ha CSV stringként van tárolva
    return [_norm(x) for x in str(val).split(",") if _norm(x)]

def _get_job_title() -> str:
    # nálad lehet session['job_title'] vagy session['user']['job_title'] – ezért rugalmas
    jt = session.get("job_title")
    if not jt and isinstance(session.get("user"), dict):
        jt = session["user"].get("job_title")
    return _norm(jt)

def _can_manage_group(role_tokens):
    jt = _get_job_title()
    if jt in IT_ROLES or jt == "IT":
        return True
    tokens = _tokens_from_db(role_tokens)
    return jt in tokens
# ============= UI =============
@bp_tables.get("/")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)  # ha akarod: TEAMLEADER_ROLES is mehet
def tables_page():
    lang = getattr(g, "lang", "hu")
    ensure_tables_meta_schema()
    return render_template(
        f"{lang}/tables_creator.html",
        lang=lang,
        active="tables",
        user=session.get("user"),
    )


# ============= API =============

@bp_tables.get("/api/groups")
@login_required()
def api_groups():
    user = session.get("user")
    groups = list_groups_for_user(user)
    return jsonify({"ok": True, "groups": groups})


@bp_tables.post("/api/groups")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_create_group():
    data = request.get_json(silent=True) or {}
    try:
        gid = create_group(
            code=data.get("code", ""),
            display_name=data.get("display_name", ""),
            sort_order=int(data.get("sort_order") or 0),
            is_active=bool(data.get("is_active", True)),
        )
        return jsonify({"ok": True, "id": gid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp_tables.get("/api/groups/<int:group_id>/permissions")
@login_required()
def api_get_group_perms(group_id: int):
    try:
        perms = get_group_permissions(group_id)
        return jsonify({"ok": True, "permissions": perms})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp_tables.put("/api/groups/<int:group_id>/permissions")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_set_group_perms(group_id: int):
    data = request.get_json(silent=True) or {}
    role_tokens = data.get("role_tokens") or []
    try:
        set_group_permissions(group_id, role_tokens)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp_tables.get("/api/raspberrydevices")
@login_required()
def api_raspberrydevices():
    try:
        devs = list_raspberry_devices()
        return jsonify({"ok": True, "devices": devs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# Rövid TTL cache: a dashboardok 10 mp-enként pollozzák, az eredményt elég
# ~8 mp-enként újraszámolni (kulcs: user + group + process).
_TABLES_CACHE: dict = {}
_TABLES_CACHE_TTL = 8.0


@bp_tables.get("/api/tables")
@login_required()
def api_tables():
    import time as _time

    user = session.get("user")
    group_id = request.args.get("group_id", type=int)

    _ck = (
        (user or {}).get("username") or (user or {}).get("id"),
        group_id,
        # a cache kulcs is a tenyleges szurot tukrozze (ures = nincs allomas-szuro)
        _norm(request.args.get("process_id") or ""),
    )
    _hit = _TABLES_CACHE.get(_ck)
    if _hit and (_time.monotonic() - _hit[1]) < _TABLES_CACHE_TTL:
        return jsonify(_hit[0])

    try:
        rows = list_tables_for_user(user, group_id=group_id) or []
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    db = get_db()
    cur = db.cursor(dictionary=True)

    def _wid_key(v):
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    # 1) Aktív loginek: device -> (worker_id, user_name, login_date)
    cur.execute("""
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
    """)
    raw_logins = cur.fetchall() or []

    login_by_device = {}
    for r in raw_logins:
        dev_key = _norm(r.get("raspberry_device"))
        if not dev_key:
            continue
        prev = login_by_device.get(dev_key)
        if not prev:
            login_by_device[dev_key] = r
            continue
        prev_dt = prev.get("login_date")
        cur_dt = r.get("login_date")
        if cur_dt and (not prev_dt or cur_dt > prev_dt):
            login_by_device[dev_key] = r

    # 2/A) Aktív munka WORKER / DEVICE alapján
    #
    # Korábban itt a process_id alapertelmezese "ASSEMBLY" volt, es a dashboard
    # process_id nelkul hivja ezt a vegpontot -> a szuro SOHA nem illeszkedett.
    # A process_id ebben a rendszerben az ALLOMAS kodjat tarolja (EMI, MTE, MDI,
    # QC, TEST, SOLD, MOLD, CRIMP ...), nem az "ASSEMBLY" szoveget. Emiatt a
    # lekerdezes 0 sort adott, es minden asztalnal "Nincs aktiv munkarendeles"
    # jelent meg. Mostantol allomasra CSAK akkor szurunk, ha a hivo kifejezetten
    # kert egyet; alapbol nem (az asztalracs EMI, CRIMP es IT csoportot is tartalmaz).
    process_id = _norm(request.args.get("process_id") or "")

    # Az "aktiv" feltetel ugyanaz, mint az Osszeszerelesi allapotok tablazatban
    # (/api/assembly_data): ACTIVE statusz VAGY meg nincs lezarva. A ket feltetel
    # AND-elve tul szigoru volt – az olyan folyamatban levo sor, aminek a statusza
    # nem pont 'ACTIVE', kimaradt.
    _where = ["""(
              UPPER(TRIM(COALESCE(ww.STATUS,''))) = 'ACTIVE'
              OR ww.END_TIME IS NULL
              OR ww.END_TIME = ''
              OR ww.END_TIME = '0000-00-00 00:00:00'
          )""", "ww.START_TIME >= CURDATE()"]
    _params = []
    if process_id:
        _where.insert(0, "UPPER(TRIM(ww.PROCESS_ID)) = %s")
        _params.append(process_id)

    cur.execute(f"""
        SELECT
            ww.WORKER_ID AS worker_id,
            wo.WO AS WO,
            wo.PN AS PN,
            ww.START_TIME AS start_time,
            ww.DEVICE_ID AS device_id,
            ww.PROCESS_ID AS process_id
        FROM workstationworkorder ww
        JOIN workorders wo ON ww.WORK_ID = wo.id
        WHERE {" AND ".join(_where)}
        ORDER BY ww.START_TIME DESC
    """, tuple(_params))

    active_rows = cur.fetchall() or []

    work_by_worker = {}
    work_by_device = {}

    for r in active_rows:
        # worker map
        wid = _wid_key(r.get("worker_id"))
        if wid:
            if wid not in work_by_worker:
                work_by_worker[wid] = r
            else:
                prev_st = work_by_worker[wid].get("start_time")
                cur_st = r.get("start_time")
                if cur_st and (not prev_st or cur_st > prev_st):
                    work_by_worker[wid] = r

        # device map (EZ a lényeg, emiatt fog megjelenni a hoverben)
        dkey = _norm(r.get("device_id"))
        if dkey:
            if dkey not in work_by_device:
                work_by_device[dkey] = r
            else:
                prev_st = work_by_device[dkey].get("start_time")
                cur_st = r.get("start_time")
                if cur_st and (not prev_st or cur_st > prev_st):
                    work_by_device[dkey] = r

    def _row_device_key(row: dict) -> str:
        # itt a lényeg, hogy a workerworkstation.RASPBERRY_DEVICE-hez passzoló kulcs legyen
        for k in ("device_name", "raspberry_device", "raspberry_name", "hostname", "device_hostname", "device_id"):
            if row.get(k):
                return _norm(row.get(k))
        if row.get("table_name"):
            return _norm(row.get("table_name"))
        if row.get("name"):
            return _norm(row.get("name"))
        return ""

    for row in rows:
        raspberry_id = row.get("raspberry_id") or row.get("raspberry") or 0
        dev_key = _row_device_key(row)

        status = "inactive"
        user_name = None
        login_time = None
        active_work = None

        if int(raspberry_id or 0) != 0:
            status = "no_user"

            login_rec = login_by_device.get(dev_key) if dev_key else None
            if login_rec:
                status = "active"
                user_name = login_rec.get("user_name")

                lt = login_rec.get("login_date")
                login_time = lt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(lt, "strftime") else lt

                # 1) elsődlegesen device alapján (stabil)
                wrec = work_by_device.get(dev_key)

                # 2) fallback worker_id alapján
                if not wrec:
                    wid = _wid_key(login_rec.get("worker_id"))
                    if wid:
                        wrec = work_by_worker.get(wid)

                if wrec:
                    st = wrec.get("start_time")
                    st_str = st.strftime("%Y-%m-%d %H:%M:%S") if hasattr(st, "strftime") else st
                    active_work = {
                        "WO": wrec.get("WO"),
                        "PN": wrec.get("PN"),
                        "start_time": st_str,
                        "process_id": (str(wrec.get("process_id") or "").strip() or None),
                    }

        row["status"] = status
        row["user_name"] = user_name
        row["login_time"] = login_time
        row["active_work"] = active_work

    cur.close()
    db.close()

    payload = {"ok": True, "tables": rows}
    _TABLES_CACHE[_ck] = (payload, _time.monotonic())
    if len(_TABLES_CACHE) > 200:
        _TABLES_CACHE.clear()
    return jsonify(payload)




@bp_tables.post("/api/tables/bulk_create")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_bulk_create():
    data = request.get_json(silent=True) or {}
    try:
        inserted = bulk_create_tables(
            group_id=int(data.get("group_id") or 0),
            prefix=data.get("prefix", ""),
            start_no=int(data.get("start_no") or 1),
            count=int(data.get("count") or 0),
        )
        return jsonify({"ok": True, "inserted": inserted})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp_tables.post("/api/tables/<int:table_id>/assign_device")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_assign_device(table_id: int):
    data = request.get_json(silent=True) or {}
    try:
        assign_device_to_table(table_id, int(data.get("raspberry_id") or 0))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp_tables.post("/api/layout/apply")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_apply_layout():
    data = request.get_json(silent=True) or {}
    try:
        res = apply_grid_layout(
            group_id=int(data.get("group_id") or 0),
            prefix=data.get("prefix", ""),
            start_no=int(data.get("start_no") or 1),
            rows=int(data.get("rows") or 0),
            cols=int(data.get("cols") or 0),
            x1=int(data.get("x1") or 1),
            y1=int(data.get("y1") or 1),
        )
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@bp_tables.post("/api/tables/<int:table_id>/update")
@login_required()
@require_roles(MANAGER_ROLES, TEAMLEADER_ROLES, IT_ROLES)
def update_table(table_id: int):
    """
    Asztal mezők frissítése (kézi sorszám / név).
    Body (JSON):
      - table_no: int|null
      - display_name: str|null   (opcionális)
    """
    payload = request.get_json(silent=True) or {}

    # --- table_no parse/validáció ---
    table_no = payload.get("table_no", None)
    if table_no in ("", "null", "None"):
        table_no = None

    if table_no is not None:
        try:
            table_no = int(table_no)
        except Exception:
            return jsonify({"ok": False, "error": "table_no must be an integer or null"}), 400
        if table_no < 1:
            return jsonify({"ok": False, "error": "table_no must be >= 1"}), 400

    # --- display_name parse ---
    display_name = payload.get("display_name", None)
    if display_name is not None:
        display_name = str(display_name).strip()
        if display_name == "":
            display_name = None

    # --- betöltjük az asztal group_id-ját (duplikáció checkhez) ---
    db = get_db()
    cur = db.cursor(dictionary=True)

    cur.execute("SELECT id, group_id FROM tables WHERE id=%s LIMIT 1", (table_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        return jsonify({"ok": False, "error": "Table not found"}), 404

    group_id = row.get("group_id")

    # --- duplikáció védelem: group-on belül egyedi table_no ---
    if table_no is not None and group_id is not None:
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM tables
            WHERE group_id = %s AND table_no = %s AND id <> %s
            """,
            (group_id, table_no, table_id),
        )
        if int((cur.fetchone() or {}).get("c", 0)) > 0:
            cur.close()
            return jsonify({"ok": False, "error": f"Ebben a csoportban már létezik {table_no} sorszám."}), 409

    # --- update mezők (csak ami jött) ---
    fields = []
    params = []

    if "table_no" in payload:
        fields.append("table_no=%s")
        params.append(table_no)

    if "display_name" in payload:
        fields.append("display_name=%s")
        params.append(display_name)

    if not fields:
        cur.close()
        return jsonify({"ok": False, "error": "No fields to update"}), 400

    params.append(table_id)

    cur.execute(f"UPDATE tables SET {', '.join(fields)} WHERE id=%s", tuple(params))
    db.commit()

    cur.close()
    return jsonify({"ok": True})

@bp_tables.route("/api/tables/<int:table_id>", methods=["DELETE"])
@login_required()
@require_roles(MANAGER_ROLES, TEAMLEADER_ROLES, IT_ROLES)
def api_delete_table(table_id: int):
    db = get_db()
    cur = db.cursor(dictionary=True)

    cur.execute("SELECT id, group_id FROM tables WHERE id=%s LIMIT 1", (table_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); db.close()
        return jsonify(ok=False, error="Nincs ilyen asztal."), 404

    try:
        cur.execute("DELETE FROM tables WHERE id = %s", (table_id,))
        db.commit()
        return jsonify(ok=True, deleted=int(cur.rowcount))
    except Exception as e:
        db.rollback()
        return jsonify(ok=False, error=f"Törlés sikertelen: {e}"), 500
    finally:
        cur.close()
        db.close()
