# -*- coding: utf-8 -*-
"""
services/qc_bartender_core.py
==============================
Blueprint neve: "qc_bartender"

Végpontok:
  GET  /<lang>/bartender/gallery           → gallery_page
  GET  /api/bartender/gallery              → api_gallery   (PN lista + keresés)
  POST /api/bartender/gallery_preview      → api_gallery_preview  (sablon lista egy PN-hez)
  POST /api/bartender/qc_confirm           → api_qc_confirm  (ellenőrzés mentése)
  DELETE /api/bartender/qc_confirm         → api_qc_confirm_delete  (ellenőrzés törlése)
  GET  /api/bartender/qc_label_checks      → api_qc_label_checks  (kipipált etikettek listája)
  POST /api/bartender/qc_label_check       → api_qc_label_check   (egy etikett pipálása/levétele)
  POST /api/bartender/qc_lock              → api_qc_lock      (lock megszerzése)
  POST /api/bartender/qc_heartbeat         → api_qc_heartbeat (lock fenntartása)
  POST /api/bartender/qc_unlock            → api_qc_unlock    (lock elengedése)

DB táblázat (egyszer kell létrehozni):
  CREATE TABLE IF NOT EXISTS bartender_qc_confirmations (
      id           INT AUTO_INCREMENT PRIMARY KEY,
      pn           VARCHAR(255) NOT NULL,
      rev          VARCHAR(50)  NOT NULL DEFAULT '',
      ecn          VARCHAR(50)  NOT NULL DEFAULT '',
      confirmed_by VARCHAR(255) DEFAULT NULL,
      confirmed_at DATETIME     DEFAULT NULL,
      revoked_by   VARCHAR(255) DEFAULT NULL,
      revoked_at   DATETIME     DEFAULT NULL,
      UNIQUE KEY uq_pn_rev_ecn (pn, rev, ecn)
  );

  -- Meglévő tábla migrálása (ha már létezik):
  --   ALTER TABLE bartender_qc_confirmations
  --     MODIFY confirmed_by VARCHAR(255) DEFAULT NULL,
  --     MODIFY confirmed_at DATETIME DEFAULT NULL,
  --     ADD COLUMN revoked_by VARCHAR(255) DEFAULT NULL AFTER confirmed_at,
  --     ADD COLUMN revoked_at DATETIME DEFAULT NULL AFTER revoked_by;

  -- QC Lock tábla (ÚJ – egyszer kell lefuttatni):
  CREATE TABLE IF NOT EXISTS bartender_qc_locks (
      id           INT AUTO_INCREMENT PRIMARY KEY,
      pn           VARCHAR(255) NOT NULL,
      rev          VARCHAR(50)  NOT NULL DEFAULT '',
      ecn          VARCHAR(50)  NOT NULL DEFAULT '',
      locked_by    VARCHAR(255) NOT NULL,
      lock_token   VARCHAR(64)  NOT NULL,
      locked_at    DATETIME     NOT NULL,
      heartbeat_at DATETIME     NOT NULL,
      UNIQUE KEY uq_qc_lock (pn, rev, ecn)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

  -- Etikett szintű QC pipálás (a kód első használatkor automatikusan létrehozza):
  CREATE TABLE IF NOT EXISTS bartender_qc_label_checks (
      id         INT AUTO_INCREMENT PRIMARY KEY,
      pn         VARCHAR(255) NOT NULL,
      rev        VARCHAR(50)  NOT NULL DEFAULT '',
      ecn        VARCHAR(50)  NOT NULL DEFAULT '',
      label_hash CHAR(40)     NOT NULL,
      label_key  TEXT,
      checked_by VARCHAR(255) NOT NULL,
      checked_at DATETIME     NOT NULL,
      UNIQUE KEY uq_qc_label (pn, rev, ecn, label_hash)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

from __future__ import annotations

import hashlib
import json
import re
import os
import uuid as _uuid_mod
import datetime as _dt_mod
from urllib.parse import quote as _urlquote

from flask import Blueprint, abort, jsonify, render_template, request, session
from routes.auth import (
    require_roles,
    MANAGER_ROLES, IT_ROLES,
    QC_ROLES, ETIKET_CREATOR_ROLE,
    QUALITY_TEAMLEADER_ROLES,
)
from services.bt_db import get_db


# =============================================================================
# Blueprint
# =============================================================================
qc_bartender_bp = Blueprint("qc_bartender", __name__)

GALLERY_DUMMY_WO  = "PREVIEW"
GALLERY_DUMMY_QTY = 100

_RE_1L        = re.compile(r"\b1L\b.*\.btw$",                              re.I)
_RE_FROM      = re.compile(r"\bFrom\b",                                     re.I)
_RE_TO        = re.compile(r"\bTo\b",                                       re.I)
_RE_ID        = re.compile(r"\bID\b|\bID\s*Label\b",                        re.I)
_RE_3L        = re.compile(r"\bID\s*Label\s*3L\.btw$",                      re.I)
_RE_4L        = re.compile(r"\bID\s*Label\s*4L\.btw$",                      re.I)
_RE_VARIAN_4L = re.compile(r"^TMT061\s*\(DAT-8\)\s*ID\s*Label\s*VARIAN\s*4L\.btw$", re.I)


def _is_con(tpl: str) -> bool:
    return bool(_RE_1L.search(tpl)) and not re.search(r"\b[234]L\b", tpl, re.I)


def _side_of_tpl(tpl: str) -> str:
    t = str(tpl or "")
    if _is_con(t):         return "con"
    if _RE_FROM.search(t): return "from"
    if _RE_TO.search(t):   return "to"
    if _RE_ID.search(t):   return "id"
    return ""


def _parse_bool(v) -> bool:
    """Rugalmas bool értelmezés (str/int/bool)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


# =============================================================================
# ID LABEL SOROK  (bartender.py TEMPLATE_RULES alapján)
# =============================================================================

def _id_label_lines(template_name: str, pn: str, rev: str) -> list[str]:
    """
    3L:        [SVK+WO, PN, ISSUE REV]
    4L:        [SVK+WO, SVK+WO, PN, ISSUE REV]
    VARIAN 4L: [SVK+WO, SVK+WO, PN, ISSUE REV]
    default:   [SVK+WO, PN, ISSUE REV]
    """
    wo_svk    = f"SVK{GALLERY_DUMMY_WO}"
    issue_rev = f"ISSUE {rev}" if rev else ""

    if _RE_VARIAN_4L.search(template_name) or _RE_4L.search(template_name):
        return [ln for ln in [wo_svk, wo_svk, pn, issue_rev] if ln]
    return [ln for ln in [wo_svk, pn, issue_rev] if ln]


# =============================================================================
# PRINTER FELOLDÁS
# =============================================================================

def _resolve_printer(cur, template_name: str, pn_printers: dict | list | None) -> str:
    try:
        cur.execute(
            "SELECT `printer_name` FROM `btw_printers` WHERE `file` = %s LIMIT 1",
            (template_name,),
        )
        row = cur.fetchone()
        if row:
            val = row.get("printer_name") if isinstance(row, dict) else (row[0] if row else None)
            if val:
                return str(val).strip()
    except Exception:
        pass

    if isinstance(pn_printers, dict):
        side = _side_of_tpl(template_name)
        if side and pn_printers.get(side):
            return str(pn_printers[side]).strip()
        return str(pn_printers.get("id") or "").strip()

    if isinstance(pn_printers, list) and pn_printers:
        return str(pn_printers[0]).strip()

    return ""


# =============================================================================
# SUMMARY PARSER
# =============================================================================

def _parse_summary(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _lines_from_ft_item(item: dict) -> list[str]:
    t1 = str(item.get("text1") or "").strip()
    t2 = str(item.get("text2") or "").strip()
    s1 = str(item.get("sel1")  or "").strip()
    s2 = str(item.get("sel2")  or "").strip()
    l1 = t1 or s1
    l2 = t2 or s2
    return [x for x in [l1, l2] if x]


def _extract_cards_and_templates(summary: dict):
    cards:        list[dict]      = []
    tpl_counts:   dict[str, int]  = {}
    line_preview: dict[str, list] = {}
    card_id = 0

    for page in (summary.get("pages") or []):
        if not isinstance(page, dict):
            continue

        # ID labels
        for lab in (page.get("id_labels") or []):
            if not isinstance(lab, dict):
                continue
            tpl = str(lab.get("template") or "").strip()
            if not tpl:
                continue
            tpl_counts[tpl] = tpl_counts.get(tpl, 0) + 1
            if tpl not in line_preview:
                line_preview[tpl] = None  # kitöltés később

        # FROM/TO párok
        for grp in (page.get("groups") or []):
            if not isinstance(grp, dict):
                continue
            for pair in (grp.get("pairs") or []):
                if not isinstance(pair, dict):
                    continue

                # --- #3 javítás: hidden flag-ek tiszteletben tartása ---
                # Ha az egész pair rejtett, kihagyjuk (nem nyomtat, ne is jelenjen meg QC-n)
                pair_hidden = _parse_bool(
                    pair.get("hidden", pair.get("hide", pair.get("is_hidden", False)))
                )
                if pair_hidden:
                    continue

                from_hidden = _parse_bool(
                    pair.get("from_hidden", pair.get("hide_from",
                        pair.get("from_hide", pair.get("fromHidden", False))))
                )
                to_hidden = _parse_bool(
                    pair.get("to_hidden", pair.get("hide_to",
                        pair.get("to_hide", pair.get("toHidden", False))))
                )

                from_items = pair.get("from_items") or []
                to_items   = pair.get("to_items")   or []

                if not from_items:
                    frm = pair.get("from") or {}
                    if isinstance(frm, dict) and frm.get("template"):
                        from_items = [frm]

                if not to_items:
                    to_ = pair.get("to") or {}
                    if isinstance(to_, dict) and to_.get("template"):
                        to_items = [to_]

                if not from_hidden:
                    for item in from_items:
                        if not isinstance(item, dict):
                            continue
                        tpl = str(item.get("template") or "").strip()
                        if not tpl:
                            continue
                        lines = _lines_from_ft_item(item)
                        cards.append({"id": f"ft_{card_id}", "template": tpl,
                                      "kind": "ft", "side": "FROM",
                                      "lines": lines, "per_unit": 1})
                        card_id += 1
                        tpl_counts[tpl] = tpl_counts.get(tpl, 0) + 1
                        if tpl not in line_preview:
                            line_preview[tpl] = lines

                if not to_hidden:
                    for item in to_items:
                        if not isinstance(item, dict):
                            continue
                        tpl = str(item.get("template") or "").strip()
                        if not tpl:
                            continue
                        lines = _lines_from_ft_item(item)
                        cards.append({"id": f"ft_{card_id}", "template": tpl,
                                      "kind": "ft", "side": "TO",
                                      "lines": lines, "per_unit": 1})
                        card_id += 1
                        tpl_counts[tpl] = tpl_counts.get(tpl, 0) + 1
                        if tpl not in line_preview:
                            line_preview[tpl] = lines

    return cards, tpl_counts, line_preview


def _extract_con_labels(summary: dict):
    """
    A connector_labels a DB-ben flat lista formátumban van tárolva:
      [ { "template": "X.btw", "connector_id": "C", "value": "S1.3 (Strana 1)" }, ... ]

    Ha a "value" mező üres, fallback: a pages → groups → connectors[id].value
    értékét használjuk – ugyanúgy ahogy a bartender.py _connector_labels_map_from_summary().
    """
    tpl_counts:   dict[str, int]  = {}
    line_preview: dict[str, list] = {}

    # --- fallback lookup: connector_id → value a pages struktúrából ---
    id2val: dict[str, str] = {}
    for p in (summary.get("pages") or []):
        if not isinstance(p, dict):
            continue
        for g in (p.get("groups") or []):
            if not isinstance(g, dict):
                continue
            for c in (g.get("connectors") or []):
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id") or c.get("code") or "").strip()
                val = str(c.get("value") or c.get("display_name") or "").strip()
                if cid and cid not in id2val:
                    id2val[cid] = val

    # --- flat connector_labels feldolgozása ---
    for cl in (summary.get("connector_labels") or []):
        if not isinstance(cl, dict):
            continue
        tpl = str(cl.get("template") or "").strip()
        if not tpl:
            continue

        cid = str(cl.get("connector_id") or "").strip()
        val = str(cl.get("value") or "").strip()

        # ha a value üres, próbáljuk a pages-ből feloldani
        if not val and cid:
            val = id2val.get(cid, "")

        tpl_counts[tpl] = tpl_counts.get(tpl, 0) + 1

        if tpl not in line_preview:
            line_preview[tpl] = []
        if val:
            line_preview[tpl].append(val)

    # ha nincs egyetlen érték sem, tegyünk be üres placeholder-t
    for tpl in line_preview:
        if not line_preview[tpl]:
            line_preview[tpl] = [""]

    return tpl_counts, line_preview


def _extract_specials(summary: dict, pn: str = "", db=None) -> list[dict]:
    raw = summary.get("special_labels")
    if isinstance(raw, list):
        specials = []
        for item in raw:
            if isinstance(item, dict):
                tpl   = str(item.get("template") or "").strip()
                qty   = max(1, int(item.get("qty") or 1))
                lines = [str(s or "").strip() for s in (item.get("lines") or [])]
            elif isinstance(item, str):
                tpl, qty, lines = item.strip(), 1, []
            else:
                continue
            if tpl:
                specials.append({"template": tpl, "qty": qty, "lines": lines})
        return specials

    if pn and db:
        specials = []
        try:
            cur = db.cursor(dictionary=True)
            cur.execute("SELECT template, text FROM special_labels WHERE refer_pn = %s", (pn,))
            for r in (cur.fetchall() or []):
                tpl = str(r.get("template") or "").strip()
                if not tpl:
                    continue
                txt = r.get("text") or ""
                try:
                    obj = json.loads(txt)
                    lines = [str(s or "").strip() for s in obj] if isinstance(obj, list) else txt.splitlines()
                except Exception:
                    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
                specials.append({"template": tpl, "qty": 1, "lines": lines[:8]})
            cur.close()
        except Exception:
            pass
        return specials

    return []


# =============================================================================
# QC MEGERŐSÍTÉS HELPER
# =============================================================================

def _fmt_dt(v):
    return v.strftime("%Y-%m-%d %H:%M:%S") if getattr(v, "strftime", None) else str(v or "")


# A javítás-visszajelzés új oszlopai (deploy/db_qc_fix.sql) – amíg a migráció
# nem futott le, a kód a régi sémával is működik.
_QC_FIX_COLS_OK: bool | None = None


def _qc_fix_cols_available(cur) -> bool:
    global _QC_FIX_COLS_OK
    if _QC_FIX_COLS_OK is None:
        try:
            cur.execute(
                "SELECT reported_by, report_message, fixed_by, fixed_at "
                "FROM bartender_qc_confirmations LIMIT 1"
            )
            cur.fetchall()
            _QC_FIX_COLS_OK = True
        except Exception:
            _QC_FIX_COLS_OK = False
    return _QC_FIX_COLS_OK


# Külön (opcionális) oszlop a címzett mérnök nevének (deploy/db_qc_assigned.sql).
# Ha nincs meg, a "kinek ment" mező az `etiket data` utolsó szerkesztőjéből jön.
_QC_ASSIGNED_COL_OK: bool | None = None


def _qc_assigned_col_available(cur) -> bool:
    global _QC_ASSIGNED_COL_OK
    if _QC_ASSIGNED_COL_OK is None:
        try:
            cur.execute("SELECT assigned_to FROM bartender_qc_confirmations LIMIT 1")
            cur.fetchall()
            _QC_ASSIGNED_COL_OK = True
        except Exception:
            _QC_ASSIGNED_COL_OK = False
    return _QC_ASSIGNED_COL_OK


def _assigned_expr(has_assigned: bool) -> str:
    """A 'címzett mérnök' kifejezés a pending lekérdezéshez."""
    if has_assigned:
        return "COALESCE(NULLIF(c.assigned_to,''), t.`updated by`, t.`edited by`, '')"
    return "COALESCE(t.`updated by`, t.`edited by`, '')"


# A "Javítások" nézet feltétele: a QC figyelmét igénylő, még jóvá nem hagyott
# projektek. Kétféle eset:
#   (1) a mérnök UGYANAZON az ECN-en javított és visszajelzett   → c.fixed_at kitöltve
#   (2) a hiba egy ÚJABB ECN-ben lett javítva (más projektként): nyitott bejelentés
#       (reported/revoked/email), amihez van AZ AKTUÁLISNÁL FRISSEBB testvér-ECN
#       ugyanarra a PN-re. Ilyenkor a régi sor fixed_at-je sosem töltődik ki, ezért
#       a klasszikus szűrő nem hozná be – ezt fogja meg a második ág.
_FIXED_VIEW_FROM = """
    FROM bartender_qc_confirmations c
    LEFT JOIN `etiket data` t
           ON t.PN = c.pn
          AND COALESCE(t.REV,'') = c.rev
          AND COALESCE(t.ECN,'') = c.ecn
"""

_FIXED_VIEW_WHERE = """
    WHERE (c.confirmed_by IS NULL OR c.confirmed_by = '')
      AND (
            c.fixed_at IS NOT NULL
         OR (
              (
                (c.reported_by IS NOT NULL AND c.reported_by <> '')
                OR (c.revoked_by IS NOT NULL AND c.revoked_by <> '')
                OR COALESCE(c.email_sent, 0) = 1
              )
              AND EXISTS (
                SELECT 1 FROM `etiket data` t2
                WHERE t2.PN = c.pn
                  AND NOT (COALESCE(t2.REV,'') = c.rev AND COALESCE(t2.ECN,'') = c.ecn)
                  AND COALESCE(t2.`updated date`, t2.`create date`)
                      > COALESCE(t.`updated date`, t.`create date`)
              )
            )
      )
"""


def _conf_row_to_dict(row: dict, fix_cols: bool) -> dict:
    d = {
        "confirmed_by": str(row.get("confirmed_by") or ""),
        "confirmed_at": _fmt_dt(row.get("confirmed_at")),
        "revoked_by":   str(row.get("revoked_by")   or ""),
        "revoked_at":   _fmt_dt(row.get("revoked_at")),
        "email_sent":   bool(row.get("email_sent")),
    }
    if fix_cols:
        d.update({
            "reported_by":    str(row.get("reported_by") or ""),
            "report_message": str(row.get("report_message") or ""),
            "fixed_by":       str(row.get("fixed_by") or ""),
            "fixed_at":       _fmt_dt(row.get("fixed_at")),
        })
    else:
        d.update({"reported_by": "", "report_message": "", "fixed_by": "", "fixed_at": ""})
    return d


def _get_confirmation(cur, pn: str, rev: str, ecn: str) -> dict | None:
    fix_cols = _qc_fix_cols_available(cur)
    extra = ", reported_by, report_message, fixed_by, fixed_at" if fix_cols else ""
    try:
        cur.execute(
            "SELECT confirmed_by, confirmed_at, revoked_by, revoked_at, "
            f"       COALESCE(email_sent, 0) AS email_sent{extra} "
            "FROM bartender_qc_confirmations "
            "WHERE pn=%s AND rev=%s AND ecn=%s LIMIT 1",
            (pn, rev or "", ecn or ""),
        )
        row = cur.fetchone()
        if row:
            return _conf_row_to_dict(row, fix_cols)
    except Exception:
        pass
    return None


def _get_confirmations_bulk(cur, keys: list[tuple[str, str, str]]) -> dict:
    """
    Több (pn, rev, ecn) kulcs confirmation-je EGY lekérdezéssel
    (a galéria korábban soronként külön query-t futtatott).
    """
    if not keys:
        return {}
    fix_cols = _qc_fix_cols_available(cur)
    extra = ", reported_by, report_message, fixed_by, fixed_at" if fix_cols else ""
    out: dict = {}
    try:
        ph = ",".join(["(%s,%s,%s)"] * len(keys))
        params: list = []
        for k in keys:
            params.extend(k)
        cur.execute(
            "SELECT pn, rev, ecn, confirmed_by, confirmed_at, revoked_by, revoked_at, "
            f"       COALESCE(email_sent, 0) AS email_sent{extra} "
            "FROM bartender_qc_confirmations "
            f"WHERE (pn, rev, ecn) IN ({ph})",
            tuple(params),
        )
        for row in (cur.fetchall() or []):
            key = (str(row.get("pn") or ""), str(row.get("rev") or ""), str(row.get("ecn") or ""))
            out[key] = _conf_row_to_dict(row, fix_cols)
    except Exception:
        pass
    return out

def _get_current_user() -> str:
    try:
        u = session.get("user") or {}
        return u.get("username") or u.get("name") or u.get("display_name") or ""
    except Exception:
        return request.headers.get("X-User") or "unknown"


# =============================================================================
# OLDAL ROUTE
# =============================================================================

@qc_bartender_bp.route("/<lang>/bartender/gallery")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def gallery_page(lang):
    if lang not in ("hu", "sk"):
        abort(404)
    return render_template(
        f"{lang}/qc_bartender.html",
        user=session.get("user"),
        lang=lang,
        other_lang="sk" if lang == "hu" else "hu",
    )


# =============================================================================
# GALLERY API  –  PN keresés
# =============================================================================

def _like_val(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_where(pn_exact: bool, pn_q: str, rev_q: str, ecn_q: str):
    conds, params = [], []
    if pn_q:
        if pn_exact:
            conds.append("t.PN = %s");                params.append(pn_q)
        else:
            conds.append("t.PN LIKE %s ESCAPE '\\\\'"); params.append(f"%{_like_val(pn_q)}%")
    if rev_q:
        conds.append("t.REV = %s"); params.append(rev_q)
    if ecn_q:
        conds.append("t.ECN = %s"); params.append(ecn_q)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def _row_count(cur, where_sql: str, params: list) -> int:
    cur.execute(f"SELECT COUNT(*) AS cnt FROM `etiket data` t {where_sql}", tuple(params))
    return int((cur.fetchone() or {}).get("cnt", 0))


@qc_bartender_bp.get("/api/bartender/gallery")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def api_gallery():
    pn_q  = (request.args.get("pn")  or "").strip()
    rev_q = (request.args.get("rev") or "").strip()
    ecn_q = (request.args.get("ecn") or "").strip()

    raw_limit = request.args.get("limit", "50")
    try:
        limit = int(raw_limit)
        limit = 99999 if limit == 0 else max(1, min(5000, limit))
    except Exception:
        limit = 50

    try:    offset = max(0, int(request.args.get("offset") or 0))
    except: offset = 0

    db  = get_db()
    cur = db.cursor(dictionary=True)

    # ── "Javítások" nézet: csak a mérnök által javított, de a QC által még
    #    újra nem jóváhagyott projektek ───────────────────────────────────────
    if (request.args.get("only_fixed") or "") == "1":
        if not _qc_fix_cols_available(cur):
            cur.close()
            return jsonify({"total": 0, "match_type": "none",
                            "dummy_wo": GALLERY_DUMMY_WO, "dummy_qty": GALLERY_DUMMY_QTY,
                            "items": [],
                            "error": "Hiányzó DB oszlopok – futtasd a deploy/db_qc_fix.sql-t."}), 200

        pn_filter = ""
        fw_params: list = []
        if pn_q:
            pn_filter = " AND c.pn LIKE %s"
            fw_params.append(f"%{pn_q}%")

        cur.execute(
            f"SELECT COUNT(*) AS cnt {_FIXED_VIEW_FROM} {_FIXED_VIEW_WHERE}{pn_filter}",
            tuple(fw_params))
        f_total = int((cur.fetchone() or {}).get("cnt") or 0)

        cur.execute(f"""
            SELECT c.pn, c.rev, c.ecn,
                   c.confirmed_by, c.confirmed_at, c.revoked_by, c.revoked_at,
                   COALESCE(c.email_sent,0) AS email_sent,
                   c.reported_by, c.report_message, c.fixed_by, c.fixed_at,
                   CASE WHEN c.fixed_at IS NOT NULL THEN 'fixed' ELSE 'superseded' END AS fix_kind,
                   (SELECT CONCAT(COALESCE(t2.REV,''),' / ',COALESCE(t2.ECN,''))
                      FROM `etiket data` t2
                     WHERE t2.PN = c.pn
                       AND NOT (COALESCE(t2.REV,'') = c.rev AND COALESCE(t2.ECN,'') = c.ecn)
                     ORDER BY COALESCE(t2.`updated date`, t2.`create date`) DESC
                     LIMIT 1)                                  AS newer_ver,
                   COALESCE(t.`updated date`, t.`create date`)  AS updated,
                   COALESCE(t.`updated by`, t.`edited by`, '')  AS updated_by
            {_FIXED_VIEW_FROM}
            {_FIXED_VIEW_WHERE}{pn_filter}
            ORDER BY COALESCE(c.fixed_at, c.email_sent_at, c.revoked_at) DESC
            LIMIT %s OFFSET %s
        """, tuple(fw_params + [limit, offset]))

        f_items = []
        for r in (cur.fetchall() or []):
            d = _conf_row_to_dict(r, True)
            f_items.append({
                "pn":         str(r.get("pn") or ""),
                "rev":        str(r.get("rev") or ""),
                "ecn":        str(r.get("ecn") or ""),
                "updated":    _fmt_dt(r.get("updated")),
                "updated_by": str(r.get("updated_by") or ""),
                "fix_kind":   str(r.get("fix_kind") or "fixed"),
                "newer_ver":  str(r.get("newer_ver") or ""),
                **d,
            })
        cur.close()
        return jsonify({"total": f_total, "match_type": "fixed",
                        "dummy_wo": GALLERY_DUMMY_WO, "dummy_qty": GALLERY_DUMMY_QTY,
                        "items": f_items}), 200

    exact_where, exact_params = _build_where(True,  pn_q, rev_q, ecn_q)
    exact_total = _row_count(cur, exact_where, exact_params) if pn_q else 0

    if exact_total > 0:
        match_type, where_sql, params, total = "exact", exact_where, exact_params, exact_total
    else:
        like_where, like_params = _build_where(False, pn_q, rev_q, ecn_q)
        like_total = _row_count(cur, like_where, like_params) if (pn_q or rev_q or ecn_q) else 0
        match_type = "like" if like_total > 0 else "none"
        where_sql, params, total = like_where, like_params, like_total

    row_sql = f"""
        SELECT t.PN,
               COALESCE(t.REV,'')                                  AS REV,
               COALESCE(t.ECN,'')                                  AS ECN,
               COALESCE(t.`updated date`, t.`create date`)         AS updated,
               COALESCE(t.`updated by`, t.`edited by`, '')         AS updated_by
        FROM `etiket data` t
        {where_sql}
        ORDER BY COALESCE(t.`updated date`, t.`create date`) DESC, t.PN ASC
        LIMIT %s OFFSET %s
    """
    cur.execute(row_sql, tuple(params + [limit, offset]))
    rows = cur.fetchall() or []

    # confirmation státuszok EGY lekérdezéssel (korábban soronként futott)
    keys = [((r.get("PN") or "").strip(), (r.get("REV") or "").strip(), (r.get("ECN") or "").strip())
            for r in rows]
    conf_map = _get_confirmations_bulk(cur, keys)

    items = []
    for r in rows:
        upd = r.get("updated")
        pn  = (r.get("PN")  or "").strip()
        rev = (r.get("REV") or "").strip()
        ecn = (r.get("ECN") or "").strip()
        conf = conf_map.get((pn, rev, ecn))
        items.append({
            "pn":           pn,
            "rev":          rev,
            "ecn":          ecn,
            "updated":      upd.strftime("%Y-%m-%d %H:%M:%S") if getattr(upd, "strftime", None) else str(upd or ""),
            "updated_by":   (r.get("updated_by") or "").strip(),
            "confirmed_by": conf["confirmed_by"] if conf else "",
            "confirmed_at": conf["confirmed_at"] if conf else "",
            "revoked_by":   conf["revoked_by"]   if conf else "",
            "revoked_at":   conf["revoked_at"]   if conf else "",
            "email_sent":   conf["email_sent"]   if conf else False,
            "reported_by":  conf["reported_by"]  if conf else "",
            "fixed_by":     conf["fixed_by"]     if conf else "",
            "fixed_at":     conf["fixed_at"]     if conf else "",
        })

    cur.close()
    return jsonify({
        "total": total, "match_type": match_type,
        "dummy_wo": GALLERY_DUMMY_WO, "dummy_qty": GALLERY_DUMMY_QTY,
        "items": items,
    }), 200


# =============================================================================
# GALLERY PREVIEW
# =============================================================================

@qc_bartender_bp.post("/api/bartender/gallery_preview")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def api_gallery_preview():
    data = request.get_json(silent=True) or {}
    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()

    if not pn:
        return jsonify({"error": "pn kötelező"}), 400

    db  = get_db()
    cur = db.cursor(dictionary=True)

    row = None
    for sql, p in [
        ("SELECT * FROM `etiket data` WHERE PN=%s AND REV=%s AND ECN=%s ORDER BY ID DESC LIMIT 1", (pn,rev,ecn)) if (rev and ecn) else None,
        ("SELECT * FROM `etiket data` WHERE PN=%s AND REV=%s ORDER BY ID DESC LIMIT 1", (pn,rev)) if rev else None,
        ("SELECT * FROM `etiket data` WHERE PN=%s ORDER BY ID DESC LIMIT 1", (pn,)),
    ]:
        if sql is None:
            continue
        cur.execute(sql, p)
        row = cur.fetchone()
        if row:
            break

    if not row:
        cur.close()
        return jsonify({"error": f"PN nem található: {pn}"}), 404

    db_rev = (row.get("REV") or "").strip()
    db_ecn = (row.get("ECN") or "").strip()

    pn_printers = None
    try:
        raw = row.get("printers")
        if raw:
            pn_printers = json.loads(raw)
    except Exception:
        pass

    summary = _parse_summary(row.get("summary"))

    cards, tpl_counts, line_preview = _extract_cards_and_templates(summary)

    for tpl in list(line_preview.keys()):
        if line_preview[tpl] is None:
            line_preview[tpl] = _id_label_lines(tpl, pn, db_rev)

    con_counts, con_lines = _extract_con_labels(summary)
    for tpl, cnt in con_counts.items():
        tpl_counts[tpl] = tpl_counts.get(tpl, 0) + cnt
    for tpl, vals in con_lines.items():
        # #4 javítás: CON értékek mindig felülírják/bekerülnek, még ha a sablon
        # FROM/TO-ból is ismert – a CON saját konnektor-értékeket hordoz
        if _is_con(tpl):
            line_preview[tpl] = vals
        elif tpl not in line_preview:
            line_preview[tpl] = vals

    specials = _extract_specials(summary, pn=pn, db=db)
    for sp in specials:
        tpl = sp.get("template", "")
        if tpl and tpl not in line_preview:
            line_preview[tpl] = sp.get("lines") or []

    all_tpls = set(tpl_counts) | {c["template"] for c in cards} | {s["template"] for s in specials}
    printers: dict[str, str] = {}
    for tpl in all_tpls:
        if tpl:
            printers[tpl] = _resolve_printer(cur, tpl, pn_printers)

    conf = _get_confirmation(cur, pn, db_rev, db_ecn)
    cur.close()

    for card in cards:
        card["printer"] = printers.get(card.get("template", ""), "")

    return jsonify({
        "pn": pn, "wo": GALLERY_DUMMY_WO, "rev": db_rev, "ecn": db_ecn,
        "qty_sum": 1,
        "templates":          dict(tpl_counts),
        "calculated_totals":  {t: 1 for t in tpl_counts},
        "printers":           printers,
        "line_preview":       line_preview,
        "cards":              cards,
        "special_labels":     specials,
        # con_label_templates: az összes sablon neve, ami connector_labels-ből jött,
        # névtől függetlenül (pl. 2L sablon is lehet konnektor etikett).
        # A frontend ezt használja a helyes CON badge megjelenítéséhez.
        "con_label_templates": list(con_counts.keys()),
        "templates_effective":         dict(tpl_counts),
        "calculated_totals_effective": {t: 1 for t in tpl_counts},
        "skipped": [], "has_templates_all": bool(tpl_counts) or bool(specials),
        "has_templates_effective": bool(tpl_counts) or bool(specials),
        "matched": {"pn": True, "rev": True, "ecn": True},
        "is_special": {s["template"]: True for s in specials if s.get("template")},
        "history_cards": [],
        "confirmed_by": conf["confirmed_by"] if conf else "",
        "confirmed_at": conf["confirmed_at"] if conf else "",
        "revoked_by":   conf["revoked_by"]   if conf else "",
        "revoked_at":   conf["revoked_at"]   if conf else "",
    }), 200


# =============================================================================
# QC CONFIRM API
# =============================================================================

@qc_bartender_bp.post("/api/bartender/qc_confirm")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def api_qc_confirm():
    """
    POST {pn, rev, ecn}
    Elmenti az ellenőrzést a bejelentkezett felhasználó nevével.
    """
    data = request.get_json(silent=True) or {}
    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()

    if not pn:
        return jsonify({"ok": False, "error": "pn kötelező"}), 400

    user = _get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Nem azonosított felhasználó"}), 401

    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO bartender_qc_confirmations
                (pn, rev, ecn, confirmed_by, confirmed_at, revoked_by, revoked_at)
            VALUES (%s, %s, %s, %s, NOW(), NULL, NULL)
            ON DUPLICATE KEY UPDATE
              confirmed_by = VALUES(confirmed_by),
              confirmed_at = NOW(),
              revoked_by   = NULL,
              revoked_at   = NULL
        """, (pn, rev or "", ecn or "", user))
        db.commit()
    except Exception:
        db.rollback()
        return jsonify({"ok": False, "error": "DB hiba"}), 500
    finally:
        try: cur.close()
        except Exception: pass

    return jsonify({"ok": True, "confirmed_by": user}), 200


@qc_bartender_bp.delete("/api/bartender/qc_confirm")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def api_qc_confirm_delete():
    """
    DELETE {pn, rev, ecn}
    Visszavonja az ellenőrzést: a sort megtartja, de confirmed_by-t törli,
    revoked_by-ba beírja az aktuális felhasználót.
    Ha nincs sor, létrehozza (edge case).
    """
    data = request.get_json(silent=True) or {}
    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()

    if not pn:
        return jsonify({"ok": False, "error": "pn kötelező"}), 400

    user = _get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Nem azonosított felhasználó"}), 401

    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO bartender_qc_confirmations
                (pn, rev, ecn, confirmed_by, confirmed_at, revoked_by, revoked_at)
            VALUES (%s, %s, %s, NULL, NULL, %s, NOW())
            ON DUPLICATE KEY UPDATE
              confirmed_by = NULL,
              confirmed_at = NULL,
              revoked_by   = VALUES(revoked_by),
              revoked_at   = NOW()
        """, (pn, rev or "", ecn or "", user))
        db.commit()
    except Exception:
        db.rollback()
        return jsonify({"ok": False, "error": "DB hiba"}), 500
    finally:
        try: cur.close()
        except Exception: pass

    return jsonify({"ok": True, "revoked_by": user}), 200


# =============================================================================
# ETIKETT SZINTŰ QC PIPÁLÁS  –  egy-egy etikett kártya kipipálása
# =============================================================================
_QC_LABEL_TABLE_OK: bool | None = None


def _ensure_label_check_table(db, cur) -> bool:
    """A bartender_qc_label_checks tábla létrehozása első használatkor."""
    global _QC_LABEL_TABLE_OK
    if _QC_LABEL_TABLE_OK is None:
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bartender_qc_label_checks (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    pn         VARCHAR(255) NOT NULL,
                    rev        VARCHAR(50)  NOT NULL DEFAULT '',
                    ecn        VARCHAR(50)  NOT NULL DEFAULT '',
                    label_hash CHAR(40)     NOT NULL,
                    label_key  TEXT,
                    checked_by VARCHAR(255) NOT NULL,
                    checked_at DATETIME     NOT NULL,
                    UNIQUE KEY uq_qc_label (pn, rev, ecn, label_hash)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            db.commit()
            _QC_LABEL_TABLE_OK = True
        except Exception:
            try: db.rollback()
            except Exception: pass
            _QC_LABEL_TABLE_OK = False
    return _QC_LABEL_TABLE_OK


def _label_hash(label_key: str) -> str:
    # A label_key hossza kötetlen (sablonnév + sorok) → a UNIQUE kulcshoz
    # fix hosszú SHA1 hash kerül, az eredeti kulcs TEXT-ben marad olvashatóan.
    return hashlib.sha1(label_key.encode("utf-8")).hexdigest()


@qc_bartender_bp.get("/api/bartender/qc_label_checks")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def api_qc_label_checks():
    """
    GET ?pn=&rev=&ecn=
    Az adott projekt (PN/REV/ECN) kipipált etikettjei:
      {ok, items: {label_key: {checked_by, checked_at}}}
    """
    pn  = (request.args.get("pn")  or "").strip()
    rev = (request.args.get("rev") or "").strip()
    ecn = (request.args.get("ecn") or "").strip()
    if not pn:
        return jsonify({"ok": False, "items": {}, "error": "pn kötelező"}), 400

    db  = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if not _ensure_label_check_table(db, cur):
            return jsonify({"ok": True, "items": {}}), 200
        cur.execute(
            "SELECT label_key, checked_by, checked_at "
            "FROM bartender_qc_label_checks "
            "WHERE pn=%s AND rev=%s AND ecn=%s",
            (pn, rev, ecn),
        )
        items = {}
        for r in (cur.fetchall() or []):
            key = str(r.get("label_key") or "")
            if not key:
                continue
            items[key] = {
                "checked_by": str(r.get("checked_by") or ""),
                "checked_at": _fmt_dt(r.get("checked_at")),
            }
        return jsonify({"ok": True, "items": items}), 200
    except Exception as e:
        return jsonify({"ok": False, "items": {}, "error": str(e)}), 200
    finally:
        try: cur.close()
        except Exception: pass


@qc_bartender_bp.post("/api/bartender/qc_label_check")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def api_qc_label_check():
    """
    POST {pn, rev, ecn, label_key, checked}
    Egy etikett kipipálása (checked=true) vagy a pipa levétele (checked=false).
    A pipa a DB-be kerül, így oldalfrissítés után és más gépen is látszik.
    """
    data      = request.get_json(silent=True) or {}
    pn        = (data.get("pn")        or "").strip()
    rev       = (data.get("rev")       or "").strip()
    ecn       = (data.get("ecn")       or "").strip()
    label_key = (data.get("label_key") or "").strip()
    checked   = _parse_bool(data.get("checked"))

    if not pn or not label_key:
        return jsonify({"ok": False, "error": "pn és label_key kötelező"}), 400

    user = _get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Nem azonosított felhasználó"}), 401

    db  = get_db()
    cur = db.cursor()
    try:
        if not _ensure_label_check_table(db, cur):
            return jsonify({"ok": False,
                            "error": "Hiányzó DB tábla (bartender_qc_label_checks)"}), 500
        lh = _label_hash(label_key)
        if checked:
            cur.execute(
                "INSERT INTO bartender_qc_label_checks "
                "(pn, rev, ecn, label_hash, label_key, checked_by, checked_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,NOW()) "
                "ON DUPLICATE KEY UPDATE "
                "  checked_by = VALUES(checked_by), checked_at = NOW()",
                (pn, rev, ecn, lh, label_key, user),
            )
        else:
            cur.execute(
                "DELETE FROM bartender_qc_label_checks "
                "WHERE pn=%s AND rev=%s AND ecn=%s AND label_hash=%s",
                (pn, rev, ecn, lh),
            )
        db.commit()
    except Exception:
        db.rollback()
        return jsonify({"ok": False, "error": "DB hiba"}), 500
    finally:
        try: cur.close()
        except Exception: pass

    return jsonify({
        "ok":         True,
        "checked":    checked,
        "checked_by": user if checked else "",
        "checked_at": (_dt_mod.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                       if checked else ""),
    }), 200


# =============================================================================
# PN VERZIÓLISTA  –  egy PN összes REV/ECN verziója státusszal
# =============================================================================
@qc_bartender_bp.get("/api/bartender/pn_versions")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def api_pn_versions():
    """
    GET ?pn=
    Az adott PN ÖSSZES REV/ECN verziója a hozzá tartozó QC-státusszal.
    A QC oldal verziólistájához: így egy beragadt régi ECN-bejelentés mellett
    rögtön látszik, hogy van-e újabb (már rendben lévő) ECN ugyanarra a PN-re.
    """
    pn = (request.args.get("pn") or "").strip()
    if not pn:
        return jsonify({"ok": False, "items": [], "error": "pn kötelező"}), 400

    db  = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT t.PN,
                   COALESCE(t.REV,'')                          AS REV,
                   COALESCE(t.ECN,'')                          AS ECN,
                   COALESCE(t.`updated date`, t.`create date`) AS updated,
                   COALESCE(t.`updated by`, t.`edited by`, '') AS updated_by
            FROM `etiket data` t
            WHERE t.PN = %s
            ORDER BY COALESCE(t.`updated date`, t.`create date`) DESC,
                     t.REV DESC, t.ECN DESC
        """, (pn,))
        rows = cur.fetchall() or []

        keys = [(pn, (r.get("REV") or "").strip(), (r.get("ECN") or "").strip())
                for r in rows]
        conf_map = _get_confirmations_bulk(cur, keys)

        items = []
        for r in rows:
            rev  = (r.get("REV") or "").strip()
            ecn  = (r.get("ECN") or "").strip()
            conf = conf_map.get((pn, rev, ecn)) or {}

            # státusz levezetése (a megjelenítés sorrendjében)
            if conf.get("confirmed_by"):
                status = "confirmed"
            elif conf.get("fixed_at"):
                status = "fixed"
            elif conf.get("reported_by") or conf.get("revoked_by") or conf.get("email_sent"):
                status = "reported"
            else:
                status = "none"

            items.append({
                "pn":             pn,
                "rev":            rev,
                "ecn":            ecn,
                "updated":        _fmt_dt(r.get("updated")),
                "updated_by":     (r.get("updated_by") or "").strip(),
                "status":         status,
                "confirmed_by":   conf.get("confirmed_by") or "",
                "confirmed_at":   conf.get("confirmed_at") or "",
                "revoked_by":     conf.get("revoked_by") or "",
                "reported_by":    conf.get("reported_by") or "",
                "report_message": conf.get("report_message") or "",
                "fixed_by":       conf.get("fixed_by") or "",
                "fixed_at":       conf.get("fixed_at") or "",
            })
        return jsonify({"ok": True, "pn": pn, "items": items, "count": len(items)}), 200
    except Exception as e:
        return jsonify({"ok": False, "items": [], "error": str(e)}), 200
    finally:
        try: cur.close()
        except Exception: pass


# =============================================================================
# ELAVULT VERZIÓ LEZÁRÁSA  –  "újabb ECN-ben javítva"
# =============================================================================
@qc_bartender_bp.post("/api/bartender/qc_supersede")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def api_qc_supersede():
    """
    POST {pn, rev, ecn}
    A QC lezár egy RÉGI/elavult verziót, aminek a hibája időközben egy ÚJABB
    ECN-ben lett javítva. A sor egyszerre confirmed ÉS fixed státuszba kerül,
    így kiesik a 'nyitott javítások' listából, és az edit oldal
    (qc_fix_status) sem mutatja többé rossznak.
    """
    data = request.get_json(silent=True) or {}
    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()
    if not pn:
        return jsonify({"ok": False, "error": "pn kötelező"}), 400

    user = _get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Nem azonosított felhasználó"}), 401

    db  = get_db()
    cur = db.cursor(dictionary=True)
    has_fix = _qc_fix_cols_available(cur)
    try:
        if has_fix:
            # fixed_at → kiesik a pending listából + az edit oldal qc_fix_status-ából;
            # confirmed_by → nem jelenik meg a "Javítások" (only_fixed) nézetben sem.
            cur.execute("""
                INSERT INTO bartender_qc_confirmations
                    (pn, rev, ecn, confirmed_by, confirmed_at,
                     revoked_by, revoked_at, fixed_by, fixed_at)
                VALUES (%s, %s, %s, %s, NOW(), NULL, NULL, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    confirmed_by = VALUES(confirmed_by),
                    confirmed_at = NOW(),
                    revoked_by   = NULL,
                    revoked_at   = NULL,
                    fixed_by     = VALUES(fixed_by),
                    fixed_at     = NOW()
            """, (pn, rev or "", ecn or "", user, user))
        else:
            cur.execute("""
                INSERT INTO bartender_qc_confirmations
                    (pn, rev, ecn, confirmed_by, confirmed_at, revoked_by, revoked_at)
                VALUES (%s, %s, %s, %s, NOW(), NULL, NULL)
                ON DUPLICATE KEY UPDATE
                    confirmed_by = VALUES(confirmed_by),
                    confirmed_at = NOW(),
                    revoked_by   = NULL,
                    revoked_at   = NULL
            """, (pn, rev or "", ecn or "", user))
        db.commit()
    except Exception:
        db.rollback()
        return jsonify({"ok": False, "error": "DB hiba"}), 500
    finally:
        try: cur.close()
        except Exception: pass

    return jsonify({"ok": True, "confirmed_by": user}), 200


# =============================================================================
# BUG REPORT API
# =============================================================================
@qc_bartender_bp.post("/api/bartender/bug_report")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES, )
def api_bug_report():
    """
    POST {pn, rev, ecn, message}
    Elmenti a hibabejelentést, majd (opcionálisan) email értesítést küld.
    """
    data       = request.get_json(silent=True) or {}
    pn         = (data.get("pn")         or "").strip()
    rev        = (data.get("rev")        or "").strip()
    ecn        = (data.get("ecn")        or "").strip()
    message    = (data.get("message")    or "").strip()
    updated_by = (data.get("updated_by") or "").strip()

    if not pn:
        return jsonify({"ok": False, "error": "pn kötelező"}), 400
    if not message:
        return jsonify({"ok": False, "error": "Üzenet kötelező"}), 400

    user = _get_current_user()

    # ── Projekt megnyitó linkek (hu + sk) ────────────────────────────────────
    _base   = os.environ.get("APP_BASE_URL", "http://10.10.2.14:5000").rstrip("/")
    _q = lambda s: _urlquote(str(s or ""), safe="")
    _params = f"?autopen=1&mode=edit&pn={_q(pn)}&rev={_q(rev)}&ecn={_q(ecn)}"
    link_hu = f"{_base}/hu/bartender/edit_v1{_params}"
    link_sk = f"{_base}/sk/bartender/edit_v1{_params}"

    # ── Email küldés (ha az email modul be van kapcsolva) ────────────────────
    email_ok = False
    try:
        from routes.email_injector import send_page_email
        send_page_email(
            page_key="qc_bartender_bug_report",
            dynamic_data={
                "pn":                      pn,
                "rev":                     rev or "—",
                "ecn":                     ecn or "—",
                "message":                 message,
                "reported_by":             user,
                "link_hu":                 link_hu,
                "link_sk":                 link_sk,
                "auto_recipient_username": updated_by,
            },
            triggered_by=user,
        )
        email_ok = True
    except ImportError:
        pass  # email modul nincs regisztrálva – nem hiba
    except Exception:
        pass  # küldési hiba nem akadályozza a választ

    # ── Ha az email kiment → email_sent flag frissítése ─────────────────────
    if email_ok:
        try:
            _db  = get_db()
            _cur = _db.cursor()
            _cur.execute("""
                INSERT INTO bartender_qc_confirmations
                    (pn, rev, ecn, email_sent, email_sent_at)
                VALUES (%s, %s, %s, 1, NOW())
                ON DUPLICATE KEY UPDATE
                    email_sent    = 1,
                    email_sent_at = NOW()
            """, (pn, rev or "", ecn or ""))
            _db.commit()
            _cur.close()
        except Exception:
            pass  # flag hiba nem akadályozza a választ

    # ── Bejelentő + üzenet tárolása a javítás-visszajelzéshez; új bejelentés
    #    nullázza az esetleges korábbi javítás-állapotot ────────────────────
    try:
        _db  = get_db()
        _cur = _db.cursor(dictionary=True)
        if _qc_fix_cols_available(_cur):
            if _qc_assigned_col_available(_cur):
                # a címzett mérnök nevét is eltároljuk (kihez ment a bejelentés)
                _cur.execute("""
                    INSERT INTO bartender_qc_confirmations
                        (pn, rev, ecn, reported_by, report_message, assigned_to, fixed_by, fixed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL)
                    ON DUPLICATE KEY UPDATE
                        reported_by    = VALUES(reported_by),
                        report_message = VALUES(report_message),
                        assigned_to    = VALUES(assigned_to),
                        fixed_by       = NULL,
                        fixed_at       = NULL,
                        confirmed_by   = NULL,
                        confirmed_at   = NULL
                """, (pn, rev or "", ecn or "", user, message[:2000], updated_by))
            else:
                _cur.execute("""
                    INSERT INTO bartender_qc_confirmations
                        (pn, rev, ecn, reported_by, report_message, fixed_by, fixed_at)
                    VALUES (%s, %s, %s, %s, %s, NULL, NULL)
                    ON DUPLICATE KEY UPDATE
                        reported_by    = VALUES(reported_by),
                        report_message = VALUES(report_message),
                        fixed_by       = NULL,
                        fixed_at       = NULL,
                        confirmed_by   = NULL,
                        confirmed_at   = NULL
                """, (pn, rev or "", ecn or "", user, message[:2000]))
            _db.commit()
        _cur.close()
    except Exception:
        pass

    return jsonify({"ok": True}), 200


# =============================================================================
# JAVÍTÁS-VISSZAJELZÉS (mérnök → QC)
# =============================================================================
@qc_bartender_bp.get("/api/bartender/qc_fix_status")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE, QC_ROLES, QUALITY_TEAMLEADER_ROLES)
def api_qc_fix_status():
    """
    GET ?pn=&rev=&ecn=
    Van-e ehhez a projekthez nyitott QC hibabejelentés, amire a mentés után
    javítás-visszajelzést érdemes küldeni.
    """
    pn  = (request.args.get("pn")  or "").strip()
    rev = (request.args.get("rev") or "").strip()
    ecn = (request.args.get("ecn") or "").strip()
    if not pn:
        return jsonify({"ok": False, "pending": False, "error": "pn kötelező"}), 400

    db  = get_db()
    cur = db.cursor(dictionary=True)
    try:
        conf = _get_confirmation(cur, pn, rev, ecn)
    finally:
        try: cur.close()
        except Exception: pass

    # Pending = van QC bejelentés (reported_by / régi revoked / kiment email)
    # ÉS még nincs javítás-visszajelzés (fixed_at). A korábbi jóváhagyás
    # (confirmed_by) nem számít – az új bejelentés úgyis nullázza azt.
    pending = bool(
        conf
        and (conf.get("reported_by") or conf.get("revoked_by") or conf.get("email_sent"))
        and not conf.get("fixed_at")
    )
    return jsonify({
        "ok": True,
        "pending": pending,
        "reported_by": (conf or {}).get("reported_by") or (conf or {}).get("revoked_by") or "",
        "message":     (conf or {}).get("report_message") or "",
    }), 200


@qc_bartender_bp.post("/api/bartender/notify_qc_fixed")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_notify_qc_fixed():
    """
    POST {pn, rev, ecn, note?}
    A mérnök jelzi, hogy a bejelentett hibát javította:
      - fixed_by / fixed_at mentése,
      - email a bejelentő QC-nek ('qc_bartender_fix_report' page_key,
        az Email admin oldalon konfigurálható hu+sk szöveggel).
    """
    data = request.get_json(silent=True) or {}
    pn   = (data.get("pn")   or "").strip()
    rev  = (data.get("rev")  or "").strip()
    ecn  = (data.get("ecn")  or "").strip()
    note = (data.get("note") or "").strip()
    if not pn:
        return jsonify({"ok": False, "error": "pn kötelező"}), 400

    user = _get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "Nem azonosított felhasználó"}), 401

    db  = get_db()
    cur = db.cursor(dictionary=True)
    if not _qc_fix_cols_available(cur):
        cur.close()
        return jsonify({"ok": False,
                        "error": "Hiányzó DB oszlopok – futtasd a deploy/db_qc_fix.sql-t."}), 500

    conf = _get_confirmation(cur, pn, rev, ecn)
    reported_by = (conf or {}).get("reported_by") or (conf or {}).get("revoked_by") or ""

    try:
        # A javítás után a projekt "javítva, QC újra-jóváhagyásra vár" állapotba
        # kerül: confirmed_by-t nullázzuk, hogy a QC 'Javítások' nézetében lássa
        # (és a régi, beragadt jóváhagyás se zavarjon be).
        cur.execute("""
            INSERT INTO bartender_qc_confirmations
                (pn, rev, ecn, fixed_by, fixed_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                fixed_by     = VALUES(fixed_by),
                fixed_at     = NOW(),
                confirmed_by = NULL,
                confirmed_at = NULL
        """, (pn, rev or "", ecn or "", user))
        db.commit()
    except Exception:
        db.rollback()
        cur.close()
        return jsonify({"ok": False, "error": "DB hiba"}), 500
    finally:
        try: cur.close()
        except Exception: pass

    # ── Email a bejelentőnek (hu+sk szöveg az Email admin sablonban) ─────────
    _base   = os.environ.get("APP_BASE_URL", "http://10.10.2.14:5000").rstrip("/")
    _q = lambda s: _urlquote(str(s or ""), safe="")
    _params = f"?pn={_q(pn)}&rev={_q(rev)}&ecn={_q(ecn)}"
    link_hu = f"{_base}/hu/bartender/gallery{_params}"
    link_sk = f"{_base}/sk/bartender/gallery{_params}"

    email_ok = False
    try:
        from routes.email_injector import send_page_email
        send_page_email(
            page_key="qc_bartender_fix_report",
            dynamic_data={
                "pn":                      pn,
                "rev":                     rev or "—",
                "ecn":                     ecn or "—",
                "fixed_by":                user,
                "note":                    note or "—",
                "link_hu":                 link_hu,
                "link_sk":                 link_sk,
                "auto_recipient_username": reported_by,
            },
            triggered_by=user,
        )
        email_ok = True
    except ImportError:
        pass
    except Exception:
        pass

    return jsonify({"ok": True, "email_ok": email_ok, "fixed_by": user}), 200


@qc_bartender_bp.get("/api/bartender/qc_fixed_count")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES)
def api_qc_fixed_count():
    """Hány javított, de a QC által még újra nem jóváhagyott projekt van (badge)."""
    db  = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if not _qc_fix_cols_available(cur):
            return jsonify({"ok": True, "count": 0})
        cur.execute(f"SELECT COUNT(*) AS cnt {_FIXED_VIEW_FROM} {_FIXED_VIEW_WHERE}")
        row = cur.fetchone()
        return jsonify({"ok": True, "count": int((row or {}).get("cnt") or 0)})
    except Exception as e:
        return jsonify({"ok": False, "count": 0, "error": str(e)})
    finally:
        try: cur.close()
        except Exception: pass


# =============================================================================
# NYITOTT JAVÍTÁSOK (mérnök oldal) – QC által bejelentett, még nem javított
# =============================================================================
def _pending_where_and_params(cur, assigned_to: str):
    """
    A 'nyitott javítás' feltétel: van QC bejelentés (reported_by) és
    még nincs javítás-visszajelzés (fixed_at NULL). A javítás+visszajelzés
    (fixed_at) viszi ki az elemet a listából; egy ÚJ bejelentés (bug_report)
    nullázza a fixed_at-et és a korábbi jóváhagyást, így újra megjelenik.
    Opcionálisan szűr a címzett mérnökre.
    Visszaad: (has_assigned, where_sql, params, assigned_sql)
    """
    has_assigned = _qc_assigned_col_available(cur)
    assigned_sql = _assigned_expr(has_assigned)

    where = ("WHERE c.reported_by IS NOT NULL AND c.reported_by <> '' "
             "AND c.fixed_at IS NULL")
    params: list = []

    name = (assigned_to or "").strip()
    if name:
        where += f" AND {assigned_sql} LIKE %s"
        params.append(f"%{name}%")
    return has_assigned, where, params, assigned_sql


@qc_bartender_bp.get("/api/bartender/qc_pending_fixes")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_qc_pending_fixes():
    """
    GET ?assigned_to=<mérnök neve>&limit=&offset=
    A QC által bejelentett, még nem javított projektek listája a mérnöknek.
    A listából akkor tűnik el egy elem, ha a mérnök mentette és visszajelzett
    a QC-nek (fixed_at), vagy a QC újra jóváhagyta.
    """
    assigned_to = (request.args.get("assigned_to") or "").strip()
    try:
        limit = max(1, min(500, int(request.args.get("limit") or 100)))
    except Exception:
        limit = 100
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except Exception:
        offset = 0

    db  = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if not _qc_fix_cols_available(cur):
            return jsonify({"ok": False, "items": [], "total": 0,
                            "error": "Hiányzó DB oszlopok – futtasd a deploy/db_qc_fix.sql-t."}), 200

        _has, where_sql, params, assigned_sql = _pending_where_and_params(cur, assigned_to)

        cur.execute(f"""
            SELECT COUNT(*) AS cnt
            FROM bartender_qc_confirmations c
            LEFT JOIN `etiket data` t
                   ON t.PN = c.pn
                  AND COALESCE(t.REV,'') = c.rev
                  AND COALESCE(t.ECN,'') = c.ecn
            {where_sql}
        """, tuple(params))
        total = int((cur.fetchone() or {}).get("cnt") or 0)

        cur.execute(f"""
            SELECT c.pn, c.rev, c.ecn,
                   c.reported_by, c.report_message,
                   c.revoked_at, c.email_sent_at,
                   {assigned_sql} AS assigned_to,
                   COALESCE(t.`updated date`, t.`create date`) AS updated
            FROM bartender_qc_confirmations c
            LEFT JOIN `etiket data` t
                   ON t.PN = c.pn
                  AND COALESCE(t.REV,'') = c.rev
                  AND COALESCE(t.ECN,'') = c.ecn
            {where_sql}
            ORDER BY COALESCE(c.email_sent_at, c.revoked_at) DESC, c.pn ASC
            LIMIT %s OFFSET %s
        """, tuple(params + [limit, offset]))

        items = []
        for r in (cur.fetchall() or []):
            items.append({
                "pn":             str(r.get("pn") or ""),
                "rev":            str(r.get("rev") or ""),
                "ecn":            str(r.get("ecn") or ""),
                "reported_by":    str(r.get("reported_by") or ""),
                "report_message": str(r.get("report_message") or ""),
                "assigned_to":    str(r.get("assigned_to") or ""),
                "reported_at":    _fmt_dt(r.get("email_sent_at") or r.get("revoked_at")),
                "updated":        _fmt_dt(r.get("updated")),
            })
        return jsonify({"ok": True, "items": items, "total": total}), 200
    except Exception as e:
        return jsonify({"ok": False, "items": [], "total": 0, "error": str(e)}), 200
    finally:
        try: cur.close()
        except Exception: pass


@qc_bartender_bp.get("/api/bartender/qc_pending_count")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_qc_pending_count():
    """Nyitott (QC által bejelentett, még nem javított) projektek száma a badge-hez."""
    assigned_to = (request.args.get("assigned_to") or "").strip()
    db  = get_db()
    cur = db.cursor(dictionary=True)
    try:
        if not _qc_fix_cols_available(cur):
            return jsonify({"ok": True, "count": 0})
        _has, where_sql, params, _asql = _pending_where_and_params(cur, assigned_to)
        cur.execute(f"""
            SELECT COUNT(*) AS cnt
            FROM bartender_qc_confirmations c
            LEFT JOIN `etiket data` t
                   ON t.PN = c.pn
                  AND COALESCE(t.REV,'') = c.rev
                  AND COALESCE(t.ECN,'') = c.ecn
            {where_sql}
        """, tuple(params))
        row = cur.fetchone()
        return jsonify({"ok": True, "count": int((row or {}).get("cnt") or 0)})
    except Exception as e:
        return jsonify({"ok": False, "count": 0, "error": str(e)})
    finally:
        try: cur.close()
        except Exception: pass


# =============================================================================
# QC LOCK SYSTEM
# =============================================================================
# Két felhasználó nem tud egyszerre ellenőrizni ugyanazt a PN-t.
# A lock 3 perces heartbeat timeout után automatikusan felszabadul.
# Szükséges DB tábla:
#   CREATE TABLE IF NOT EXISTS bartender_qc_locks (
#       id           INT AUTO_INCREMENT PRIMARY KEY,
#       pn           VARCHAR(255) NOT NULL,
#       rev          VARCHAR(50)  NOT NULL DEFAULT '',
#       ecn          VARCHAR(50)  NOT NULL DEFAULT '',
#       locked_by    VARCHAR(255) NOT NULL,
#       lock_token   VARCHAR(64)  NOT NULL,
#       locked_at    DATETIME     NOT NULL,
#       heartbeat_at DATETIME     NOT NULL,
#       UNIQUE KEY uq_qc_lock (pn, rev, ecn)
#   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

_QC_LOCK_TIMEOUT_SEC = 180  # 3 perc inaktivitás után elavult a lock


def _qc_lock_acquire(db, pn: str, rev: str, ecn: str, user: str):
    """
    Megpróbálja megszerezni a QC-lockoт a megadott PN/REV/ECN-hez.
    Visszaad: (is_owner: bool, payload: dict)
    """
    cur = db.cursor(dictionary=True)
    now = _dt_mod.datetime.now()

    try:
        cur.execute("""
            SELECT locked_by, lock_token, locked_at, heartbeat_at
            FROM bartender_qc_locks
            WHERE pn=%s AND rev=%s AND ecn=%s
            LIMIT 1
            FOR UPDATE
        """, (pn, rev, ecn))
        row = cur.fetchone()

        token = str(_uuid_mod.uuid4())

        if not row:
            # Nincs lock – foglaljuk el
            cur.execute("""
                INSERT INTO bartender_qc_locks
                    (pn, rev, ecn, locked_by, lock_token, locked_at, heartbeat_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (pn, rev, ecn, user, token, now, now))
            db.commit()
            return True, {"locked_by": user, "token": token}

        hb  = row.get("heartbeat_at") or row.get("locked_at") or now
        age = (now - hb).total_seconds() if isinstance(hb, _dt_mod.datetime) else _QC_LOCK_TIMEOUT_SEC + 1

        if row.get("locked_by") == user or age > _QC_LOCK_TIMEOUT_SEC:
            # Saját lock újra-acquire, vagy lejárt lock átvétele
            cur.execute("""
                UPDATE bartender_qc_locks
                SET locked_by=%s, lock_token=%s, locked_at=%s, heartbeat_at=%s
                WHERE pn=%s AND rev=%s AND ecn=%s
            """, (user, token, now, now, pn, rev, ecn))
            db.commit()
            return True, {"locked_by": user, "token": token, "stolen": age > _QC_LOCK_TIMEOUT_SEC}

        # Más felhasználóé és aktív
        db.commit()
        return False, {
            "locked_by":    row.get("locked_by"),
            "locked_at":    str(row.get("locked_at") or ""),
            "heartbeat_at": str(row.get("heartbeat_at") or ""),
        }

    except Exception:
        try: db.rollback()
        except Exception: pass
        raise
    finally:
        cur.close()


@qc_bartender_bp.post("/api/bartender/qc_lock")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES)
def api_qc_lock():
    """
    POST {pn, rev, ecn}
    Lock megszerzése a PN ellenőrzéshez.
    """
    j   = request.get_json(silent=True) or {}
    pn  = (j.get("pn")  or "").strip()
    rev = (j.get("rev") or "").strip()
    ecn = (j.get("ecn") or "").strip()

    if not pn:
        return jsonify({"status": "error", "message": "pn kötelező"}), 400

    user = _get_current_user()
    if not user:
        return jsonify({"status": "error", "message": "Nem azonosított felhasználó"}), 401

    db = get_db()
    try:
        owner, payload = _qc_lock_acquire(db, pn, rev or "", ecn or "", user)
    except Exception:
        # Lock hiba esetén ne blokkoljuk a munkát
        return jsonify({
            "status":   "success",
            "acquired": True,
            "is_owner": True,
            "locked_by": user,
            "lock_token": None,
            "_fallback": True,
        }), 200

    if owner:
        return jsonify({
            "status":     "success",
            "acquired":   True,
            "is_owner":   True,
            "locked_by":  payload.get("locked_by"),
            "lock_token": payload.get("token"),
        }), 200

    return jsonify({
        "status":       "success",
        "acquired":     False,
        "is_owner":     False,
        "locked_by":    payload.get("locked_by"),
        "locked_at":    payload.get("locked_at"),
        "heartbeat_at": payload.get("heartbeat_at"),
    }), 200


@qc_bartender_bp.post("/api/bartender/qc_heartbeat")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES)
def api_qc_heartbeat():
    """
    POST {pn, rev, ecn, lock_token}
    Lock fenntartása – 60 másodpercenként kell hívni.
    """
    j     = request.get_json(silent=True) or {}
    pn    = (j.get("pn")         or "").strip()
    rev   = (j.get("rev")        or "").strip()
    ecn   = (j.get("ecn")        or "").strip()
    token = (j.get("lock_token") or "").strip()

    if not (pn and token):
        return jsonify({"status": "error", "message": "pn + lock_token kötelező"}), 400

    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE bartender_qc_locks
            SET heartbeat_at = %s
            WHERE pn=%s AND rev=%s AND ecn=%s AND lock_token=%s
        """, (_dt_mod.datetime.now(), pn, rev or "", ecn or "", token))
        db.commit()
        if cur.rowcount == 0:
            return jsonify({"status": "lost"}), 409
        return jsonify({"status": "ok"}), 200
    finally:
        try: cur.close()
        except Exception: pass


@qc_bartender_bp.post("/api/bartender/qc_unlock")
@require_roles(MANAGER_ROLES, IT_ROLES, QC_ROLES, QUALITY_TEAMLEADER_ROLES)
def api_qc_unlock():
    """
    POST {pn, rev, ecn, lock_token}
    Lock elengedése (bezáráskor / oldal elhagyásakor).
    """
    j     = request.get_json(silent=True) or {}
    pn    = (j.get("pn")         or "").strip()
    rev   = (j.get("rev")        or "").strip()
    ecn   = (j.get("ecn")        or "").strip()
    token = (j.get("lock_token") or "").strip()

    if not (pn and token):
        return jsonify({"status": "error", "message": "pn + lock_token kötelező"}), 400

    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            DELETE FROM bartender_qc_locks
            WHERE pn=%s AND rev=%s AND ecn=%s AND lock_token=%s
        """, (pn, rev or "", ecn or "", token))
        db.commit()
        return jsonify({"status": "ok"}), 200
    finally:
        try: cur.close()
        except Exception: pass