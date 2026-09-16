
# -*- coding: utf-8 -*-
from __future__ import annotations
from collections import Counter, defaultdict
from flask import Blueprint, current_app, render_template, request, jsonify, session, abort
from datetime import datetime
from io import BytesIO
import gzip
from routes.auth import (
    require_roles,
    MANAGER_ROLES, TEAMLEADER_ROLES, IT_ROLES,
    PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE
)

# --- DB / BT szolgáltatások (régiek + újak) ---------------------------------
from services.bt_db import (
    # régiek (print/preview + compatibility)
    get_db, fetch_label_row_by_pn, fetch_qty_for_wo, expand_templates,

    # újak (normalizált séma, kis upsertek)
    get_or_create_config_id, template_counts_from_summary, upsert_id_label, upsert_ft_template,
    upsert_ft_line, upsert_connector_group, upsert_connector_item,
    fetch_config_as_ui_json,
)
from services.bt_xml import (
    _escape_attr, _escape_text, _resolve_btw_path, build_print_xml, print_command_dynamic, write_print_xml
)
from services.bt_config import TEMPLATE_DIR
from services.cache import cache

from pathlib import Path
from typing import Callable, List, Tuple, Dict, Optional
import json, re

# egyszerűsített heurisztika – testre szabhatod regexekkel
_RE_ID   = re.compile(r"\bID\b|\bID\s*Label\b", re.I)
_RE_FROM = re.compile(r"\bFrom\b", re.I)
_RE_TO   = re.compile(r"\bTo\b", re.I)

bartender_bp = Blueprint("bartender", __name__)

# =============================================================================
# Sablonfüggő mező-összeállítás (template fájlnév alapján)
# =============================================================================


# --- add these below the existing helpers in services.bt_xml -----------------

def _doc_named_substrings(substrings: dict[str, str]) -> str:
    if not substrings:
        return ""
    return "\n".join(
        f'      <NamedSubString Name="{_escape_attr(str(k))}"><Value>{_escape_text(str(v))}</Value></NamedSubString>'
        for k, v in substrings.items()
    )

def _batch_document_xml(template: str, copies: int, printer: str, substrings: dict[str, str]) -> str:
    copies = max(0, int(copies))
    btw_path = _resolve_btw_path(template)
    # kulcsok a BTW-hez igazítva
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
    docs = "\n".join(documents_xml)
    return (
        f'<BatchPrint JobName="{_escape_attr(job_name)}">\n'
        "  <Documents>\n"
        f"{docs}\n"
        "  </Documents>\n"
        "</BatchPrint>"
    )


def build_grouped_print_xml(grouped: dict[str, list[dict]]) -> str:
    # extra védelem: elvetjük az üres printert / hiányos tételeket
    safe_grouped = {}
    for printer, docs in (grouped or {}).items():
        if not printer or not str(printer).strip():
            continue
        good = []
        for d in (docs or []):
            tpl = (d or {}).get("template", "")
            copies = int((d or {}).get("copies") or 0)
            if tpl and str(tpl).strip() and copies > 0:
                good.append(d)
        if good:
            safe_grouped[printer] = good

    grouped = safe_grouped
    # --- az eddigi implementáció mehet tovább változatlanul ---
    parts = []
    for printer, docs in grouped.items():
        for idx, d in enumerate(docs, start=1):
            btw_path = _resolve_btw_path(d["template"])
            copies = max(0, int(d.get("copies", 1)))
            fields = _remap_to_template_substrings(d["template"], d.get("substrings") or {})
            named = "\n".join(
                f'  <NamedSubString Name="{_escape_attr(k)}"><Value>{_escape_text(v)}</Value></NamedSubString>'
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
        '<XMLScript Version="2.0"><Command>\n' +
        "\n".join(parts) +
        '\n</Command></XMLScript>'
    )


def build_batch_print_xml(grouped: dict[str, list[dict]]) -> str:
    """
    grouped: {
      "<printer>": [
        {"template": str, "copies": int, "substrings": dict}, ...
      ],
      ...
    }
    """
    batches: list[str] = []
    for printer, docs in grouped.items():
        doc_xmls = [
            _batch_document_xml(d["template"], int(d.get("copies", 1)), printer, d.get("substrings") or {})
            for d in docs if d and d.get("template")
        ]
        if doc_xmls:
            jobname = f"Batch_{printer}"
            batches.append(_batch_block_xml(jobname, doc_xmls))

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<XMLScript Version="2.0"><Command>\n'
        + ("\n".join(batches)) +
        '\n</Command></XMLScript>'
    )

def _extract_lines(txt: str | None, max_lines: int = 4) -> list[str]:
    if not txt:
        return []
    # támogatjuk a \n, CRLF és a '\n' szekvenciát is
    s = str(txt).replace("\\n", "\n")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    return lines[:max_lines]

def _preview_lines_from_fields(fields: dict[str, str]) -> list[str]:
    # Prioritás: L1..L4 → FROM_UP/DOWN → TO_UP/DOWN → PN/WO/REV
    out = []
    for k in ("L1", "L2", "L3", "L4"):
        if k in fields and str(fields[k]).strip():
            out.append(str(fields[k]).strip())
    if out:
        return out
    if fields.get("FROM_UP") or fields.get("FROM_DOWN"):
        return [fields.get("FROM_UP", ""), fields.get("FROM_DOWN", "")]
    if fields.get("TO_UP") or fields.get("TO_DOWN"):
        return [fields.get("TO_UP", ""), fields.get("TO_DOWN", "")]
    # default
    base = []
    if fields.get("PN"):  base.append(f'PN: {fields["PN"]}')
    if fields.get("WO"):  base.append(f'WO: {fields["WO"]}')
    if fields.get("REV"): base.append(f'ISSUE {fields["REV"]}')
    return base

def _build_fields_for_preview(template_name: str, pn: str, wo: str, rev: str, row: dict | None) -> dict[str, str]:
    """
    1) Először megpróbáljuk a summary JSON-ból kiolvasni a két sort (connector value-k).
    2) Ha nincs infó a summary-ban, marad a korábbi fallback (text_from/text_to vagy L1..L4 stb.).
    """
    fields = build_substrings_for(template_name, pn, wo, rev).copy()

    # 1) summary-ból érkező pontos értékek (ha vannak)
    try:
        summary_src = (row or {}).get("summary") if isinstance(row, dict) else None
    except Exception:
        summary_src = None

    summ_fields = _fields_from_summary_for_template(template_name, summary_src)
    if summ_fields:
        fields.update(summ_fields)
        return fields  # kész: preview ehhez a sablonhoz

    # 2) fallback a régi text_from/text_to-ra (JSON-érzékeny tördeléssel)
    is_from = bool(_RE_FROM.search(template_name))
    is_to   = bool(_RE_TO.search(template_name))

    if is_from and not (fields.get("FROM_UP") or fields.get("FROM_DOWN")):
        tf = None
        if isinstance(row, dict):
            tf = row.get("text_from")
        up, dn = _coerce_two_lines(tf)
        if not (up or dn):
            up, dn = f"{pn}", f"ISSUE {rev}"
        fields["FROM_UP"]   = up
        fields["FROM_DOWN"] = dn

    if is_to and not (fields.get("TO_UP") or fields.get("TO_DOWN")):
        tt = None
        if isinstance(row, dict):
            tt = row.get("text_to")
        up, dn = _coerce_two_lines(tt)
        if not (up or dn):
            up, dn = f"{pn}", f"WO {wo}"
        fields["TO_UP"]   = up
        fields["TO_DOWN"] = dn

    return fields


def _default_fields(pn: str, wo: str, rev: str) -> Dict[str, str]:
    """Alapeset: a .btw-ben PN/WO/REV nevű NamedSubString-ek vannak."""
    return {"PN": pn, "WO": wo, "REV": rev}

def _DAT34_3l_fields(pn: str, wo: str, rev: str) -> Dict[str, str]:
    # PN/WO/REV: ezt várják a legtöbb BTW-k
    fields = {"PN": pn, "WO": f"SVK{wo}", "REV": rev}
    # kompat: L1..L3-t is küldjük
    fields.update({"L1": f"SVK{wo}", "L2": pn, "L3": f"ISSUE {rev}"})
    return fields

def _DAT34_4l_fields(pn: str, wo: str, rev: str) -> Dict[str, str]:
    fields = {"PN": pn, "WO": f"SVK{wo}", "REV": rev}
    fields.update({"L1": f"SVK{wo}", "L2": f"SVK{wo}", "L3": pn, "L4": f"ISSUE {rev}"})
    return fields

def _DAT37_3l_fields(pn: str, wo: str, rev: str) -> Dict[str, str]:
    fields = {"PN": pn, "WO": f"SVK{wo}", "REV": rev}
    fields.update({"L1": f"SVK{wo}", "L2": pn, "L3": f"ISSUE {rev}"})
    return fields

def _DAT37_4l_fields(pn: str, wo: str, rev: str) -> Dict[str, str]:
    fields = {"PN": pn, "WO": f"SVK{wo}", "REV": rev}
    fields.update({"L1": f"SVK{wo}", "L2": f"SVK{wo}", "L3": pn, "L4": f"ISSUE {rev}"})
    return fields


# Ide vehetsz fel további speciális .btw neveket / regexeket:
TEMPLATE_RULES: List[Tuple[re.Pattern, Callable[[str, str, str], Dict[str, str]]]] = [
    (re.compile(r"9320-5807 \(DAT-34\) ID Label 4L\.btw$", re.I), _DAT34_4l_fields),
    (re.compile(r"9320-5807 \(DAT-34\) ID Label 3L\.btw$", re.I), _DAT34_3l_fields),
    (re.compile(r"TMT093 \(DAT-37\) ID Label 3L\.btw$", re.I), _DAT37_3l_fields),
    (re.compile(r"TMT093 \(DAT-37\) ID Label 4L\.btw$", re.I), _DAT37_4l_fields),
    # példa: (re.compile(r"^9320-5807 .* 4L\.btw$", re.I), _DAT34_4l_fields),
]

def build_substrings_for(template_name: str, pn: str, wo: str, rev: str) -> Dict[str, str]:
    """Template fájlnév alapján kiválasztjuk a mező-kiosztást (ID címkékhez)."""
    for pat, fn in TEMPLATE_RULES:
        if pat.search(template_name):
            return fn(pn, wo, rev)
    return _default_fields(pn, wo, rev)


def _sanitize_substrings_for_template(template_name: str, fields: dict[str, str]) -> dict[str, str]:
    """
    A BarTender-nek csak azokat a NamedSubString-eket küldjük, amelyeket az adott
    sablontípus (ID / FROM / TO) ténylegesen használ.
    - ID  : PN, WO, REV
    - FROM: FROM_UP, FROM_DOWN
    - TO  : TO_UP, TO_DOWN
    """
    side = _side_of_template(template_name)  # "id" | "from" | "to" | None
    if side == "from":
        allow = {"FROM_UP", "FROM_DOWN"}
    elif side == "to":
        allow = {"TO_UP", "TO_DOWN"}
    else:
        # ID vagy ismeretlen -> kezeld ID-ként
        allow = {"PN", "WO", "REV"}
    return {k: v for k, v in (fields or {}).items() if k in allow}

# --- FROM/TO segédek ---------------------------------------------------------

def _split_two_lines(s: str|None) -> tuple[str, str]:
    """
    Egy szöveget kettévág 2 sorra (UP/DOWN). Üreseket átugorja.
    Vissza: (up, down) – ha nincs második sor, down = "".
    """
    if not s:
        return "", ""
    parts = [ln.strip() for ln in str(s).splitlines() if ln.strip()]
    up = parts[0] if len(parts) >= 1 else ""
    down = parts[1] if len(parts) >= 2 else ""
    return up, down

def _ft_fields_for_template(template_name: str, up: str, down: str) -> Dict[str, str]:
    """
    FROM .btw → FROM_UP / FROM_DOWN
    TO   .btw → TO_UP   / TO_DOWN
    Egyébként üres dict.
    """
    if _RE_FROM.search(template_name):
        return {"FROM_UP": up, "FROM_DOWN": down}
    if _RE_TO.search(template_name):
        return {"TO_UP": up, "TO_DOWN": down}
    return {}

# =============================================================================
# Oldalak
# =============================================================================

@bartender_bp.route("/<lang>/bartender/print")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def print_page(lang):
    if lang not in ("hu", "sk"):
        abort(404)
    return render_template(f"{lang}/bartender_print_page.html", user=session.get("user"))

@bartender_bp.route("/<lang>/bartender/edit_v1")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def edit_v1(lang):
    if lang not in ("hu", "sk"):
        abort(404)
    return render_template(f"{lang}/bartender_edit_page_V1.html", user=session.get("user"))

@bartender_bp.route("/<lang>/bartender/preview")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def preview_page(lang):
    if lang not in ("hu", "sk"):
        abort(404)
    return render_template(f"{lang}/bartender_preview.html", user=session.get("user"))

# =============================================================================
# Nyomtatás / Preview (összegzés) – RÉGI LOGIKA
# =============================================================================


@bartender_bp.route("/api/bartender/print", methods=["POST"])
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_print():
    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Nincs szöveg."}), 400

    # opcionális: csak kijelölt sablonok
    selected_raw = request.form.get("selected")
    selected_set = set()
    if selected_raw:
        try:
            arr = json.loads(selected_raw)
            if isinstance(arr, list):
                selected_set = {str(x).strip() for x in arr if str(x).strip()}
        except Exception:
            selected_set = set()

    # opcionális: override példányszámok (template → copies)
    selected_copies = None
    try:
        sc_raw = request.form.get("selected_copies")
        if sc_raw:
            selected_copies = json.loads(sc_raw)
            if not isinstance(selected_copies, dict):
                selected_copies = None
    except Exception:
        selected_copies = None

    pn_m  = re.search(r"PN-([^|]+)\|",  text)
    wo_m  = re.search(r"WO-([^|]+)\|",  text)
    rev_m = re.search(r"REV-([^|]+)\|", text)
    ecn_m = re.search(r"ECN-([^|]+)\|", text)
    qty_m = re.search(r"QTY-(\d+)\|",   text)
    if not pn_m or not wo_m:
        return jsonify({"error": "Hibás formátum (PN-/WO- kötelező)."}), 400

    pn  = pn_m.group(1).strip()
    wo  = wo_m.group(1).strip()
    rev = (rev_m.group(1).strip() if rev_m else "")
    ecn = (ecn_m.group(1).strip() if ecn_m else "")
    qty = (int(qty_m.group(1)) if qty_m else None)

    try:
        db = get_db()

        # PN+REV+ECN sor feloldása (kompat fallback-kel)
        try:
            row = fetch_label_row_by_pn_rev_ecn(db, pn, rev or None, ecn or None)  # type: ignore[name-defined]
        except Exception:
            row = fetch_label_row_by_pn(db, pn)
        if not row:
            return jsonify({"error": "A megadott PN/REV/ECN nem található az 'etiket data' táblában."}), 404

        # legyen biztosan text_from/text_to
        row = _row_with_from_to(db, row if isinstance(row, dict) else {}, pn)

        # sablonok / egység
        template_counts = template_counts_from_summary(row.get("summary"))
        if not template_counts:
            template_counts = expand_templates(row.get("ID_sablon", "[]"))

        # ha csak kijelölteket kér, itt szűrjük
        if selected_set:
            template_counts = {
                t: c for t, c in (template_counts or {}).items()
                if t in selected_set
            }
            if not template_counts:
                return jsonify({"error": "A kijelölt sablonok üresek vagy ismeretlenek."}), 400

        # mennyiség
        if qty is None:
            qty = fetch_qty_for_wo(db, pn, wo)
            if qty is None:
                return jsonify({"error": "QTY nem található a WO alapján."}), 404

        # PN-hez elmentett printers JSON (ha van)
        try:
            pn_printers_json = json.loads(row["printers"]) if row.get("printers") else None
        except Exception:
            pn_printers_json = None

        # FROM/TO – preferáld a summary-t
        summ_map = _ft_lines_map_from_summary(row.get("summary") if isinstance(row, dict) else None)

        by_printer: dict[str, list[dict]] = defaultdict(list)
        printers_used: dict[str, str] = {}
        printed_templates: list[str] = []
        fields_per_tpl: dict[str, dict[str, str]] = {}

        for template_name, per_unit in (template_counts or {}).items():
            printer = _resolve_printer_for_template(db, template_name, pn_printers_json)
            if not printer:
                return jsonify({"error": f"Nincs nyomtató a sablonra: {template_name}"}), 400

            # mezők – summary → FROM/TO → ID
            sm = summ_map.get(template_name) if 'summ_map' in locals() else None
            if sm and sm.get("side") == "FROM":
                fields = {"FROM_UP": sm.get("up", ""), "FROM_DOWN": sm.get("down", "")}
            elif sm and sm.get("side") == "TO":
                fields = {"TO_UP": sm.get("up", ""), "TO_DOWN": sm.get("down", "")}
            else:
                if _RE_FROM.search(template_name):
                    f_up, f_dn = _split_two_lines(row.get("text_from") if isinstance(row, dict) else None)
                    fields = _ft_fields_for_template(template_name, f_up, f_dn)
                elif _RE_TO.search(template_name):
                    t_up, t_dn = _split_two_lines(row.get("text_to") if isinstance(row, dict) else None)
                    fields = _ft_fields_for_template(template_name, t_up, t_dn)
                else:
                    fields = build_substrings_for(template_name, pn, wo, rev)

            # csak a sablontípushoz illeszkedő kulcsok + L* kigyomlálása
            fields = _sanitize_substrings_for_template(template_name, fields)
            fields = {k: v for k, v in (fields or {}).items()
                      if not (isinstance(k, str) and k.upper().startswith("L"))}
            fields_per_tpl[template_name] = fields

            # példányszám (all vs selected override)
            per_unit = int(per_unit)
            total = per_unit * int(qty)
            if selected_copies is not None:
                desired = int((selected_copies or {}).get(template_name, 0))
                copies = max(0, min(desired, total))
            else:
                copies = total

            if copies > 0:
                by_printer[printer].append({
                    "template": template_name,
                    "copies": copies,                      # <-- ténylegesen nyomtatandó
                    "substrings": fields
                })
                printers_used[template_name] = printer
                printed_templates.append(template_name)

        if not by_printer:
            return jsonify({"error": "Nincs nyomtatható parancs."}), 400

        # XML generálás és kiírás
        # XML generálás és kiírás – csak érvényes elemek
        by_printer_filtered, skipped = _filter_grouped_for_output(by_printer)
        if not by_printer_filtered:
            return jsonify({
                "error": "Nincs nyomtatható parancs (hiányzó sablon/printer).",
                "skipped": skipped
            }), 400

        xml_text = build_grouped_print_xml(by_printer_filtered)
        write_print_xml(xml_text)


        # --- NAPLÓZÁS --------------------------------------------------------
        source = "selected" if selected_copies else "all"
        if (request.form.get("reprint") or "") == "1":
            source = "reprint"
        log_items = _build_log_items_for_print(
            template_counts=template_counts,
            qty=int(qty),
            printers_used=printers_used,
            selected_copies=selected_copies,
            fields_per_tpl=fields_per_tpl,
            source=source
        )

        # user név
        try:
            user_name = (session.get("user") or {}).get("username") or (session.get("user") or {}).get("name")
        except Exception:
            user_name = None
        if not user_name:
            user_name = request.headers.get("X-User") or "unknown"

        _log_print_batch(
            db,
            user_name=user_name,
            header={"pn": pn, "wo": wo, "rev": rev, "ecn": ecn, "qty": int(qty)},
            items=log_items,
            xml_text=xml_text,
            source=source,
            config_id=None,
            printer_id=None
        )
        # ---------------------------------------------------------------------

        return jsonify({
            "pn": pn, "wo": wo, "rev": rev, "ecn": ecn,
            "qty_sum": int(qty),
            "templates": dict(template_counts),
            "calculated_totals": {t: int(c) * int(qty) for t, c in template_counts.items()},
            "printers": printers_used,
            "printed_templates": printed_templates,
            "skipped": skipped                      # <-- ÚJ
        }), 200


    except Exception as e:
        print("api_print hiba:", repr(e))
        return jsonify({"error": "Szerver hiba nyomtatás közben."}), 500





@bartender_bp.route("/api/bartender/preview", methods=["POST"])
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def api_preview():
    try:
        data = request.get_json(silent=True) or {}
        pn  = (data.get("pn")  or "").strip()
        wo  = (data.get("wo")  or "").strip()
        rev = (data.get("rev") or "").strip()
        ecn = (data.get("ecn") or "").strip()
        if not pn or not wo:
            return jsonify({"error": "Hiányzó mezők (pn, wo)."}), 400

        db = get_db()
        try:
            row = fetch_label_row_by_pn(db, pn, rev, ecn)  # type: ignore[arg-type]
        except TypeError:
            row = fetch_label_row_by_pn(db, pn)
        if not row:
            return jsonify({"error": "PN/REV/ECN nincs az 'etiket data' táblában."}), 404

        # biztosítsuk a text_from/text_to jelenlétét
        row = _row_with_from_to(db, row if isinstance(row, dict) else {}, pn)

        template_counts = template_counts_from_summary(row.get("summary"))
        if not template_counts:
            template_counts = expand_templates(row.get("ID_sablon", "[]"))

        qty = fetch_qty_for_wo(db, pn, wo) or 0

        pn_printers_json = json.loads(row["printers"]) if row.get("printers") else None
        printers_resolved = {
            tpl: _resolve_printer_for_template(db, tpl, pn_printers_json) or ""
            for tpl in template_counts.keys()
        }
        skipped_preview = []
        templates_effective = {}
        for tpl, per_unit in (template_counts or {}).items():
            has_printer = bool(printers_resolved.get(tpl))
            has_tpl = bool(tpl and str(tpl).strip())
            if has_printer and has_tpl:
                templates_effective[tpl] = per_unit
            else:
                reason = []
                if not has_tpl: reason.append("no_template")
                if not has_printer: reason.append("no_printer")
                skipped_preview.append({"template": tpl, "reason": ",".join(reason)})

        calculated_totals_effective = {t: int(c) * int(qty) for t, c in templates_effective.items()}

        # Vonal-preview (ID: L1..L4, FROM/TO: 2 sor) – az új koercióval
        line_preview: dict[str, list[str]] = {}
        for tpl in template_counts.keys():
            fields = _build_fields_for_preview(tpl, pn, wo, rev, row)
            line_preview[tpl] = _preview_lines_from_fields(fields)

        return jsonify({
            "pn": pn,
            "wo": wo,
            "rev": rev,
            "qty_sum": int(qty),
            "templates": dict(template_counts),
            "calculated_totals": {t: int(c) * int(qty) for t, c in template_counts.items()},
            "printers": printers_resolved,
            "line_preview": line_preview,
            "templates_effective": templates_effective,
            "calculated_totals_effective": calculated_totals_effective,
            "skipped": skipped_preview,
        }), 200

    except Exception as e:
        print("api_preview hiba:", repr(e))
        return jsonify({"error": "Szerver hiba preview közben."}), 500



# =============================================================================
# Rendszernyomtatók listája – fix, beégetett
# =============================================================================

@bartender_bp.route("/api/bartender/get_printers")
def get_printers():
    printers = [
        "TOSHIBA B-EX4T1 (305 dpi) TEC3",
        "TOSHIBA B-EX4T1 (305 dpi) TEC2",
        "TOSHIBA B-EX4T1 (305 dpi)",
        "CAB A4+M/300",
        "CAB SQUIX 4/300M",
    ]
    return jsonify({"printers": printers}), 200

# =============================================================================
# BTW sablonok listázása
# =============================================================================

# használd ezt a gyökeret – ha nem elérhető, TEMPLATE_DIR-re esik vissza
TEMPLATE_DIR_FS = Path(r"\\10.10.2.15\Users\ntrencik\Documents\BarTender\Integrations\Templates")

@bartender_bp.route("/api/bartender/get_btw_files")
@cache.cached(timeout=60)
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def get_btw_files():
    try:
        base = TEMPLATE_DIR_FS if TEMPLATE_DIR_FS.exists() else TEMPLATE_DIR
        files = sorted([p.name for p in Path(base).glob("*.btw")])
        return jsonify({"files": files}), 200
    except Exception as e:
        print("get_btw_files hiba:", repr(e))
        return jsonify({"files": [], "message": "Hiba a sablonok listázásakor."}), 500

# =============================================================================
# ÚJ, NORMALIZÁLT SÉMÁS VÉGKAPCSOLATOK (karcsú mentés)
# =============================================================================

@bartender_bp.get("/api/pn")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_get_pn():
    """Összeállított konfiguráció visszaadása (UI struktúra) PN/REV/ECN alapján."""
    pn  = (request.args.get("pn")  or "").strip()
    rev = (request.args.get("rev") or "").strip()
    ecn = (request.args.get("ecn") or "").strip()
    if not pn:
        return jsonify({"error": "pn kötelező"}), 400
    try:
        cfg_id = get_or_create_config_id(pn, rev or None, ecn or None)
        data = fetch_config_as_ui_json(cfg_id)
        data.update({"config_id": cfg_id, "pn": pn, "rev": rev, "ecn": ecn})
        return jsonify(data), 200
    except Exception as e:
        print("api_get_pn hiba:", repr(e))
        return jsonify({"error": "Szerver hiba PN betöltésekor."}), 500

@bartender_bp.post("/api/id-label/save")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_save_id_label():
    j = request.get_json(silent=True) or {}
    try:
        upsert_id_label(
            int(j["config_id"]),
            int(j.get("label_count", 1)),
            j.get("template_id"),
            j.get("printer_id"),
            j.get("side_label"),
            j.get("group_label"),
        )
        return jsonify({"ok": True})
    except Exception as e:
        print("api_save_id_label hiba:", repr(e))
        return jsonify({"ok": False, "error": "Szerver hiba id_label mentésekor."}), 500

@bartender_bp.post("/api/ft/template")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_save_ft_template():
    j = request.get_json(silent=True) or {}
    try:
        pair_id = upsert_ft_template(
            int(j["config_id"]),
            int(j["page_no"]),
            int(j["group_no"]),
            int(j["pair_no"]),
            str(j["side"]).upper(),          # 'FROM' / 'TO'
            j.get("template_id"),
        )
        return jsonify({"ok": True, "pair_id": pair_id})
    except Exception as e:
        print("api_save_ft_template hiba:", repr(e))
        return jsonify({"ok": False, "error": "Szerver hiba ft_template mentésekor."}), 500

@bartender_bp.post("/api/ft/line")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_save_ft_line():
    j = request.get_json(silent=True) or {}
    try:
        # biztosítjuk, hogy létezzen a pair (template_id itt opcionális)
        pair_id = upsert_ft_template(
            int(j["config_id"]),
            int(j["page_no"]),
            int(j["group_no"]),
            int(j["pair_no"]),
            str(j["side"]).upper(),
            j.get("template_id"),
        )
        upsert_ft_line(
            int(pair_id),
            str(j["line_pos"]),          # 'top' / 'bottom'
            j.get("connector_code"),
            j.get("text"),
        )
        return jsonify({"ok": True})
    except Exception as e:
        print("api_save_ft_line hiba:", repr(e))
        return jsonify({"ok": False, "error": "Szerver hiba ft_line mentésekor."}), 500

@bartender_bp.post("/api/connectors/group")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_save_c_group():
    j = request.get_json(silent=True) or {}
    try:
        gid = upsert_connector_group(
            int(j["config_id"]),
            int(j["page_no"]),
            int(j["group_no"]),
        )
        return jsonify({"ok": True, "group_id": gid})
    except Exception as e:
        print("api_save_c_group hiba:", repr(e))
        return jsonify({"ok": False, "error": "Szerver hiba connector group mentésekor."}), 500

@bartender_bp.post("/api/connectors/item")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_save_c_item():
    j = request.get_json(silent=True) or {}
    try:
        upsert_connector_item(
            int(j["group_id"]),
            int(j["ord_index"]),
            j["code"],
            j.get("display_name"),
        )
        return jsonify({"ok": True})
    except Exception as e:
        print("api_save_c_item hiba:", repr(e))
        return jsonify({"ok": False, "error": "Szerver hiba connector item mentésekor."}), 500

# =============================================================================
# Régi hasznos API-k: t_dump ellenőrzés, label adatok, config mentés/betöltés,
# PN ↔ printer mentés (kompatibilitás megőrizve)
# =============================================================================

@bartender_bp.route('/api/bartender/check_pn_in_t_dump', methods=['POST'])
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def check_pn_in_t_dump():
    try:
        data = request.get_json(silent=True) or request.form
        pn = (data.get('pn') or '').strip()
        if not pn:
            return jsonify({'found': False, 'message': 'Hiányzik a PN'}), 400

        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT 1 FROM t_dump WHERE LOWER(`PART.NBR`) = %s LIMIT 1", (pn.lower(),))
        found = cur.fetchone() is not None
        cur.close()

        return jsonify({'found': found}), 200
    except Exception as e:
        print("PN ellenőrzési hiba (t_dump):", e)
        return jsonify({'found': False, 'message': 'Szerver hiba történt a PN ellenőrzésekor.'}), 500

@bartender_bp.route('/api/bartender/get_label_data', methods=['POST'])
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def get_label_data():
    """
    A régi 'etiket data' tábla text_from/text_to mezőinek betöltése PN alapján.
    """
    conn = None
    cur = None
    raw_db = None
    try:
        data = request.get_json(silent=True) or request.form or {}
        pn = (data.get("pn") or "").strip()
        if not pn:
            return jsonify({"status": "error", "message": "PN nincs megadva"}), 400

        raw_db = get_db()
        conn = getattr(raw_db, "conn", raw_db)
        cur = conn.cursor(dictionary=True)

        cur.execute("""
            SELECT text_from, text_to
            FROM `etiket data`
            WHERE PN = %s
            LIMIT 1
        """, (pn,))
        row = cur.fetchone()

        if not row:
            return jsonify({"status": "not_found", "message": "Nincs ilyen PN"}), 404

        return jsonify({"status": "success", "data": row}), 200

    except Exception as e:
        print("Hiba a /api/bartender/get_label_data során:", repr(e))
        return jsonify({"status": "error", "message": "Szerver hiba a PN betöltésekor."}), 500

    finally:
        try:
            if cur: cur.close()
        except Exception:
            pass
        try:
            if conn and hasattr(conn, "close"):
                conn.close()
            elif raw_db and hasattr(raw_db, "close"):
                raw_db.close()
        except Exception:
            pass


@bartender_bp.route('/api/bartender/load_label_config', methods=['POST'])
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def load_label_config():
    import os, sys, traceback, json
    data = request.get_json(silent=True) or {}
    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()

    if not pn or not rev or not ecn:
        return jsonify({"status": "error", "message": "PN/REV/ECN kötelező"}), 400

    try:
        db = get_db()
        try:
            cur = db.cursor(dictionary=True)
        except TypeError:
            cur = db.cursor()

        cur.execute("""
            SELECT PN, REV, ECN, summary, ID_sablon
            FROM `etiket data`
            WHERE PN=%s AND REV=%s AND ECN=%s
            LIMIT 1
        """, (pn, rev, ecn))
        row = cur.fetchone()
        cur.close()

        if not row:
            return jsonify({"status": "error", "message": "Nem található mentett konfiguráció."}), 404

        if isinstance(row, dict):
            summary_raw = row.get("summary")
            id_template = row.get("ID_sablon")
            db_pn, db_rev, db_ecn = row.get("PN"), row.get("REV"), row.get("ECN")
        else:
            db_pn, db_rev, db_ecn, summary_raw, id_template = row

        try:
            parsed = json.loads(summary_raw) if summary_raw else []
        except Exception as e:
            current_app.logger.exception("Summary JSON parse error")
            return jsonify({"status":"error","message":f"Rossz summary JSON a DB-ben: {e}"}), 500

        # ÚJ: lehet dict {pages: [...], connector_labels: [...] } vagy régi: csak lista
        if isinstance(parsed, dict):
            pages = parsed.get("pages") or parsed.get("summary") or []
            connector_labels = parsed.get("connector_labels") or []
        else:
            pages = parsed
            connector_labels = []

        def norm_text_block(x):
            if not isinstance(x, dict):
                return {"template":"", "text1":"", "text2":""}
            return {
                "template": (x.get("template") or "").strip(),
                "text1":    (x.get("text1") or "").strip(),
                "text2":    (x.get("text2") or "").strip(),
            }

        norm_pages = []
        for i, p in enumerate(pages or []):
            page_no = int(p.get("page") or i + 1) if isinstance(p, dict) else (i + 1)
            id_labels = []
            if isinstance(p, dict):
                for lab in (p.get("id_labels") or []):
                    if isinstance(lab, dict):
                        id_labels.append({
                            "id_index": int(lab.get("id_index") or 0),
                            "template": (lab.get("template") or "").strip(),
                            "printer":  (lab.get("printer")  or "").strip(),
                            "page":     int(lab.get("page") or page_no),
                            "group":    int(lab.get("group") or 1),
                        })

            groups_out = []
            for gi, g in enumerate((p.get("groups") if isinstance(p, dict) else []) or []):
                group_no = int(g.get("group") or gi + 1) if isinstance(g, dict) else (gi + 1)

                connectors_out = []
                for c in (g.get("connectors") or []):
                    if isinstance(c, dict):
                        cid = (c.get("id") or "").strip()
                        cval = (c.get("value") or "").strip()
                        if cid:
                            connectors_out.append({"id": cid, "value": cval})

                pairs_out = []
                for pr in (g.get("pairs") or []):
                    if isinstance(pr, dict):
                        frm = norm_text_block(pr.get("from"))
                        to  = norm_text_block(pr.get("to"))
                        if any([frm["template"], frm["text1"], frm["text2"],
                                to["template"],  to["text1"],  to["text2"]]):
                            pairs_out.append({"from": frm, "to": to})

                groups_out.append({"group": group_no, "connectors": connectors_out, "pairs": pairs_out})

            norm_pages.append({"page": page_no, "id_labels": id_labels, "groups": groups_out})

        payload = {
            "pn": db_pn or pn,
            "rev": db_rev or rev,
            "ecn": db_ecn or ecn,
            "id_template": id_template or "",
            "summary": norm_pages,
            # ÚJ: visszaadjuk a konnektor etiketteket is (ha vannak)
            "connector_labels": connector_labels
        }
        return jsonify({"status": "success", "data": payload}), 200

    except Exception as e:
        tb = traceback.format_exc()
        info = {
            "exc": f"{type(e).__name__}: {e}",
            "cwd": os.getcwd(),
            "py": sys.executable,
            "user": os.getenv("USERNAME") or os.getenv("USER") or "n/a",
        }
        current_app.logger.error("load_label_config failed | %s\n%s", info, tb)
        return jsonify({"status": "error", "message": "Szerver hiba betöltés közben.", "debug": info}), 500


@bartender_bp.get("/api/_diag/env")
def diag_env():
    import os, sys, socket
    out = {
        "cwd": os.getcwd(),
        "py": sys.executable,
        "user": os.getenv("USERNAME") or os.getenv("USER"),
        "host": socket.gethostname(),
    }
    # DB gyors próba
    ok = False
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        ok = True
    except Exception as e:
        out["db_error"] = str(e)
    out["db_ok"] = ok
    return jsonify(out)


@bartender_bp.route('/api/bartender/save_label_config', methods=['POST'])
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def save_label_config():
    """
    Vár: JSON:
      {
        "pn": "...", "rev": "...", "ecn": "...",
        "id_template": "...",
        "pages": [...],
        "connector_labels": [...]           # ÚJ (opcionális)
      }
    Mentés: `etiket data` (PN, REV, ECN, summary(JSON), ID_sablon, printers)
    A summary mostantól lehet objektum: {"pages":[...], "connector_labels":[...]} – régi kliensnél marad lista.
    """
    content_len = request.content_length or 0
    HARD_LIMIT = 12 * 1024 * 1024  # 12MB
    if content_len > HARD_LIMIT:
        return jsonify({
            "status": "error",
            "message": f"A kérés túl nagy ({content_len} byte). Max {HARD_LIMIT} byte engedélyezett."
        }), 413

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not data:
        data = request.form.to_dict() if request.form else {}
        if isinstance(data.get("pages"), str):
            try:
                data["pages"] = json.loads(data["pages"])
            except Exception:
                data["pages"] = []

        # (opcionális) connector_labels űrlapról, ha string
        if isinstance(data.get("connector_labels"), str):
            try:
                data["connector_labels"] = json.loads(data["connector_labels"])
            except Exception:
                data["connector_labels"] = []

    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()
    id_template = (data.get("id_template") or "").strip()

    pages = data.get("pages") or []
    connector_labels = data.get("connector_labels") or []

    if not pn or not rev or not ecn:
        return jsonify({"status": "error", "message": "PN/REV/ECN kötelező"}), 400

    # A summary-t mostantól objektumként is tárolhatjuk (visszafelé kompatibilis)
    summary_obj = {"pages": pages}
    if isinstance(connector_labels, list) and connector_labels:
        summary_obj["connector_labels"] = connector_labels

    try:
        # Ha nincs extra adat, régi viselkedés: sima pages lista mehet (spórol a tárhelyen)
        summary_json = json.dumps(summary_obj if summary_obj else pages,
                                  ensure_ascii=False, separators=(",", ":"))
    except Exception as serr:
        return jsonify({"status": "error", "message": f"JSON szerializációs hiba: {serr}"}), 400

    if len(summary_json.encode("utf-8")) > 8 * 1024 * 1024:
        print(f"[WARN] Nagy summary_json: {len(summary_json)} chars")

    try:
        db = get_db()

        # printer-ek feloldása továbbra is CSAK a pages alapján történik
        printers_obj = _resolve_printers_from_pages(db, pages)
        printers_json = json.dumps(printers_obj, ensure_ascii=False) if printers_obj else None

        cur = db.cursor()
        cur.execute("""
            INSERT INTO `etiket data` (PN, REV, ECN, summary, ID_sablon, printers)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                summary   = VALUES(summary),
                ID_sablon = VALUES(ID_sablon),
                printers  = VALUES(printers)
        """, (pn, rev, ecn, summary_json, id_template, printers_json))
        try:
            db.commit()
        finally:
            cur.close()
        return jsonify({"status": "success"}), 200

    except Exception as e:
        print("save_label_config hiba:", repr(e))
        try:
            get_db().rollback()
        except Exception:
            pass
        if "max_allowed_packet" in str(e).lower():
            return jsonify({"status": "error",
                            "message": "A MySQL max_allowed_packet túl kicsi. Állítsd legalább 64M/128M értékre."}), 500
        return jsonify({"status": "error", "message": "Szerver hiba mentés közben."}), 500


@bartender_bp.route('/api/bartender/save_printer_to_pn', methods=['POST'])
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def save_printer_to_pn():
    """
    JSON: { pn, printer_id, printer_from, printer_to }
    Mentés:
      - `etiket data`.printers = {"id": "...", "from": "...", "to": "..."} (JSON)
      - kompatibilitás: ha van `etiket` tábla, oda lista formában is.
    """
    try:
        data = request.get_json() or {}
        pn = (data.get("pn") or "").strip()
        printer_id = (data.get("printer_id") or "").strip()
        printer_from = (data.get("printer_from") or "").strip()
        printer_to = (data.get("printer_to") or "").strip()

        if not pn or not printer_id or not printer_from or not printer_to:
            return jsonify({"status": "error", "message": "Minden mező kötelező (pn, printer_id, printer_from, printer_to)."}), 400

        printers_obj = {"id": printer_id, "from": printer_from, "to": printer_to}
        printers_obj_json = json.dumps(printers_obj, ensure_ascii=False)

        db = get_db()
        cur = db.cursor()

        # etiket data: update/insert
        cur.execute("SELECT 1 FROM `etiket data` WHERE PN = %s LIMIT 1", (pn,))
        exists = cur.fetchone() is not None

        if exists:
            cur.execute("UPDATE `etiket data` SET printers = %s WHERE PN = %s", (printers_obj_json, pn))
        else:
            cur.execute("""
                INSERT INTO `etiket data` (PN, REV, ECN, summary, ID_sablon, printers)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (pn, "", "", "[]", "", printers_obj_json))

        # kompatibilitás az 'etiket' táblával (ha létezik)
        try:
            cur.execute("SELECT 1 FROM `etiket` WHERE PN = %s LIMIT 1", (pn,))
            if cur.fetchone():
                printers_list_json = json.dumps([printer_id], ensure_ascii=False)
                cur.execute("UPDATE `etiket` SET printers = %s WHERE PN = %s", (printers_list_json, pn))
        except Exception:
            pass

        db.commit()
        cur.close()

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print("save_printer_to_pn hiba:", repr(e))
        try:
            db = get_db()
            db.rollback()
        except Exception:
            pass
        return jsonify({"status": "error", "message": "Szerver hiba a nyomtatók mentésekor."}), 500

@bartender_bp.get("/api/bartender/get_btw_printers")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_get_btw_printers():
    """
    Válasz: { status: "success", map: { "<file.btw>": "<printer name>", ... } }
    """
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT `file`, `printer_name` FROM `btw_printers`")
        mapping = {row["file"]: row["printer_name"] for row in cur.fetchall()}
        cur.close()
        return jsonify({"status": "success", "map": mapping}), 200
    except Exception as e:
        print("get_btw_printers hiba:", repr(e))
        return jsonify({"status": "error", "message": "Szerver hiba a mapping olvasásakor."}), 500

@bartender_bp.post("/api/bartender/save_btw_printers")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_save_btw_printers():
    """
    Body: { "assignments": [ { "file": "X.btw", "printer": "Zebra-1" }, ... ] }
    Mentés: UPSERT btw_printers(file PK) → printer_name
    """
    try:
        payload = request.get_json(silent=True) or {}
        items = payload.get("assignments") or []
        if not isinstance(items, list):
            return jsonify({"status": "error", "message": "assignments nem lista"}), 400

        db = get_db()
        cur = db.cursor()
        sql = """
            INSERT INTO `btw_printers` (`file`, `printer_name`)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE
                `printer_name` = VALUES(`printer_name`),
                `updated_at`   = CURRENT_TIMESTAMP
        """
        saved = 0
        for it in items:
            f = (it or {}).get("file") or ""
            p = (it or {}).get("printer") or ""
            if not f or not p:
                continue
            cur.execute(sql, (f, p))
            saved += 1
        db.commit()
        cur.close()
        return jsonify({"status": "success", "saved": saved}), 200

    except Exception as e:
        print("save_btw_printers hiba:", repr(e))
        try:
            get_db().rollback()
        except Exception:
            pass
        return jsonify({"status": "error", "message": "Szerver hiba a mapping mentésekor."}), 500

# =============================================================================
# Helper-ek (printer/sablon feloldás)
# =============================================================================

def _pick_most_common(values):
    vals = [v for v in values if v]
    if not vals:
        return None
    c = Counter(vals).most_common()
    return c[0][0] if c else None

def _resolve_printers_from_pages(db, pages):
    """
    pages: a kliens által küldött 'pages' (list of dict).
    Vissza: dict pl. {"id": "Zebra A", "from": "ZT410", "to": "ZT410"}
    """
    id_templates   = []
    from_templates = []
    to_templates   = []

    # 1) ID sablonok
    for p in pages or []:
        for lab in (p.get("id_labels") or []):
            t = (lab.get("template") or "").strip()
            if t:
                id_templates.append(t)

    # 2) FROM/TO sablonok
    for p in pages or []:
        for g in (p.get("groups") or []):
            for pr in (g.get("pairs") or []):
                frm = (pr.get("from") or {})
                to  = (pr.get("to")   or {})
                ft  = (frm.get("template") or "").strip()
                tt  = (to.get("template")  or "").strip()
                if ft:
                    from_templates.append(ft)
                if tt:
                    to_templates.append(tt)

    # hozzárendelések btw_printers-ből
    all_needed = list({*id_templates, *from_templates, *to_templates})
    printer_map = {}
    if all_needed:
        cur = db.cursor()
        CH = 100
        for i in range(0, len(all_needed), CH):
            chunk = all_needed[i:i+CH]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT `file`,`printer_name` FROM `btw_printers` WHERE `file` IN ({placeholders})",
                tuple(chunk)
            )
            for f, pn in cur.fetchall():
                printer_map[f] = pn
        cur.close()

    id_printers   = [printer_map.get(t) for t in id_templates]
    from_printers = [printer_map.get(t) for t in from_templates]
    to_printers   = [printer_map.get(t) for t in to_templates]

    out = {}
    pid   = _pick_most_common(id_printers)
    pfrom = _pick_most_common(from_printers)
    pto   = _pick_most_common(to_printers)
    if pid:   out["id"]   = pid
    if pfrom: out["from"] = pfrom
    if pto:   out["to"]   = pto
    return out

def _side_of_template(filename: str) -> str|None:
    if _RE_ID.search(filename):   return "id"
    if _RE_FROM.search(filename): return "from"
    if _RE_TO.search(filename):   return "to"
    return None

def _resolve_printer_for_template(db, template_name: str, pn_printers: dict|list|None) -> str|None:
    # 1) btw_printers tábla
    cur = db.cursor()
    cur.execute("SELECT `printer_name` FROM `btw_printers` WHERE `file`=%s LIMIT 1", (template_name,))
    row = cur.fetchone()
    cur.close()
    if row:
        return row[0] if isinstance(row, tuple) else row.get("printer_name")

    # 2) PN-hez elmentett printers JSON-ból (oldalszerint, ha felismerhető)
    side = _side_of_template(template_name)
    if isinstance(pn_printers, dict) and side:
        if pn_printers.get(side):
            return pn_printers[side]

    # 3) fallback: id kulcs vagy lista első eleme (régi forma)
    if isinstance(pn_printers, dict):
        return pn_printers.get("id") or None
    if isinstance(pn_printers, list) and pn_printers:
        return pn_printers[0]
    return None


from collections import defaultdict

def _filter_grouped_for_output(grouped: dict[str, list[dict]]):
    """
    Vissza: (cleaned_grouped, skipped)
      - cleaned_grouped: csak érvényes (printer != "", template != "", copies>0) elemek
      - skipped: list[{"template":..., "printer":..., "reason": "..."}]
    """
    cleaned = defaultdict(list)
    skipped = []

    for printer, docs in (grouped or {}).items():
        if not printer or not str(printer).strip():
            # ennél a kulcsnál minden elemet elhagyunk
            for d in (docs or []):
                skipped.append({
                    "template": (d or {}).get("template", ""),
                    "printer": "",
                    "reason": "no_printer"
                })
            continue

        for d in (docs or []):
            tpl = (d or {}).get("template", "") or ""
            tpl = str(tpl).strip()
            copies = int((d or {}).get("copies") or 0)

            if not tpl:
                skipped.append({"template": "", "printer": printer, "reason": "no_template"})
                continue
            if copies <= 0:
                skipped.append({"template": tpl, "printer": printer, "reason": "zero_copies"})
                continue

            cleaned[printer].append(d)

    # ha valamelyik printer alatt nincs már elem, ne maradjon üres kulcs
    cleaned = {p: ds for p, ds in cleaned.items() if ds}
    return cleaned, skipped


# --- ide a helper szekcióba (a többi helper mellé) --------------------------

def _fetch_from_to_by_pn(db, pn: str) -> tuple[str, str]:
    """
    Biztos fallback: beolvassa a text_from / text_to értékeket az `etiket data` táblából.
    Ha nincs találat, üreseket ad vissza.
    """
    try:
        cur = db.cursor(dictionary=True)
    except TypeError:
        cur = db.cursor()

    try:
        cur.execute("""
            SELECT text_from, text_to
            FROM `etiket data`
            WHERE PN = %s
            LIMIT 1
        """, (pn,))
        row = cur.fetchone()
    finally:
        try:
            cur.close()
        except Exception:
            pass

    if not row:
        return "", ""

    tf = (row.get("text_from") if isinstance(row, dict) else row[0]) or ""
    tt = (row.get("text_to")   if isinstance(row, dict) else row[1]) or ""
    return str(tf), str(tt)



def _row_with_from_to(db, row: dict | None, pn: str) -> dict:
    """
    Visszaad egy dict-et, amiben garantáltan benne van a 'text_from' és 'text_to'.
    Ha az eredeti row-ban nincs, külön lekérjük.
    """
    out = dict(row or {})
    tf = out.get("text_from") if isinstance(out, dict) else None
    tt = out.get("text_to")   if isinstance(out, dict) else None
    if not (tf and tf.strip()) or not (tt and tt.strip()):
        ft, tt2 = _fetch_from_to_by_pn(db, pn)
        if not (tf and tf.strip()):
            out["text_from"] = ft
        if not (tt and tt.strip()):
            out["text_to"] = tt2
    return out


def _coerce_two_lines(value: str | None, max_len: int = 60) -> tuple[str, str]:
    """
    A DB-ből jövő text_from/text_to lehet:
      - sima több soros szöveg
      - JSON dict: {"top": "...", "bottom": "..."} / {"up": "...","down":"..."} / {"text1": "...","text2":"..."}
      - JSON lista: pl. [["T93",""], ["T94","X"], ...] vagy ["A","B", ...]
    Ebből két sor (up, down) előállítása preview/print célra.
    """
    if not value:
        return "", ""

    s = str(value).strip()

    # 1) Próbáljuk JSON-ként értelmezni
    try:
        obj = json.loads(s)
        # 1/a) dict mint két sor
        if isinstance(obj, dict):
            up = obj.get("top") or obj.get("up") or obj.get("line1") or obj.get("text1") or ""
            dn = obj.get("bottom") or obj.get("down") or obj.get("line2") or obj.get("text2") or ""
            return str(up).strip(), str(dn).strip()

        # 1/b) lista -> stringek listájává lapítjuk
        if isinstance(obj, list):
            items: list[str] = []
            for it in obj:
                if isinstance(it, (list, tuple)):
                    parts = [str(x).strip() for x in it if str(x).strip()]
                    if parts:
                        items.append(" ".join(parts))
                elif isinstance(it, dict):
                    # ritka eset: {"code":"...", "value":"..."}
                    code = (it.get("code") or it.get("id") or "").strip()
                    val  = (it.get("value") or it.get("text") or "").strip()
                    txt = " ".join([p for p in [code, val] if p])
                    if txt:
                        items.append(txt)
                else:
                    txt = str(it).strip()
                    if txt:
                        items.append(txt)

            # a stringekből két sort építünk, hogy kb. ne lépjük túl a max_len-t
            up, dn, cur, filled_up = "", "", "", False
            for token in items:
                sep = (", " if cur else "")
                if len(cur) + len(sep) + len(token) <= max_len:
                    cur = cur + sep + token
                else:
                    if not filled_up:
                        up = cur
                        cur = token
                        filled_up = True
                    else:
                        if dn:
                            dn = dn + ", " + cur
                        else:
                            dn = cur
                        cur = token
            if cur:
                if not filled_up:
                    up = cur
                else:
                    dn = (dn + ", " + cur) if dn else cur
            return up.strip(), dn.strip()
    except Exception:
        pass

    # 2) Egyszerű több-soros szöveg
    text = s.replace("\\n", "\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    up = lines[0] if len(lines) >= 1 else ""
    dn = lines[1] if len(lines) >= 2 else ""
    return up, dn


def _split_two_lines(s: str | None) -> tuple[str, str]:
    """A régi helper JSON-érzékeny verziója – a print is ezt használja."""
    return _coerce_two_lines(s)


def _ft_lines_map_from_summary(summary_json) -> dict[str, dict]:
    """
    A summary JSON-ból kinyeri a FROM/TO sablonokhoz tartozó (UP, DOWN) sorokat,
    a connector kódokhoz tartozó *value* alapján.

    Vissza: { "<template.btw>": {"side": "FROM"|"TO", "up": "...", "down": "..."} }
    """
    out: dict[str, dict] = {}
    if not summary_json:
        return out

    try:
        pages = json.loads(summary_json) if isinstance(summary_json, str) else (summary_json or [])
    except Exception:
        # ha már dict/list formában jött
        pages = summary_json if isinstance(summary_json, (list, dict)) else []

    # A régi formátum támogatása: ha dict-ben 'pages' kulcs van
    if isinstance(pages, dict):
        pages = pages.get("pages") or pages.get("summary") or []

    for p in pages or []:
        groups = (p.get("groups") if isinstance(p, dict) else []) or []
        for g in groups:
            # connector kód -> érték map
            cmap = {}
            for c in (g.get("connectors") or []):
                cid = (c.get("id") or c.get("code") or "").strip()
                val = (c.get("value") or c.get("display_name") or "").strip()
                if cid:
                    cmap[cid] = val

            for pr in (g.get("pairs") or []):
                frm = (pr.get("from") or {})
                to  = (pr.get("to")   or {})

                def pick_lines(block: dict) -> tuple[str, str]:
                    # A 'text1' és 'text2' a kiválasztott connector KÓDJA (pl. "A", "B")
                    k1 = (block.get("text1") or "").strip()
                    k2 = (block.get("text2") or "").strip()
                    up = cmap.get(k1, k1)   # ha nincs a mapben, marad a nyers szöveg
                    dn = cmap.get(k2, k2)
                    return up, dn

                if frm and frm.get("template"):
                    up, dn = pick_lines(frm)
                    out[frm["template"]] = {"side": "FROM", "up": up, "down": dn}

                if to and to.get("template"):
                    up, dn = pick_lines(to)
                    out[to["template"]] = {"side": "TO", "up": up, "down": dn}

    return out


def _fields_from_summary_for_template(template_name: str, summary_json) -> dict[str, str]:
    """
    Egyetlen sablonhoz visszaadja a *konkrét* mezőket (FROM_UP/DOWN vagy TO_UP/DOWN),
    ha a summary JSON tartalmaz hozzá értéket.
    """
    mp = _ft_lines_map_from_summary(summary_json)
    info = mp.get(template_name)
    if not info:
        return {}

    up = (info.get("up") or "").strip()
    dn = (info.get("down") or "").strip()

    if info.get("side") == "FROM":
        return {"FROM_UP": up, "FROM_DOWN": dn}
    if info.get("side") == "TO":
        return {"TO_UP": up, "TO_DOWN": dn}
    return {}


# --- remappelés a .btw elvárásaihoz (csak XML-kimenet előtt) -----------------

_ID_3L = re.compile(r"(?:DAT-3[47]).*ID Label 3L\.btw$", re.I)
_ID_4L = re.compile(r"(?:DAT-3[47]).*ID Label 4L\.btw$", re.I)

def _remap_to_template_substrings(template_name: str, fields: dict[str, str]) -> dict[str, str]:
    """
    A BTW-ben elvárt NamedSubString-ekre térképez:
      - FROM*:  FROM_UP / FROM_DOWN
      - TO*:    TO_UP   / TO_DOWN
      - ID:     PN / WO / REV  (REV elé "ISSUE " kerül, ha nincs ott)
    Csak ezeket a kulcsokat engedi ki.
    """
    side = _side_of_template(template_name)
    f = {k: (v or "") for k, v in (fields or {}).items()}

    # FROM oldal
    if side == "from":
        return {
            "FROM_UP":   f.get("FROM_UP", ""),
            "FROM_DOWN": f.get("FROM_DOWN", ""),
        }

    # TO oldal
    if side == "to":
        return {
            "TO_UP":   f.get("TO_UP", ""),
            "TO_DOWN": f.get("TO_DOWN", ""),
        }

    # ID oldal → PN/WO/REV (REV elé "ISSUE ")
    pn  = f.get("PN", "")
    wo  = f.get("WO", "")
    rev = f.get("REV", "")
    if rev and not str(rev).strip().upper().startswith("ISSUE"):
        rev = f"ISSUE {rev}"

    return {"PN": pn, "WO": wo, "REV": rev}


def _gzip_bytes(data: str | None) -> bytes:
    if not data:
        return b""
    buf = BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as z:
        z.write(data.encode("utf-8"))
    return buf.getvalue()


def _build_log_items_for_print(*, template_counts: dict, qty: int,
                               printers_used: dict, selected_copies: dict | None,
                               fields_per_tpl: dict[str, dict[str, str]],
                               source: str) -> list[dict]:
    """
    Egy sor = 1 template nyomtatása.
    'all'   → printed_copies = per_unit * qty
    'selected' → printed_copies = min(override, per_unit * qty)
    """
    out = []
    for tpl, per_unit in (template_counts or {}).items():
        if source == "selected" and (not selected_copies or tpl not in selected_copies):
            continue

        total = int(per_unit) * int(qty)
        printed = total
        if source == "selected":
            try:
                desired = int((selected_copies or {}).get(tpl, 0))
            except Exception:
                desired = 0
            printed = max(0, min(desired, total))
        if printed <= 0:
            continue

        # préview-vonalak a naplóhoz
        fields = fields_per_tpl.get(tpl) or {}
        lines = _preview_lines_from_fields(fields)

        out.append({
            "template": tpl,
            "printer_name": printers_used.get(tpl, ""),
            "copies_per_unit": int(per_unit),
            "total_copies": total,
            "printed_copies": printed,
            "lines": lines,
        })
    return out


def _log_print_batch(db, *,
                     user_name: str,
                     header: dict,                  # {pn, wo, rev, ecn, qty}
                     items: list[dict],             # _build_log_items_for_print()
                     xml_text: str,
                     source: str,
                     config_id: int | None = None,
                     printer_id: str | None = None):
    """
    Minden template külön rekord a print_log táblában, ugyanazzal a btxml_gz-zel.
    """
    gz = _gzip_bytes(xml_text)
    printed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    sql = ("""
        INSERT INTO print_log
        (config_id, user_name, printer_id, qty, printed_at, btxml_gz,
         pn, wo, rev, ecn,
         template, printer_name,
         copies_per_unit, total_copies, printed_copies,
         lines_json, source)
        VALUES
        (%s,%s,%s,%s,%s,%s,
         %s,%s,%s,%s,
         %s,%s,
         %s,%s,%s,
         %s,%s)
    """)

    cur = db.cursor()
    try:
        for it in items:
            cur.execute(sql, (
                config_id,
                user_name or "unknown",
                printer_id,
                int(header.get("qty") or 0),
                printed_at,
                gz,

                header.get("pn",""),
                header.get("wo",""),
                header.get("rev",""),
                header.get("ecn",""),

                it["template"],
                it.get("printer_name",""),

                it.get("copies_per_unit"),
                it.get("total_copies"),
                it.get("printed_copies"),

                json.dumps(it.get("lines") or [], ensure_ascii=False),
                source
            ))
        db.commit()
    finally:
        try: cur.close()
        except Exception: pass

# ===== Riport oldal ===========================================================

@bartender_bp.route("/<lang>/bartender/reports")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def reports_page(lang):
    if lang not in ("hu","sk"):
        abort(404)
    return render_template(f"{lang}/bartender_reports.html", user=session.get("user"))

# ===== API: részletes sorok ===================================================

@bartender_bp.get("/api/bartender/print_log")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def api_print_log():
    """Részletes napló lekérdezése szűrőkkel + pagináció."""
    q = request.args

    pn       = (q.get("pn") or "").strip()
    wo       = (q.get("wo") or "").strip()
    rev      = (q.get("rev") or "").strip()
    user     = (q.get("user") or "").strip()
    printer  = (q.get("printer") or "").strip()
    template = (q.get("template") or "").strip()
    source   = (q.get("source") or "").strip()  # 'all' | 'selected' | 'reprint'
    date_from = (q.get("date_from") or "").strip()  # YYYY-MM-DD
    date_to   = (q.get("date_to")   or "").strip()  # YYYY-MM-DD
    limit     = int(q.get("limit") or 100)
    offset    = int(q.get("offset") or 0)

    where = []
    args  = []

    def add(cond, val, op="="):
        if val:
            where.append(f"{cond} {op} %s")
            args.append(val)

    add("pn", pn)
    add("wo", wo)
    add("rev", rev)
    add("user_name", user)
    add("printer_name", printer)
    add("template", template)
    add("source", source)

    if date_from:
        where.append("printed_at >= %s")
        args.append(f"{date_from} 00:00:00")
    if date_to:
        where.append("printed_at <= %s")
        args.append(f"{date_to} 23:59:59")

    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT id, user_name, pn, wo, rev, template, printer_name,
               copies_per_unit, total_copies, printed_copies,
               source, printed_at, lines_json
        FROM print_log
        {sql_where}
        ORDER BY printed_at DESC, id DESC
        LIMIT %s OFFSET %s
    """
    args2 = args + [limit, offset]

    cnt_sql = f"SELECT COUNT(*) FROM print_log {sql_where}"

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(cnt_sql, tuple(args))
        total = cur.fetchone()["COUNT(*)"]

        cur.execute(sql, tuple(args2))
        rows = cur.fetchall() or []

        # kicsi normalizálás
        for r in rows:
            try:
                r["lines"] = json.loads(r.get("lines_json") or "[]")
            except Exception:
                r["lines"] = []
            r.pop("lines_json", None)

        return jsonify({"total": total, "items": rows}), 200
    finally:
        cur.close()

# ===== API: összesítő =========================================================

@bartender_bp.get("/api/bartender/print_log/summary")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def api_print_log_summary():
    """
    Összesítés (sum printed_copies, job darabszám) user/printer/template szerint.
    Paraméter: group_by = user|printer|template|pn|wo (alap: user)
    """
    q = request.args
    group_by = (q.get("group_by") or "user").lower()
    valid = {
        "user": "user_name",
        "printer": "printer_name",
        "template": "template",
        "pn": "pn",
        "wo": "wo",
    }
    col = valid.get(group_by, "user_name")

    # opcionális időszűkítés
    date_from = (q.get("date_from") or "").strip()
    date_to   = (q.get("date_to")   or "").strip()

    where = []
    args = []
    if date_from:
        where.append("printed_at >= %s")
        args.append(f"{date_from} 00:00:00")
    if date_to:
        where.append("printed_at <= %s")
        args.append(f"{date_to} 23:59:59")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
      SELECT {col} AS grp,
             SUM(printed_copies) AS labels,
             COUNT(*) AS jobs,
             SUM(CASE WHEN source='reprint' THEN printed_copies ELSE 0 END) AS labels_reprint
      FROM print_log
      {sql_where}
      GROUP BY {col}
      ORDER BY labels DESC
      LIMIT 500
    """

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(sql, tuple(args))
        return jsonify({"group_by": group_by, "rows": cur.fetchall() or []}), 200
    finally:
        cur.close()

# ===== API: CSV export ========================================================

@bartender_bp.get("/api/bartender/print_log/export")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE)
def api_print_log_export():
    """CSV export a részletes listából (ugyanazok a szűrők, mint /print_log)."""
    from flask import Response
    # újra felhasználjuk a fenti lekérdezés szűrőit
    # (egyszerűség: limit nélkül, de max 50k sor)
    q = request.args.to_dict()
    q["limit"] = "50000"
    q["offset"] = "0"
    with bartender_bp.test_request_context(query_string=q):
        resp = api_print_log()
    data, status = resp
    if status != 200: return resp

    items = data.get_json()["items"]
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["printed_at","user","pn","wo","rev","template","printer","per_unit","total","printed","source"])
    for r in items:
        w.writerow([r["printed_at"], r["user_name"], r["pn"], r["wo"], r["rev"],
                    r["template"], r["printer_name"], r["copies_per_unit"],
                    r["total_copies"], r["printed_copies"], r["source"]])
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=print_log.csv"}
    )

