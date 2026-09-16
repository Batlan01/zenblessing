# -*- coding: utf-8 -*-
"""
routes/qc_bartender_injector.py
================================
Blueprint neve: "bt_injector"

Regisztrálás __init__.py-ban:
    from routes.qc_bartender_injector import bt_injector_bp
    app.register_blueprint(bt_injector_bp)
"""

from __future__ import annotations

import json

from flask import Blueprint, current_app, jsonify, request, session

from routes.auth import (
    require_roles,
    MANAGER_ROLES, IT_ROLES,
    PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE,
)
from services.bt_db import get_db
from services.bt_xml import _escape_attr, _escape_text, _resolve_btw_path, write_print_xml


# =============================================================================
# Blueprint  –  EGYEDI NÉV: "bt_injector"
# =============================================================================
bt_injector_bp = Blueprint("bt_injector", __name__)


# =============================================================================
# XML BUILDER FÜGGVÉNYEK
# =============================================================================

def _doc_named_substrings(substrings: dict[str, str]) -> str:
    if not substrings:
        return ""
    return "\n".join(
        f'      <NamedSubString Name="{_escape_attr(str(k))}">'
        f'<Value>{_escape_text(str(v))}</Value></NamedSubString>'
        for k, v in substrings.items()
    )


def _batch_document_xml(template: str, copies: int, printer: str, substrings: dict) -> str:
    from services.qc_bartender_core import _remap_to_template_substrings  # lazy
    copies     = max(0, int(copies))
    btw_path   = _resolve_btw_path(template)
    substrings = _remap_to_template_substrings(template, substrings or {})
    return (
        "    <Document>\n"
        f"      <Format>{_escape_text(btw_path)}</Format>\n"
        "      <PrintSetup>\n"
        f"        <IdenticalCopiesOfLabel>{copies}</IdenticalCopiesOfLabel>\n"
        f"        <Printer>{_escape_text(printer or '')}</Printer>\n"
        "      </PrintSetup>\n"
        f"{_doc_named_substrings(substrings)}\n"
        "    </Document>"
    )


def _batch_block_xml(job_name: str, documents_xml: list[str]) -> str:
    return (
        f'<BatchPrint JobName="{_escape_attr(job_name)}">\n'
        "  <Documents>\n"
        + "\n".join(documents_xml) +
        "\n  </Documents>\n"
        "</BatchPrint>"
    )


def build_grouped_print_xml(grouped: dict[str, list[dict]]) -> str:
    from services.qc_bartender_core import _remap_to_template_substrings  # lazy

    safe: dict[str, list[dict]] = {}
    for printer, docs in (grouped or {}).items():
        if not printer or not str(printer).strip():
            continue
        good = [d for d in (docs or [])
                if d and str((d or {}).get("template","")).strip()
                and int((d or {}).get("copies") or 0) > 0]
        if good:
            safe[printer] = good

    parts = []
    for printer, docs in safe.items():
        for d in docs:
            btw_path = _resolve_btw_path(d["template"])
            copies   = max(0, int(d.get("copies", 1)))
            fields   = _remap_to_template_substrings(d["template"], d.get("substrings") or {})
            named    = "\n".join(
                f'  <NamedSubString Name="{_escape_attr(k)}">'
                f'<Value>{_escape_text(v)}</Value></NamedSubString>'
                for k, v in fields.items()
            )
            parts.append(
                f'<Print JobName="{_escape_attr(f"Batch_{printer}")}">\n'
                f'  <Format>{_escape_text(btw_path)}</Format>\n'
                f'  <PrintSetup>\n'
                f'    <IdenticalCopiesOfLabel>{copies}</IdenticalCopiesOfLabel>\n'
                f'    <Printer>{_escape_text(printer)}</Printer>\n'
                f'  </PrintSetup>\n'
                f'{named}\n'
                f'</Print>'
            )

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<XMLScript Version="2.0"><Command>\n'
        + "\n".join(parts)
        + '\n</Command></XMLScript>'
    )


def build_batch_print_xml(grouped: dict[str, list[dict]]) -> str:
    batches = []
    for printer, docs in grouped.items():
        doc_xmls = [
            _batch_document_xml(d["template"], int(d.get("copies",1)), printer, d.get("substrings") or {})
            for d in docs if d and d.get("template")
        ]
        if doc_xmls:
            batches.append(_batch_block_xml(f"Batch_{printer}", doc_xmls))
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<XMLScript Version="2.0"><Command>\n'
        + "\n".join(batches)
        + '\n</Command></XMLScript>'
    )


# =============================================================================
# PRINT ROUTE
# =============================================================================

@bt_injector_bp.route("/api/bartender/print", methods=["POST"])
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_print():
    """
    ⚠️  Másold ide a bartender.py 455–960. sorát.
        Lazy import példa a függvény elejére:

        from services.qc_bartender_core import (
            build_substrings_for, _sanitize_substrings_for_template,
            _remap_to_template_substrings, _filter_grouped_for_output,
            _resolve_printer_for_template, _log_print_batch,
            _side_of_template, _ft_fields_for_template, _split_two_lines,
            _fields_from_card_lines, _fields_from_summary_for_template,
            _preview_lines_from_fields, _collect_specials_for,
            _pick_most_common, _row_with_from_to,
            template_counts_from_summary_nodup, _id_label_counts_from_summary,
            _connector_labels_map_from_summary, _ft_lines_map_from_summary,
            fetch_label_row_by_pn_rev_ecn,
        )
    """
    raise NotImplementedError(
        "Másold ide az api_print implementációját (bartender.py 455–960. sor)"
    )


# =============================================================================
# PRINT COUNTER  +  REPRINT LOG
# =============================================================================

@bt_injector_bp.get("/bartender/api/printed_status", endpoint="api_printed_status_v1")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_printed_status_v1():
    pn  = (request.args.get("pn")  or "").strip()
    rev = (request.args.get("rev") or "").strip()
    ecn = (request.args.get("ecn") or "").strip()
    key = (request.args.get("key") or "").strip()
    if not (pn and rev and ecn and key):
        return jsonify({"ok": False, "error": "missing params"}), 400
    db  = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT printed, total_allowed FROM bartender_printed_counter "
            "WHERE pn=%s AND rev=%s AND ecn=%s AND card_key=%s LIMIT 1",
            (pn, rev, ecn, key),
        )
        row = cur.fetchone() or {}
        return jsonify({"ok": True,
                        "printed":       int(row.get("printed")       or 0),
                        "total_allowed": int(row.get("total_allowed") or 0)}), 200
    finally:
        try: cur.close()
        except Exception: pass


@bt_injector_bp.post("/bartender/api/printed_set_total")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_printed_set_total():
    data  = request.get_json(silent=True) or {}
    pn    = (data.get("pn")  or "").strip()
    rev   = (data.get("rev") or "").strip()
    ecn   = (data.get("ecn") or "").strip()
    key   = (data.get("key") or "").strip()
    total = int(data.get("total") or 0)
    if not (pn and rev and ecn and key):
        return jsonify({"ok": False, "error": "missing params"}), 400
    if total < 0:
        return jsonify({"ok": False, "error": "bad total"}), 400
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT INTO bartender_printed_counter (pn,rev,ecn,card_key,total_allowed,printed) "
            "VALUES (%s,%s,%s,%s,%s,0) ON DUPLICATE KEY UPDATE "
            "total_allowed=GREATEST(total_allowed,VALUES(total_allowed))",
            (pn, rev, ecn, key, total),
        )
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("printed_set_total failed")
        return jsonify({"ok": False, "error": "db error"}), 500
    finally:
        try: cur.close()
        except Exception: pass
    cur2 = db.cursor(dictionary=True)
    try:
        cur2.execute(
            "SELECT printed, total_allowed FROM bartender_printed_counter "
            "WHERE pn=%s AND rev=%s AND ecn=%s AND card_key=%s LIMIT 1",
            (pn, rev, ecn, key),
        )
        row = cur2.fetchone() or {}
        return jsonify({"ok": True,
                        "printed":       int(row.get("printed")       or 0),
                        "total_allowed": int(row.get("total_allowed") or total)}), 200
    finally:
        try: cur2.close()
        except Exception: pass


@bt_injector_bp.post("/bartender/api/printed_add", endpoint="api_printed_add_v1")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_printed_add_v1():
    data = request.get_json(silent=True) or {}
    pn   = (data.get("pn")  or "").strip()
    rev  = (data.get("rev") or "").strip()
    ecn  = (data.get("ecn") or "").strip()
    key  = (data.get("key") or "").strip()
    add  = int(data.get("add") or 0)
    if not (pn and rev and ecn and key):
        return jsonify({"ok": False, "error": "missing params"}), 400
    if add <= 0:
        return jsonify({"ok": False, "error": "bad add"}), 400
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT INTO bartender_printed_counter (pn,rev,ecn,card_key,printed) "
            "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "printed=printed+VALUES(printed), updated_at=CURRENT_TIMESTAMP",
            (pn, rev, ecn, key, add),
        )
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("printed_add failed")
        return jsonify({"ok": False, "error": "db error"}), 500
    finally:
        try: cur.close()
        except Exception: pass
    cur2 = db.cursor(dictionary=True)
    try:
        cur2.execute(
            "SELECT printed FROM bartender_printed_counter "
            "WHERE pn=%s AND rev=%s AND ecn=%s AND card_key=%s LIMIT 1",
            (pn, rev, ecn, key),
        )
        row = cur2.fetchone() or {}
        return jsonify({"ok": True, "printed": int(row.get("printed") or 0)}), 200
    finally:
        try: cur2.close()
        except Exception: pass


@bt_injector_bp.post("/bartender/api/reprint_add")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_reprint_add():
    data     = request.get_json(silent=True) or {}
    pn       = (data.get("pn")                          or "").strip()
    wo       = (data.get("wo")                          or "").strip()
    rev      = (data.get("rev")                         or "").strip()
    ecn      = (data.get("ecn")                         or "").strip()
    card_key = (data.get("card_key") or data.get("key") or "").strip()
    template = (data.get("template")                    or "").strip()
    printer  = (data.get("printer")                     or "").strip()
    copies   = int(data.get("copies") or 0)
    lines    = data.get("lines") or []
    if not (pn and card_key and template):
        return jsonify({"ok": False, "error": "missing params"}), 400
    if copies <= 0:
        return jsonify({"ok": False, "error": "bad copies"}), 400
    if not isinstance(lines, list):
        lines = []
    try:
        user_name = ((session.get("user") or {}).get("username")
                     or (session.get("user") or {}).get("name"))
    except Exception:
        user_name = None
    if not user_name:
        user_name = request.headers.get("X-User") or "unknown"
    lines_json = json.dumps(lines, ensure_ascii=False)
    db  = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT INTO bartender_reprint_log "
            "(pn,wo,rev,ecn,card_key,template,printer,copies,lines_json,created_by,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)",
            (pn, wo or None, rev or None, ecn or None, card_key,
             template, printer or None, copies, lines_json, user_name),
        )
        db.commit()
        return jsonify({"ok": True}), 200
    except Exception:
        db.rollback()
        current_app.logger.exception("reprint_add failed")
        return jsonify({"ok": False, "error": "db error"}), 500
    finally:
        try: cur.close()
        except Exception: pass