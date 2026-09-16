# routes/tl_settings_injector.py
# -*- coding: utf-8 -*-
"""
"Egyéb beállítások" oldal (Raspberry – TL menü, csak IT).

A TeamLeader oldal jogosultságainak kezelése: kik férnek hozzá a WO
keresőhöz, ki melyik állomást (EMI/MTE/MDI/QC/TEST/SOLD/MOLD) és melyik
OTD scope-ot látja. A szabályok a tl_page_permissions táblában élnek,
lásd services/tl_settings.py.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from routes.auth import login_required, require_roles
from services import tl_settings
from utils.roles import IT_ROLES

log = logging.getLogger(__name__)

bp_tl_settings = Blueprint("tl_settings", __name__, url_prefix="/<lang>/tl-settings")


def _actor() -> str:
    u = session.get("user") or {}
    return str(u.get("username") or u.get("display_name") or "")[:190]


def _payload() -> dict:
    return request.get_json(silent=True) or request.form.to_dict(flat=True) or {}


@bp_tl_settings.route("/", methods=["GET"])
@login_required()
@require_roles(IT_ROLES)
def page(lang):
    if lang not in ("hu", "sk"):
        return redirect(url_for("tl_settings.page", lang="hu"))

    try:
        rules = tl_settings.list_rules()
        load_error = ""
    except Exception as e:
        log.exception("tl_settings lista betöltése sikertelen")
        rules = []
        load_error = str(e)

    return render_template(
        f"{lang}/tl_settings.html",
        user=session.get("user"),
        lang=lang,
        active="tl_settings",
        rules=rules,
        load_error=load_error,
        all_stations=tl_settings.ALL_STATIONS,
        all_otd_scopes=tl_settings.ALL_OTD_SCOPES,
    )


@bp_tl_settings.get("/api/rules")
@login_required()
@require_roles(IT_ROLES)
def api_list(lang):
    try:
        return jsonify(ok=True, rules=tl_settings.list_rules())
    except Exception as e:
        log.exception("api_list hiba")
        return jsonify(ok=False, msg=str(e)), 500


@bp_tl_settings.post("/api/rules")
@login_required()
@require_roles(IT_ROLES)
def api_create(lang):
    try:
        new_id = tl_settings.create_rule(_payload(), actor=_actor())
        return jsonify(ok=True, id=new_id, rules=tl_settings.list_rules())
    except tl_settings.RuleError as e:
        return jsonify(ok=False, msg=str(e)), 400
    except Exception as e:
        log.exception("api_create hiba")
        return jsonify(ok=False, msg=str(e)), 500


@bp_tl_settings.post("/api/rules/<int:rule_id>")
@bp_tl_settings.put("/api/rules/<int:rule_id>")
@login_required()
@require_roles(IT_ROLES)
def api_update(lang, rule_id):
    try:
        tl_settings.update_rule(rule_id, _payload(), actor=_actor())
        return jsonify(ok=True, rules=tl_settings.list_rules())
    except tl_settings.RuleError as e:
        return jsonify(ok=False, msg=str(e)), 400
    except Exception as e:
        log.exception("api_update hiba")
        return jsonify(ok=False, msg=str(e)), 500


@bp_tl_settings.delete("/api/rules/<int:rule_id>")
@login_required()
@require_roles(IT_ROLES)
def api_delete(lang, rule_id):
    try:
        tl_settings.delete_rule(rule_id, actor=_actor())
        return jsonify(ok=True, rules=tl_settings.list_rules())
    except Exception as e:
        log.exception("api_delete hiba")
        return jsonify(ok=False, msg=str(e)), 500


@bp_tl_settings.get("/api/ad-users")
@login_required()
@require_roles(IT_ROLES)
def api_ad_users(lang):
    """Névkiegészítés új szabály felvételéhez (AD keresés)."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(ok=True, users=[])
    try:
        from services.ad_users import list_users
        users = list_users(query=q, limit=25) or []
    except Exception as e:
        # AD nem elérhető: a felvétel kézi gépeléssel továbbra is működik
        log.warning("AD keresés sikertelen: %s", e)
        return jsonify(ok=True, users=[], msg="Az AD keresés most nem elérhető.")

    return jsonify(ok=True, users=[
        {
            "username": u.get("username") or "",
            "display_name": u.get("display_name") or "",
            "job_title": u.get("job_title") or "",
            "disabled": bool(u.get("disabled")),
        }
        for u in users if u.get("username")
    ])
