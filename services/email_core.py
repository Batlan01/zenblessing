# -*- coding: utf-8 -*-
"""
services/email_core.py
=======================
Blueprint neve: "email_module"

Végpontok:
  GET  /<lang>/email/admin                               → admin_page
  POST /api/email/smtp_test                              → SMTP kapcsolat teszt

  GET    /api/email/users                                → felhasználók
  POST   /api/email/users                                → új felhasználó
  PUT    /api/email/users/<id>                           → szerkesztés
  DELETE /api/email/users/<id>                           → törlés

  GET    /api/email/groups                               → csoportok
  POST   /api/email/groups                               → új csoport
  PUT    /api/email/groups/<id>                          → szerkesztés
  DELETE /api/email/groups/<id>                          → törlés
  GET    /api/email/groups/<id>/members                  → tagok
  POST   /api/email/groups/<id>/members                  → tag hozzáadása
  DELETE /api/email/groups/<id>/members/<uid>            → tag eltávolítása

  GET    /api/email/page_configs                         → oldal konfigurációk
  POST   /api/email/page_configs                         → új konfiguráció
  PUT    /api/email/page_configs/<id>                    → szerkesztés
  DELETE /api/email/page_configs/<id>                    → törlés
  GET    /api/email/page_configs/<id>/recipients         → fogadók
  POST   /api/email/page_configs/<id>/recipients         → fogadó hozzáadása
  DELETE /api/email/page_configs/<id>/recipients/<rid>   → fogadó eltávolítása

  POST /api/email/send                                   → manuális küldés
  GET  /api/email/send_log                               → küldési napló

─────────────────────────────────────────────────────────────────────────────
.env beállítások (SMTP – sosem kerül adatbázisba):
─────────────────────────────────────────────────────────────────────────────
  EMAIL_SMTP_HOST=smtp.office365.com
  EMAIL_SMTP_PORT=587
  EMAIL_SMTP_USERNAME=sender@company.com
  EMAIL_SMTP_PASSWORD=your_password_or_app_password
  EMAIL_FROM_NAME=Factory App
  EMAIL_FROM_EMAIL=sender@company.com

  O365 megjegyzés:
    Exchange Admin Center → Users → Mailboxes → [fiók] → Mail Apps →
    SMTP → Enable authenticated SMTP  (ha le van tiltva a tenanton)

─────────────────────────────────────────────────────────────────────────────
DB táblák (egyszer kell létrehozni):
─────────────────────────────────────────────────────────────────────────────
  CREATE TABLE IF NOT EXISTS email_users (
      id         INT AUTO_INCREMENT PRIMARY KEY,
      name       VARCHAR(255) NOT NULL,
      email      VARCHAR(255) NOT NULL,
      username   VARCHAR(100) DEFAULT NULL,   -- rendszer login (pl. "ntrencik") az auto-küldéshez
      active     TINYINT(1)   NOT NULL DEFAULT 1,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      KEY ix_eu_email (email)
  );

  -- Meglévő email_users migrálás:
  --   ALTER TABLE email_users ADD COLUMN username VARCHAR(100) DEFAULT NULL AFTER email;
  --   Az e-mail cím egyediségi tiltás feloldása (ugyanaz az e-mail több
  --   felhasználóhoz is beállítható legyen):
  --     ALTER TABLE email_users DROP INDEX uq_eu_email;
  --     ALTER TABLE email_users ADD INDEX ix_eu_email (email);

  CREATE TABLE IF NOT EXISTS email_groups (
      id          INT AUTO_INCREMENT PRIMARY KEY,
      name        VARCHAR(255) NOT NULL,
      description TEXT,
      created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
      UNIQUE KEY uq_eg_name (name)
  );

  CREATE TABLE IF NOT EXISTS email_group_members (
      group_id INT NOT NULL,
      user_id  INT NOT NULL,
      PRIMARY KEY (group_id, user_id)
  );

  CREATE TABLE IF NOT EXISTS email_page_configs (
      id                     INT AUTO_INCREMENT PRIMARY KEY,
      page_key               VARCHAR(100)  NOT NULL,
      page_label             VARCHAR(255)  NOT NULL,
      subject_template       VARCHAR(500)  NOT NULL DEFAULT '',
      body_template          LONGTEXT,
      is_html                TINYINT(1)    NOT NULL DEFAULT 1,
      variables_hint         TEXT,
      auto_recipient_enabled TINYINT(1)    NOT NULL DEFAULT 0,  -- auto küldés a létrehozónak
      created_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at             DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY uq_epc_page_key (page_key)
  );

  -- Meglévő email_page_configs migrálás:
  --   ALTER TABLE email_page_configs ADD COLUMN auto_recipient_enabled TINYINT(1) NOT NULL DEFAULT 0;

  CREATE TABLE IF NOT EXISTS email_page_recipients (
      id             INT AUTO_INCREMENT PRIMARY KEY,
      config_id      INT                  NOT NULL,
      recipient_type ENUM('user','group') NOT NULL,
      recipient_id   INT                  NOT NULL,
      recipient_role ENUM('to','cc')      NOT NULL DEFAULT 'to',
      UNIQUE KEY uq_epr (config_id, recipient_type, recipient_id, recipient_role)
  );

  -- Meglévő email_page_recipients migrálás:
  --   ALTER TABLE email_page_recipients
  --     ADD COLUMN recipient_role ENUM('to','cc') NOT NULL DEFAULT 'to',
  --     DROP INDEX uq_epr,
  --     ADD UNIQUE KEY uq_epr (config_id, recipient_type, recipient_id, recipient_role);

  CREATE TABLE IF NOT EXISTS email_send_log (
      id           INT AUTO_INCREMENT PRIMARY KEY,
      page_key     VARCHAR(100),
      triggered_by VARCHAR(255),
      subject      VARCHAR(500),
      to_addresses TEXT,
      status       ENUM('ok','error') NOT NULL DEFAULT 'ok',
      error_msg    TEXT,
      sent_at      DATETIME DEFAULT CURRENT_TIMESTAMP
  );
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Blueprint, abort, current_app, jsonify, render_template, request, session

from routes.auth import require_roles, MANAGER_ROLES, IT_ROLES
from services.bt_db import get_db


# =============================================================================
# Blueprint
# =============================================================================
email_module_bp = Blueprint("email_module", __name__)


# =============================================================================
# SMTP KONFIGURÁCIÓ  –  kizárólag .env-ből, soha nem DB-ből
# =============================================================================

def _smtp_cfg() -> dict:
    return {
        "host":       os.environ.get("EMAIL_SMTP_HOST",     "smtp.office365.com"),
        "port":       int(os.environ.get("EMAIL_SMTP_PORT", "587")),
        "username":   os.environ.get("EMAIL_SMTP_USERNAME", ""),
        "password":   os.environ.get("EMAIL_SMTP_PASSWORD", ""),
        "from_name":  os.environ.get("EMAIL_FROM_NAME",     ""),
        "from_email": os.environ.get("EMAIL_FROM_EMAIL",    ""),
    }


def _smtp_cfg_safe() -> dict:
    """Jelszó nélküli verzió – a template-nek átadható."""
    cfg = _smtp_cfg()
    return {
        "host":         cfg["host"],
        "port":         cfg["port"],
        "username":     cfg["username"],
        "from_name":    cfg["from_name"],
        "from_email":   cfg["from_email"],
        "password_set": bool(cfg["password"]),
    }


# =============================================================================
# TEMPLATE RENDERELÉS  –  {{változó}} helyettesítés
# =============================================================================

_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _render(template: str, data: dict) -> str:
    """
    {{változó}} → érték csere.
    Ismeretlen változók érintetlenül maradnak.
    """
    if not template:
        return ""

    def _sub(m: re.Match) -> str:
        val = data.get(m.group(1).strip())
        return str(val) if val is not None else m.group(0)

    return _VAR_RE.sub(_sub, template)


# =============================================================================
# SMTP KÜLDÉS  (blokkoló – mindig háttérszálból hívd)
# =============================================================================

def _do_smtp_send(to_addresses: list[str], subject: str, body: str,
                  is_html: bool = True,
                  cc_addresses: list[str] | None = None) -> None:
    cfg = _smtp_cfg()
    if not cfg["username"] or not cfg["password"]:
        raise RuntimeError("EMAIL_SMTP_USERNAME / EMAIL_SMTP_PASSWORD nincs beállítva .env-ben")
    if not to_addresses:
        raise ValueError("Nincs fogadó cím")

    from_addr = (
        f"{cfg['from_name']} <{cfg['from_email']}>"
        if cfg["from_name"] and cfg["from_email"]
        else cfg["from_email"] or cfg["username"]
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addresses)
    if cc_addresses:
        msg["Cc"] = ", ".join(cc_addresses)
    msg.attach(MIMEText(body, "html" if is_html else "plain", "utf-8"))

    all_recipients = list(to_addresses) + list(cc_addresses or [])

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as srv:
        srv.ehlo()
        srv.starttls()
        srv.ehlo()
        srv.login(cfg["username"], cfg["password"])
        srv.sendmail(cfg["from_email"] or cfg["username"], all_recipients, msg.as_string())


def _send_async(app, to_addresses: list[str], subject: str, body: str,
                is_html: bool, page_key: str, triggered_by: str,
                cc_addresses: list[str] | None = None) -> None:
    """Háttérszálban fut – küld, majd loggolja az eredményt."""
    with app.app_context():
        status, error_msg = "ok", None
        try:
            _do_smtp_send(to_addresses, subject, body, is_html, cc_addresses)
        except Exception as exc:
            status    = "error"
            error_msg = str(exc)
            current_app.logger.exception("email_module: send failed")

        try:
            db  = get_db()
            cur = db.cursor()
            cur.execute(
                "INSERT INTO email_send_log "
                "(page_key, triggered_by, subject, to_addresses, status, error_msg) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (page_key or "", triggered_by or "", subject,
                 json.dumps({
                     "to": to_addresses,
                     "cc": cc_addresses or [],
                 }, ensure_ascii=False),
                 status, error_msg),
            )
            db.commit()
            cur.close()
        except Exception:
            current_app.logger.exception("email_module: log write failed")


# =============================================================================
# FOGADÓK FELOLDÁSA  –  config_id → egyedi aktív e-mail lista
# =============================================================================

def _resolve_recipients(cur, config_id: int) -> tuple[list[str], list[str]]:
    """
    Visszaadja (to_emails, cc_emails) tuple-t.
    A recipient_role mező alapján választja szét TO és CC listát.
    """
    cur.execute(
        "SELECT recipient_type, recipient_id, recipient_role "
        "FROM email_page_recipients WHERE config_id = %s",
        (config_id,),
    )
    rows = cur.fetchall() or []
    to_emails: set[str] = set()
    cc_emails: set[str] = set()

    for row in rows:
        rtype = row.get("recipient_type") if isinstance(row, dict) else row[0]
        rid   = row.get("recipient_id")   if isinstance(row, dict) else row[1]
        role  = (row.get("recipient_role") if isinstance(row, dict) else row[2]) or "to"

        found: set[str] = set()

        if rtype == "user":
            cur.execute(
                "SELECT email FROM email_users WHERE id=%s AND active=1 LIMIT 1", (rid,)
            )
            r = cur.fetchone()
            if r:
                em = r.get("email") if isinstance(r, dict) else r[0]
                if em:
                    found.add(em.strip())

        elif rtype == "group":
            cur.execute(
                "SELECT u.email FROM email_users u "
                "JOIN email_group_members m ON m.user_id=u.id "
                "WHERE m.group_id=%s AND u.active=1",
                (rid,),
            )
            for gr in (cur.fetchall() or []):
                em = gr.get("email") if isinstance(gr, dict) else gr[0]
                if em:
                    found.add(em.strip())

        if role == "cc":
            cc_emails.update(found)
        else:
            to_emails.update(found)

    return sorted(to_emails), sorted(cc_emails)


def _resolve_auto_recipient(cur, username: str) -> str | None:
    """
    Megkeresi az email_users táblában a username mezőben az adott
    rendszer-felhasználó nevet (pl. "ntrencik") és visszaadja az e-mail címét.
    """
    if not username:
        return None
    cur.execute(
        "SELECT email FROM email_users WHERE username=%s AND active=1 LIMIT 1",
        (username.strip(),),
    )
    r = cur.fetchone()
    if r:
        em = r.get("email") if isinstance(r, dict) else r[0]
        return em.strip() if em else None
    return None


# =============================================================================
# PUBLIKUS KÜLDŐ API  –  email_injector.py és más blueprint-ek használják
# =============================================================================

def send_for_page_key(page_key: str, dynamic_data: dict,
                      triggered_by: str = "system") -> dict:
    """
    Küldi az emailt az adott page_key konfigurációja alapján.
    Háttérszálban fut, azonnal visszatér {"ok", "queued", "error"}.

    Ha auto_recipient_enabled=1 a konfigban ÉS a dynamic_data tartalmaz
    "auto_recipient_username" kulcsot, automatikusan hozzáadja az illető
    e-mail címét a TO listához (email_users.username alapján).
    """
    db  = get_db()
    cur = db.cursor(dictionary=True)

    cur.execute(
        "SELECT id, subject_template, body_template, is_html, auto_recipient_enabled "
        "FROM email_page_configs WHERE page_key=%s LIMIT 1",
        (page_key,),
    )
    cfg_row = cur.fetchone()
    if not cfg_row:
        cur.close()
        return {"ok": False, "queued": 0, "error": f"Nincs konfiguráció: {page_key}"}

    to_addresses, cc_addresses = _resolve_recipients(cur, cfg_row["id"])

    # ── Automatikus célzott küldés a létrehozónak ──────────────────────────
    if cfg_row.get("auto_recipient_enabled"):
        auto_uname = str(dynamic_data.get("auto_recipient_username") or "").strip()
        if auto_uname:
            auto_email = _resolve_auto_recipient(cur, auto_uname)
            if auto_email and auto_email not in to_addresses:
                to_addresses = sorted(set(to_addresses) | {auto_email})

    cur.close()

    if not to_addresses:
        return {"ok": False, "queued": 0, "error": "Nincs aktív fogadó ehhez a konfigurációhoz"}

    subject = _render(cfg_row.get("subject_template") or "", dynamic_data)
    body    = _render(cfg_row.get("body_template")    or "", dynamic_data)
    is_html = bool(cfg_row.get("is_html", 1))

    app = current_app._get_current_object()
    threading.Thread(
        target=_send_async,
        args=(app, to_addresses, subject, body, is_html, page_key, triggered_by, cc_addresses or None),
        daemon=True,
    ).start()

    return {"ok": True, "queued": len(to_addresses), "cc": len(cc_addresses), "error": None}


def send_direct(to_addresses: list[str], subject: str, body: str,
                is_html: bool = True, page_key: str = "direct",
                triggered_by: str = "system",
                cc_addresses: list[str] | None = None) -> dict:
    """
    Közvetlen küldés page_key konfiguráció nélkül.
    """
    if not to_addresses:
        return {"ok": False, "queued": 0, "error": "Nincs fogadó"}
    app = current_app._get_current_object()
    threading.Thread(
        target=_send_async,
        args=(app, to_addresses, subject, body, is_html, page_key, triggered_by, cc_addresses),
        daemon=True,
    ).start()
    return {"ok": True, "queued": len(to_addresses), "cc": len(cc_addresses or []), "error": None}


# =============================================================================
# HELPERS
# =============================================================================

def _current_user() -> str:
    try:
        u = session.get("user") or {}
        return u.get("username") or u.get("name") or u.get("display_name") or "unknown"
    except Exception:
        return request.headers.get("X-User") or "unknown"


def _jb() -> dict:
    return request.get_json(silent=True) or {}


def _fmt_dt(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M") if hasattr(v, "strftime") else str(v or "")


# =============================================================================
# OLDAL ROUTE
# =============================================================================

@email_module_bp.get("/<lang>/email/admin")
@require_roles(MANAGER_ROLES, IT_ROLES)
def admin_page(lang):
    if lang not in ("hu", "sk"):
        abort(404)
    return render_template(
        f"{lang}/email.html",
        user=session.get("user"),
        lang=lang,
        smtp_cfg=_smtp_cfg_safe(),
    )


# =============================================================================
# SMTP TESZT
# =============================================================================

@email_module_bp.post("/api/email/smtp_test")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_smtp_test():
    cfg = _smtp_cfg()
    if not cfg["username"] or not cfg["password"]:
        return jsonify({"ok": False, "error": "EMAIL_SMTP_USERNAME vagy EMAIL_SMTP_PASSWORD nincs beállítva .env-ben"}), 400
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(cfg["username"], cfg["password"])
        return jsonify({"ok": True, "msg": f"Sikeres kapcsolat – {cfg['host']}:{cfg['port']}"}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200


# =============================================================================
# FELHASZNÁLÓK CRUD
# =============================================================================

@email_module_bp.get("/api/email/users")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_users_list():
    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, name, email, username, active, created_at FROM email_users ORDER BY name")
    rows = cur.fetchall() or []
    cur.close()
    for r in rows:
        r["created_at"] = _fmt_dt(r.get("created_at"))
    return jsonify({"ok": True, "items": rows}), 200


@email_module_bp.post("/api/email/users")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_users_create():
    data     = _jb()
    name     = (data.get("name")     or "").strip()
    email    = (data.get("email")    or "").strip().lower()
    username = (data.get("username") or "").strip().lower()
    if not name or not email:
        return jsonify({"ok": False, "error": "Név és e-mail kötelező"}), 400
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"ok": False, "error": "Érvénytelen e-mail formátum"}), 400
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("INSERT INTO email_users (name, email, username) VALUES (%s,%s,%s)",
                    (name, email, username or None))
        db.commit()
        new_id = cur.lastrowid
    except Exception as exc:
        db.rollback()
        # Az e-mail cím egyediségi tiltása feloldva: ugyanaz az e-mail cím
        # több felhasználóhoz is beállítható. A DB egyedi indexét is el kell
        # távolítani (lásd a fenti sémánál a DROP INDEX uq_eu_email migrációt).
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True, "id": new_id}), 201


@email_module_bp.put("/api/email/users/<int:uid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_users_update(uid):
    data     = _jb()
    name     = (data.get("name")     or "").strip()
    email    = (data.get("email")    or "").strip().lower()
    username = (data.get("username") or "").strip().lower()
    active   = 1 if data.get("active", True) else 0
    if not name or not email:
        return jsonify({"ok": False, "error": "Név és e-mail kötelező"}), 400
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE email_users SET name=%s, email=%s, username=%s, active=%s WHERE id=%s",
            (name, email, username or None, active, uid),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 200


@email_module_bp.delete("/api/email/users/<int:uid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_users_delete(uid):
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM email_group_members WHERE user_id=%s", (uid,))
        cur.execute("DELETE FROM email_page_recipients WHERE recipient_type='user' AND recipient_id=%s", (uid,))
        cur.execute("DELETE FROM email_users WHERE id=%s", (uid,))
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 200


# =============================================================================
# CSOPORTOK CRUD
# =============================================================================

@email_module_bp.get("/api/email/groups")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_groups_list():
    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT g.id, g.name, g.description, COUNT(m.user_id) AS member_count "
        "FROM email_groups g "
        "LEFT JOIN email_group_members m ON m.group_id=g.id "
        "GROUP BY g.id ORDER BY g.name"
    )
    rows = cur.fetchall() or []
    cur.close()
    return jsonify({"ok": True, "items": rows}), 200


@email_module_bp.post("/api/email/groups")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_groups_create():
    data = _jb()
    name = (data.get("name") or "").strip()
    desc = (data.get("description") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Csoport neve kötelező"}), 400
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("INSERT INTO email_groups (name, description) VALUES (%s,%s)", (name, desc or None))
        db.commit()
        new_id = cur.lastrowid
    except Exception as exc:
        db.rollback()
        msg = "Ez a csoportnév már létezik" if "Duplicate" in str(exc) else str(exc)
        return jsonify({"ok": False, "error": msg}), 409
    finally:
        cur.close()
    return jsonify({"ok": True, "id": new_id}), 201


@email_module_bp.put("/api/email/groups/<int:gid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_groups_update(gid):
    data = _jb()
    name = (data.get("name") or "").strip()
    desc = (data.get("description") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Csoport neve kötelező"}), 400
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE email_groups SET name=%s, description=%s WHERE id=%s", (name, desc or None, gid))
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 200


@email_module_bp.delete("/api/email/groups/<int:gid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_groups_delete(gid):
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM email_group_members WHERE group_id=%s", (gid,))
        cur.execute("DELETE FROM email_page_recipients WHERE recipient_type='group' AND recipient_id=%s", (gid,))
        cur.execute("DELETE FROM email_groups WHERE id=%s", (gid,))
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 200


# ── Csoport tagok ─────────────────────────────────────────────────────────────

@email_module_bp.get("/api/email/groups/<int:gid>/members")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_group_members_list(gid):
    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT u.id, u.name, u.email, u.active "
        "FROM email_users u "
        "JOIN email_group_members m ON m.user_id=u.id "
        "WHERE m.group_id=%s ORDER BY u.name",
        (gid,),
    )
    rows = cur.fetchall() or []
    cur.close()
    return jsonify({"ok": True, "items": rows}), 200


@email_module_bp.post("/api/email/groups/<int:gid>/members")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_group_members_add(gid):
    data    = _jb()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"ok": False, "error": "user_id kötelező"}), 400
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT IGNORE INTO email_group_members (group_id, user_id) VALUES (%s,%s)",
            (gid, user_id),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 201


@email_module_bp.delete("/api/email/groups/<int:gid>/members/<int:uid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_group_members_remove(gid, uid):
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM email_group_members WHERE group_id=%s AND user_id=%s", (gid, uid))
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 200


# =============================================================================
# OLDAL KONFIGURÁCIÓK CRUD
# =============================================================================

@email_module_bp.get("/api/email/page_configs")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_page_configs_list():
    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT id, page_key, page_label, subject_template, body_template, "
        "is_html, variables_hint, auto_recipient_enabled, updated_at "
        "FROM email_page_configs ORDER BY page_label"
    )
    rows = cur.fetchall() or []
    cur.close()
    for r in rows:
        r["updated_at"] = _fmt_dt(r.get("updated_at"))
    return jsonify({"ok": True, "items": rows}), 200


@email_module_bp.post("/api/email/page_configs")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_page_configs_create():
    data     = _jb()
    page_key = (data.get("page_key")   or "").strip()
    label    = (data.get("page_label") or "").strip()
    if not page_key or not label:
        return jsonify({"ok": False, "error": "page_key és page_label kötelező"}), 400
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT INTO email_page_configs "
            "(page_key, page_label, subject_template, body_template, is_html, variables_hint, auto_recipient_enabled) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                page_key, label,
                (data.get("subject_template") or "").strip(),
                (data.get("body_template")    or "").strip() or None,
                1 if data.get("is_html", True) else 0,
                (data.get("variables_hint")   or "").strip() or None,
                1 if data.get("auto_recipient_enabled") else 0,
            ),
        )
        db.commit()
        new_id = cur.lastrowid
    except Exception as exc:
        db.rollback()
        msg = "Ez a page_key már létezik" if "Duplicate" in str(exc) else str(exc)
        return jsonify({"ok": False, "error": msg}), 409
    finally:
        cur.close()
    return jsonify({"ok": True, "id": new_id}), 201


@email_module_bp.put("/api/email/page_configs/<int:cid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_page_configs_update(cid):
    data  = _jb()
    label = (data.get("page_label") or "").strip()
    if not label:
        return jsonify({"ok": False, "error": "page_label kötelező"}), 400
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE email_page_configs SET "
            "page_label=%s, subject_template=%s, body_template=%s, "
            "is_html=%s, variables_hint=%s, auto_recipient_enabled=%s "
            "WHERE id=%s",
            (
                label,
                (data.get("subject_template") or "").strip(),
                (data.get("body_template")    or "").strip() or None,
                1 if data.get("is_html", True) else 0,
                (data.get("variables_hint")   or "").strip() or None,
                1 if data.get("auto_recipient_enabled") else 0,
                cid,
            ),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 200


@email_module_bp.delete("/api/email/page_configs/<int:cid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_page_configs_delete(cid):
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM email_page_recipients WHERE config_id=%s", (cid,))
        cur.execute("DELETE FROM email_page_configs WHERE id=%s", (cid,))
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 200


# ── Fogadók ───────────────────────────────────────────────────────────────────

@email_module_bp.get("/api/email/page_configs/<int:cid>/recipients")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_recipients_list(cid):
    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT r.id, r.recipient_type, r.recipient_id, r.recipient_role, "
        "CASE WHEN r.recipient_type='user' THEN u.name  ELSE g.name  END AS display_name, "
        "CASE WHEN r.recipient_type='user' THEN u.email ELSE NULL     END AS email "
        "FROM email_page_recipients r "
        "LEFT JOIN email_users  u ON r.recipient_type='user'  AND u.id=r.recipient_id "
        "LEFT JOIN email_groups g ON r.recipient_type='group' AND g.id=r.recipient_id "
        "WHERE r.config_id=%s ORDER BY r.recipient_role, r.recipient_type, display_name",
        (cid,),
    )
    rows = cur.fetchall() or []
    cur.close()
    return jsonify({"ok": True, "items": rows}), 200


@email_module_bp.post("/api/email/page_configs/<int:cid>/recipients")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_recipients_add(cid):
    data  = _jb()
    rtype = (data.get("recipient_type") or "").strip()
    rid   = data.get("recipient_id")
    role  = (data.get("recipient_role") or "to").strip()
    if rtype not in ("user", "group") or not rid:
        return jsonify({"ok": False, "error": "recipient_type (user/group) és recipient_id kötelező"}), 400
    if role not in ("to", "cc"):
        role = "to"
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT IGNORE INTO email_page_recipients (config_id, recipient_type, recipient_id, recipient_role) "
            "VALUES (%s,%s,%s,%s)",
            (cid, rtype, rid, role),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 201


@email_module_bp.delete("/api/email/page_configs/<int:cid>/recipients/<int:rid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_recipients_remove(cid, rid):
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "DELETE FROM email_page_recipients WHERE config_id=%s AND id=%s", (cid, rid)
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 200


# =============================================================================
# MANUÁLIS KÜLDÉS  (admin UI-ból)
# =============================================================================

@email_module_bp.post("/api/email/send")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_send():
    data     = _jb()
    page_key = (data.get("page_key") or "").strip()
    dyn_data = data.get("dynamic_data") or {}
    if not page_key:
        return jsonify({"ok": False, "error": "page_key kötelező"}), 400
    result = send_for_page_key(page_key, dyn_data, triggered_by=_current_user())
    return jsonify(result), 200 if result["ok"] else 400


# =============================================================================
# KÜLDÉSI NAPLÓ
# =============================================================================

# =============================================================================
# ÜTEMEZETT RIPORTOK  –  CRUD
# =============================================================================

@email_module_bp.get("/api/email/schedules")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_schedules_list():
    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT id,name,report_type,frequency,hour,minute,day_of_week,day_of_month,"
        "active,last_run_at,last_status,last_error,created_at,"
        "subject_template,body_template "
        "FROM email_report_schedules ORDER BY id"
    )
    rows = cur.fetchall() or []
    cur.close()
    for r in rows:
        r["last_run_at"]  = _fmt_dt(r.get("last_run_at"))
        r["created_at"]   = _fmt_dt(r.get("created_at"))
    return jsonify({"ok": True, "items": rows}), 200


@email_module_bp.post("/api/email/schedules")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_schedules_create():
    data = _jb()
    name = (data.get("name") or "").strip()
    rtype = (data.get("report_type") or "").strip()
    freq  = (data.get("frequency") or "daily").strip()
    if not name or not rtype:
        return jsonify({"ok": False, "error": "name és report_type kötelező"}), 400
    valid_types = {"users_day","users_month","wo_day","wo_month","both_day","both_month"}
    if rtype not in valid_types:
        return jsonify({"ok": False, "error": f"Érvénytelen report_type: {rtype}"}), 400
    if freq not in ("daily","weekly","monthly"):
        freq = "daily"
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT INTO email_report_schedules "
            "(name,report_type,frequency,hour,minute,day_of_week,day_of_month,active,"
            "subject_template,body_template) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (name, rtype, freq,
             int(data.get("hour") or 15), int(data.get("minute") or 30),
             data.get("day_of_week"), data.get("day_of_month"),
             1 if data.get("active", True) else 0,
             (data.get("subject_template") or "").strip() or None,
             (data.get("body_template")    or "").strip() or None))
        db.commit(); new_id = cur.lastrowid
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    _reload()
    return jsonify({"ok": True, "id": new_id}), 201


@email_module_bp.put("/api/email/schedules/<int:sid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_schedules_update(sid):
    data = _jb()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name kötelező"}), 400
    freq = (data.get("frequency") or "daily").strip()
    if freq not in ("daily","weekly","monthly"):
        freq = "daily"
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "UPDATE email_report_schedules SET "
            "name=%s,report_type=%s,frequency=%s,hour=%s,minute=%s,"
            "day_of_week=%s,day_of_month=%s,active=%s,"
            "subject_template=%s,body_template=%s WHERE id=%s",
            (name, (data.get("report_type") or "").strip(), freq,
             int(data.get("hour") or 15), int(data.get("minute") or 30),
             data.get("day_of_week"), data.get("day_of_month"),
             1 if data.get("active", True) else 0,
             (data.get("subject_template") or "").strip() or None,
             (data.get("body_template")    or "").strip() or None,
             sid))
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    _reload()
    return jsonify({"ok": True}), 200


@email_module_bp.delete("/api/email/schedules/<int:sid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_schedules_delete(sid):
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM email_report_schedule_recipients WHERE schedule_id=%s", (sid,))
        cur.execute("DELETE FROM email_report_schedules WHERE id=%s", (sid,))
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    _reload()
    return jsonify({"ok": True}), 200


@email_module_bp.post("/api/email/schedules/<int:sid>/run_now")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_schedules_run_now(sid):
    from services.email_scheduler import run_now
    run_now(current_app._get_current_object(), sid)
    return jsonify({"ok": True, "msg": "Elindítva háttérszálban"}), 200


# ── Fogadók ───────────────────────────────────────────────────────────────────

@email_module_bp.get("/api/email/schedules/<int:sid>/recipients")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_sched_recipients_list(sid):
    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT r.id, r.recipient_type, r.recipient_id, r.recipient_role, "
        "CASE WHEN r.recipient_type='user' THEN u.name  ELSE g.name  END AS display_name, "
        "CASE WHEN r.recipient_type='user' THEN u.email ELSE NULL     END AS email "
        "FROM email_report_schedule_recipients r "
        "LEFT JOIN email_users  u ON r.recipient_type='user'  AND u.id=r.recipient_id "
        "LEFT JOIN email_groups g ON r.recipient_type='group' AND g.id=r.recipient_id "
        "WHERE r.schedule_id=%s ORDER BY r.recipient_role, r.recipient_type, display_name", (sid,))
    rows = cur.fetchall() or []
    cur.close()
    return jsonify({"ok": True, "items": rows}), 200


@email_module_bp.post("/api/email/schedules/<int:sid>/recipients")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_sched_recipients_add(sid):
    data  = _jb()
    rtype = (data.get("recipient_type") or "").strip()
    rid   = data.get("recipient_id")
    role  = (data.get("recipient_role") or "to").strip()
    if rtype not in ("user","group") or not rid:
        return jsonify({"ok": False, "error": "recipient_type és recipient_id kötelező"}), 400
    if role not in ("to","cc"):
        role = "to"
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT IGNORE INTO email_report_schedule_recipients "
            "(schedule_id,recipient_type,recipient_id,recipient_role) VALUES (%s,%s,%s,%s)",
            (sid, rtype, rid, role))
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 201


@email_module_bp.delete("/api/email/schedules/<int:sid>/recipients/<int:rid>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_sched_recipients_remove(sid, rid):
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "DELETE FROM email_report_schedule_recipients WHERE schedule_id=%s AND id=%s", (sid, rid))
        db.commit()
    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()
    return jsonify({"ok": True}), 200


def _reload():
    """Scheduler újratöltése mentés/törlés után (ha fut)."""
    try:
        from services.email_scheduler import reload_schedules
        reload_schedules(current_app._get_current_object())
    except Exception as e:
        import logging
        logging.getLogger("email_core").warning(f"reload_schedules hiba: {e}")


@email_module_bp.get("/api/email/send_log")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_send_log():
    try:    limit  = max(1, min(500, int(request.args.get("limit",  100))))
    except: limit  = 100
    try:    offset = max(0, int(request.args.get("offset", 0)))
    except: offset = 0

    db  = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT COUNT(*) AS cnt FROM email_send_log")
    total = int((cur.fetchone() or {}).get("cnt", 0))
    cur.execute(
        "SELECT id, page_key, triggered_by, subject, to_addresses, "
        "status, error_msg, sent_at "
        "FROM email_send_log ORDER BY sent_at DESC LIMIT %s OFFSET %s",
        (limit, offset),
    )
    rows = cur.fetchall() or []
    cur.close()

    for r in rows:
        r["sent_at"] = _fmt_dt(r.get("sent_at"))
        try:
            r["to_addresses"] = json.loads(r.get("to_addresses") or "[]")
        except Exception:
            r["to_addresses"] = []

    return jsonify({"ok": True, "total": total, "items": rows}), 200