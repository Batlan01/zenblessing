# qr_logs.py  (új blueprint)
from flask import Blueprint, request, jsonify, render_template, session, abort, current_app
from datetime import datetime
import json
from services.bt_db import get_db
from routes.auth import require_roles, MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE
import os, sys, subprocess

qr_log = Blueprint("qr", __name__, url_prefix="")

def _whoami():
    try:
        u = (session.get("user") or {})
        return u.get("username") or u.get("name") or request.headers.get("X-User") or "unknown"
    except Exception:
        return request.headers.get("X-User") or "unknown"

def log_qr_print(*, file_name:str, printer_name:str|None, copies:int=1,
                 status:str="ok", message:str|None=None, meta:dict|None=None):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO qr_print_log
        (user_name, file_name, printer_name, copies, status, message, meta_json, ip_addr, user_agent, printed_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        _whoami(),
        file_name,
        printer_name or None,
        int(copies or 1),
        "ok" if status not in ("error","err","fail") else "error",
        message or None,
        json.dumps(meta or {}, ensure_ascii=False) if meta else None,
        request.headers.get("X-Forwarded-For") or request.remote_addr,
        (request.headers.get("User-Agent") or "")[:255],
        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    ))
    db.commit()
    cur.close()

# ---- API: logolás hívható közvetlenül a QR nyomtató végpontból
@qr_log.post("/api/qr/log_print")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def api_qr_log_print():
    j = request.get_json(silent=True) or request.form or {}
    try:
        log_qr_print(
            file_name=(j.get("file_name") or "").strip(),
            printer_name=(j.get("printer_name") or "").strip() or None,
            copies=int(j.get("copies") or 1),
            status=(j.get("status") or "ok").lower(),
            message=(j.get("message") or None),
            meta=j.get("meta") if isinstance(j.get("meta"), dict) else None,
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---- API: lista szűrőkkel + pagináció
# --- segéd a tetejére
def _cap_int(v, *, lo=0, hi=1000, default=100):
    try:
        x = int(v)
    except Exception:
        return default
    return max(lo, min(x, hi))

@qr_log.get("/api/qr/logs")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def api_qr_logs():
    q = request.args
    user  = (q.get("user") or "").strip()
    fileq = (q.get("file") or "").strip()
    stat  = (q.get("status") or "").strip().lower()
    prn   = (q.get("printer") or "").strip()
    d1    = (q.get("date_from") or "").strip()
    d2    = (q.get("date_to")   or "").strip()
    limit = _cap_int(q.get("limit"), lo=1, hi=1000, default=500)
    offset= _cap_int(q.get("offset"), lo=0, hi=10_000_000, default=0)

    where, args = [], []
    if user: where.append("user_name = %s"); args.append(user)
    if fileq: where.append("file_name LIKE %s"); args.append(f"%{fileq}%")  # <-- LIKE
    if stat in ("ok", "error"): where.append("status = %s"); args.append(stat)
    if prn: where.append("printer_name LIKE %s"); args.append(f"%{prn}%")
    if d1: where.append("printed_at >= %s"); args.append(f"{d1} 00:00:00")
    if d2: where.append("printed_at <= %s"); args.append(f"{d2} 23:59:59")
    wh = ("WHERE " + " AND ".join(where)) if where else ""

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(f"SELECT COUNT(*) AS c FROM qr_print_log {wh}", tuple(args))
    total = cur.fetchone()["c"]
    cur.execute(f"""
        SELECT id, printed_at, user_name, file_name, printer_name,
               copies, status, message, meta_json, ip_addr
        FROM qr_print_log
        {wh}
        ORDER BY printed_at DESC, id DESC
        LIMIT %s OFFSET %s
    """, tuple(args + [limit, offset]))
    rows = cur.fetchall() or []
    for r in rows:
        try: r["meta"] = json.loads(r.pop("meta_json") or "{}")
        except Exception: r["meta"] = {}
    cur.close()
    return jsonify({"total": total, "items": rows})


# ---- API: export CSV
@qr_log.get("/api/qr/logs/export")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def api_qr_logs_export():
    from flask import Response
    # újrafelhasználjuk a fenti /logs szűrőit
    q = request.args.to_dict()
    q["limit"] = "50000"; q["offset"] = "0"
    with qr_log.test_request_context(query_string=q):
        data = api_qr_logs()[0].get_json()
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["printed_at","user","file","printer","copies","status","ip","message"])
    for r in data["items"]:
        w.writerow([r["printed_at"], r["user_name"], r["file_name"], r["printer_name"] or "",
                    r["copies"], r["status"], r.get("ip_addr",""), r.get("message","")])
    return Response(buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":"attachment; filename=qr_print_log.csv"})

# ---- Oldal: lista (külön oldal)
@qr_log.route("/<lang>/qr/logs")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def qr_logs_page(lang):
    if lang not in ("hu","sk"): abort(404)
    return render_template(f"{lang}/qr_log.html", user=session.get("user"))


@qr_log.post("/api/qr/open_path")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def api_qr_open_path():
    j = request.get_json(silent=True) or {}
    raw = (j.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "path hiányzik"}), 400

    # csak az instance_path alatt engedjük (ahová az uploadok és a DOCX kerül)
    base = os.path.abspath(current_app.instance_path)
    # normáljuk és ellenőrizzük, hogy a base alatt van-e
    target = os.path.abspath(raw)
    # opcionális: ha a DOCX az uploads alkönyvtárában van (qr_uploads), külön is ellenőrizheted
    if not target.startswith(base + os.sep):
        return jsonify({"ok": False, "error": "A megnyitás csak a szerver instance mappájában engedélyezett."}), 403

    if not os.path.exists(target):
        return jsonify({"ok": False, "error": "Az útvonal nem létezik a szerveren."}), 404

    try:
        if sys.platform.startswith("win"):
            # ha fájl → jelöld ki, ha mappa → nyisd meg
            if os.path.isdir(target):
                subprocess.Popen(["explorer", target])
            else:
                # explorer /select, "C:\path\to\file"
                subprocess.Popen(["explorer", "/select,", target])
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", target])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            return jsonify({"ok": False, "error": f"Nem támogatott platform: {sys.platform}"}), 500
        return jsonify({"ok": True, "opened": target})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500