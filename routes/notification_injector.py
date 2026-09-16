# routes/notification_injector.py
from __future__ import annotations

from flask import Blueprint, render_template, request, jsonify, g, session

from routes.auth import login_required, require_roles
from utils.roles import IT_ROLES, MANAGER_ROLES
from services.notification_core import hub

bp_notify = Blueprint("notifications", __name__, url_prefix="/<lang>/notifications")


@bp_notify.url_value_preprocessor
def pull_lang(endpoint, values):
    g.lang = values.pop("lang", "hu")


# --- Admin UI ---
@bp_notify.get("/")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def notification_admin_page():
    return render_template(f"{g.lang}/notification.html", lang=g.lang, active="notifications", user=session.get("user"))


# --- API: állapot (notices + maintenance + clients) ---
@bp_notify.get("/api/state")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_state():
    return jsonify({
        "ok": True,
        "maintenance": hub.maintenance.to_public(),
        "notices": hub.list_notices(active_only=False),
        "clients": hub.list_clients(visible_only=True, active_within_seconds=35),

    })


# --- API: küldés ---
@bp_notify.post("/api/send")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_send_notice():
    data = request.get_json(force=True) or {}

    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "Üzenet nem lehet üres."}), 400

    ui = (data.get("ui") or "toast").strip().lower()
    level = (data.get("level") or "warning").strip().lower()

    # modal után toast?
    toast_after_modal = bool(data.get("toast_after_modal", False))

    # toast sticky?
    sticky = bool(data.get("sticky", False))

    # ETA / shutdown_at
    eta_minutes = data.get("eta_minutes", None)
    try:
        eta_minutes = int(eta_minutes) if eta_minutes not in (None, "", False) else None
    except Exception:
        eta_minutes = None

    shutdown_at = (data.get("shutdown_at") or "").strip() or None

    # targets
    targets = []

    # single target
    target_type = (data.get("target_type") or "all").strip().lower()
    target_value = (data.get("target_value") or "").strip()
    if target_type != "all" and target_value:
        targets.append({"type": target_type, "value": target_value})

    # multi targets
    mt = data.get("multi_targets", []) or []
    if isinstance(mt, list):
        for t in mt:
            if not isinstance(t, dict):
                continue
            ttype = (t.get("type") or "").strip().lower()
            val = (t.get("value") or "").strip()
            if ttype and val:
                targets.append({"type": ttype, "value": val})

    created_by = None
    u = session.get("user") or {}
    if u:
        created_by = str(u.get("display_name") or u.get("username") or u.get("id") or "")

    notice = hub.create_notice(
        message=message,
        level=level,
        ui=ui,
        sticky=sticky,
        toast_after_modal=toast_after_modal,
        eta_minutes=eta_minutes,
        shutdown_at=shutdown_at,
        targets=targets,
        created_by=created_by,
    )

    hub.dispatch_notice(notice)
    return jsonify({"ok": True, "notice": notice.to_public()})


# --- API: maintenance mode ---
@bp_notify.post("/api/maintenance")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_maintenance():
    data = request.get_json(force=True) or {}

    enabled = bool(data.get("enabled", False))
    message = (data.get("message") or "").strip()
    level = (data.get("level") or "warning").strip().lower()

    eta_minutes = data.get("eta_minutes", None)
    try:
        eta_minutes = int(eta_minutes) if eta_minutes not in (None, "", False) else None
    except Exception:
        eta_minutes = None

    shutdown_at = (data.get("shutdown_at") or "").strip() or None

    hub.set_maintenance(
        enabled=enabled,
        message=message,
        level=level,
        eta_minutes=eta_minutes,
        shutdown_at=shutdown_at,
    )
    hub.broadcast_maintenance()
    return jsonify({"ok": True, "maintenance": hub.maintenance.to_public()})


# --- API: stop notice (aktív->false + kliens oldali eltüntetés) ---
@bp_notify.post("/api/notices/<notice_id>/stop")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_stop_notice(notice_id: str):
    ok = hub.stop_notice(notice_id)  # ebben már benne van a broadcast
    return jsonify({"ok": ok})

@bp_notify.delete("/api/notices/<notice_id>")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_delete_notice(notice_id: str):
    ok = hub.delete_notice(notice_id)  # ebben már benne van a broadcast
    return jsonify({"ok": ok})



# --- API: clients ---
@bp_notify.get("/api/clients")
@login_required()
@require_roles(IT_ROLES | MANAGER_ROLES)
def api_list_clients():
    visible_only = request.args.get("visible_only", "1") == "1"
    clients = hub.list_clients(visible_only=visible_only, active_within_seconds=35)
    return jsonify({"ok": True, "clients": clients})


# --- API: acks ---
@bp_notify.get("/api/acks/<notice_id>")
@login_required()
@require_roles(IT_ROLES, MANAGER_ROLES)
def api_acks(notice_id: str):
    return jsonify({"ok": True, "notice_id": notice_id, "acks": hub.get_acks(notice_id)})


# --- Public UI (no login) ---
@bp_notify.get("/public")
def notification_public_page():
    # Itt bármilyen egyszerű template jó, ami extends-eli a base.html-t
    # és opcionálisan beállítja az active-t (de a base fallback miatt nem kötelező).
    return render_template(
        f"{g.lang}/notification_public.html",
        lang=g.lang,
        active="notifications_public",
        user=session.get("user"),  # publikusnál ez None lesz, oké
    )
