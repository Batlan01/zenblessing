# -*- coding: utf-8 -*-
"""
Active Directory felhasználó-kezelő modul route-jai.

UI:   GET  /<lang>/ad_users/
API:  GET  /<lang>/ad_users/api/list            -> felhasználók listája
      GET  /<lang>/ad_users/api/user/<username>  -> egy felhasználó property-jei
      POST /<lang>/ad_users/api/user/<username>/title    -> job title módosítás
      POST /<lang>/ad_users/api/user/<username>/password -> jelszó reset
      POST /<lang>/ad_users/api/create           -> új felhasználó
"""
from __future__ import annotations

from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, session, g

from services.ad_users import (
    list_users,
    get_user,
    update_job_title,
    reset_password,
    create_user,
    ADError,
)
from routes.auth import login_required, require_roles
from utils.roles import IT_ROLES, MANAGER_ROLES

BP_TEMPLATES = str((Path(__file__).resolve().parents[1] / "templates"))

bp_ad_users = Blueprint(
    "ad_users",
    __name__,
    url_prefix="/<lang>/ad_users",
    template_folder=BP_TEMPLATES,
)

# Az AD admin műveletekhez csak IT (illetve menedzsment) férhet hozzá
AD_ADMIN_ACCESS = IT_ROLES | MANAGER_ROLES


@bp_ad_users.url_value_preprocessor
def pull_lang(endpoint, values):
    lang = values.pop("lang", "hu")
    if lang not in {"hu", "sk", "en"}:
        lang = "hu"
    g.lang = lang


def _ad_error(e: ADError, status: int = 400):
    return jsonify({"ok": False, "error": str(e)}), status


# ============================ UI ============================
@bp_ad_users.get("/")
@login_required()
@require_roles(AD_ADMIN_ACCESS)
def ad_users_home():
    lang = getattr(g, "lang", "hu")
    # Egy közös sablon, ami a nyelvi base.html-t terjeszti ki (lang ~ '/base.html')
    return render_template(
        "ad_users.html",
        lang=lang,
        active="ad_users",
        user=session.get("user"),
    )


# ============================ API ============================
@bp_ad_users.get("/api/list")
@login_required()
@require_roles(AD_ADMIN_ACCESS)
def api_list():
    query = (request.args.get("q") or "").strip() or None
    try:
        users = list_users(query=query)
        return jsonify({"ok": True, "users": users, "count": len(users)})
    except ADError as e:
        return _ad_error(e)


@bp_ad_users.get("/api/user/<username>")
@login_required()
@require_roles(AD_ADMIN_ACCESS)
def api_get_user(username):
    try:
        user = get_user(username)
        if not user:
            return jsonify({"ok": False, "error": "A felhasználó nem található."}), 404
        return jsonify({"ok": True, "user": user})
    except ADError as e:
        return _ad_error(e)


@bp_ad_users.post("/api/user/<username>/title")
@login_required()
@require_roles(AD_ADMIN_ACCESS)
def api_update_title(username):
    data = request.get_json(silent=True) or {}
    new_title = (data.get("job_title") or "").strip()
    try:
        result = update_job_title(username, new_title)
        return jsonify({"ok": True, "result": result})
    except ADError as e:
        return _ad_error(e)


@bp_ad_users.post("/api/user/<username>/password")
@login_required()
@require_roles(AD_ADMIN_ACCESS)
def api_reset_password(username):
    data = request.get_json(silent=True) or {}
    new_password = data.get("new_password") or ""
    must_change = bool(data.get("must_change", True))
    if not new_password:
        return jsonify({"ok": False, "error": "Hiányzó új jelszó."}), 400
    try:
        result = reset_password(username, new_password, must_change=must_change)
        return jsonify({"ok": True, "result": result})
    except ADError as e:
        return _ad_error(e)


@bp_ad_users.post("/api/create")
@login_required()
@require_roles(AD_ADMIN_ACCESS)
def api_create_user():
    data = request.get_json(silent=True) or {}
    try:
        result = create_user(
            username=data.get("username", ""),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            password=data.get("password", ""),
            job_title=data.get("job_title", ""),
            department=data.get("department", ""),
            mail=data.get("mail", ""),
            must_change=bool(data.get("must_change", True)),
        )
        return jsonify({"ok": True, "result": result})
    except ADError as e:
        return _ad_error(e)
