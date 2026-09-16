# routes/database_viewer_injector.py
from __future__ import annotations
from flask import Blueprint, render_template, request, jsonify, session, abort, g, url_for
from services.database_viewer_core import (
    engine_registry, DBCreds, test_connection, list_databases, list_tables,
    preview_table, get_columns, describe_table, get_indexes, show_create_table,
    row_count, distinct_values, aggregate, run_sql,
    get_primary_keys, update_cell, insert_row, delete_rows
)
from routes.auth import login_required, require_roles
from utils.roles import MANAGER_ROLES, IT_ROLES

dbv_bp = Blueprint("db_viewer", __name__, url_prefix="/<lang>/dbv")

@dbv_bp.url_value_preprocessor
def pull_lang(endpoint, values):
    g.lang = values.pop('lang', 'hu')

def _sid() -> str:
    sid = session.get("dbv_sid")
    if not sid:
        import secrets
        sid = secrets.token_hex(16)
        session["dbv_sid"] = sid
    return sid

def _csrf_get() -> str:
    tok = session.get("dbv_csrf")
    if not tok:
        import secrets
        tok = secrets.token_hex(16)
        session["dbv_csrf"] = tok
    return tok

def _csrf_check(tok: str) -> None:
    if not tok or tok != session.get("dbv_csrf"):
        abort(403, description="Érvénytelen vagy hiányzó CSRF token.")

# ---------- PAGE ----------
@dbv_bp.route("/", methods=["GET"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def page():
    lang = getattr(g, "lang", "hu")
    csrf = _csrf_get()
    base_url = url_for("db_viewer.page", lang=lang)
    return render_template(
        f"{lang}/database_viewer_site.html",
        lang=lang, csrf_token=csrf, base_url=base_url,
        active="dbv", user=session.get("user"),
    )

# ---------- AUTH ----------
@dbv_bp.route("/api/login", methods=["POST"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_login():
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    host = (data.get("host") or "10.10.2.15").strip()
    try:    port = int(data.get("port") or 3306)
    except: port = 3306
    user = (data.get("user") or "").strip()
    password = (data.get("password") or "").strip()
    if not user:
        return jsonify({"ok": False, "error": "Hiányzó felhasználónév."}), 400
    try:
        engine_registry.create_or_update(_sid(), DBCreds(host=host, port=port, user=user, password=password))
        ok, err = test_connection(_sid())
        if not ok:
            return jsonify({"ok": False, "error": f"Kapcsolódási hiba: {err}"}), 400
        session["dbv_logged_in"] = True
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@dbv_bp.route("/api/logout", methods=["POST"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_logout():
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    engine_registry.logout(_sid())
    session.pop("dbv_logged_in", None)
    return jsonify({"ok": True})

def _require_login():
    if not session.get("dbv_logged_in"):
        abort(401, description="Nem vagy bejelentkezve az adatbázisba.")

# ---------- META / LIST ÁGAK (read) ----------
@dbv_bp.route("/api/databases", methods=["GET"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_databases():
    _require_login()
    try:
        return jsonify({"ok": True, "databases": list_databases(_sid())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@dbv_bp.route("/api/tables", methods=["GET"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_tables():
    _require_login()
    dbname = (request.args.get("db") or "").strip()
    if not dbname:
        return jsonify({"ok": False, "error": "Hiányzik a db paraméter."}), 400
    try:
        return jsonify({"ok": True, "tables": list_tables(_sid(), dbname)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@dbv_bp.route("/api/columns", methods=["GET"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_columns():
    _require_login()
    dbname = (request.args.get("db") or "").strip()
    table  = (request.args.get("table") or "").strip()
    if not dbname or not table:
        return jsonify({"ok": False, "error": "Hiányzó db vagy table paraméter."}), 400
    try:
        pks = get_primary_keys(_sid(), dbname, table)
        cols = get_columns(_sid(), dbname, table)
        return jsonify({"ok": True, "columns": cols, "primary_keys": pks})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@dbv_bp.route("/api/describe", methods=["GET"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_describe():
    _require_login()
    dbname = (request.args.get("db") or "").strip()
    table  = (request.args.get("table") or "").strip()
    if not dbname or not table:
        return jsonify({"ok": False, "error": "Hiányzó db vagy table paraméter."}), 400
    try:
        desc = describe_table(_sid(), dbname, table)
        idxs = get_indexes(_sid(), dbname, table)
        create_sql = show_create_table(_sid(), dbname, table)
        return jsonify({"ok": True, "describe": desc, "indexes": idxs, "create_sql": create_sql})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---------- COUNT / DISTINCT / AGG ----------
@dbv_bp.route("/api/rowcount", methods=["POST"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_rowcount():
    _require_login()
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    dbname = (data.get("db") or "").strip()
    table  = (data.get("table") or "").strip()
    wheres = data.get("wheres") or None
    logic  = data.get("logic") or "AND"
    if not dbname or not table:
        return jsonify({"ok": False, "error": "Hiányzó db vagy table paraméter."}), 400
    try:
        n = row_count(_sid(), dbname, table, wheres=wheres, logic=logic)
        return jsonify({"ok": True, "count": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@dbv_bp.route("/api/distinct", methods=["POST"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_distinct():
    _require_login()
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    dbname = (data.get("db") or "").strip()
    table  = (data.get("table") or "").strip()
    column = (data.get("column") or "").strip()
    limit  = int(data.get("limit") or 100)
    offset = int(data.get("offset") or 0)
    wheres = data.get("wheres") or None
    logic  = data.get("logic") or "AND"
    if not dbname or not table or not column:
        return jsonify({"ok": False, "error": "Hiányzó db/table/column paraméter."}), 400
    try:
        res = distinct_values(_sid(), dbname, table, column, limit=limit, offset=offset, wheres=wheres, logic=logic)
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@dbv_bp.route("/api/aggregate", methods=["POST"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_aggregate():
    _require_login()
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    dbname = (data.get("db") or "").strip()
    table  = (data.get("table") or "").strip()
    func   = (data.get("func") or "").strip()
    column = (data.get("column") or None)
    wheres = data.get("wheres") or None
    logic  = data.get("logic") or "AND"
    if not dbname or not table or not func:
        return jsonify({"ok": False, "error": "Hiányzó db/table/func paraméter."}), 400
    try:
        val = aggregate(_sid(), dbname, table, func, column, wheres=wheres, logic=logic)
        return jsonify({"ok": True, "value": val})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---------- PREVIEW ----------
@dbv_bp.route("/api/preview", methods=["POST"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_preview():
    _require_login()
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    dbname = (data.get("db") or "").strip()
    table  = (data.get("table") or "").strip()
    limit  = int(data.get("limit") or 50)
    offset = int(data.get("offset") or 0)
    orders = data.get("orders") or None
    wheres = data.get("wheres") or None
    logic  = data.get("logic") or "AND"
    if not dbname or not table:
        return jsonify({"ok": False, "error": "Hiányzó db vagy table paraméter."}), 400
    try:
        res = preview_table(_sid(), dbname, table, limit=limit, offset=offset, orders=orders, wheres=wheres, logic=logic)
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---------- SQL futtató ----------
@dbv_bp.route("/api/sql", methods=["POST"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_sql():
    _require_login()
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    dbname   = (data.get("db") or "").strip()
    sql_text = (data.get("sql") or "").strip()
    readonly = bool(data.get("readonly") if data.get("readonly") is not None else True)
    if not dbname or not sql_text:
        return jsonify({"ok": False, "error": "Hiányzó db vagy SQL."}), 400
    try:
        res = run_sql(_sid(), dbname, sql_text, readonly=readonly)
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# ---------- ÍRÓ VÉGPONTOK (csak IT) ----------
@dbv_bp.route("/api/update_cell", methods=["POST"])
@login_required()
@require_roles(IT_ROLES)  # ← csak IT
def api_update_cell():
    _require_login()
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    dbname  = (data.get("db") or "").strip()
    table   = (data.get("table") or "").strip()
    column  = (data.get("column") or "").strip()
    pk_vals = data.get("pk") or {}
    new_val = data.get("value")
    if not dbname or not table or not column or not pk_vals:
        return jsonify({"ok": False, "error": "Hiányzó paraméter."}), 400
    try:
        n = update_cell(_sid(), dbname, table, pk_vals, column, new_val)
        return jsonify({"ok": True, "affected": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@dbv_bp.route("/api/insert_row", methods=["POST"])
@login_required()
@require_roles(IT_ROLES)
def api_insert_row():
    _require_login()
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    dbname = (data.get("db") or "").strip()
    table  = (data.get("table") or "").strip()
    row    = data.get("row") or {}
    if not dbname or not table or not row:
        return jsonify({"ok": False, "error": "Hiányzó paraméter."}), 400
    try:
        n = insert_row(_sid(), dbname, table, row)
        return jsonify({"ok": True, "affected": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@dbv_bp.route("/api/delete_rows", methods=["POST"])
@login_required()
@require_roles(IT_ROLES)
def api_delete_rows():
    _require_login()
    data = request.get_json(silent=True) or {}
    _csrf_check(data.get("csrf"))
    dbname = (data.get("db") or "").strip()
    table  = (data.get("table") or "").strip()
    pklist = data.get("rows") or []
    if not dbname or not table or not pklist:
        return jsonify({"ok": False, "error": "Hiányzó paraméter."}), 400
    try:
        n = delete_rows(_sid(), dbname, table, pklist)
        return jsonify({"ok": True, "affected": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@dbv_bp.route("/api/ping", methods=["GET"])
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_ping():
    if not session.get("dbv_logged_in"):
        return jsonify({"ok": True, "logged_in": False})
    ok, err = test_connection(_sid())
    return jsonify({"ok": ok, "error": err, "logged_in": True})
