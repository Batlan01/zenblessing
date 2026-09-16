# routes/bartender_project_creator_report_injector.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from flask import Blueprint, render_template, request, session, jsonify, g
from routes.auth import login_required, require_roles
from utils.roles import IT_ROLES, MANAGER_ROLES
from services.db import get_db

from services.bartender_project_creator_report_core import (
    list_bartender_activity_filters,
    query_bartender_project_activity_aggregated,
    ensure_bartender_project_activity_schema,
    query_active_project_locks,
    query_project_size_batch,
    force_delete_project_lock,
    query_project_history,
)



bp_bartender_project_report = Blueprint(
    "bartender_project_report",
    __name__,
    url_prefix="/<lang>/bartender_project_report",
)

@bp_bartender_project_report.url_value_preprocessor
def pull_lang(endpoint, values):
    g.lang = values.pop("lang", "hu")


@bp_bartender_project_report.get("/")
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def page():
    lang = getattr(g, "lang", "hu")
    try:
        db = get_db()
        ensure_bartender_project_activity_schema(db)
    except Exception:
        pass

    return render_template(
        f"{lang}/bartender_project_creator_report.html",
        lang=lang,
        active="bartender_project_report",
        user=session.get("user"),
    )


@bp_bartender_project_report.get("/api/filters")
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_filters():
    import services.bartender_project_creator_report_core as core
    print("CORE FILE:", core.__file__)

    db = get_db()
    start = request.args.get("start") or None
    end = request.args.get("end") or None

    try:
        data = list_bartender_activity_filters(db, start=start, end=end)
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp_bartender_project_report.get("/api/activity")
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_activity():
    db = get_db()

    start = request.args.get("start") or None
    end = request.args.get("end") or None
    username = request.args.get("username") or None
    project = request.args.get("project") or None

    try:
        limit = int(request.args.get("limit") or 5000)
    except Exception:
        limit = 5000
    limit = max(1, min(limit, 20000))

    try:
        rows = query_bartender_project_activity_aggregated(
            db,
            start=start,
            end=end,
            username=username,
            project=project,
            limit=limit,
        )
        return jsonify({"ok": True, "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp_bartender_project_report.get("/api/locks")
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_locks():
    db = get_db()
    try:
        max_age_sec = int(request.args.get("max_age_sec") or 180)
    except Exception:
        max_age_sec = 180

    try:
        rows = query_active_project_locks(db, max_age_sec=max_age_sec, limit=200)
        return jsonify({"ok": True, "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    
@bp_bartender_project_report.post("/api/force_unlock")
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_force_unlock():
    """
    Admin kényszerített lock törlés.
    Body: { "config_id": <int> }
    """
    db = get_db()
    data = request.get_json(silent=True) or {}
    config_id = data.get("config_id")

    if config_id is None:
        return jsonify({"ok": False, "error": "config_id kötelező"}), 400

    try:
        result = force_delete_project_lock(db, config_id=int(config_id))
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp_bartender_project_report.get("/api/project_size")
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_project_size():
    """
    Projekt méret adatok (oldal / kábel / konnektor szám).
    Query param: projects = vesszővel elválasztott 'PN | REV | ECN' lista
    Pl.: /api/project_size?projects=1234+|+A+|+10001,5678+|+B+|+20002
    """
    db = get_db()
    raw = request.args.get("projects") or ""
    project_keys = [p.strip() for p in raw.split(",") if p.strip()]

    if not project_keys:
        return jsonify({"ok": True, "sizes": {}})

    try:
        sizes = query_project_size_batch(db, project_keys)
        return jsonify({"ok": True, "sizes": sizes})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp_bartender_project_report.get("/api/project_history")
@login_required()
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_project_history():
    """
    Egy projekt szerkesztési előzményei.
    Query param: pn, rev, ecn  (kötelező)
    """
    db = get_db()
    pn  = (request.args.get("pn")  or "").strip()
    rev = (request.args.get("rev") or "").strip()
    ecn = (request.args.get("ecn") or "").strip()

    if not pn:
        return jsonify({"ok": False, "error": "pn kötelező"}), 400

    try:
        limit = int(request.args.get("limit") or 200)
    except Exception:
        limit = 200

    try:
        rows = query_project_history(db, pn=pn, rev=rev, ecn=ecn, limit=limit)
        return jsonify({"ok": True, "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400