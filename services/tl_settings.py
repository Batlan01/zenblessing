# services/tl_settings.py
# -*- coding: utf-8 -*-
"""
TeamLeader oldal jogosultságainak tárolása és feloldása.

Korábban a TL oldal jogosultságai kódba voltak égetve több helyen
(routes/teamleaders.py, routes/api.py, routes/bartender.py). Ez a modul
adatbázisba mozgatja őket, hogy az "Egyéb beállítások" oldalról (csak IT)
szerkeszthetőek legyenek.

Két szintű szabály van:
  * subject_type='user'      -> egy konkrét felhasználónévre (sAMAccountName)
  * subject_type='job_title' -> egy AD job title-re (mindenki, akinek ez a titulusa)

A felhasználó szintű szabály felülírja a job title szintűt.
Ha nincs egyetlen illeszkedő szabály sem, a hívó a régi (kódba égetett)
logikára esik vissza – így a modul bevezetése nem változtat viselkedést.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable, Optional

import mysql.connector

from services.dbpool import get_pooled_connection

log = logging.getLogger(__name__)

# ── Választható értékek (a UI is ezeket kínálja fel) ──────────────────────────
ALL_STATIONS = ["EMI", "MTE", "MDI", "QC", "TEST", "SOLD", "MOLD"]
ALL_OTD_SCOPES = ["EMI", "MTE", "MDI", "SIEMENS", "SWISSVARIAN"]

SUBJECT_TYPES = ("user", "job_title")

# Megszakadt kapcsolatra utaló MySQL hibakódok
_RETRYABLE_ERRNOS = {2006, 2013, 2055}

# Rövid életű cache: a TL oldal API-jai kérésenként többször is kérdeznek.
_CACHE_TTL_S = 10.0
_cache_lock = threading.Lock()
_cache_rules: Optional[list[dict]] = None
_cache_stamp = 0.0

_schema_ready = False
_schema_lock = threading.Lock()


# ── DB segédek ───────────────────────────────────────────────────────────────
def _execute(query: str, params: tuple | list = (), *,
             fetchone: bool = False, fetchall: bool = False,
             return_lastrowid: bool = False):
    """Pool-ból vett kapcsolat, garantált close/visszaadás, egy retry."""
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        conn = None
        cur = None
        try:
            conn = get_pooled_connection()
            cur = conn.cursor(dictionary=True, buffered=True)
            cur.execute(query, params or ())
            if cur.with_rows:
                data = cur.fetchone() if fetchone else cur.fetchall()
            elif return_lastrowid:
                data = cur.lastrowid or 0
            else:
                data = cur.rowcount
            try:
                needs_commit = bool(conn.in_transaction)
            except Exception:
                needs_commit = True
            if needs_commit:
                conn.commit()
            return data
        except mysql.connector.Error as e:
            last_err = e
            if attempt == 1 and getattr(e, "errno", None) in _RETRYABLE_ERRNOS:
                continue
            raise
        finally:
            try:
                if cur is not None:
                    cur.close()
            except Exception:
                pass
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    raise last_err  # elvileg nem érünk ide


def ensure_schema() -> None:
    """Tábla létrehozása + egyszeri seed a régi, kódba égetett értékekből."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        _execute("""
            CREATE TABLE IF NOT EXISTS tl_page_permissions (
                id            INT AUTO_INCREMENT PRIMARY KEY,
                subject_type  ENUM('user','job_title') NOT NULL DEFAULT 'user',
                subject       VARCHAR(190) NOT NULL,
                display_name  VARCHAR(190) NOT NULL DEFAULT '',
                full_access   TINYINT(1)   NOT NULL DEFAULT 0,
                can_wo_search TINYINT(1)   NOT NULL DEFAULT 0,
                can_set_priority TINYINT(1) NOT NULL DEFAULT 0,
                stations      VARCHAR(255) NOT NULL DEFAULT '',
                otd_scopes    VARCHAR(255) NOT NULL DEFAULT '',
                note          VARCHAR(500) NOT NULL DEFAULT '',
                active        TINYINT(1)   NOT NULL DEFAULT 1,
                created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_by    VARCHAR(190) NOT NULL DEFAULT '',
                updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
                updated_by    VARCHAR(190) NOT NULL DEFAULT '',
                UNIQUE KEY uq_tl_perm_subject (subject_type, subject)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        _migrate()
        _schema_ready = True
        try:
            _seed_defaults()
        except Exception:
            log.exception("tl_settings seed sikertelen")


def _migrate() -> None:
    """Korábbi telepítésekben még nincs can_set_priority oszlop – pótoljuk."""
    try:
        row = _execute(
            "SELECT COUNT(*) AS c FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tl_page_permissions' "
            "AND COLUMN_NAME = 'can_set_priority'",
            fetchone=True,
        )
        if not (row or {}).get("c"):
            _execute("ALTER TABLE tl_page_permissions "
                     "ADD COLUMN can_set_priority TINYINT(1) NOT NULL DEFAULT 0 "
                     "AFTER can_wo_search")
            log.info("tl_settings: can_set_priority oszlop pótolva")
    except Exception:
        log.exception("tl_settings migráció sikertelen")


def _seed_defaults() -> None:
    """
    Első indításkor átemeli a korábban kódba égetett kivételeket, hogy a
    viselkedés ne változzon. Üres táblánál fut csak le.
    """
    row = _execute("SELECT COUNT(*) AS c FROM tl_page_permissions", fetchone=True)
    if (row or {}).get("c"):
        return

    seeds = [
        # (subject_type, subject, display_name, full_access, can_wo_search,
        #  can_set_priority, stations, otd_scopes, note)
        ("user", "ntrencik", "", 0, 1, 1, "", "",
         "WO kereső – átemelve a korábbi kódba égetett listából"),
        ("job_title", "IT", "", 0, 0, 1, "", "",
         "Prioritás állítás – hogy a bevezetéskor senki ne maradjon kizárva. "
         "Nyugodtan törölhető vagy szűkíthető."),
        ("job_title", "SHIFT SUPERVISOR", "", 1, 0, 0, "", "",
         "Minden állomás – átemelve a korábbi kivétellistából"),
        ("job_title", "QUALITY TEAM LEADER", "", 0, 0, 0, "QC,TEST", "",
         "Csak QC + TEST – átemelve a korábbi kódból"),
    ]
    for s in seeds:
        _execute(
            "INSERT IGNORE INTO tl_page_permissions "
            "(subject_type, subject, display_name, full_access, can_wo_search, "
            " can_set_priority, stations, otd_scopes, note, created_by, updated_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'seed','seed')",
            s,
        )
    log.info("tl_settings: alapértelmezett szabályok betöltve (%d db)", len(seeds))


# ── Normalizálás ─────────────────────────────────────────────────────────────
def norm_username(s: Any) -> str:
    return str(s or "").strip().lower()


def norm_job_title(s: Any) -> str:
    return str(s or "").strip().upper()


def norm_subject(subject_type: str, subject: Any) -> str:
    return norm_username(subject) if subject_type == "user" else norm_job_title(subject)


def username_of(user: dict | None) -> str:
    u = user or {}
    return norm_username(
        u.get("username")
        or u.get("user_name")
        or u.get("login")
        or u.get("email")
        or u.get("name")
    )


def job_title_of(user: dict | None) -> str:
    u = user or {}
    return norm_job_title(u.get("job_title") or u.get("title"))


def _split_csv(value: Any, allowed: Iterable[str]) -> list[str]:
    allowed_set = {a.upper() for a in allowed}
    out: list[str] = []
    for part in str(value or "").replace(";", ",").split(","):
        p = part.strip().upper()
        if p and p in allowed_set and p not in out:
            out.append(p)
    return out


# ── Olvasás (cache-elt) ──────────────────────────────────────────────────────
def _all_rules(force: bool = False) -> list[dict]:
    global _cache_rules, _cache_stamp
    now = time.monotonic()
    with _cache_lock:
        if (not force and _cache_rules is not None
                and (now - _cache_stamp) < _CACHE_TTL_S):
            return _cache_rules
    ensure_schema()
    rows = _execute(
        "SELECT * FROM tl_page_permissions ORDER BY subject_type, subject",
        fetchall=True,
    ) or []
    with _cache_lock:
        _cache_rules = rows
        _cache_stamp = time.monotonic()
    return rows


def invalidate_cache() -> None:
    global _cache_rules, _cache_stamp
    with _cache_lock:
        _cache_rules = None
        _cache_stamp = 0.0


def list_rules() -> list[dict]:
    """Az admin felület listája (a nyers sorok, kiegészített mezőkkel)."""
    out = []
    for r in _all_rules(force=True):
        d = dict(r)
        d["full_access"] = bool(r.get("full_access"))
        d["can_wo_search"] = bool(r.get("can_wo_search"))
        d["can_set_priority"] = bool(r.get("can_set_priority"))
        d["active"] = bool(r.get("active"))
        d["stations"] = _split_csv(r.get("stations"), ALL_STATIONS)
        d["otd_scopes"] = _split_csv(r.get("otd_scopes"), ALL_OTD_SCOPES)
        for key in ("created_at", "updated_at"):
            v = d.get(key)
            d[key] = v.strftime("%Y-%m-%d %H:%M") if hasattr(v, "strftime") else (v or "")
        out.append(d)
    return out


def get_rule(rule_id: int) -> dict | None:
    ensure_schema()
    return _execute(
        "SELECT * FROM tl_page_permissions WHERE id=%s LIMIT 1",
        (int(rule_id),),
        fetchone=True,
    )


def resolve(user: dict | None) -> dict | None:
    """
    A userre érvényes szabály. A user szintű felülírja a job title szintűt.
    None, ha nincs illeszkedő aktív szabály (-> a hívó a régi logikát használja).
    """
    if not user:
        return None
    uname = username_of(user)
    jtitle = job_title_of(user)

    user_rule = None
    jt_rule = None
    for r in _all_rules():
        if not r.get("active"):
            continue
        subj = str(r.get("subject") or "")
        if r.get("subject_type") == "user":
            if uname and subj.lower() == uname:
                user_rule = r
        elif jtitle and subj.upper() == jtitle:
            jt_rule = r

    if not user_rule and not jt_rule:
        return None

    primary = user_rule or jt_rule
    fallback = jt_rule if user_rule else None

    def _pick_csv(field: str, allowed: list[str]) -> list[str]:
        vals = _split_csv(primary.get(field), allowed)
        if not vals and fallback is not None:
            vals = _split_csv(fallback.get(field), allowed)
        return vals

    full_access = bool(primary.get("full_access"))
    stations = ALL_STATIONS[:] if full_access else _pick_csv("stations", ALL_STATIONS)
    otd = ALL_OTD_SCOPES[:] if full_access else _pick_csv("otd_scopes", ALL_OTD_SCOPES)

    return {
        "source": "user" if user_rule else "job_title",
        "subject": primary.get("subject"),
        "full_access": full_access,
        "can_wo_search": bool(primary.get("can_wo_search")),
        "can_set_priority": bool(primary.get("can_set_priority")),
        "stations": stations,
        "otd_scopes": otd,
    }


def _safe_resolve(user: dict | None) -> dict | None:
    """A TL oldal sosem dőlhet el egy DB hiba miatt – ilyenkor None (régi logika)."""
    try:
        return resolve(user)
    except Exception:
        log.exception("tl_settings.resolve hiba – visszaesés a kódba égetett logikára")
        return None


# ── A hívók által használt kényelmi függvények ────────────────────────────────
def can_wo_search(user: dict | None) -> bool | None:
    """True/False ha van szabály, None ha nincs (-> régi allowlist dönt)."""
    rule = _safe_resolve(user)
    return None if rule is None else bool(rule["can_wo_search"])


def can_set_priority(user: dict | None) -> bool:
    """
    Állíthat-e prioritást (WO kereső / Összeszerelési állapotok / OTD tábla).
    Ez KIFEJEZETT engedély: szabály nélkül senki nem állíthat, hogy az
    "Egyéb beállítások" oldalon lehessen kiválasztani, kik kapják meg.
    """
    rule = _safe_resolve(user)
    return bool(rule and rule["can_set_priority"])


def stations_for(user: dict | None) -> list[str] | None:
    """Engedélyezett állomások, vagy None ha nincs erre vonatkozó szabály."""
    rule = _safe_resolve(user)
    if rule is None or not rule["stations"]:
        return None
    return rule["stations"]


def otd_scopes_for(user: dict | None) -> list[str] | None:
    """Engedélyezett OTD scope-ok, vagy None ha nincs erre vonatkozó szabály."""
    rule = _safe_resolve(user)
    if rule is None or not rule["otd_scopes"]:
        return None
    return rule["otd_scopes"]


# ── Írás ─────────────────────────────────────────────────────────────────────
class RuleError(ValueError):
    """Érvénytelen bemenet az admin felületről."""


def _clean_payload(data: dict) -> dict:
    subject_type = str(data.get("subject_type") or "user").strip().lower()
    if subject_type not in SUBJECT_TYPES:
        raise RuleError("Érvénytelen típus (user vagy job_title lehet).")

    subject = norm_subject(subject_type, data.get("subject"))
    if not subject:
        raise RuleError("A felhasználónév / job title megadása kötelező.")
    if len(subject) > 190:
        raise RuleError("A megadott név túl hosszú (max. 190 karakter).")

    stations = _split_csv(data.get("stations"), ALL_STATIONS)
    otd = _split_csv(data.get("otd_scopes"), ALL_OTD_SCOPES)

    return {
        "subject_type": subject_type,
        "subject": subject,
        "display_name": str(data.get("display_name") or "").strip()[:190],
        "full_access": 1 if data.get("full_access") else 0,
        "can_wo_search": 1 if data.get("can_wo_search") else 0,
        "can_set_priority": 1 if data.get("can_set_priority") else 0,
        "stations": ",".join(stations),
        "otd_scopes": ",".join(otd),
        "note": str(data.get("note") or "").strip()[:500],
        "active": 0 if str(data.get("active", "1")).lower() in ("0", "false", "no", "") else 1,
    }


def create_rule(data: dict, actor: str = "") -> int:
    ensure_schema()
    p = _clean_payload(data)
    exists = _execute(
        "SELECT id FROM tl_page_permissions WHERE subject_type=%s AND subject=%s LIMIT 1",
        (p["subject_type"], p["subject"]),
        fetchone=True,
    )
    if exists:
        raise RuleError("Erre a névre már van szabály – szerkeszd a meglévőt.")

    new_id = _execute(
        "INSERT INTO tl_page_permissions "
        "(subject_type, subject, display_name, full_access, can_wo_search, "
        " can_set_priority, stations, otd_scopes, note, active, created_by, updated_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (p["subject_type"], p["subject"], p["display_name"], p["full_access"],
         p["can_wo_search"], p["can_set_priority"], p["stations"], p["otd_scopes"],
         p["note"], p["active"], actor, actor),
        return_lastrowid=True,
    )
    invalidate_cache()
    log.info("tl_settings: új szabály %s/%s (%s)", p["subject_type"], p["subject"], actor)
    return int(new_id or 0)


def update_rule(rule_id: int, data: dict, actor: str = "") -> None:
    ensure_schema()
    if not get_rule(rule_id):
        raise RuleError("A szabály nem található.")
    p = _clean_payload(data)
    clash = _execute(
        "SELECT id FROM tl_page_permissions "
        "WHERE subject_type=%s AND subject=%s AND id<>%s LIMIT 1",
        (p["subject_type"], p["subject"], int(rule_id)),
        fetchone=True,
    )
    if clash:
        raise RuleError("Erre a névre már van másik szabály.")

    _execute(
        "UPDATE tl_page_permissions SET subject_type=%s, subject=%s, display_name=%s, "
        "full_access=%s, can_wo_search=%s, can_set_priority=%s, stations=%s, "
        "otd_scopes=%s, note=%s, active=%s, updated_by=%s WHERE id=%s",
        (p["subject_type"], p["subject"], p["display_name"], p["full_access"],
         p["can_wo_search"], p["can_set_priority"], p["stations"], p["otd_scopes"],
         p["note"], p["active"], actor, int(rule_id)),
    )
    invalidate_cache()
    log.info("tl_settings: szabály módosítva #%s (%s)", rule_id, actor)


def delete_rule(rule_id: int, actor: str = "") -> None:
    ensure_schema()
    _execute("DELETE FROM tl_page_permissions WHERE id=%s", (int(rule_id),))
    invalidate_cache()
    log.info("tl_settings: szabály törölve #%s (%s)", rule_id, actor)
