# routes/teamleaders.py
from flask import Blueprint, render_template, session, request, redirect, url_for, flash, jsonify
from services.db import get_db
from services import tl_settings
from utils.helpers import get_assembly_data
from datetime import datetime
from routes.auth import (
    login_required,
    require_roles,
    MANAGER_ROLES,
    TEAMLEADER_ROLES,
    IT_ROLES,
)

teamleaders_bp = Blueprint("teamleaders", __name__)

@teamleaders_bp.route("/<lang>/teamleaders", methods=["GET"])
@login_required()
@require_roles(MANAGER_ROLES, TEAMLEADER_ROLES, IT_ROLES)
def teamleaders(lang):
    if lang not in ("hu", "sk"):
        return redirect(url_for("teamleaders.teamleaders", lang="hu"))

    # Lapozás paraméterek (a frontend úgyis API-n tölti a táblát)
    page = request.args.get("page", 1, type=int) or 1

    user_obj = session.get("user") or {}
    allowed_assembly = _assembly_allowed_stations_for_user(user_obj)  # station_id scope

    # NEM preloadolunk adatot process_id=ASSEMBLY alapján, mert station_id-t nézünk
    assembly_data = []
    total_pages = 1

    return render_template(
        f"{lang}/dashboard.html",
        user=session["user"],
        assembly_data=assembly_data,
        current_page=page,
        total_pages=total_pages,
        active="teamleaders",
        lang=lang,
        can_wo_search=_can_use_wo_search(user_obj),
        can_set_priority=tl_settings.can_set_priority(user_obj),
        allowed_assembly_stations=allowed_assembly,
    )

def _assembly_allowed_stations_for_user(user=None):
    user = user or {}

    # Elsődleges: "Egyéb beállítások" oldalon felvett állomás-szabály.
    override = tl_settings.stations_for(user)
    if override:
        return override

    # session user általában dict
    if isinstance(user, dict):
        jt = (user.get("job_title") or user.get("title") or "").strip().upper()
        roles = user.get("roles") or user.get("role") or ""
        if isinstance(roles, (list, tuple)):
            roles_txt = " ".join(str(x) for x in roles)
        else:
            roles_txt = str(roles)
        blob = f"{jt} {roles_txt}".upper()
    else:
        jt = (getattr(user, "job_title", "") or getattr(user, "title", "") or "").strip().upper()
        blob = jt

    ALL = ["EMI", "MTE", "MDI", "QC", "TEST", "SOLD", "MOLD"]

    # IT / Manager jelleg -> mindent láthat
    if "IT" in blob or "MANAGER" in blob or "SUPERVISOR" in blob:
        return ALL

    # ha konkrét állomás szerepel
    for s in ALL:
        if s in blob:
            return [s]

    # fallback
    return ["EMI", "MTE", "MDI"]




# ================== OTD: táblák / scope-ok ==================
# TODO: itt hagyd úgy, ahogy nálad már megvan (csoport -> DB tábla)
_OTD_TABLES = {
    "EMI": "OTD_EMI",
    "MTE": "OTD_MTE",
    "MDI": "OTD_MDI",
    "SIEMENS": "OTD_Siemens",
    "SWISSVARIAN": "OTD_Swissvarian",
}


# ================== OTD: kivételek (később töltöd) ==================
_OTD_EXCEPTION_NAMES = {
    # "NIKOLAS TRENCÍK",
    # "DAVID XYZ",
}
_OTD_EXCEPTION_JOB_TITLES = {
    # "PRODUCTION MANAGER",
    # "PLANNER",
}

_OTD_DISPLAY_COLUMNS = [
    "WO Nbr","Start Date","Due Date","Ship Date","Oper","Cell","DOL","LOC","CUR WC",
    "Part Number","PR ST","Cust Code","QTY MFG","Hours"
]

_WO_SEARCH_ALLOWED_USERNAMES = {
    "ntrencik",
}
_WO_SEARCH_ALLOWED_USERNAMES = {str(x).strip().lower() for x in _WO_SEARCH_ALLOWED_USERNAMES if str(x).strip()}


def _get_username(u: dict) -> str:
    return str(
        u.get("username")
        or u.get("user_name")
        or u.get("login")
        or u.get("email")
        or u.get("name")
        or ""
    ).strip().lower()

def _can_use_wo_search(u: dict) -> bool:
    # Elsődleges: "Egyéb beállítások" oldalon felvett szabály (tl_page_permissions).
    # Ha nincs a userre/job title-jére szabály, marad a régi allowlist.
    allowed = tl_settings.can_wo_search(u)
    if allowed is not None:
        return allowed
    return _get_username(u) in _WO_SEARCH_ALLOWED_USERNAMES

def _otd_norm_key(s: str) -> str:
    # "WO Nbr" -> "wonbr", "CUR WC" -> "curwc", "WO_NBR" -> "wonbr"
    return "".join(ch.lower() for ch in str(s) if ch.isalnum())

def _otd_to_display_row(db_row: dict) -> dict:
    """
    DB row kulcsai lehetnek pl: WO_NBR / woNbr / WO Nbr / etc.
    Frontend viszont fixen: "WO Nbr", "Start Date", ...
    """
    if not isinstance(db_row, dict):
        return {k: "" for k in _OTD_DISPLAY_COLUMNS}

    # gyors lookup: normalizált kulcs -> eredeti kulcs
    lookup = {_otd_norm_key(k): k for k in db_row.keys()}

    out = {}
    for disp_key in _OTD_DISPLAY_COLUMNS:
        nk = _otd_norm_key(disp_key)
        src_key = lookup.get(nk)

        # extra próbák (ha eltérő elnevezések vannak)
        if src_key is None:
            # pl. "QTY MFG" -> "qtymfg", lehet a DB-ben "QTY_MANUF" stb.
            # ide később felvehetsz kézi aliasokat, ha kell
            alias_norms = {
                "wonbr": ["wonbr", "wonumber", "workorder", "wo"],
                "partnumber": ["partnumber", "pn"],
                "startdate": ["startdate", "start", "startdt"],
                "duedate": ["duedate", "due", "duedt"],
                "shipdate": ["shipdate", "ship", "shipdt"],
                "qtymfg": ["qtymfg", "qty", "quantity", "qtymanufactured", "qtymanuf"],
                "curwc": ["curwc", "currentwc", "currwc"],
                "custcode": ["custcode", "customer", "customercode"],
            }.get(nk, [])

            for cand in alias_norms:
                src_key = lookup.get(cand)
                if src_key is not None:
                    break

        out[disp_key] = (db_row.get(src_key) if src_key is not None else "")

    return out


def _otd_is_exception_user(u: dict) -> bool:
    name = str(u.get("name") or u.get("user_name") or u.get("username") or "").strip().upper()
    job  = str(u.get("job_title") or u.get("title") or "").strip().upper()
    return (name in _OTD_EXCEPTION_NAMES) or (job in _OTD_EXCEPTION_JOB_TITLES)

def _otd_allowed_scopes_for_user() -> list[str]:
    u = session.get("user") or {}

    # Elsődleges: "Egyéb beállítások" oldalon felvett OTD scope szabály.
    override = tl_settings.otd_scopes_for(u)
    if override:
        return [s for s in override if s in _OTD_TABLES]

    # ✅ KIVÉTEL: mindent láthat
    if _otd_is_exception_user(u):
        return list(_OTD_TABLES.keys())

    job_title = str(u.get("job_title") or u.get("title") or "").upper()

    roles = u.get("roles") or u.get("role") or ""
    if isinstance(roles, (list, tuple)):
        roles_txt = " ".join([str(x) for x in roles])
    else:
        roles_txt = str(roles)

    blob = f"{job_title} {roles_txt}".upper()

    # IT / manager: mindent lát
    if "IT" in blob or "MANAGER" in blob:
        return list(_OTD_TABLES.keys())

    scopes = []
    for k in _OTD_TABLES.keys():
        if k.upper() in blob:
            scopes.append(k)

    return scopes or []


# ================== ASSEMBLY STATUS: station scope + kivételek ==================
# ================== ASSEMBLY STATUS: station scope + kivételek ==================
_ASSEMBLY_STATUS_STATIONS = ["EMI", "MTE", "MDI", "QC", "TEST", "SOLD", "MOLD"]


_ASSEMBLY_STATUS_EXCEPTION_NAMES = {
    # "NIKOLAS TRENČÍK",
}
_ASSEMBLY_STATUS_EXCEPTION_JOB_TITLES = {
    # "PRODUCTION MANAGER",
    # "PLANNER",
}

def _assembly_status_is_exception_user(u: dict) -> bool:
    name = str(u.get("name") or u.get("user_name") or u.get("username") or "").strip().upper()
    job  = str(u.get("job_title") or u.get("title") or "").strip().upper()
    return (name in _ASSEMBLY_STATUS_EXCEPTION_NAMES) or (job in _ASSEMBLY_STATUS_EXCEPTION_JOB_TITLES)

# teamleaders.py

def _assembly_allowed_stations_for_user(user):
    # Elsődleges: "Egyéb beállítások" oldalon felvett állomás-szabály.
    override = tl_settings.stations_for(user if isinstance(user, dict) else {
        "job_title": getattr(user, "job_title", "") or getattr(user, "title", ""),
        "username": getattr(user, "username", ""),
    })
    if override:
        return override

    # user jöhet dict-ként (session["user"]) vagy objektumként
    if isinstance(user, dict):
        jt = (user.get("job_title") or user.get("title") or "").strip().upper()

        # ✅ Quality Team Leader csak QC + TEST
        if jt == "QUALITY TEAM LEADER":
            return ["QC", "TEST"]

        roles = user.get("roles") or user.get("role") or ""
        if isinstance(roles, (list, tuple)):
            roles_txt = " ".join(str(x) for x in roles)
        else:
            roles_txt = str(roles)
        blob = f"{jt} {roles_txt}".upper()
    else:
        jt = (getattr(user, "job_title", "") or getattr(user, "title", "") or "").strip().upper()

        # ✅ Quality Team Leader csak QC + TEST
        if jt == "QUALITY TEAM LEADER":
            return ["QC", "TEST"]

        blob = jt

    ALL = ["EMI", "MTE", "MDI", "QC", "TEST", "SOLD", "MOLD"]

    # IT / manager: mindent láthat
    if "IT" in blob or "MANAGER" in blob:
        return ALL

    # ha konkrét állomás szerepel
    for s in ALL:
        if s in blob:
            return [s]

    # fallback
    return ["EMI", "MTE", "MDI"]