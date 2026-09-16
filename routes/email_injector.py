# -*- coding: utf-8 -*-
"""
routes/email_injector.py
=========================
Más blueprint-ek ezt a modult importálják email küldéshez.

Regisztrálás __init__.py-ban:
    from routes.email_injector import email_injector_bp
    app.register_blueprint(email_injector_bp)

Használat más blueprint-ből:
    from routes.email_injector import send_page_email, send_direct_email

    # 1) Előre konfigurált sablon alapján (ajánlott):
    send_page_email(
        page_key="qc_bartender_confirm",
        dynamic_data={
            "pn":           "9320-5807",
            "rev":          "A",
            "confirmed_by": "jkovacs",
            "confirmed_at": "2025-03-01 14:22",
        },
    )

    # 2) Közvetlen küldés sablonkonfiguráció nélkül:
    send_direct_email(
        to_addresses=["vezeto@ceg.hu", "minosegellenor@ceg.hu"],
        subject="[QC] Ellenőrzés kész",
        body="<p>A <b>9320-5807</b> PN sablonjai ellenőrizve.</p>",
    )
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, session

from routes.auth import require_roles, MANAGER_ROLES, IT_ROLES
from services.email_core import send_for_page_key, send_direct


# =============================================================================
# Blueprint  –  opcionális; csak akkor szükséges, ha a /email/* route-okat
# különálló fájlból akarod registrálni az email_core-tól.
# Ha az email_core.py-t közvetlenül regisztrálod, ez a blueprint elhagyható.
# =============================================================================
email_injector_bp = Blueprint("email_injector", __name__)


# =============================================================================
# PUBLIKUS SEGÉDFÜGGVÉNYEK  –  más blueprint-ek importálják
# =============================================================================

def send_page_email(
    page_key: str,
    dynamic_data: dict | None = None,
    triggered_by: str | None = None,
) -> dict:
    """
    Küldi az emailt az adott page_key konfigurációja alapján.
    A hívás azonnal visszatér (háttérszál).

    Paraméterek:
        page_key      – az email_page_configs.page_key értéke
        dynamic_data  – {{'változó': 'érték'}} dict a sablon tölteléséhez
        triggered_by  – ki indította el (pl. session username); ha None,
                        a Flask session-ből olvassa

    Visszatér:
        {"ok": bool, "queued": int, "error": str|None}
    """
    if triggered_by is None:
        try:
            u = session.get("user") or {}
            triggered_by = u.get("username") or u.get("name") or "system"
        except Exception:
            triggered_by = "system"

    return send_for_page_key(
        page_key=page_key,
        dynamic_data=dynamic_data or {},
        triggered_by=triggered_by,
    )


def send_direct_email(
    to_addresses: list[str],
    subject: str,
    body: str,
    is_html: bool = True,
    page_key: str = "direct",
    triggered_by: str = "system",
) -> dict:
    """
    Közvetlen küldés – nincs szükség előre beállított page_key konfigurációra.
    Hasznos egyszeri, specifikus értesítéseknél.

    Paraméterek:
        to_addresses  – e-mail cím lista
        subject       – tárgy (nem sablon, már renderelt)
        body          – törzs (HTML vagy plain text)
        is_html       – True ha HTML, False ha plain text
        page_key      – a napló bejegyzéshez (opcionális, alapértelmezett: "direct")
        triggered_by  – naplóba kerülő felhasználó neve

    Visszatér:
        {"ok": bool, "queued": int, "error": str|None}
    """
    return send_direct(
        to_addresses=to_addresses,
        subject=subject,
        body=body,
        is_html=is_html,
        page_key=page_key,
        triggered_by=triggered_by,
    )


# =============================================================================
# OPCIONÁLIS WEBHOOK  –  ha más szolgáltatások HTTP-n keresztül akarnak
# emailt indítani (pl. Raspberry Pi script, N8N workflow stb.)
# =============================================================================

@email_injector_bp.post("/api/email/trigger")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_trigger():
    """
    POST {
        "page_key":    "qc_bartender_confirm",
        "dynamic_data": {"pn": "...", "confirmed_by": "..."}
    }
    """
    data     = request.get_json(silent=True) or {}
    page_key = (data.get("page_key") or "").strip()
    dyn_data = data.get("dynamic_data") or {}

    if not page_key:
        return jsonify({"ok": False, "error": "page_key kötelező"}), 400

    try:
        u = session.get("user") or {}
        caller = u.get("username") or u.get("name") or "api_trigger"
    except Exception:
        caller = request.headers.get("X-User") or "api_trigger"

    result = send_page_email(page_key=page_key, dynamic_data=dyn_data, triggered_by=caller)
    return jsonify(result), 200 if result["ok"] else 400