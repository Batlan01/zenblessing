# routes/auth.py
from functools import wraps
from urllib.parse import urlparse, urljoin
from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from services.ldap_auth import authenticate_user
from utils.roles import (
    MANAGER_ROLES, IT_ROLES, TEAMLEADER_ROLES, QUALITY_TEAMLEADER_ROLES,
    PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE,
    STORE_ROLES, QC_ROLES, TEST_ROLES, HR_ROLES,
    has_any_role,
)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# --- szerepkör halmazok ---
MANAGER_ROLES     = {"MANAGER", "SUPERVISOR", "SHIFT SUPERVISOR"}
IT_ROLES          = {"IT", "INFORMATION TECHNOLOGY"}
TEAMLEADER_ROLES  = {"TEAM LEAD", "TEAMLEADER", "EMI", "MTE", "MDI", "QUALITY TEAM LEADER", "SOLD"}
QUALITY_TEAMLEADER_ROLES = {"QUALITY TEAM LEADER"}  # csak a Quality Team Leader job title
PRINTOPERATOR_ROLE     = {"PRINT OPERATOR"}
ETIKET_CREATOR_ROLE = {"SENIOR PRODUCTION ENGINEER", "PRODUCTION ENGINEER", "NPI DEPARTMENT SPEC WRITER"}
STORE_ROLES = {"STORE", "WAREHOUSE", "LOGISTICS", "Shift Sepervisor STORE"}  # tetszőlegesen bővíthető
QC_ROLES = {"QC"}
TEST_ROLES = {"TEST"}
HR_ROLES = {"HR"}


def _normalize(s: str) -> str:
    return (s or "").strip().upper()

def user_in_any_role(job_title: str, roles: set[str]) -> bool:
    return has_any_role(job_title, roles)

def route_for_job_title(job_title: str, lang: str) -> str | None:
    jt = _normalize(job_title)
    if user_in_any_role(jt, MANAGER_ROLES) or user_in_any_role(jt, TEAMLEADER_ROLES):
        return url_for("teamleaders.teamleaders", lang=lang)
    if user_in_any_role(jt, IT_ROLES):
        return url_for("main.index", lang=lang)
    if user_in_any_role(jt, PRINTOPERATOR_ROLE):
        return url_for("bartender.print_page", lang=lang)
    if user_in_any_role(jt, ETIKET_CREATOR_ROLE):
        return url_for("bartender.edit_v1", lang=lang)
    # ÚJ: STORE kezdőoldal (ha van külön store blueprinted)
    if user_in_any_role(jt, STORE_ROLES):
        return url_for("qr_injector.form", lang=lang)
    if user_in_any_role(jt, QC_ROLES):
        return url_for("scan.scan_page", lang=lang)
    if user_in_any_role(jt, TEST_ROLES):
        return url_for("scan.scan_page", lang=lang)
    if user_in_any_role(jt, HR_ROLES):
        return url_for("human_resources.hr_home", lang=lang)
    return None


# ---------- HELPER: biztonságos "vissza" átirányítás ----------
def _is_safe_url(target: str) -> bool:
    ref = urlparse(request.host_url)
    test = urlparse(urljoin(request.host_url, target))
    return test.scheme in {"http", "https"} and ref.netloc == test.netloc

def _redirect_back_or_home():
    """Menjen vissza a referrerre vagy az utolsó OK oldalra; végső fallback a 'home'."""
    back = request.referrer or session.get("last_ok_url")
    if back and _is_safe_url(back):
        return redirect(back)

    # ha nincs referrer/last_ok_url: a szerep szerinti kezdőoldal vagy a login
    lang = session.get("lang", "sk")
    user = session.get("user") or {}
    fallback = route_for_job_title(user.get("job_title", ""), lang) or url_for("auth.login", lang=lang)
    return redirect(fallback)

# ---------- HELPERS ----------
def _is_api_request() -> bool:
    """API kérés-e? (path alapján, vagy ha JSON választ vár)"""
    path = request.path or ""
    if path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False

def _api_session_expired():
    """API kérésekre 401-es JSON válasz session lejáratkor."""
    from flask import jsonify
    return jsonify({
        "error": "session_expired",
        "redirect": url_for("auth.login", lang=session.get("lang", "sk"))
    }), 401

# ---------- DEKORÁTOROK ----------
def login_required():
    """Ha nincs login -> loginra (vagy API esetén 401). Ha van, elengedjük és eltároljuk az oldalt 'last_ok_url'-be."""
    def _decorator(view):
        @wraps(view)
        def _wrapped(*args, **kwargs):
            if not session.get("user"):
                if _is_api_request():
                    return _api_session_expired()
                return redirect(url_for("auth.login", lang=session.get("lang", "sk")))
            # be van jelentkezve: jegyezzük meg a sikeresen elért oldalt
            session["last_ok_url"] = request.full_path if request.query_string else request.path
            return view(*args, **kwargs)
        return _wrapped
    return _decorator

def require_roles(*role_sets: set[str]):
    """
    Ha nincs megfelelő jogosultság:
      - API kérésekre: 401 JSON (session_expired) vagy 403 JSON (nincs jog)
      - Oldal kérésekre: NEM loginra dobjuk, hanem vissza az előző/utolsó működő oldalra,
        és piros flash üzenetet küldünk (toastként jelenik meg).
    """
    allowed = set().union(*role_sets)

    def _decorator(view):
        @wraps(view)
        def _wrapped(*args, **kwargs):
            user = session.get("user")
            if not user:
                if _is_api_request():
                    return _api_session_expired()
                return redirect(url_for("auth.login", lang=session.get("lang", "sk")))

            jt = user.get("job_title", "")
            if not has_any_role(jt, allowed):
                if _is_api_request():
                    from flask import jsonify
                    return jsonify({"error": "forbidden"}), 403
                flash("Nincs jogosultságod ehhez az oldalhoz.", "danger")
                return _redirect_back_or_home()

            # jogosult: jegyezzük meg ezt az oldalt mint 'utolsó OK'
            session["last_ok_url"] = request.full_path if request.query_string else request.path
            return view(*args, **kwargs)
        return _wrapped
    return _decorator

# ---------- LOGIN / LOGOUT ----------
@auth_bp.route("/login/<lang>", methods=["GET", "POST"])
def login(lang):
    if lang not in ("hu", "sk"):
        lang = "sk"

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_info = authenticate_user(username, password)

        if user_info:
            session["user"] = user_info
            session["lang"] = lang
            session.permanent = True

            target = route_for_job_title(user_info.get("job_title", ""), lang)
            if target:
                return redirect(target)

            flash("Nincs megfelelő jogosultságod!", "danger")
            return redirect(url_for("auth.login", lang=lang))

        flash("Hibás felhasználónév vagy jelszó!", "danger")

    return render_template(f"{lang}/login.html")

@auth_bp.route("/logout")
def logout():
    lang = session.get("lang", "sk")
    session.clear()
    return redirect(url_for("auth.login", lang=lang))