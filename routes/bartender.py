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

from pathlib import Path
import os, json, tempfile
from datetime import datetime

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

MAX_SPECIAL_LINES = 8


def _extra_line_keys(x: dict) -> dict:
    """A 3. sortól felfelé lévő textN/selN kulcsok átvezetése mentéskor.

    A szerkesztő ma csak text1/text2-t küld; a többsoros From/To címkékhez
    text3..textN kell. Ami jön, azt megőrizzük, ami nem, arról nem gyártunk
    üres kulcsot – így a régi és az új mentés is olvasható marad.
    """
    out = {}
    for i in range(3, MAX_SPECIAL_LINES + 1):
        for pref in ("text", "sel"):
            k = f"{pref}{i}"
            v = (x.get(k) or "").strip()
            if v:
                out[k] = v
    return out
# egyszerűsített heurisztika – testre szabhatod regexekkel
_RE_ID   = re.compile(r"\bID\b|\bID\s*Label\b", re.I)
_RE_FROM = re.compile(r"\bFrom\b", re.I)
_RE_TO   = re.compile(r"\bTo\b", re.I)
_RE_1L  = re.compile(r"\b1L\b.*\.btw$", re.I)
_RE_FT_GENERIC = re.compile(r"\bDAT-16-607\b.*\b2L\b.*\.btw$", re.I)
_RE_TMT50195_2L = re.compile(r"^TMT50195\s+2L\.btw$", re.I)
# A DAT-34 3L From/To mindkét névsorrendben ugyanaz a sablon:
#   "... From 3L.btw"  és  "... 3L From (2-1).btw"
_RE_DAT34_FROM_3L = re.compile(
    r"9320-5807\s*\(DAT-34\)\s*(?:From\s*3L|3L\s*From(?:\s*\([^)]*\))?)\.btw$", re.I)
_RE_DAT34_TO_3L   = re.compile(
    r"9320-5807\s*\(DAT-34\)\s*(?:To\s*3L|3L\s*To(?:\s*\([^)]*\))?)\.btw$", re.I)
# A VARIAN ID címke bármelyik gyártmánynál ID1..ID4-et vár.
_RE_VARIAN_ID4L = re.compile(r"\bID\s*Label\s*VARIAN\s*4L\.btw$", re.I)
# Sima "<gyártmány> 2L.btw" (nincs benne From/To/ID Label): 2 soros ID címke.
# A 3 sorosokra a PN/WO/REV pont ráillik, kettőre nem – ezért csak a 2L kap ID1/ID2-t.
_RE_PLAIN_2L = re.compile(r"\b2L\.btw$", re.I)
# "From 3L" / "3L From (2-1)" – mindkét névsorrendből kiolvassuk a sorok számát.
_RE_FROM_NL = re.compile(r"(?:\bFrom\s*(\d+)\s*L\b|\b(\d+)\s*L\s*From\b)", re.I)
_RE_TO_NL   = re.compile(r"(?:\bTo\s*(\d+)\s*L\b|\b(\d+)\s*L\s*To\b)", re.I)
_RE_DAT34_2L_AS_FROM = re.compile(r"^9320-5807\s*\(DAT-34\)\s*2L\.btw$", re.I)
_RE_TMT20045_DAT39_2L_AS_FROM = re.compile(r"^TMT-20045\s*\(DAT-39\)\s*2L\.btw$", re.I)




# =============================================================================
# Mezőnév-tábla – EGY forrás a sanitize / remap / kártya / summary ágaknak
# =============================================================================

def _lines_in_name(template_name: str, pat: re.Pattern) -> int | None:
    """Hány soros a címke a fájlnév szerint (pl. "From 4L" -> 4)."""
    m = pat.search(template_name)
    if not m:
        return None
    for g in m.groups():
        if g:
            return int(g)
    return None


def _from_field_names(template_name: str, n: int) -> list[str]:
    """FROM oldal mezőnevei n soros címkéhez."""
    # DAT-34 3L kivétel: a második alsó sor neve FROM_UP_2, nem FROM_DOWN_2.
    if _RE_DAT34_FROM_3L.search(template_name):
        return ["FROM_UP", "FROM_DOWN_1", "FROM_UP_2"]
    if n <= 2:
        return ["FROM_UP", "FROM_DOWN"]
    # 3L: FROM_UP, FROM_DOWN_1, FROM_DOWN_2 | 4L: +FROM_DOWN_3 | 5L: +FROM_DOWN_4
    return ["FROM_UP"] + [f"FROM_DOWN_{i}" for i in range(1, n)]


def _to_field_names(template_name: str, n: int) -> list[str]:
    """TO oldal mezőnevei n soros címkéhez (a FROM tükörképe)."""
    if n <= 2:
        return ["TO_UP", "TO_DOWN"]
    # 3L: TO_UP_1, TO_UP_2, TO_DOWN | 4L: +TO_UP_3 | 5L: +TO_UP_4
    return [f"TO_UP_{i}" for i in range(1, n)] + ["TO_DOWN"]


def _field_names_for(template_name: str) -> list[str] | None:
    """A sablon végleges NamedSubString nevei, a címkén látható sorrendben.
    None = ID / FT / alapeset, azt a PN/WO/REV ág kezeli."""
    side = _side_of_template(template_name)
    if side == "con":
        return ["CON"]
    if side == "id2":
        return ["ID1", "ID2"]
    if side not in ("from", "to"):
        return None

    # Kombinált "From - To" címke: nem tudjuk, hány mezője van -> marad a régi 2 mező.
    combined = bool(_RE_FROM.search(template_name) and _RE_TO.search(template_name))
    if side == "from":
        n = 2 if combined else (_lines_in_name(template_name, _RE_FROM_NL) or 2)
        return _from_field_names(template_name, n)
    n = 2 if combined else (_lines_in_name(template_name, _RE_TO_NL) or 2)
    return _to_field_names(template_name, n)


def _normalize_ft_fields(template_name: str, fields: dict[str, str]) -> dict[str, str]:
    """A meglévő mezőket a sablon nevei alá rendezi.

    A régi kód mindenhol FROM_UP/FROM_DOWN (TO_UP/TO_DOWN) párt gyárt; ezeket
    a többsoros sablonoknál a helyükre tesszük, hogy ne vesszenek el a szűrésnél.
    """
    names = _field_names_for(template_name)
    if not names:
        return dict(fields or {})
    f = {k: (v or "") for k, v in (fields or {}).items()}
    out = {k: f.get(k, "") for k in names}

    if names == ["CON"]:
        if not out["CON"]:
            out["CON"] = f.get("LINE1", "")
        return out

    # örökölt párnevek: az UP az első, a DOWN a FROM-nál a második, a TO-nál az utolsó slot
    if names[0].startswith("FROM"):
        if not out[names[0]]:
            out[names[0]] = f.get("FROM_UP", "")
        if len(names) > 1 and not out[names[1]]:
            out[names[1]] = f.get("FROM_DOWN", "")
    elif names[0].startswith("TO"):
        if not out[names[0]]:
            out[names[0]] = f.get("TO_UP", "")
        if not out[names[-1]]:
            out[names[-1]] = f.get("TO_DOWN", "")
    return out


def _fields_from_lines(template_name: str, lines: list[str]) -> dict[str, str]:
    """Sorok -> mezőnevek pozíció szerint (1. sor az első mezőbe, stb.)."""
    names = _field_names_for(template_name)
    if not names:
        return {}
    l = [str(x or "").strip() for x in (lines or [])]
    return {k: (l[i] if i < len(l) else "") for i, k in enumerate(names)}


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

# A többsoros FROM/TO mezők megjelenítési sorrendje sablonnév nélkül is.
_FT_DISPLAY_ORDER = [
    "FROM_UP", "FROM_DOWN", "FROM_DOWN_1", "FROM_UP_2",
    "FROM_DOWN_2", "FROM_DOWN_3", "FROM_DOWN_4",
    "TO_UP", "TO_UP_1", "TO_UP_2", "TO_UP_3", "TO_UP_4", "TO_DOWN",
]


def _preview_lines_from_fields(fields: dict[str, str], template_name: str | None = None) -> list[str]:
    # Prioritás: mezőnév-tábla → L1..L4 → ID1..ID4 → FROM/TO → PN/WO/REV
    if template_name:
        names = _field_names_for(template_name)
        if names:
            vals = [str(fields.get(k, "") or "").strip() for k in names]
            if any(vals):
                return [v for v in vals] if names != ["CON"] else vals[:1]

    out = []
    for k in ("L1", "L2", "L3", "L4"):
        if k in fields and str(fields[k]).strip():
            out.append(str(fields[k]).strip())
    if out:
        return out

    out = []
    for k in ("ID1", "ID2", "ID3", "ID4"):
        if k in fields and str(fields[k]).strip():
            out.append(str(fields[k]).strip())
    if out:
        return out

    ft = [str(fields.get(k, "") or "").strip() for k in _FT_DISPLAY_ORDER if k in fields]
    if any(ft):
        return ft
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

    _ft_names = _field_names_for(template_name) or []

    def _ft_filled() -> bool:
        return any(str(fields.get(k) or "").strip() for k in _ft_names)

    if is_from and not _ft_filled():
        tf = None
        if isinstance(row, dict):
            tf = row.get("text_from")
        up, dn = _coerce_two_lines(tf)
        if not (up or dn):
            up, dn = f"{pn}", f"ISSUE {rev}"
        # a sablon saját mezőnevei alá (3L+ címkéknél nem FROM_UP/FROM_DOWN)
        fields.update(_ft_fields_for_template(template_name, up, dn))

    if is_to and not _ft_filled():
        tt = None
        if isinstance(row, dict):
            tt = row.get("text_to")
        up, dn = _coerce_two_lines(tt)
        if not (up or dn):
            up, dn = f"{pn}", f"WO {wo}"
        fields.update(_ft_fields_for_template(template_name, up, dn))

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


def _DAT8_3l_fields(pn: str, wo: str, rev: str) -> Dict[str, str]:
    # PN/WO/REV: ezt várják a legtöbb BTW-k
    fields = {"PN": pn, "WO": f"SVK{wo}", "REV": rev}
    # kompat: L1..L3-t is küldjük
    fields.update({"L1": f"SVK{wo}", "L2": pn, "L3": f"ISSUE {rev}"})
    return fields

def _DAT8_4l_fields(pn: str, wo: str, rev: str) -> Dict[str, str]:
    fields = {"PN": pn, "WO": f"SVK{wo}", "REV": rev}
    fields.update({"L1": f"SVK{wo}", "L2": f"SVK{wo}", "L3": pn, "L4": f"ISSUE {rev}"})
    return fields


def _TMT061_VAR_ID4L_fields(pn: str, wo: str, rev: str) -> Dict[str, str]:
    return {
        "ID1": f"SVK{wo}",
        "ID2": f"SVK{wo}",
        "ID3": pn,
        "ID4": f"ISSUE {rev}",

        # preview kompat
        "L1": f"SVK{wo}",
        "L2": f"SVK{wo}",
        "L3": pn,
        "L4": f"ISSUE {rev}",
    }


TEMPLATE_RULES: List[Tuple[re.Pattern, Callable[[str, str, str], Dict[str, str]]]] = [
    # --- VARIAN special: ID1..ID4 (bármelyik gyártmánynál) ---
    (_RE_VARIAN_ID4L, _TMT061_VAR_ID4L_fields),

    (re.compile(r"9320-5807 \(DAT-34\) ID Label 4L\.btw$", re.I), _DAT34_4l_fields),
    (re.compile(r"9320-5807 \(DAT-34\) ID Label 3L\.btw$", re.I), _DAT34_3l_fields),
    (re.compile(r"TMT093 \(DAT-37\) ID Label 3L\.btw$", re.I), _DAT37_3l_fields),
    (re.compile(r"TMT093 \(DAT-37\) ID Label 4L\.btw$", re.I), _DAT37_4l_fields),

    # --- DAT-8 általános ---
    (re.compile(r"ID Label\s*3L\.btw$", re.I), _DAT8_3l_fields),
    (re.compile(r"ID Label\s*4L\.btw$", re.I), _DAT8_4l_fields),
]



def build_substrings_for(template_name: str, pn: str, wo: str, rev: str) -> Dict[str, str]:
    """Template fájlnév alapján kiválasztjuk a mező-kiosztást (ID címkékhez)."""
    for pat, fn in TEMPLATE_RULES:
        if pat.search(template_name):
            return fn(pn, wo, rev)
    # 2 soros ID címke: a 3 sorosból az alsó két sor (SVK{wo} nélkül)
    if _side_of_template(template_name) == "id2":
        return {"ID1": pn, "ID2": f"ISSUE {rev}"}
    return _default_fields(pn, wo, rev)


def _sanitize_substrings_for_template(template_name: str, fields: dict[str, str]) -> dict[str, str]:
    # --- VARIAN ID1..ID4 ---
    if _RE_VARIAN_ID4L.search(template_name):
        allow = {"ID1", "ID2", "ID3", "ID4"}
        return {k: v for k, v in (fields or {}).items() if k in allow}

    # --- FROM / TO / CON / 2 soros ID: a mezőnév-tábla dönt ---
    names = _field_names_for(template_name)
    if names:
        return _normalize_ft_fields(template_name, fields)

    side = _side_of_template(template_name)  # "id" | "ft" | None
    if side == "ft":  # generikus FROM-TO
        allow = {"FROM_UP", "FROM_DOWN", "TO_UP", "TO_DOWN"}
    else:
        allow = {"PN", "WO", "REV"}

    return {k: v for k, v in (fields or {}).items() if k in allow}



# --- FROM/TO segédek ---------------------------------------------------------

def _split_two_lines_legacy(s: str|None) -> tuple[str, str]:
    """
    (MEGTARTOTT RÉGI VÁLTOZAT – ha kell máshol)
    """
    if not s:
        return "", ""
    parts = [ln.strip() for ln in str(s).splitlines() if ln.strip()]
    up = parts[0] if len(parts) >= 1 else ""
    down = parts[1] if len(parts) >= 2 else ""
    return up, down

def _ft_fields_for_template(template_name: str, up: str, down: str) -> Dict[str, str]:
    """A két ismert sort (up/down) a sablon mezőneveire képezi.
    A 3. sortól a mezők üresek – oda a summary ma nem ad adatot."""
    if _RE_FT_GENERIC.search(template_name):
        return {"FROM_UP": up, "FROM_DOWN": down, "TO_UP": up, "TO_DOWN": down}

    names = _field_names_for(template_name)
    if not names or names == ["CON"]:
        return {}

    out = {k: "" for k in names}
    out[names[0]] = up
    # FROM: a második mező az alsó sor; TO: az utolsó (TO_DOWN).
    out[names[1] if names[0].startswith("FROM") else names[-1]] = down
    return out


# =============================================================================
# Oldalak
# =============================================================================

@bartender_bp.route("/<lang>/bartender/print")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def print_page(lang):
    if lang not in ("hu", "sk"):
        abort(404)
    return render_template(f"{lang}/bartender_print_page.html", user=session.get("user"))

@bartender_bp.route("/<lang>/bartender/edit_v1")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def edit_v1(lang):
    if lang not in ("hu", "sk", "en"):
        abort(404)
    return render_template(f"{lang}/bartender_edit_page_V1.html", user=session.get("user"))

@bartender_bp.route("/<lang>/bartender/archive_v1")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def archive_v1(lang):
    """
    ARCHÍV NÉZET: egy bartender_archive snapshot megjelenítése (csak olvasható).
    Query param: ?autopen=1&mode=edit&pn=..&rev=..&ecn=..&archive_id=<id>
    """
    if lang not in ("hu", "sk", "en"):
        abort(404)
    return render_template(f"{lang}/bartender_archive_view_V1.html", user=session.get("user"))

@bartender_bp.route("/<lang>/bartender/preview")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE, PRINTOPERATOR_ROLE)
def preview_page(lang):
    if lang not in ("hu", "sk"):
        abort(404)
    return render_template(f"{lang}/bartender_preview.html",
                           user=session.get("user"),
                           lang=lang,
                           other_lang=("sk" if lang == "hu" else "hu"))

# =============================================================================
# Nyomtatás / Preview (összegzés) – RÉGI LOGIKA
# =============================================================================

def _fields_from_card_lines(template_name: str, lines: list[str], pn: str, wo: str, rev: str) -> dict[str, str]:
    """
    Kifejezetten a kártya Print-hez: a kártyán látható 'lines' → NamedSubString-ek.
    Nem okoskodik – azt küldi, ami a kártyán volt (CON/FROM/TO), ID esetben PN/WO/REV-et ad.
    """
    side = _side_of_template(template_name)  # "con" | "from" | "to" | "ft" | "id" | None
    l = [str(x or "").strip() for x in (lines or [])]

    if side == "ft":  # generikus FROM-TO: mindkét oldal ugyanazt kapja
        up = l[0] if len(l) > 0 else ""
        dn = l[1] if len(l) > 1 else ""
        return {"FROM_UP": up, "FROM_DOWN": dn, "TO_UP": up, "TO_DOWN": dn}

    # CON / FROM / TO / 2 soros ID: annyi sort veszünk át, ahány mezője van
    if _field_names_for(template_name):
        return _fields_from_lines(template_name, l)

    # ID / default
    return build_substrings_for(template_name, pn, wo, rev)



def _remaining_allowed_for_card(pn: str, rev: str, ecn: str, card_key: str):
    """
    Hány darab nyomtatható még erről a kártyáról.
    None  -> nincs beállított maximum (nem korlátozunk),
    int   -> a hátralévő darabszám (0 = elfogyott).
    Hiba esetén None, hogy egy DB gond soha ne akadályozza a nyomtatást.
    """
    if not (pn and card_key):
        return None
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute("""
                SELECT printed, total_allowed
                FROM bartender_printed_counter
                WHERE pn=%s AND rev=%s AND ecn=%s AND card_key=%s
                LIMIT 1
            """, (pn, rev or "", ecn or "", card_key))
            row = cur.fetchone()
        finally:
            try: cur.close()
            except Exception: pass
    except Exception:
        current_app.logger.exception("_remaining_allowed_for_card lekérdezés sikertelen")
        return None

    if not row:
        return None
    total = int(row.get("total_allowed") or 0)
    if total <= 0:
        return None  # nincs beállított maximum
    printed = int(row.get("printed") or 0)
    return max(0, total - printed)


@bartender_bp.route("/api/bartender/print", methods=["POST"])
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_print():
    """
    Két üzemmód:

    A) Normál (összes/batch): ha NINCS only_template → a summary/id/connector/special alapján
       csoportosított XML megy (eddigi viselkedés).

    B) Kártyánkénti: ha VAN only_template → CSAK EGY sablon megy ki.
       Opcionálisan:
         - only_copies: int   → hány példány
         - only_fields: JSON  → már kész NamedSubString-ek (pl. {"PN":"...", "WO":"...", "REV":"..."})
         - only_lines:  JSON  → nyers sorok (pl. ["line1","line2","..."]), backend map-pel mezőre

       Ilyenkor az összes többi sablon ignorálva lesz.
    """
    # ---- 0) kötelező input string (PN/WO/REV/QTY…) ---------------------------
    text = (request.form.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Nincs szöveg."}), 400

    # ---- DRY-RUN kapcsoló (globális config + kérésben felülírható) ----------
    cfg_dry = bool(current_app.config.get("BT_DRY_RUN", False))
    form_dry = (request.form.get("dry_run") or "").strip().lower() in ("1", "true", "yes", "on")
    DRY_RUN = cfg_dry or form_dry

    # ---- 1) quick-parse a headerből -----------------------------------------
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

    # ---- 2) KÁRTYÁNKÉNTI ÜZEMMÓD (early exit) -------------------------------
    only_tpl = (request.form.get("only_template") or "").strip()
    if only_tpl:
        # példányszám
        try:
            only_copies = int(request.form.get("only_copies") or "0")
        except Exception:
            only_copies = 0

        # ✅ MAXIMUM VÉDELEM: sima nyomtatásnál nem mehetünk a megengedett
        # darabszám fölé. Az újranyomtatás (reprint=1) szándékos, azt nem
        # korlátozzuk, és a "printed" számlálóba sem számít bele.
        _is_reprint = (request.form.get("reprint") or "") == "1"
        _card_key = (request.form.get("card_key") or "").strip()
        if not _is_reprint and _card_key:
            _remain = _remaining_allowed_for_card(pn, rev, ecn, _card_key)
            if _remain is not None:
                if _remain <= 0:
                    return jsonify({
                        "error": "Ebből a kártyából már nincs nyomtatható darab "
                                 "(a maximum elfogyott)."
                    }), 400
                if only_copies > _remain:
                    return jsonify({
                        "error": f"Túl sok példány. Ebből a kártyából még {_remain} nyomtatható."
                    }), 400

        # opcionális: kész NamedSubString-ek
        only_fields = None
        try:
            raw = request.form.get("only_fields")
            if raw:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    only_fields = {str(k): str(v) for k, v in obj.items()}
        except Exception:
            only_fields = None

        # opcionális: nyers sorok (ha only_fields nincs, ebből képezünk)
        only_lines = []
        try:
            raw = request.form.get("only_lines")
            if raw:
                arr = json.loads(raw)
                if isinstance(arr, list):
                    only_lines = [str(x or "") for x in arr]
        except Exception:
            only_lines = []

        try:
            db = get_db()
            # PN rekord + fallback FROM/TO
            try:
                row = fetch_label_row_by_pn(db, pn, rev, ecn)  # type: ignore[arg-type]
            except TypeError:
                row = fetch_label_row_by_pn(db, pn)
            if not row:
                return jsonify({"error": "A megadott PN/REV/ECN nincs az 'etiket data' táblában."}), 404
            row = _row_with_from_to(db, row if isinstance(row, dict) else {}, pn)

            # printer meghatározása ehhez az egy sablonhoz
            pn_printers_json = None
            try:
                pn_printers_json = json.loads(row["printers"]) if row.get("printers") else None
            except Exception:
                pass
            printer = _resolve_printer_for_template(db, only_tpl, pn_printers_json)
            if not printer:
                return jsonify({"error": f"Nincs nyomtató a sablonra: {only_tpl}"}), 400

            # qty feloldása (ha kell)
            if qty is None:
                qty = fetch_qty_for_wo(db, pn, wo)
                if qty is None:
                    return jsonify({"error": "QTY nem található a WO alapján."}), 404

            # végső példányszám
            copies = int(only_copies or 0)
            if copies <= 0:
                copies = 1 * int(qty)

            # NamedSubString-ek összeállítása:
            if only_fields and isinstance(only_fields, dict):
                substr = _sanitize_substrings_for_template(only_tpl, only_fields)
            elif only_lines:
                substr = _fields_from_card_lines(only_tpl, only_lines, pn, wo, rev)
            else:
                summ_fields = _fields_from_summary_for_template(only_tpl, row.get("summary"))
                substr = summ_fields or build_substrings_for(only_tpl, pn, wo, rev)
                substr = _sanitize_substrings_for_template(only_tpl, substr)

            by_printer = {
                printer: [{
                    "template": only_tpl,
                    "copies": copies,
                    "substrings": substr
                }]
            }

            # XML
            xml_text = (
                build_grouped_print_xml(*_filter_grouped_for_output(by_printer))[0]
                if isinstance(build_grouped_print_xml, tuple)
                else build_grouped_print_xml(by_printer)
            )

            # >>> DRY-RUN őr: csak logolunk, nem küldjük ki az XML-t
            if DRY_RUN:
                current_app.logger.info(
                    "[BT DRY-RUN] XML NEM megy ki (single). printer=%s tpl=%s copies=%s\n%s",
                    printer, only_tpl, copies, xml_text
                )
            else:
                write_print_xml(xml_text)

            # napló egyetlen tétellel
            lines_for_log = _preview_lines_from_fields(substr, only_tpl)
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
                items=[{
                    "template": only_tpl,
                    "printer_name": printer,
                    "copies_per_unit": 1,
                    "total_copies": copies,
                    "printed_copies": copies,
                    "lines": lines_for_log
                }],
                xml_text=xml_text,
                source=("reprint" if (request.form.get("reprint") or "") == "1" else "selected"),
                config_id=None,
                printer_id=None
            )

            return jsonify({
                "pn": pn, "wo": wo, "rev": rev, "ecn": ecn,
                "qty_sum": int(qty),
                "printed_templates": [only_tpl],
                "printers": {only_tpl: printer},
                "calculated_totals": {only_tpl: copies},
                "is_special": {only_tpl: False},
                "dry_run": DRY_RUN
            }), 200

        except Exception:
            current_app.logger.exception("api_print (only_template) hiba")
            return jsonify({"error": "Szerver hiba nyomtatás közben (kártya)."}), 500

    # ---- 3) Ha NINCS only_template → megy a korábbi, összesítő/batch logika --
    # --------------------------------------------------------------------------
    # --- opcionális “selected” szűrés / override példányszámok ---------------
    selected_raw = request.form.get("selected")
    selected_set = set()
    if selected_raw:
        try:
            arr = json.loads(selected_raw)
            if isinstance(arr, list):
                selected_set = {str(x).strip() for x in arr if str(x).strip()}
        except Exception:
            selected_set = set()

    selected_copies = None
    try:
        sc_raw = request.form.get("selected_copies")
        if sc_raw:
            sc = json.loads(sc_raw)
            if isinstance(sc, dict):
                tmp = {}
                for k, v in sc.items():
                    try:
                        kk = str(k).strip()
                        vv = int(v)
                        if kk and vv > 0:
                            tmp[kk] = vv
                    except Exception:
                        continue
                selected_copies = tmp or None
            else:
                selected_copies = None
    except Exception:
        selected_copies = None

    # --- KÁRTYÁNKÉNTI PRINT OVERRIDE (ha érkezik) -----------------------------
    only_tpl = (request.form.get("only_template") or "").strip()
    if only_tpl:
        selected_set = {only_tpl}
        try:
            only_copies = request.form.get("only_copies")
            if only_copies is not None:
                n = int(only_copies)
                if n > 0:
                    selected_copies = {only_tpl: n}
        except Exception:
            pass

    # alap mezők felbontása (batch-hoz újra)
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

        # biztosítsuk a text_from/text_to jelenlétét
        row = _row_with_from_to(db, row if isinstance(row, dict) else {}, pn)

        # --- sablonszámlálás (FROM/TO + ID) ---------------------------------
        template_counts = template_counts_from_summary_nodup(row.get("summary"))
        id_counts = _id_label_counts_from_summary(row.get("summary"))
        for t, c in (id_counts or {}).items():
            template_counts[t] = template_counts.get(t, 0) + int(c)

        if not template_counts:
            template_counts = expand_templates(row.get("ID_sablon", "[]"))

        conn_map = _connector_labels_map_from_summary(row.get("summary"))

        if selected_set:
            template_counts = {t: c for t, c in (template_counts or {}).items() if t in selected_set}

        if selected_set and not template_counts:
            template_counts = {tpl: 1 for tpl in selected_set}

        if qty is None:
            qty = fetch_qty_for_wo(db, pn, wo)
            if qty is None:
                return jsonify({"error": "QTY nem található a WO alapján."}), 404

        # PN-hez elmentett printers JSON (ha van)
        try:
            pn_printers_json = json.loads(row["printers"]) if row.get("printers") else None
        except Exception:
            pn_printers_json = None

        summ_map = _ft_lines_map_from_summary(row.get("summary") if isinstance(row, dict) else None)

        by_printer: dict[str, list[dict]] = defaultdict(list)
        printers_used: dict[str, str] = {}
        printed_templates: list[str] = []
        fields_per_tpl: dict[str, dict[str, str]] = {}

        # --- NORMÁL TÉTELEK (ID/FROM/TO + CON) ------------------------------
        for template_name, per_unit in (template_counts or {}).items():
            printer = _resolve_printer_for_template(db, template_name, pn_printers_json)
            if not printer:
                return jsonify({"error": f"Nincs nyomtató a sablonra: {template_name}"}), 400

            side = _side_of_template(template_name)

            # -- CON oldal (1L) --
            if side == "con":
                items = conn_map.get(template_name) or []
                items = [it for it in items if (it.get("value") or "").strip()]

                if isinstance(selected_copies, dict) and selected_copies.get(template_name, 0) > 0:
                    desired   = int(selected_copies[template_name])
                    max_items = max(0, min(len(items), desired // max(1, int(qty))))
                    items     = items[:max_items]

                for it in items:
                    val = (it.get("value") or "")
                    fields = {"CON": val}
                    fields = _sanitize_substrings_for_template(template_name, fields)
                    fields_per_tpl[template_name] = fields

                    by_printer[printer].append({
                        "template": template_name,
                        "copies": int(qty),
                        "substrings": fields
                    })
                if items:
                    printers_used[template_name] = printer
                    printed_templates.append(template_name)
                continue

            # --- FROM/TO/ID normál logika ---
            sm = summ_map.get(template_name) if 'summ_map' in locals() else None
            if sm and sm.get("side") in ("FROM", "TO"):
                _sm_lines = [str(x or "").strip() for x in (sm.get("lines") or [])]
                if len([x for x in _sm_lines if x]) > 2:
                    fields = _fields_from_lines(template_name, _sm_lines)
                else:
                    fields = _ft_fields_for_template(
                        template_name, sm.get("up", ""), sm.get("down", ""))
            else:
                if _RE_FROM.search(template_name):
                    f_up, f_dn = _split_two_lines(row.get("text_from") if isinstance(row, dict) else None)
                    fields = _ft_fields_for_template(template_name, f_up, f_dn)
                elif _RE_TO.search(template_name):
                    t_up, t_dn = _split_two_lines(row.get("text_to") if isinstance(row, dict) else None)
                    fields = _ft_fields_for_template(template_name, t_up, t_dn)
                else:
                    fields = build_substrings_for(template_name, pn, wo, rev)

            fields = _sanitize_substrings_for_template(template_name, fields)
            fields = {k: v for k, v in (fields or {}).items()
                      if not (isinstance(k, str) and k.upper().startswith("L"))}
            fields_per_tpl[template_name] = fields

            per_unit = int(per_unit)
            total = per_unit * int(qty)

            if isinstance(selected_copies, dict) and selected_copies.get(template_name, 0) > 0:
                desired = int(selected_copies[template_name])
                copies = max(0, min(desired, total))
            else:
                copies = total

            if copies > 0:
                by_printer[printer].append({
                    "template": template_name,
                    "copies": copies,
                    "substrings": fields
                })
                printers_used[template_name] = printer
                printed_templates.append(template_name)

        # --- SPECIALS fallback printer meghatározása -------------------------
        default_printer_for_specials = _pick_most_common(list(printers_used.values()))

        # --- SPECIÁLIS ETIKETTEK (prioritás + dedupe) -----------------------
        specials = _collect_specials_for(pn, rev, ecn, row.get("summary"))
        for rec in specials:
            tpl, qtys, lines = rec["template"], int(rec["qty"]), (rec.get("lines") or [])
            if not tpl or qtys <= 0:
                continue
            if selected_set and tpl not in selected_set:
                continue

            printer = _resolve_printer_for_template(db, tpl, pn_printers_json)
            if not printer:
                printer = default_printer_for_specials
            if not printer:
                continue

            side = _side_of_template(tpl)

            if side == "con":
                def _first_non_empty(arr):
                    for s in arr or []:
                        if str(s or "").strip():
                            return str(s).strip()
                    return ""
                val = _first_non_empty(lines) or " ".join([str(s or "").strip() for s in lines if str(s or "").strip()])
                fields = {"CON": val}
            else:
                fields = {f"L{i+1}": (lines[i] if i < len(lines) else "") for i in range(4)}

            fields = _sanitize_substrings_for_template(tpl, fields)

            copies = qtys * int(qty)
            if isinstance(selected_copies, dict) and selected_copies.get(tpl, 0) > 0:
                desired = max(0, int(selected_copies[tpl]))
                copies = min(desired, qtys * int(qty))

            if copies > 0:
                by_printer[printer].append({
                    "template": tpl,
                    "copies": copies,
                    "substrings": fields
                })
                fields_per_tpl[tpl] = fields
                printers_used[tpl] = printer
                printed_templates.append(tpl)

        if not by_printer:
            dbg = {
                "templates": dict(template_counts or {}),
                "qty": int(qty),
                "selected_set": list(selected_set or []),
                "selected_copies": (selected_copies or {}),
                "printers_used_so_far": printers_used,
            }
            current_app.logger.warning("PRINT EMPTY | %s", json.dumps(dbg, ensure_ascii=False))
            return jsonify({"error": "Nincs nyomtatható parancs.", "debug": dbg, "dry_run": DRY_RUN}), 400

        # XML generálás
        by_printer_filtered, skipped = _filter_grouped_for_output(by_printer)
        if not by_printer_filtered:
            return jsonify({
                "error": "Nincs nyomtatható parancs (hiányzó sablon/printer).",
                "skipped": skipped,
                "dry_run": DRY_RUN
            }), 400

        xml_text = build_grouped_print_xml(by_printer_filtered)
        # >>> DRY-RUN őr: csak logolunk
        if DRY_RUN:
            current_app.logger.info("[BT DRY-RUN] XML NEM megy ki (batch).\n%s", xml_text)
        else:
            write_print_xml(xml_text)

        # --- naplózás
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

        return jsonify({
            "pn": pn, "wo": wo, "rev": rev, "ecn": ecn,
            "qty_sum": int(qty),
            "templates": dict(template_counts),
            "calculated_totals": {t: int(c) * int(qty) for t, c in template_counts.items()},
            "printers": printers_used,
            "printed_templates": printed_templates,
            "skipped": skipped,
            "special_labels": specials,
            "is_special": {tpl: (tpl in {rec["template"] for rec in specials}) for tpl in template_counts},
            "dry_run": DRY_RUN
        }), 200

    except Exception as e:
        print("api_print hiba:", repr(e))
        return jsonify({"error": "Szerver hiba nyomtatás közben.", "dry_run": DRY_RUN}), 500




def _dedupe_specials(items: list[dict]) -> list[dict]:
    uniq, out = set(), []
    for raw in items or []:
        it = _norm_special_item(raw if isinstance(raw, dict) else {})
        if not it["template"]:
            continue
        key = (it["template"], tuple(it["lines"]))
        if key in uniq:
            # If this template and lines combination already exists, skip it
            continue
        uniq.add(key)
        out.append(it)
    return out




@bartender_bp.route("/api/bartender/preview", methods=["POST"])
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
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

        # ===== PN/REV/ECN egyezés jelzők (UI-hoz) =====
        req_rev = (rev or "").strip()
        req_ecn = (ecn or "").strip()
        db_rev  = (row.get("REV") or "").strip() if isinstance(row, dict) else ""
        db_ecn  = (row.get("ECN") or "").strip() if isinstance(row, dict) else ""

        # ===== QC JÓVÁHAGYÁS ELLENŐRZÉS =====
        _qc_cur = db.cursor(dictionary=True)
        try:
            _qc_cur.execute(
                "SELECT confirmed_by, confirmed_at, revoked_by, revoked_at "
                "FROM bartender_qc_confirmations "
                "WHERE pn=%s AND rev=%s AND ecn=%s LIMIT 1",
                (pn, db_rev or req_rev or "", db_ecn or req_ecn or "")
            )
            _qc_row = _qc_cur.fetchone()
        except Exception:
            _qc_row = None
        finally:
            try: _qc_cur.close()
            except Exception: pass

        if _qc_row and _qc_row.get("confirmed_by") and not _qc_row.get("revoked_by"):
            _qc_blocked     = False
            _qc_confirmed_by = str(_qc_row["confirmed_by"])
            _qc_revoked_by   = ""
        else:
            _qc_blocked      = True
            _qc_confirmed_by = ""
            _qc_revoked_by   = str((_qc_row or {}).get("revoked_by") or "") if _qc_row else ""

        # TESZT MÓD: a QC-blokk nem szakítja meg az előnézetet (emailt sem küldünk,
        # azt a notify_* végpontok külön kezelik)
        _test_mode = _bt_test_mode_enabled()
        if _test_mode and _qc_blocked:
            _qc_blocked = False
        matched = {
            "pn": True,
            "rev": (not req_rev) or (db_rev and db_rev == req_rev),
            "ecn": (not req_ecn) or (db_ecn and db_ecn == req_ecn),
        }

        # --- FROM/TO számlálás + ✨ ID label-ek összeadása (duplázás nélkül) ---
        template_counts = template_counts_from_summary_nodup(row.get("summary"))
        id_counts = _id_label_counts_from_summary(row.get("summary"))
        for t, c in (id_counts or {}).items():
            template_counts[t] = template_counts.get(t, 0) + int(c)

        # ha így sincs semmi, essünk vissza a régi ID_sablon listára
        if not template_counts:
            template_counts = expand_templates(row.get("ID_sablon", "[]"))

        # --- konnektor sablonok hozzáadása (darab = értékek száma) -------------
        # QC-mód: névtől függetlenül számít (pl. 2L sablon is lehet konnektor
        # etikett), nem csak a CON (1L) nevűek – ld. qc_bartender_core #4 javítás
        conn_map = _connector_labels_map_from_summary(row.get("summary"))
        for tpl, arr in conn_map.items():
            if arr:
                template_counts[tpl] = template_counts.get(tpl, 0) + len(arr)

        qty = fetch_qty_for_wo(db, pn, wo) or 0
        pn_printers_json = json.loads(row["printers"]) if row.get("printers") else None
        printers_resolved = {
            tpl: _resolve_printer_for_template(db, tpl, pn_printers_json) or ""
            for tpl in template_counts.keys()
        }

        # előnézet-sorok minden sablonhoz
        _ft_lines_map = _ft_lines_map_from_summary(row.get("summary"))
        line_preview: dict[str, list[str]] = {}
        for tpl in template_counts.keys():
            side = _side_of_template(tpl)
            _con_vals = [(e.get("value") or "").strip() for e in conn_map.get(tpl, [])]
            _con_vals = [v for v in _con_vals if v]
            if side == "con":
                # QC-mód: CON sablonnál a konnektor-értékek mindig felülírnak
                line_preview[tpl] = _con_vals or [""]
                continue
            if _con_vals and tpl not in _ft_lines_map:
                # QC-mód: konnektor-etikett nem CON nevű sablonnal (pl. 2L) –
                # ha nincs FROM/TO sora, a konnektor értékeit mutatjuk
                line_preview[tpl] = _con_vals
                continue
            fields = _build_fields_for_preview(tpl, pn, wo, rev, row)
            line_preview[tpl] = _preview_lines_from_fields(fields, tpl)

        # --- SPECIÁLIS ETIKETTEK (prioritás: summary → fallback: DB, dedupe) ---
        specials = _collect_specials_for(pn, rev, ecn, row.get("summary"))
        for rec in specials:
            tpl, qtys, lines = rec["template"], int(rec["qty"]), rec["lines"]
            if not tpl or qtys <= 0:
                continue
            # példányszám hozzáadása (NEM WO-val szorozva)
            template_counts[tpl] = template_counts.get(tpl, 0) + qtys
            # nyomtató + preview sorok
            if tpl not in printers_resolved:
                printers_resolved[tpl] = _resolve_printer_for_template(get_db(), tpl, pn_printers_json) or ""
            if tpl not in line_preview:
                line_preview[tpl] = (lines[:MAX_SPECIAL_LINES] if lines else [""])

        # preview-ból kiszűrjük, amihez nincs printer/sablon
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

        # --- ÚJ: special_labels visszaadása ---
        special_set = {rec["template"] for rec in specials if rec.get("template")}
        is_special = {tpl: (tpl in special_set) for tpl in template_counts.keys()}
        print("\n\n\n===================")
        _debug_dump_specials(pn, wo, rev, ecn, specials)
        print("Processed special labels:", specials)
        print("===================\n\n\n")

        try:
            history_cards = _fetch_history_cards_grouped(
                db, pn, wo,
                rev=(db_rev or rev or "").strip(),
                ecn=(db_ecn or ecn or "").strip(),
                limit=300
            )


            allowed_lines = _allowed_lines_map_from_preview(line_preview, specials)

            history_cards = _filter_history_to_current(
                history_cards,
                templates_effective=templates_effective,
                printers_resolved=printers_resolved,
                allowed_lines=allowed_lines
            )
        except Exception:
            current_app.logger.exception("history_cards build failed")
            history_cards = []

        
        # --- ÚJ: kártyák NEM összevonva (FROM/TO + ID) ---
        # A template_counts összevon sablon szerint, ezért a preview eddig csak 1-1 kártyát mutatott ugyanarra a sablonra.
        # Itt per "előfordulás" listát adunk vissza, hogy a frontend mindent ki tudjon rajzolni.
        cards: list[dict] = []
        try:
            ft_cards = _ft_cards_from_summary(row.get("summary"))
            for c in (ft_cards or []):
                tpl = (c.get("template") or "").strip()
                if not tpl:
                    continue
                cards.append({
                    "id": c.get("id") or ("ft_" + str(len(cards))),
                    "template": tpl,
                    "kind": c.get("kind") or "ft",
                    "side": c.get("side") or "",
                    "lines": c.get("lines") or [],
                    "per_unit": int(c.get("per_unit") or 1),
                    "printer": printers_resolved.get(tpl, ""),
                })

            id_list = _id_label_list_from_summary(row.get("summary"))
            for ii, tpl in enumerate(id_list or []):
                tpl = (tpl or "").strip()
                if not tpl:
                    continue
                cards.append({
                    "id": f"id:{ii}",
                    "template": tpl,
                    "kind": "id",
                    "side": "id",
                    "lines": line_preview.get(tpl, []),
                    "per_unit": 1,
                    "printer": printers_resolved.get(tpl, ""),
                })
        except Exception:
            current_app.logger.exception("cards build failed")
            cards = []

        # --- DEBUG: logold ki mit küldünk a frontendnek (csak ha debug=1) ---
        debug_on = bool((data.get("debug") or False)) or bool(current_app.config.get("BT_PREVIEW_DEBUG", False))
        if debug_on:
            try:
                current_app.logger.info(
                    "api_preview debug pn=%s wo=%s rev=%s qty=%s | template_counts=%s | cards=%s",
                    pn, wo, rev, qty, dict(template_counts), len(cards)
                )
                # a summary-t ne spammeljük végtelenre:
                sm = row.get("summary") or ""
                current_app.logger.info("api_preview summary len=%s head=%s", len(sm), (sm[:800] if isinstance(sm, str) else str(sm)[:800]))
            except Exception:
                pass


        return jsonify({
            "pn": pn,
            "wo": wo,
            "rev": rev,
            "qty_sum": int(qty),

            "templates": dict(template_counts),
            "calculated_totals": {t: int(c) * int(qty) for t, c in template_counts.items()},
            "printers": printers_resolved,
            "line_preview": line_preview,
            "cards": cards,

            "templates_effective": templates_effective,
            "calculated_totals_effective": calculated_totals_effective,
            "skipped": skipped_preview,

            "matched": matched,
            "has_templates_all": bool(template_counts),
            "has_templates_effective": bool(templates_effective),

            # --- ÚJ mezők ---
            "special_labels": specials,      # [{template, qty, lines[]}, ...]
            "is_special": is_special,         # { "<tpl.btw>": true/false }
            "history_cards": history_cards,

            # --- QC JÓVÁHAGYÁS ---
            "qc_blocked":      _qc_blocked,
            "qc_confirmed_by": _qc_confirmed_by,
            "qc_revoked_by":   _qc_revoked_by,

            # --- TESZT MÓD (QC-blokk + auto-email felfüggesztve) ---
            "test_mode": _test_mode,

        }), 200

    except Exception as e:
        print("api_preview hiba:", repr(e))
        return jsonify({"error": "Szerver hiba preview közben."}), 500


@bartender_bp.get("/api/bartender/list_saved_pn")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_list_saved_pn():
    """
    Elmentett PN-ek listája.
    Query:
      q:      szabad szöveges kereső (PN/REV/ECN)
      sort:   updated_desc | updated_asc | pn_asc | pn_desc   (default: updated_desc)
      limit:  max sorok (alap 50, max 200)
      offset: kihagyás (alap 0)
      latest_only: 1/true/yes -> PN-enként csak a legutolsó (MAX(ID))
    Válasz:
      { total: int, items: [ {pn, rev, ecn, updated, updated_by} ... ] }
    """
    q = (request.args.get("q") or "").strip()
    sort = (request.args.get("sort") or "updated_desc").strip().lower()
    latest_only = (request.args.get("latest_only") or "").strip().lower() in ("1", "true", "yes")
    # csak a QC által ellenőrzött (jóváhagyott, nem visszavont) projektek
    qc_only = (request.args.get("qc_only") or "").strip().lower() in ("1", "true", "yes")

    try:
        limit = max(1, min(200, int(request.args.get("limit") or 50)))
    except:
        limit = 50
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except:
        offset = 0

    db = get_db()
    cur = db.cursor(dictionary=True)

    # 🔹 WHERE (t.*-ra prefixelve, hogy JOIN-nál is jó legyen)
    conds = []
    params = []
    if q:
        # zárójelezve, hogy a többi AND-feltétellel ne keveredjen az OR
        conds.append(
            "(t.PN LIKE %s OR t.REV LIKE %s OR t.ECN LIKE %s "
            "OR COALESCE(t.`updated by`, t.`edited by`, '') LIKE %s "
            "OR CAST(COALESCE(t.`updated date`, t.`create date`) AS CHAR) LIKE %s)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    if qc_only:
        # csak azok a PN/REV/ECN-ek, amikre van QC jóváhagyás és nincs visszavonva
        conds.append(
            "EXISTS (SELECT 1 FROM bartender_qc_confirmations c "
            "        WHERE c.pn = t.PN "
            "          AND COALESCE(c.rev,'') = COALESCE(t.REV,'') "
            "          AND COALESCE(c.ecn,'') = COALESCE(t.ECN,'') "
            "          AND c.confirmed_by IS NOT NULL AND c.confirmed_by <> '' "
            "          AND (c.revoked_by IS NULL OR c.revoked_by = ''))"
        )
    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""

    # 🔹 ORDER BY (szintén t.*)
    if sort == "updated_asc":
        order_sql = "ORDER BY COALESCE(t.`updated date`, t.`create date`) ASC, t.PN ASC"
    elif sort == "pn_asc":
        order_sql = "ORDER BY t.PN ASC, COALESCE(t.`updated date`, t.`create date`) DESC"
    elif sort == "pn_desc":
        order_sql = "ORDER BY t.PN DESC, COALESCE(t.`updated date`, t.`create date`) DESC"
    else:
        order_sql = "ORDER BY COALESCE(t.`updated date`, t.`create date`) DESC, t.PN ASC"

    # 🔹 TOTAL
    if latest_only:
        cnt_sql = f"""
            SELECT COUNT(*) AS cnt FROM (
                SELECT t.PN
                FROM `etiket data` t
                JOIN (
                    SELECT PN, MAX(ID) AS max_id
                    FROM `etiket data`
                    GROUP BY PN
                ) m ON m.PN = t.PN AND m.max_id = t.ID
                {where_sql}
                GROUP BY t.PN
            ) x
        """
    else:
        # itt nem kell t alias, mert nincs JOIN, de maradhat egységesen:
        cnt_sql = f"""
            SELECT COUNT(*) AS cnt
            FROM `etiket data` t
            {where_sql}
        """
    cur.execute(cnt_sql, tuple(params))
    total = int((cur.fetchone() or {}).get("cnt", 0))

    # 🔹 ROWS  (‼️ itt volt a hiba: nem kezelted a latest_only-t)
    if latest_only:
        sql = f"""
            SELECT
                t.PN,
                COALESCE(t.REV, '') AS REV,
                COALESCE(t.ECN, '') AS ECN,
                COALESCE(t.`updated date`, t.`create date`) AS updated,
                COALESCE(t.`updated by`, t.`edited by`, '') AS updated_by
            FROM `etiket data` t
            JOIN (
                SELECT PN, MAX(ID) AS max_id
                FROM `etiket data`
                GROUP BY PN
            ) m ON m.PN = t.PN AND m.max_id = t.ID
            {where_sql}
            {order_sql}
            LIMIT %s OFFSET %s
        """
    else:
        sql = f"""
            SELECT
                t.PN,
                COALESCE(t.REV, '') AS REV,
                COALESCE(t.ECN, '') AS ECN,
                COALESCE(t.`updated date`, t.`create date`) AS updated,
                COALESCE(t.`updated by`, t.`edited by`, '') AS updated_by
            FROM `etiket data` t
            {where_sql}
            {order_sql}
            LIMIT %s OFFSET %s
        """

    params2 = params + [limit, offset]
    cur.execute(sql, tuple(params2))
    rows = cur.fetchall() or []
    cur.close()

    items = []
    for r in rows:
        upd = r.get("updated")
        items.append({
            "pn": (r.get("PN") or "").strip(),
            "rev": (r.get("REV") or "").strip(),
            "ecn": (r.get("ECN") or "").strip(),
            "updated": upd.strftime("%Y-%m-%d %H:%M:%S") if getattr(upd, "strftime", None) else str(upd or ""),
            "updated_by": (r.get("updated_by") or "").strip(),
        })

    return jsonify({"total": total, "items": items}), 200


def _debug_dump_specials(pn, wo, rev, ecn, specials):
    # 1) Próbáljuk az instance/ mappát (Flask-hoz ajánlott, írható szokott lenni)
    try:
        base_dir = Path(current_app.instance_path) / "preview_debug"
        base_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # 2) Ha az instance nem írható, essünk vissza a rendszer temp mappára
        base_dir = Path(tempfile.gettempdir()) / "preview_debug"
        base_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_pn = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in pn)
    safe_wo = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in wo)
    fname = f"preview_{safe_pn}_{safe_wo}_{ts}.json"
    fpath = base_dir / fname

    payload = {
        "pn": pn, "wo": wo, "rev": rev, "ecn": ecn,
        "specials": specials,
    }

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # plusz logba is írunk, hogy tudd hova ment
    current_app.logger.info("Specials dumped to %s", str(fpath))

# =============================================================================
# Rendszernyomtatók listája – fix, beégetett
# =============================================================================

@bartender_bp.route("/api/bartender/get_printers")
def get_printers():
    printers = [
        "TOSHIBA B-EX4T1 (305 dpi) TEC3",
        "TOSHIBA B-EX4T1 (305 dpi) TEC2",
        "Toshiba b-ex4t1 NEW",
        "TOSHIBA B-EX4T1 (305 dpi)",
        "TOSHIBA BX430 (600 dpi)",
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
        pn  = (data.get('pn')  or '').strip()
        rev = (data.get('rev') or '').strip()
        ecn = (data.get('ecn') or '').strip()

        if not pn:
            return jsonify({'found': False, 'message': 'Hiányzik a PN'}), 400

        # normalizálás (case + space)
        pn_n  = pn.lower().strip()
        rev_n = rev.lower().strip()
        ecn_n = ecn.lower().strip()

        db = get_db()
        cur = db.cursor(dictionary=True)

        # 1) PN létezik?
        cur.execute("""
            SELECT DISTINCT
              TRIM(CAST(`PROD.REV` AS CHAR)) AS rev,
              TRIM(CAST(`CURRENT ECN` AS CHAR)) AS ecn
            FROM t_dump
            WHERE LOWER(`PART.NBR`) = %s
            LIMIT 200
        """, (pn_n,))
        rows = cur.fetchall() or []
        pn_found = len(rows) > 0

        # ha PN sincs → kész
        if not pn_found:
            cur.close()
            return jsonify({
                'found': False,
                'pn_found': False,
                'rev_found': False,
                'ecn_found': False,
                'message': 'PN nem található (t_dump)'
            }), 200

        # 2) REV egyezés?
        rev_found = True
        if rev_n:
            rev_found = any((r.get('rev') or '').strip().lower() == rev_n for r in rows)

        # 3) ECN egyezés? (csak akkor értelmezett igazán, ha REV is adott)
        ecn_found = True
        if ecn_n:
            if rev_n:
                ecn_found = any(
                    (r.get('rev') or '').strip().lower() == rev_n and
                    (r.get('ecn') or '').strip().lower() == ecn_n
                    for r in rows
                )
            else:
                # ha nincs rev megadva, ECN-t önmagában PN+ECN-re nézzük
                ecn_found = any((r.get('ecn') or '').strip().lower() == ecn_n for r in rows)

        # Összesített found:
        # - ha rev+ecn is meg van adva: mindkettő egyezzen
        # - ha csak rev: rev egyezzen
        # - ha csak ecn: ecn egyezzen
        # - ha semmi: PN elég
        if rev_n and ecn_n:
            found = pn_found and rev_found and ecn_found
        elif rev_n:
            found = pn_found and rev_found
        elif ecn_n:
            found = pn_found and ecn_found
        else:
            found = pn_found

        # segéd: milyen REV/ECN párok vannak a dumpban ehhez a PN-hez (UI-hoz hasznos)
        pairs = []
        seen = set()
        for r in rows:
            rr = (r.get('rev') or '').strip()
            ee = (r.get('ecn') or '').strip()
            key = (rr, ee)
            if key not in seen:
                seen.add(key)
                pairs.append({'rev': rr, 'ecn': ee})

        cur.close()
        return jsonify({
            'found': found,
            'pn_found': pn_found,
            'rev_found': rev_found,
            'ecn_found': ecn_found,
            'pairs': pairs[:50]
        }), 200

    except Exception as e:
        print("PN/REV/ECN ellenőrzési hiba (t_dump):", e)
        return jsonify({'found': False, 'message': 'Szerver hiba történt az ellenőrzéskor.'}), 500


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

        # Lehet dict {pages: [...], connector_labels: [...], special_labels: [...] } vagy régi: csak lista
        if isinstance(parsed, dict):
            pages            = parsed.get("pages") or parsed.get("summary") or []
            connector_labels = parsed.get("connector_labels") or []
            special_labels   = parsed.get("special_labels") or []   # ← ÚJ
        else:
            pages = parsed
            connector_labels = []
            special_labels   = []                                    # ← ÚJ

        def norm_text_block(x):
            if not isinstance(x, dict):
                return {"template":"", "text1":"", "text2":""}
            return {
                "template": (x.get("template") or "").strip(),
                "text1":    (x.get("text1") or "").strip(),
                "text2":    (x.get("text2") or "").strip(),
                **_extra_line_keys(x),
            }

        def norm_ft_item(x):
            if not isinstance(x, dict):
                return {"template":"", "sel1":"", "sel2":"", "text1":"", "text2":""}
            return {
                "template": (x.get("template") or "").strip(),
                "sel1":     (x.get("sel1") or "").strip(),
                "sel2":     (x.get("sel2") or "").strip(),
                "text1":    (x.get("text1") or "").strip(),
                "text2":    (x.get("text2") or "").strip(),
                **_extra_line_keys(x),
            }

        # Speciális etikett rekord normalizálása (template, qty, lines[])
        def norm_special(x):
            if not isinstance(x, dict):
                return {"template":"", "qty":1, "lines":[]}
            tpl  = (x.get("template") or "").strip()
            qty  = int(x.get("qty") or 1)
            lines = x.get("lines")
            if not isinstance(lines, list):
                lines = []
            lines = [(str(s) if s is not None else "").strip() for s in lines]
            return {"template": tpl, "qty": max(1, qty), "lines": lines}

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
                        cid  = (c.get("id") or "").strip()
                        cval = (c.get("value") or "").strip()
                        if cid:
                            connectors_out.append({"id": cid, "value": cval})

                pairs_out = []
                for pr in (g.get("pairs") or []):
                    if not isinstance(pr, dict):
                        continue

                    frm = norm_text_block(pr.get("from"))
                    to  = norm_text_block(pr.get("to"))

                    mode = (pr.get("mode") or "").strip().lower() or "standard"

                    from_items_raw = pr.get("from_items")
                    to_items_raw   = pr.get("to_items")

                    from_items = [norm_ft_item(frm)] if not isinstance(from_items_raw, list) else [norm_ft_item(x) for x in from_items_raw]
                    to_items   = [norm_ft_item(to)]  if not isinstance(to_items_raw,   list) else [norm_ft_item(x) for x in to_items_raw]

                    hidden_raw = pr.get("hidden", pr.get("hide", pr.get("is_hidden", False)))
                    hidden = hidden_raw
                    if isinstance(hidden, str):
                        hidden = hidden.strip().lower() in ("1","true","yes","on")
                    else:
                        hidden = bool(hidden)

                    has_any = any([
                        frm["template"], frm["text1"], frm["text2"],
                        to["template"],  to["text1"],  to["text2"]
                    ]) or any(any(v for v in it.values()) for it in from_items + to_items)

                    if has_any or hidden:
                        # --- új: oldalankénti rejtés is számítson "nem üresnek" ---
                        from_hidden_raw = pr.get("from_hidden", pr.get("hide_from", pr.get("from_hide", pr.get("fromHidden", False))))
                        to_hidden_raw   = pr.get("to_hidden",   pr.get("hide_to",   pr.get("to_hide",   pr.get("toHidden", False))))

                        from_hidden = _parse_bool(from_hidden_raw)
                        to_hidden   = _parse_bool(to_hidden_raw)

                        include_pair = (has_any or hidden or from_hidden or to_hidden)

                        if include_pair:
                            pairs_out.append({
                                "from": frm,
                                "to": to,
                                "mode": mode,
                                "from_items": from_items,
                                "to_items": to_items,
                                "hidden": hidden,
                                "from_hidden": from_hidden,
                                "to_hidden": to_hidden,
                            })

                groups_out.append({"group": group_no, "connectors": connectors_out, "pairs": pairs_out})

            norm_pages.append({"page": page_no, "id_labels": id_labels, "groups": groups_out})

        cfg_id = int(get_or_create_config_id(pn, rev or None, ecn or None))

        payload = {
            "pn": db_pn or pn,
            "rev": db_rev or rev,
            "ecn": db_ecn or ecn,
            "config_id": cfg_id,  # <<< EZ KELL
            "id_template": id_template or "",
            "summary": norm_pages,
            "pages": norm_pages,
            "connector_labels": connector_labels,
            "special_labels": [norm_special(x) for x in special_labels],
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


@bartender_bp.route('/api/bartender/load_archive', methods=['POST'])
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def load_archive():
    """
    Egy bartender_archive snapshot betöltése MEGJELENÍTÉSRE (csak olvasás).

    Vár: JSON: { "archive_id": <int> }

    Válasz: ugyanaz a payload-forma, mint a load_label_config-é,
    hogy az archív nézet (bartender_archive_view_V1.html) változtatás nélkül
    tudja megjeleníteni az adatokat:
      { status:"success", data:{ pn, rev, ecn, config_id:null, id_template,
                                 summary, pages, connector_labels, special_labels,
                                 archive:{id, action, username, saved_at} } }
    """
    import json
    data = request.get_json(silent=True) or {}

    try:
        archive_id = int(str(data.get("archive_id") or "").strip())
    except Exception:
        return jsonify({"status": "error", "message": "archive_id kötelező (szám)."}), 400

    try:
        db = get_db()

        from services.bartender_project_creator_report_core import fetch_archive_row
        row = fetch_archive_row(db, archive_id)

        if not row:
            return jsonify({"status": "error", "message": "Nem található archív mentés ezzel az azonosítóval."}), 404

        # summary: mentéskor már normalizált formában került be
        # ({"pages":[...], "connector_labels":[...], "special_labels":[...]})
        try:
            parsed = json.loads(row["summary"]) if row.get("summary") else {}
        except Exception as e:
            current_app.logger.exception("Archive summary JSON parse error")
            return jsonify({"status": "error", "message": f"Rossz summary JSON az archívban: {e}"}), 500

        if isinstance(parsed, dict):
            pages            = parsed.get("pages") or parsed.get("summary") or []
            connector_labels = parsed.get("connector_labels") or []
            special_labels   = parsed.get("special_labels") or []
        else:
            pages            = parsed if isinstance(parsed, list) else []
            connector_labels = []
            special_labels   = []

        payload = {
            "pn":  row.get("pn") or "",
            "rev": row.get("rev") or "",
            "ecn": row.get("ecn") or "",
            # szándékosan None: archív nézetben nincs lock / config kezelés
            "config_id": None,
            "id_template": row.get("id_template") or "",
            "summary": pages,
            "pages": pages,
            "connector_labels": connector_labels,
            "special_labels": special_labels,
            "archive": {
                "id":       row.get("id"),
                "action":   row.get("action"),
                "username": row.get("username"),
                "saved_at": row.get("saved_at"),
            },
        }

        return jsonify({"status": "success", "data": payload}), 200

    except Exception as e:
        current_app.logger.exception("load_archive hiba")
        return jsonify({"status": "error", "message": f"Szerver hiba archív betöltés közben: {e}"}), 500


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
        "connector_labels": [...],   # opcionális
        "special_labels": [...]      # opcionális
        "lock_token": "..."          # opcionális (ha nincs / invalid -> megpróbáljuk megszerezni)
      }

    Mentés: `etiket data`
      - PN, REV, ECN, summary(JSON), ID_sablon, printers
      - + meta: `create date`, `updated date`, `edited by`, `updated by`

    Válasz: { status: "success", config_id: <int>, lock_token: "<token>" }

    FONTOS: kezeli azt is, ha FROM/TO template/text üres, de hidden/from_hidden/to_hidden be van állítva:
      - megőrzi a pair-t és a hide flag-eket (nem dobja ki "üresként")
      - alias mezőkből is felveszi (hide_from/fromHidden stb.)
    """
    import json
    from flask import request, jsonify, session, current_app

    content_len = request.content_length or 0
    HARD_LIMIT = 12 * 1024 * 1024  # 12MB
    if content_len > HARD_LIMIT:
        return jsonify({
            "status": "error",
            "message": f"A kérés túl nagy ({content_len} byte). Max {HARD_LIMIT} byte engedélyezett."
        }), 413

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}

    # --- mezők normalizálása
    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()
    id_template = (data.get("id_template") or "").strip()

    pages            = data.get("pages") or []
    connector_labels = data.get("connector_labels") or []
    special_labels   = data.get("special_labels") or []

    if not pn or not rev or not ecn:
        return jsonify({"status": "error", "message": "PN/REV/ECN kötelező"}), 400

    if not isinstance(pages, list): pages = []
    if not isinstance(connector_labels, list): connector_labels = []
    if not isinstance(special_labels, list): special_labels = []

    # ---------- helpers: normalizálás (server-side védelem) ----------
    def norm_text_block(x):
        if not isinstance(x, dict):
            return {"template": "", "text1": "", "text2": ""}
        return {
            "template": (x.get("template") or "").strip(),
            "text1":    (x.get("text1") or "").strip(),
            "text2":    (x.get("text2") or "").strip(),
            **_extra_line_keys(x),
        }

    def norm_ft_item(x):
        if not isinstance(x, dict):
            return {"template": "", "sel1": "", "sel2": "", "text1": "", "text2": ""}
        return {
            "template": (x.get("template") or "").strip(),
            "sel1":     (x.get("sel1") or "").strip(),
            "sel2":     (x.get("sel2") or "").strip(),
            "text1":    (x.get("text1") or "").strip(),
            "text2":    (x.get("text2") or "").strip(),
            **_extra_line_keys(x),
        }

    def norm_special(x):
        if not isinstance(x, dict):
            return {"template": "", "qty": 1, "lines": []}
        tpl = (x.get("template") or "").strip()
        try:
            qty = int(x.get("qty") or 1)
        except Exception:
            qty = 1
        lines = x.get("lines")
        if not isinstance(lines, list):
            lines = []
        lines = [(str(s) if s is not None else "").strip() for s in lines]
        return {"template": tpl, "qty": max(1, qty), "lines": lines}

    def any_text_in_block(b: dict) -> bool:
        return bool((b.get("template") or "").strip() or (b.get("text1") or "").strip() or (b.get("text2") or "").strip())

    def any_value_in_item(it: dict) -> bool:
        # itt elég, ha bármelyik mező nem üres
        return any((str(v).strip() for v in (it.get("template"), it.get("sel1"), it.get("sel2"), it.get("text1"), it.get("text2")) if v is not None))

    def parse_side_hidden(pr: dict, side: str) -> bool:
        # side: "from" | "to"
        if side == "from":
            raw = pr.get("from_hidden", pr.get("hide_from", pr.get("from_hide", pr.get("fromHidden", False))))
        else:
            raw = pr.get("to_hidden", pr.get("hide_to", pr.get("to_hide", pr.get("toHidden", False))))
        return _parse_bool(raw)

    def parse_pair_hidden(pr: dict) -> bool:
        raw = pr.get("hidden", pr.get("hide", pr.get("is_hidden", False)))
        return _parse_bool(raw)

    # --- pages normalizálás: kiszűrünk mindent, ami tényleg teljesen üres ÉS nincs elrejtés,
    # --- DE: ha hidden/from_hidden/to_hidden True, akkor megőrizzük akkor is, ha template/text üres.
    norm_pages = []
    for pi, p in enumerate(pages):
        if not isinstance(p, dict):
            continue

        try:
            page_no = int(p.get("page") or (pi + 1))
        except Exception:
            page_no = pi + 1

        # id_labels átengedése (minimálisan normalizálva)
        id_labels_out = []
        for lab in (p.get("id_labels") or []):
            if not isinstance(lab, dict):
                continue
            try:
                id_labels_out.append({
                    "id_index": int(lab.get("id_index") or 0),
                    "template": (lab.get("template") or "").strip(),
                    "printer":  (lab.get("printer")  or "").strip(),
                    "page":     int(lab.get("page") or page_no),
                    "group":    int(lab.get("group") or 1),
                })
            except Exception:
                id_labels_out.append({
                    "id_index": int(lab.get("id_index") or 0) if str(lab.get("id_index") or "").isdigit() else 0,
                    "template": (lab.get("template") or "").strip(),
                    "printer":  (lab.get("printer")  or "").strip(),
                    "page":     page_no,
                    "group":    1,
                })

        groups_out = []
        for gi, g in enumerate((p.get("groups") or [])):
            if not isinstance(g, dict):
                continue
            try:
                group_no = int(g.get("group") or (gi + 1))
            except Exception:
                group_no = gi + 1

            # connectors normalizálás (id/value)
            connectors_out = []
            for c in (g.get("connectors") or []):
                if not isinstance(c, dict):
                    continue
                cid = (c.get("id") or "").strip()
                if cid:
                    connectors_out.append({
                        "id": cid,
                        "value": (c.get("value") or "").strip()
                    })

            pairs_out = []
            for pr in (g.get("pairs") or []):
                if not isinstance(pr, dict):
                    continue

                frm = norm_text_block(pr.get("from"))
                to  = norm_text_block(pr.get("to"))

                mode = (pr.get("mode") or "").strip().lower() or "standard"

                # items: ha nincs lista, csináljunk 1 elemű listát a primary blockból (kompatibilitás)
                from_items_raw = pr.get("from_items")
                to_items_raw   = pr.get("to_items")

                if isinstance(from_items_raw, list):
                    from_items = [norm_ft_item(x) for x in from_items_raw]
                else:
                    from_items = [norm_ft_item(frm)]

                if isinstance(to_items_raw, list):
                    to_items = [norm_ft_item(x) for x in to_items_raw]
                else:
                    to_items = [norm_ft_item(to)]

                # --- KÉNYSZERÍTETT KONNEKTOR-SZINKRON (gyökérok-javítás) -----------
                # A FROM/TO szövegmezők a kiválasztott konnektor (sel1/sel2) értékét
                # tükrözik – a konnektor a végső igazság. Mentéskor a text1/text2-t
                # MINDIG a hivatkozott konnektor aktuális értékéből írjuk felül, így
                # akkor sem kerülhet be inkonzisztens rekord, ha a frontend (időzítési
                # hiba miatt) beragadt, elavult szöveget küldött. Üres sel = szabad
                # szöveg, azt békén hagyjuk. Standard és egyedi módban egyaránt fut
                # (minden item-re, nem csak az elsőre).
                _conn_val = {c["id"]: c["value"] for c in connectors_out if c.get("id")}

                def _enforce_conn_text(items):
                    for it in items:
                        s1 = (it.get("sel1") or "").strip()
                        s2 = (it.get("sel2") or "").strip()
                        if s1 and _conn_val.get(s1):
                            it["text1"] = _conn_val[s1]
                        if s2 and _conn_val.get(s2):
                            it["text2"] = _conn_val[s2]
                    return items

                from_items = _enforce_conn_text(from_items)
                to_items   = _enforce_conn_text(to_items)

                # a legacy from/to blokkot szinkronban tartjuk az első item-mel,
                # hogy a QC és a print (ami olykor a from/to blokkot olvassa) is
                # ugyanazt lássa
                if from_items:
                    frm["text1"] = from_items[0].get("text1", frm.get("text1", ""))
                    frm["text2"] = from_items[0].get("text2", frm.get("text2", ""))
                if to_items:
                    to["text1"] = to_items[0].get("text1", to.get("text1", ""))
                    to["text2"] = to_items[0].get("text2", to.get("text2", ""))
                # -------------------------------------------------------------------

                hidden      = parse_pair_hidden(pr)
                from_hidden = parse_side_hidden(pr, "from")
                to_hidden   = parse_side_hidden(pr, "to")

                has_any = (
                    any_text_in_block(frm) or any_text_in_block(to) or
                    any(any_value_in_item(it) for it in (from_items or [])) or
                    any(any_value_in_item(it) for it in (to_items or []))
                )

                # EZ A LÉNYEG: ha csak a hide flag miatt kell megmaradnia, akkor is mentjük
                if has_any or hidden or from_hidden or to_hidden:
                    pairs_out.append({
                        "from": frm,
                        "to": to,
                        "mode": mode,
                        "from_items": from_items,
                        "to_items": to_items,
                        "hidden": hidden,
                        "from_hidden": from_hidden,
                        "to_hidden": to_hidden,
                    })

            groups_out.append({
                "group": group_no,
                "connectors": connectors_out,
                "pairs": pairs_out
            })

        norm_pages.append({
            "page": page_no,
            "id_labels": id_labels_out,
            "groups": groups_out
        })

    # --- special_labels normalizálás (biztonság)
    norm_special_labels = [norm_special(x) for x in (special_labels or []) if isinstance(x, (dict, list, str, int, float, type(None)))]

    # --- summary objektum: új formát mentünk
    summary_obj = {"pages": norm_pages}
    if connector_labels:
        summary_obj["connector_labels"] = connector_labels
    if norm_special_labels:
        summary_obj["special_labels"] = norm_special_labels

    try:
        summary_json = json.dumps(summary_obj, ensure_ascii=False, separators=(",", ":"))
    except Exception as serr:
        return jsonify({"status": "error", "message": f"JSON szerializációs hiba: {serr}"}), 400

    # --- usernév (session → header fallback)
    try:
        user_name = (session.get("user") or {}).get("username") or (session.get("user") or {}).get("name")
    except Exception:
        user_name = None
    if not user_name:
        user_name = request.headers.get("X-User") or "unknown"

    db = get_db()

    # ✅ ÚJ: frontend mód (add/edit) – ha nincs, legyen edit
    mode = (data.get("mode") or "").strip().lower()
    if mode not in ("add", "edit"):
        mode = "edit"

    # ✅ ÚJ: duplikáció ellenőrzés csak ADD módban
    # (még a config_id/lock előtt, hogy ne generáljunk új configot és ne írjunk felül)
    if mode == "add":
        cur = db.cursor()
        try:
            cur.execute("""
                SELECT 1
                FROM `etiket data`
                WHERE UPPER(PN)=UPPER(%s)
                AND UPPER(REV)=UPPER(%s)
                AND UPPER(ECN)=UPPER(%s)
                LIMIT 1
            """, (pn, rev, ecn))
            if cur.fetchone():
                return jsonify({
                    "status": "error",
                    "code": "already_exists",
                    "message": "Már létezik ilyen projekt (PN/REV/ECN alapján)."
                }), 409
        finally:
            cur.close()

    # --- config_id + LOCK
    try:
        config_id = int(get_or_create_config_id(pn, rev or None, ecn or None))

        lock_token = (data.get("lock_token") or data.get("lockToken") or data.get("token") or "").strip()

        # 1) ha van token és valid -> OK
        if lock_token and _lock_validate(db, config_id, lock_token):
            pass
        else:
            # 2) ha nincs / invalid -> lock szerzés
            owner, payload = _lock_acquire(db, config_id, user_name)
            if not owner:
                return jsonify({
                    "status": "error",
                    "message": "A projekt zárolva van (más szerkeszti).",
                    "locked_by": payload.get("locked_by"),
                    "locked_at": str(payload.get("locked_at") or ""),
                    "heartbeat_at": str(payload.get("heartbeat_at") or ""),
                }), 423
            lock_token = (payload.get("token") or "").strip()

    except Exception:
        current_app.logger.exception("config_id/lock failed")
        return jsonify({"status": "error", "message": "Nem sikerült a lock/config kezelése."}), 500

    # --- mentés DB-be
    try:
        # printer-ek feloldása CSAK a pages alapján
        printers_obj = _resolve_printers_from_pages(db, norm_pages)
        printers_json = json.dumps(printers_obj, ensure_ascii=False) if printers_obj else None

        cur = db.cursor()
        cur.execute("""
            INSERT INTO `etiket data`
                (PN, REV, ECN, summary, ID_sablon, printers,
                 `create date`, `updated date`, `edited by`, `updated by`)
            VALUES
                (%s, %s, %s, %s, %s, %s,
                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s, %s)
            ON DUPLICATE KEY UPDATE
                REV            = VALUES(REV),          -- case-only (b->B) módosítás miatt
                summary        = VALUES(summary),
                ID_sablon      = VALUES(ID_sablon),
                printers       = VALUES(printers),
                `updated date` = CURRENT_TIMESTAMP,
                `edited by`    = VALUES(`edited by`),
                `updated by`   = VALUES(`updated by`)
        """, (pn, rev, ecn, summary_json, id_template, printers_json, user_name, user_name))
        db.commit()
        cur.close()

        # --- ARCHÍV snapshot (best-effort): minden mentésről időbélyeges másolat
        # a bartender_archive táblába – soha nem ír felül, mindig új sort szúr be.
        archive_id = None
        try:
            from services.bartender_project_creator_report_core import (
                insert_archive_snapshot, prune_project_archives,
            )
            archive_id = insert_archive_snapshot(
                db,
                pn=pn, rev=rev, ecn=ecn,
                username=user_name,
                action=mode,
                summary_json=summary_json,
                id_template=id_template,
                printers_json=printers_json,
            )

            # retention: projektenként csak az utolsó N snapshot maradjon
            # (0 vagy negatív érték = nincs törlés / korlátlan megőrzés)
            try:
                keep = int(current_app.config.get("BT_ARCHIVE_KEEP", 30))
            except Exception:
                keep = 30
            if keep and keep > 0:
                prune_project_archives(db, pn=pn, rev=rev, ecn=ecn, keep=keep)
        except Exception:
            archive_id = None

        # --- history log (best-effort, hiba esetén nem dob kivételt)
        try:
            from services.bartender_project_creator_report_core import insert_project_history
            insert_project_history(db, pn=pn, rev=rev, ecn=ecn, username=user_name, action=mode, archive_id=archive_id)
        except Exception:
            pass

        return jsonify({
            "status": "success",
            "config_id": int(config_id),
            "lock_token": lock_token
        }), 200

    except Exception as e:
        current_app.logger.exception("save_label_config hiba")
        try:
            db.rollback()
        except Exception:
            pass

        if "max_allowed_packet" in str(e).lower():
            return jsonify({
                "status": "error",
                "message": "A MySQL max_allowed_packet túl kicsi. Állítsd legalább 64M/128M értékre."
            }), 500

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
    db = None
    cur = None
    try:
        data = request.get_json(silent=True) or {}

        pn = (data.get("pn") or "").strip()
        printer_id = (data.get("printer_id") or "").strip()
        printer_from = (data.get("printer_from") or "").strip()
        printer_to = (data.get("printer_to") or "").strip()

        if not pn or not printer_id or not printer_from or not printer_to:
            return jsonify({
                "status": "error",
                "message": "Minden mező kötelező (pn, printer_id, printer_from, printer_to)."
            }), 400

        printers_obj = {"id": printer_id, "from": printer_from, "to": printer_to}
        printers_obj_json = json.dumps(printers_obj, ensure_ascii=False)

        db = get_db()
        cur = db.cursor()

        # 1) etiket data: megkeressük PN alapján (case-insensitive)
        cur.execute("""
            SELECT `ID`
            FROM `etiket data`
            WHERE UPPER(PN)=UPPER(%s)
            LIMIT 1
        """, (pn,))
        row = cur.fetchone()

        if row:
            # update
            cur.execute("""
                UPDATE `etiket data`
                SET printers = %s
                WHERE `ID` = %s
            """, (printers_obj_json, row[0]))
        else:
            # insert (minimál record, hogy legyen hova menteni a printers-t)
            cur.execute("""
                INSERT INTO `etiket data` (PN, REV, ECN, summary, ID_sablon, printers)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (pn, "", "", "[]", "", printers_obj_json))

        # 2) kompatibilitás az 'etiket' táblával (ha létezik)
        try:
            cur.execute("SELECT 1 FROM `etiket` WHERE UPPER(PN)=UPPER(%s) LIMIT 1", (pn,))
            if cur.fetchone():
                printers_list_json = json.dumps([printer_id], ensure_ascii=False)
                cur.execute("UPDATE `etiket` SET printers = %s WHERE UPPER(PN)=UPPER(%s)", (printers_list_json, pn))
        except Exception:
            pass

        db.commit()
        return jsonify({"status": "success"}), 200

    except Exception as e:
        print("save_printer_to_pn hiba:", repr(e))
        try:
            if db:
                db.rollback()
        except Exception:
            pass
        return jsonify({"status": "error", "message": "Szerver hiba a nyomtatók mentésekor."}), 500

    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass

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

def _parse_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)

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

    - kezeli az új 'from_items' / 'to_items' formát is
    - tiszteletben tartja: hidden (pair), from_hidden, to_hidden
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
                if _parse_bool(pr.get("hidden", pr.get("hide", pr.get("is_hidden", False)))):
                    continue

                from_hidden = _parse_bool(pr.get("from_hidden",
                                  pr.get("hide_from", pr.get("from_hide", pr.get("fromHidden", False)))))
                to_hidden   = _parse_bool(pr.get("to_hidden",
                                  pr.get("hide_to",   pr.get("to_hide",   pr.get("toHidden", False)))))

                from_items = pr.get("from_items") if isinstance(pr, dict) else None
                to_items   = pr.get("to_items")   if isinstance(pr, dict) else None

                # új forma: from_items / to_items
                if isinstance(from_items, list) and not from_hidden:
                    for it in from_items:
                        if isinstance(it, dict):
                            t = (it.get("template") or "").strip()
                            if t:
                                from_templates.append(t)

                if isinstance(to_items, list) and not to_hidden:
                    for it in to_items:
                        if isinstance(it, dict):
                            t = (it.get("template") or "").strip()
                            if t:
                                to_templates.append(t)

                # legacy fallback (ha nincs items lista)
                if (not isinstance(from_items, list)) and (not from_hidden):
                    frm = (pr.get("from") or {}) if isinstance(pr, dict) else {}
                    t = (frm.get("template") or "").strip()
                    if t:
                        from_templates.append(t)

                if (not isinstance(to_items, list)) and (not to_hidden):
                    to = (pr.get("to") or {}) if isinstance(pr, dict) else {}
                    t = (to.get("template") or "").strip()
                    if t:
                        to_templates.append(t)

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
    if _RE_DAT34_2L_AS_FROM.search(filename): return "from"
    if _RE_TMT20045_DAT39_2L_AS_FROM.search(filename): return "from"
    if _RE_TMT50195_2L.search(filename): return "from"
    if _RE_FROM.search(filename): return "from"
    if _RE_TO.search(filename):   return "to"
    if _RE_PLAIN_2L.search(filename): return "id2"    # 2 soros ID címke
    if _RE_FT_GENERIC.search(filename): return "ft"
    if _RE_1L.search(filename):   return "con"
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

    Támogatott formák:
      - régi:   pages: [ { groups: [ { connectors: [...], pairs: [ {from:{}, to:{}} ] } ] } ]
      - új:     { "pages": [...], ... }
      - elemenként több sablon: pairs[].from_items / pairs[].to_items (mindegyik külön bejegyzés)
        ahol item: { template, text1, text2 } és text1/text2 = connector KÓD (pl. "A", "B")
    """
    out: dict[str, dict] = {}
    if not summary_json:
        return out

    # lazán parse-oljuk (string/dict/list mind jöhet)
    try:
        root = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    except Exception:
        root = summary_json

    # régi vs. új szerkezet normalizálása
    pages = []
    if isinstance(root, dict):
        pages = root.get("pages") or root.get("summary") or []
    elif isinstance(root, list):
        pages = root

    for p in (pages or []):
        groups = (p.get("groups") if isinstance(p, dict) else []) or []
        for g in groups:
            # connector kód -> érték map felépítése a csoport szintjén
            cmap = {}
            for c in (g.get("connectors") or []):
                cid = (c.get("id") or c.get("code") or "").strip()
                val = (c.get("value") or c.get("display_name") or "").strip()
                if cid:
                    cmap[cid] = val

            for pr in (g.get("pairs") or []):
                hidden = pr.get("hidden", pr.get("hide", pr.get("is_hidden", False)))
                if isinstance(hidden, str):
                    hidden = hidden.strip().lower() in ("1","true","yes","on")
                else:
                    hidden = bool(hidden)
                if hidden:
                    continue

                # QC-mód: oldalankénti rejtés (from_hidden / to_hidden) tisztelete
                from_hidden = _parse_bool(pr.get("from_hidden",
                                  pr.get("hide_from", pr.get("from_hide", pr.get("fromHidden", False)))))
                to_hidden   = _parse_bool(pr.get("to_hidden",
                                  pr.get("hide_to",   pr.get("to_hide",   pr.get("toHidden", False)))))

                frm = (pr.get("from") or {})
                to  = (pr.get("to")   or {})

                def pick_lines(block: dict) -> tuple[str, str, list[str]]:
                    # A 'textN' a kiválasztott connector KÓDJA (pl. "A", "B");
                    # QC-mód: ha üres, a selN (legördülő) érték a fallback.
                    # text1/text2 mindig van; a text3-tól a többsoros címkék sorai
                    # jönnének – ma a szerkesztő nem írja őket, akkor üresen marad.
                    vals: list[str] = []
                    for i in range(1, MAX_SPECIAL_LINES + 1):
                        k = (block.get(f"text{i}") or "").strip() or (block.get(f"sel{i}") or "").strip()
                        # egységes feloldás a kártyákkal (_ft_cards_from_summary._mk_item):
                        # üres map-érték esetén a nyers szöveg marad
                        vals.append((cmap.get(k) or k).strip())
                    while len(vals) > 2 and not vals[-1]:
                        vals.pop()
                    up = vals[0] if vals else ""
                    dn = vals[1] if len(vals) > 1 else ""
                    return up, dn, vals

                # 1) legacy: egyetlen from/to blokk
                if (not from_hidden) and frm.get("template"):
                    up, dn, lns = pick_lines(frm)
                    out[frm["template"]] = {"side": "FROM", "up": up, "down": dn, "lines": lns}

                if (not to_hidden) and to.get("template"):
                    up, dn, lns = pick_lines(to)
                    out[to["template"]] = {"side": "TO", "up": up, "down": dn, "lines": lns}

                # 2) új: több elem külön sablonnal (from_items / to_items)
                if not from_hidden:
                    for it in (pr.get("from_items") or []):
                        tpl = (it.get("template") or "").strip()
                        if tpl:
                            up, dn, lns = pick_lines(it)
                            out[tpl] = {"side": "FROM", "up": up, "down": dn, "lines": lns}

                if not to_hidden:
                    for it in (pr.get("to_items") or []):
                        tpl = (it.get("template") or "").strip()
                        if tpl:
                            up, dn, lns = pick_lines(it)
                            out[tpl] = {"side": "TO", "up": up, "down": dn, "lines": lns}

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

    if info.get("side") not in ("FROM", "TO"):
        return {}

    # ha a summary több sort ad, mindet a helyére tesszük
    lines = [str(x or "").strip() for x in (info.get("lines") or [])]
    if len([x for x in lines if x]) > 2:
        fields = _fields_from_lines(template_name, lines)
        if fields:
            return fields

    up = (info.get("up") or "").strip()
    dn = (info.get("down") or "").strip()
    return _ft_fields_for_template(template_name, up, dn)


# --- remappelés a .btw elvárásaihoz (csak XML-kimenet előtt) -----------------

_ID_3L = re.compile(r"(?:DAT-3[47]).*ID Label 3L\.btw$", re.I)
_ID_4L = re.compile(r"(?:DAT-3[47]).*ID Label 4L\.btw$", re.I)

def _remap_to_template_substrings(template_name: str, fields: dict[str, str]) -> dict[str, str]:
    f = {k: (v or "") for k, v in (fields or {}).items()}

    # --- VARIAN ID Label 4L: ID1..ID4 mezőnevek ---
    if _RE_VARIAN_ID4L.search(template_name):
        return {
            "ID1": f.get("ID1") or f.get("L1") or "",
            "ID2": f.get("ID2") or f.get("L2") or "",
            "ID3": f.get("ID3") or f.get("L3") or "",
            "ID4": f.get("ID4") or f.get("L4") or "",
        }

    # --- FROM / TO / CON / 2 soros ID: a mezőnév-tábla ---
    if _field_names_for(template_name):
        return _normalize_ft_fields(template_name, f)

    pn, wo, rev = f.get("PN",""), f.get("WO",""), f.get("REV","")
    if rev and not str(rev).strip().upper().startswith("ISSUE"):
        rev = f"ISSUE {rev}"
    return {"PN": pn, "WO": wo, "REV": rev}



def _connector_labels_map_from_summary(summary_json) -> dict[str, list[dict]]:
    """
    Vissza: { "<template.btw>": [ { "value": "...", "connector_id": "X" }, ... ] }
    A 'value' ha hiányzik, megpróbáljuk a pages→groups→connectors id→value mapből kivenni.
    """
    out: dict[str, list[dict]] = {}
    if not summary_json:
        return out

    try:
        root = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    except Exception:
        root = summary_json

    # lehet régi (lista) vagy új (dict: {"pages":[...], "connector_labels":[...]})
    pages = []
    conn_labels = []
    if isinstance(root, dict):
        pages = root.get("pages") or root.get("summary") or []
        conn_labels = root.get("connector_labels") or []
    elif isinstance(root, list):
        pages = root

    # id → value index a pages-ből
    id2val = {}
    for p in (pages or []):
        for g in (p.get("groups") or []):
            for c in (g.get("connectors") or []):
                cid = (c.get("id") or c.get("code") or "").strip()
                val = (c.get("value") or c.get("display_name") or "").strip()
                if cid and cid not in id2val:
                    id2val[cid] = val

    for cl in (conn_labels or []):
        tpl = (cl.get("template") or "").strip()
        cid = (cl.get("connector_id") or "").strip()
        val = (cl.get("value") or "").strip()
        if not val and cid:
            val = id2val.get(cid, "")
        if tpl:
            out.setdefault(tpl, []).append({"connector_id": cid, "value": val})
    return out



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
        lines = _preview_lines_from_fields(fields, tpl)

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
@require_roles(MANAGER_ROLES, IT_ROLES)
def reports_page(lang):
    if lang not in ("hu","sk"):
        abort(404)
    return render_template(f"{lang}/bartender_reports.html", user=session.get("user"))

# ===== API: részletes sorok ===================================================

@bartender_bp.get("/api/bartender/print_log")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
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

# ===== API: napló-rekord törlése ==============================================

@bartender_bp.delete("/api/bartender/print_log/<int:log_id>")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_print_log_delete(log_id):
    """
    Egy nyomtatási napló-rekord törlése (riport oldalról).

    A nyomtatott számlálót (bartender_printed_counter) is visszacsökkenti a
    törölt rekord printed_copies értékével, hogy a nyomtatás oldalon a kártya
    újra megjelenjen / nyomtatható legyen.
    """
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT pn, rev, ecn, template, printed_copies, lines_json
            FROM print_log WHERE id=%s LIMIT 1
        """, (log_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "A rekord nem található."}), 404

        cur.execute("DELETE FROM print_log WHERE id=%s", (log_id,))

        # --- nyomtatott számláló visszacsökkentése ---------------------------
        counter_adjusted = 0
        printed = int(row.get("printed_copies") or 0)
        tpl = (row.get("template") or "").strip()
        if printed > 0 and tpl:
            pn  = (row.get("pn")  or "").strip()
            rev = (row.get("rev") or "").strip()
            ecn = (row.get("ecn") or "").strip()

            # 1) pontos kulcs-egyezés (a frontend cardKeyOf formátumában:
            #    JSON.stringify({tpl, lines}) – szóköz nélküli JSON)
            try:
                lines = json.loads(row.get("lines_json") or "[]")
            except Exception:
                lines = []
            exact_key = json.dumps(
                {"tpl": tpl, "lines": [str(x or "").strip() for x in (lines or [])]},
                ensure_ascii=False, separators=(",", ":")
            )
            cur.execute("""
                UPDATE bartender_printed_counter
                SET printed = GREATEST(CAST(printed AS SIGNED) - %s, 0)
                WHERE pn=%s AND rev=%s AND ecn=%s AND card_key=%s
            """, (printed, pn, rev, ecn, exact_key))
            counter_adjusted = cur.rowcount

            # 2) fallback: sablon szerint (a naplózott sorok eltérhetnek a
            #    kártyán megjelenített soroktól, pl. scan-token csere miatt)
            if counter_adjusted == 0:
                like_tpl = tpl.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                cur.execute("""
                    UPDATE bartender_printed_counter
                    SET printed = GREATEST(CAST(printed AS SIGNED) - %s, 0)
                    WHERE pn=%s AND rev=%s AND ecn=%s
                      AND card_key LIKE CONCAT('%%"tpl":"', %s, '"%%')
                """, (printed, pn, rev, ecn, like_tpl))
                counter_adjusted = cur.rowcount

        db.commit()
        return jsonify({"ok": True, "counter_adjusted": int(counter_adjusted)}), 200
    except Exception as exc:
        db.rollback()
        current_app.logger.exception("print_log törlés hiba (id=%s)", log_id)
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        cur.close()

# ===== API: összesítő =========================================================

@bartender_bp.get("/api/bartender/print_log/summary")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
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

    # Streamelt CSV: nem építjük fel a teljes (akár 50k soros) fájlt memóriában,
    # a letöltés azonnal indul, soronként megy ki a kliensnek.
    def _gen():
        buf = io.StringIO()
        w = csv.writer(buf)

        def _flush():
            chunk = buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            return chunk

        w.writerow(["printed_at","user","pn","wo","rev","template","printer","per_unit","total","printed","source"])
        yield _flush()
        for r in items:
            w.writerow([r["printed_at"], r["user_name"], r["pn"], r["wo"], r["rev"],
                        r["template"], r["printer_name"], r["copies_per_unit"],
                        r["total_copies"], r["printed_copies"], r["source"]])
            yield _flush()

    return Response(
        _gen(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=print_log.csv"}
    )

def _template_counts_from_summary_enhanced(summary_json) -> dict[str, int]:
    """
    Összeszámolja a FROM/TO sablonokat a summary-ból úgy, hogy
    a pairs[].from_items / to_items tömbben lévő elemeket is beleszámolja.
    """
    out: dict[str, int] = {}
    if not summary_json:
        return out

    try:
        root = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    except Exception:
        root = summary_json

    pages = []
    if isinstance(root, dict):
        pages = root.get("pages") or root.get("summary") or []
    elif isinstance(root, list):
        pages = root

    def _bump(t: str):
        if t:
            out[t] = out.get(t, 0) + 1

    for p in (pages or []):
        for g in (p.get("groups") or []):
            for pr in (g.get("pairs") or []):
                frm = (pr.get("from") or {})
                to  = (pr.get("to")   or {})
                # legacy: ha csak sima from/to volt
                if frm.get("template"): _bump((frm.get("template") or "").strip())
                if  to.get("template"): _bump(( to.get("template") or "").strip())

                # új: from_items / to_items
                for it in (pr.get("from_items") or []):
                    _bump((it.get("template") or "").strip())
                for it in (pr.get("to_items") or []):
                    _bump((it.get("template") or "").strip())

    return out

def template_counts_from_summary_nodup(summary_json) -> dict[str, int]:
    """
    FROM/TO sablonok darabszáma a summary-ból duplázás nélkül + side hide támogatással.

    - hidden (pair) -> semmi
    - from_hidden / to_hidden -> csak az adott oldal nem számít
    - items-first logika marad
    """
    out: dict[str, int] = {}
    if not summary_json:
        return out

    try:
        root = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    except Exception:
        root = summary_json

    pages = []
    if isinstance(root, dict):
        pages = root.get("pages") or root.get("summary") or []
    elif isinstance(root, list):
        pages = root

    def bump(t: str):
        t = (t or "").strip()
        if t:
            out[t] = out.get(t, 0) + 1

    for p in pages or []:
        for g in (p.get("groups") or []):
            for pr in (g.get("pairs") or []):
                if _parse_bool(pr.get("hidden", pr.get("hide", pr.get("is_hidden", False)))):
                    continue

                from_hidden = _parse_bool(pr.get("from_hidden",
                                  pr.get("hide_from", pr.get("from_hide", pr.get("fromHidden", False)))))
                to_hidden   = _parse_bool(pr.get("to_hidden",
                                  pr.get("hide_to",   pr.get("to_hide",   pr.get("toHidden", False)))))

                # ---- FROM oldal ----
                if not from_hidden:
                    from_items = [
                        (it.get("template") or "").strip()
                        for it in (pr.get("from_items") or [])
                        if isinstance(it, dict) and (it.get("template") or "").strip()
                    ]
                    if from_items:
                        for tpl in from_items:
                            bump(tpl)
                    else:
                        ft = ((pr.get("from") or {}).get("template") or "").strip()
                        if ft:
                            bump(ft)

                # ---- TO oldal ----
                if not to_hidden:
                    to_items = [
                        (it.get("template") or "").strip()
                        for it in (pr.get("to_items") or [])
                        if isinstance(it, dict) and (it.get("template") or "").strip()
                    ]
                    if to_items:
                        for tpl in to_items:
                            bump(tpl)
                    else:
                        tt = ((pr.get("to") or {}).get("template") or "").strip()
                        if tt:
                            bump(tt)

    return out

def _id_label_counts_from_summary(summary_json) -> dict[str, int]:
    """
    ID sablonok darabszáma a summary-ból: pages[].id_labels[].template alapján.
    Vissza: { "<id_template>.btw": count }
    """
    out: dict[str, int] = {}
    if not summary_json:
        return out

    try:
        root = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    except Exception:
        root = summary_json

    pages = []
    if isinstance(root, dict):
        pages = root.get("pages") or root.get("summary") or []
    elif isinstance(root, list):
        pages = root

    for p in (pages or []):
        for lab in (p.get("id_labels") or []):
            tpl = (lab.get("template") or "").strip()
            if tpl:
                out[tpl] = out.get(tpl, 0) + 1
    return out

def _id_label_list_from_summary(summary_json) -> list[str]:
    """ID sablonok listája (nem összevonva) a summary-ból: pages[].id_labels[].template"""
    out: list[str] = []
    if not summary_json:
        return out
    try:
        root = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    except Exception:
        root = summary_json

    pages = []
    if isinstance(root, dict):
        pages = root.get("pages") or root.get("summary") or []
    elif isinstance(root, list):
        pages = root

    for p in (pages or []):
        for lab in (p.get("id_labels") or []):
            tpl = (lab.get("template") or "").strip()
            if tpl:
                out.append(tpl)
    return out


def _ft_cards_from_summary(summary_json) -> list[dict]:
    """FROM/TO kártyák listája a summary JSON-ból (NEM összevonva sablon szerint). Side hide támogatással."""
    cards: list[dict] = []
    if not summary_json:
        return cards

    try:
        root = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    except Exception:
        root = summary_json

    pages = []
    if isinstance(root, dict):
        pages = root.get("pages") or root.get("summary") or []
    elif isinstance(root, list):
        pages = root

    for pi, p in enumerate(pages or []):
        for gi, g in enumerate((p.get("groups") or [])):
            # connector code -> value map (a, b, c...)
            cmap = {}
            for c in (g.get("connectors") or []):
                code = (c.get("id") or c.get("code") or "").strip()
                val  = (c.get("value") or "").strip()
                if code:
                    cmap[code] = val

            for qi, pair in enumerate((g.get("pairs") or [])):
                if _parse_bool(pair.get("hidden", pair.get("hide", pair.get("is_hidden", False)))):
                    continue

                from_hidden = _parse_bool(pair.get("from_hidden",
                                  pair.get("hide_from", pair.get("from_hide", pair.get("fromHidden", False)))))
                to_hidden   = _parse_bool(pair.get("to_hidden",
                                  pair.get("hide_to",   pair.get("to_hide",   pair.get("toHidden", False)))))

                def _mk_item(item: dict, side: str, ii: int):
                    tpl = (item.get("template") or "").strip()
                    if not tpl:
                        return
                    # QC-mód: text1/text2 után a sel1/sel2 (legördülő) érték is számít
                    t1 = (item.get("text1") or item.get("up")   or item.get("sel1") or "").strip()
                    t2 = (item.get("text2") or item.get("down") or item.get("sel2") or "").strip()
                    up = (cmap.get(t1) or t1).strip()
                    dn = (cmap.get(t2) or t2).strip()
                    cards.append({
                        "id": f"ft:{pi}:{gi}:{qi}:{side}:{ii}",
                        "template": tpl,
                        "kind": "ft",
                        "side": side,              # "from" / "to"
                        "lines": [up, dn],
                        "per_unit": 1,
                        "meta": {"page": pi, "group": gi, "pair": qi, "index": ii},
                    })

                from_items = pair.get("from_items") or []
                to_items   = pair.get("to_items") or []

                # új forma
                if from_items or to_items:
                    if not from_hidden:
                        for ii, it in enumerate(from_items):
                            if isinstance(it, dict):
                                _mk_item(it, "from", ii)
                    if not to_hidden:
                        for ii, it in enumerate(to_items):
                            if isinstance(it, dict):
                                _mk_item(it, "to", ii)
                else:
                    # legacy: pair.from / pair.to
                    f = pair.get("from") or {}
                    t = pair.get("to")   or {}
                    if (not from_hidden) and isinstance(f, dict) and (f.get("template") or "").strip():
                        _mk_item(f, "from", 0)
                    if (not to_hidden) and isinstance(t, dict) and (t.get("template") or "").strip():
                        _mk_item(t, "to", 0)

    return cards





@bartender_bp.post("/api/bartender/special/save")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def special_labels_save():
    """
    Ment csak a 'connector_labels' részt az `etiket data`.summary JSON-ba.
    Body (JSON):
      { "pn":"...", "rev":"...", "ecn":"...", 
        "connector_labels":[ { "template":"X.btw", "connector_id":"A", "value":"..." }, ... ] }
    """
    import json
    data = request.get_json(silent=True) or {}
    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()
    cl  = data.get("connector_labels") or []

    if not pn or not rev or not ecn:
        return jsonify({"status":"error","message":"PN/REV/ECN kötelező"}), 400
    if not isinstance(cl, list):
        return jsonify({"status":"error","message":"connector_labels legyen lista"}), 400

    try:
        db = get_db()
        cur = db.cursor(dictionary=True)

        # Beolvassuk a jelenlegi summary-t (ha nincs sor, létrehozzuk)
        cur.execute("""
            SELECT summary, ID_sablon
            FROM `etiket data`
            WHERE PN=%s AND REV=%s AND ECN=%s
            LIMIT 1
        """, (pn, rev, ecn))
        row = cur.fetchone()

        if row:
            raw = row.get("summary")
            try:
                root = json.loads(raw) if raw else {}
            except Exception:
                root = {}
        else:
            root = {}

        # Normalizálás: dict formát használunk {"pages":[...], "connector_labels":[...]}
        if isinstance(root, list):
            root = {"pages": root}
        if not isinstance(root, dict):
            root = {}

        pages = root.get("pages") if isinstance(root.get("pages"), list) else []
        # csak ezt frissítjük:
        root["connector_labels"] = cl

        summary_json = json.dumps({"pages": pages, "connector_labels": cl},
                                  ensure_ascii=False, separators=(",",":"))

        if row:
            cur.execute("""
                UPDATE `etiket data`
                SET summary = %s
                WHERE PN=%s AND REV=%s AND ECN=%s
            """, (summary_json, pn, rev, ecn))
        else:
            # minimál insert – a több mezőt üresen hagyjuk
            cur.execute("""
                INSERT INTO `etiket data` (PN, REV, ECN, summary, ID_sablon)
                VALUES (%s, %s, %s, %s, %s)
            """, (pn, rev, ecn, summary_json, ""))

        db.commit()
        cur.close()
        return jsonify({"status":"success"}), 200

    except Exception as e:
        try:
            get_db().rollback()
        except Exception:
            pass
        print("special_labels_save hiba:", repr(e))
        return jsonify({"status":"error","message":"Szerver hiba mentés közben."}), 500


@bartender_bp.get("/api/bartender/special/load")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def special_labels_load():
    """
    Csak a 'connector_labels' visszaadása.
    Query: ?pn=...&rev=...&ecn=...
    Válasz: { status:"success", connector_labels:[ ... ] }
    """
    import json
    pn  = (request.args.get("pn")  or "").strip()
    rev = (request.args.get("rev") or "").strip()
    ecn = (request.args.get("ecn") or "").strip()

    if not pn or not rev or not ecn:
        return jsonify({"status":"error","message":"PN/REV/ECN kötelező"}), 400

    try:
        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("""
            SELECT summary
            FROM `etiket data`
            WHERE PN=%s AND REV=%s AND ECN=%s
            LIMIT 1
        """, (pn, rev, ecn))
        row = cur.fetchone()
        cur.close()

        if not row:
            return jsonify({"status":"success","connector_labels":[]}), 200

        raw = row.get("summary")
        try:
            root = json.loads(raw) if raw else {}
        except Exception:
            root = {}

        # támogatjuk a régi formát is
        if isinstance(root, list):
            # régi formában nincs connector_labels
            return jsonify({"status":"success","connector_labels":[]}), 200

        cl = root.get("connector_labels") or []
        if not isinstance(cl, list):
            cl = []

        return jsonify({"status":"success","connector_labels":cl}), 200

    except Exception as e:
        print("special_labels_load hiba:", repr(e))
        return jsonify({"status":"error","message":"Szerver hiba betöltés közben."}), 500

# ===== SPECIAL LABELS – DB helper-ek =========================================

def _split_text_to_lines(s: str | None, max_lines: int = MAX_SPECIAL_LINES) -> list[str]:
    if not s:
        return []
    s = str(s).replace("\\n", "\n")
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    return lines[:max_lines]


def _special_fetch_all_by_pn(db, refer_pn: str) -> list[dict]:
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT id, refer_pn, template, text, created_at, updated_at
            FROM special_labels
            WHERE refer_pn = %s
            ORDER BY id ASC
        """, (refer_pn,))
        rows = cur.fetchall() or []
        for r in rows:
            r["lines"] = _split_text_to_lines(r.get("text") or "", MAX_SPECIAL_LINES)
        return rows
    finally:
        cur.close()

def _special_insert(db, refer_pn: str, template: str, text: str) -> int:
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO special_labels (refer_pn, template, text)
            VALUES (%s, %s, %s)
        """, (refer_pn, template, text))
        db.commit()
        return cur.lastrowid or 0
    finally:
        cur.close()

def _special_update(db, rec_id: int, template: str, text: str) -> bool:
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE special_labels
            SET template=%s, text=%s
            WHERE id=%s
        """, (template, text, rec_id))
        db.commit()
        return cur.rowcount > 0
    finally:
        cur.close()

def _special_delete(db, rec_id: int) -> bool:
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM special_labels WHERE id=%s", (rec_id,))
        db.commit()
        return cur.rowcount > 0
    finally:
        cur.close()

def _special_delete_by_pn(db, refer_pn: str) -> int:
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM special_labels WHERE refer_pn=%s", (refer_pn,))
        db.commit()
        return cur.rowcount or 0
    finally:
        cur.close()


# =============================================================================
# SPECIAL LABELS – minimal CRUD (refer_pn, template, text)
# =============================================================================

@bartender_bp.get("/api/special-labels")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def special_list():
    pn = (request.args.get("pn") or "").strip()
    if not pn:
        return jsonify({"status":"error", "message":"pn kötelező"}), 400
    db = get_db()
    rows = _special_fetch_all_by_pn(db, pn)
    return jsonify({"status":"success", "items": rows}), 200

@bartender_bp.post("/api/special-labels")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def special_create():
    j = request.get_json(silent=True) or {}
    pn  = (j.get("refer_pn") or "").strip()
    tpl = (j.get("template") or "").strip()
    txt = (j.get("text") or "").strip()
    if not pn or not tpl or not txt:
        return jsonify({"status":"error","message":"refer_pn, template, text kötelező"}), 400
    db = get_db()
    new_id = _special_insert(db, pn, tpl, txt)
    return jsonify({"status":"success","id": new_id}), 201

@bartender_bp.put("/api/special-labels/<int:rec_id>")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def special_update(rec_id: int):
    j = request.get_json(silent=True) or {}
    tpl = (j.get("template") or "").strip()
    txt = (j.get("text") or "").strip()
    if not tpl or not txt:
        return jsonify({"status":"error","message":"template és text kötelező"}), 400
    db = get_db()
    ok = _special_update(db, rec_id, tpl, txt)
    if not ok:
        return jsonify({"status":"error","message":"Nem található rekord"}), 404
    return jsonify({"status":"success"}), 200

@bartender_bp.delete("/api/special-labels/<int:rec_id>")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def special_delete(rec_id: int):
    db = get_db()
    ok = _special_delete(db, rec_id)
    if not ok:
        return jsonify({"status":"error","message":"Nem található rekord"}), 404
    return jsonify({"status":"success"}), 200

@bartender_bp.delete("/api/special-labels")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def special_delete_all_for_pn():
    pn = (request.args.get("pn") or "").strip()
    if not pn:
        return jsonify({"status":"error","message":"pn kötelező"}), 400
    db = get_db()
    cnt = _special_delete_by_pn(db, pn)
    return jsonify({"status":"success","deleted": cnt}), 200


# routes/bartender.py
def _collect_specials_for(pn: str, rev: str, ecn: str, summary_json) -> list[dict]:
    """
    Visszaad: [ {template, lines:[...], qty:int}, ... ]
    Prioritás: ha summary.special_labels létezik → azt használjuk;
               különben special_labels tábla PN alapján (fallback).
    Mindig deduplikálva (template+lines).
    """
    # 1) próbáljuk a summary JSON-ból
    try:
        root = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    except Exception:
        root = summary_json

    if isinstance(root, dict) and isinstance(root.get("special_labels"), list):
        return _dedupe_specials(root.get("special_labels"))

    # 2) fallback: DB
    items = []
    try:
        db = get_db()
        for r in _special_fetch_all_by_pn(db, pn):
            tpl = (r.get("template") or "").strip()
            if not tpl:
                continue
            txt = r.get("text") or ""
            try:
                obj = json.loads(txt)
                if isinstance(obj, list):
                    lines = [str(s or "").strip() for s in obj][:MAX_SPECIAL_LINES]
                else:
                    lines = _split_text_to_lines(txt, MAX_SPECIAL_LINES)
            except Exception:
                lines = _split_text_to_lines(txt, MAX_SPECIAL_LINES)
            items.append({"template": tpl, "lines": lines, "qty": 1})
    except Exception:
        pass

    return _dedupe_specials(items)


def _norm_special_item(it: dict) -> dict:
    tpl = (it.get("template") or "").strip()

    # lines normalizálás
    raw_lines = it.get("lines") or []
    if not isinstance(raw_lines, list):
        raw_lines = []

    lines = []
    for s in raw_lines:
        s2 = (str(s) if s is not None else "").strip()
        if s2:
            lines.append(s2)

    # qty normalizálás
    try:
        qty = int(it.get("qty") or 1)
    except Exception:
        qty = 1
    qty = max(1, qty)

    return {"template": tpl, "lines": lines[:MAX_SPECIAL_LINES], "qty": qty}


def _dedupe_specials(items: list[dict]) -> list[dict]:
    """
    Dedupe kulcs: (template, lines)
    ÚJ viselkedés: ha duplikált, NEM eldobjuk, hanem összeadjuk a qty-t.
    Így 2 teljesen egyforma speciális etikett -> 1 rekord qty=2-vel.
    """
    merged: dict[tuple[str, tuple[str, ...]], dict] = {}

    for raw in (items or []):
        it = _norm_special_item(raw if isinstance(raw, dict) else {})
        tpl = it.get("template") or ""
        if not tpl:
            continue

        key = (tpl, tuple(it.get("lines") or []))

        if key not in merged:
            merged[key] = {"template": tpl, "lines": it.get("lines") or [], "qty": int(it.get("qty") or 1)}
        else:
            merged[key]["qty"] = int(merged[key].get("qty") or 1) + int(it.get("qty") or 1)

    # stabil sorrend: a dict insertion-ordert megtartja (Python 3.7+)
    return list(merged.values())


@bartender_bp.post("/api/bartender/save_special_labels")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def save_special_labels():
    j = request.get_json(silent=True) or {}
    labels = j.get("labels") or j.get("items") or []
    if not isinstance(labels, list): labels = []
    
    cleaned = _dedupe_specials(labels)

    # PN feloldás (ha csak config_id jön, kerítsük elő a PN-t a konfigurációból)
    pn = (j.get("pn") or "").strip()
    if not pn:
        try:
            cfg = j.get("config_id")
            if cfg:
                info = fetch_config_as_ui_json(int(cfg))
                pn = (info.get("pn") or "").strip()
        except Exception:
            pn = ""
    if not pn:
        return jsonify({"status": "error", "message": "pn kötelező (vagy adj config_id-t, amelyből kiolvasható)"}), 400

    cleaned = _dedupe_specials(labels)
    try:
        db = get_db()
        cur = db.cursor()
        # TELJES TÖRLÉS PN ALAPJÁN, majd újraírás
        cur.execute("DELETE FROM special_labels WHERE refer_pn = %s", (pn,))
        if cleaned:
            sql = "INSERT INTO special_labels (refer_pn, template, text) VALUES (%s, %s, %s)"
            rows = [(pn, it["template"], json.dumps(it["lines"], ensure_ascii=False)) for it in cleaned]
            cur.executemany(sql, rows)
        db.commit(); cur.close()
        return jsonify({"status": "success", "pn": pn, "inserted": len(cleaned)}), 200
    except Exception as e:
        try: get_db().rollback()
        except Exception: pass
        current_app.logger.exception("save_special_labels hiba")
        return jsonify({"status": "error", "message": str(e)}), 500

    
    
def _special_load_for_preview(db, pn: str, rev: str, ecn: str) -> list[dict]:
    """
    special_labels táblából összeszedjük a rekordokat, és egységesítjük:
      vissza: [ { "template": "...", "qty": 1, "lines": ["...", "...", ...] }, ... ]

    * Kompat:** támogatjuk a régi referenciát (refer_pn = PN) és az új hibás insertet is
      (refer_pn = config_id). Emiatt mindkettőre rákeresünk.
    """
    rows: list[dict] = []

    # 1) PN szerinti rekordok
    try:
        for r in _special_fetch_all_by_pn(db, pn):
            tpl = (r.get("template") or "").strip()
            if not tpl:
                continue
            # a 'text' oszlop lehet JSON lista is — egységesítsük sorokra
            txt = r.get("text") or ""
            try:
                obj = json.loads(txt)
                if isinstance(obj, list):
                    lines = [str(s or "").strip() for s in obj][:MAX_SPECIAL_LINES]
                else:
                    lines = _split_text_to_lines(txt, MAX_SPECIAL_LINES)
            except Exception:
                lines = _split_text_to_lines(txt, MAX_SPECIAL_LINES)

            rows.append({"template": tpl, "qty": 1, "lines": lines})
    except Exception:
        pass

    # 2) config_id szerinti (ha valaha így lett mentve)
    try:
        cfg = get_or_create_config_id(pn, rev or None, ecn or None)
        cur = db.cursor(dictionary=True)
        # refer_pn lehet szám vagy string; mindkettőt lefedjük
        cur.execute("""
            SELECT id, refer_pn, template, text
            FROM special_labels
            WHERE refer_pn = %s OR refer_pn = %s
            ORDER BY id ASC
        """, (str(cfg), int(cfg)))
        for r in cur.fetchall() or []:
            tpl = (r.get("template") or "").strip()
            if not tpl:
                continue
            txt = r.get("text") or ""
            try:
                obj = json.loads(txt)
                if isinstance(obj, list):
                    lines = [str(s or "").strip() for s in obj][:MAX_SPECIAL_LINES]
                else:
                    lines = _split_text_to_lines(txt, MAX_SPECIAL_LINES)
            except Exception:
                lines = _split_text_to_lines(txt, MAX_SPECIAL_LINES)
            rows.append({"template": tpl, "qty": 1, "lines": lines})
        cur.close()
    except Exception:
        pass

    return rows
    
    
def _collect_special_labels(db, row: dict, pn: str, rev: str, ecn: str) -> list[dict]:
    """
    Összegyűjti a SPECIAL címkéket két helyről és egységes formában adja vissza:
      - summary JSON:  { "special_labels":[{template, qty, lines[]}...] }
      - special_labels tábla:  refer_pn = PN (és kompat: refer_pn = config_id)
    Kimenet: [ { "template": str, "qty": int>=1, "lines": [str,str,str,str] }, ... ]
    """
    out: dict[str, dict] = {}

    # 1) summary JSON
    try:
        parsed_summary = json.loads(row.get("summary") or "{}")
    except Exception:
        parsed_summary = {}
    if isinstance(parsed_summary, dict):
        for rec in (parsed_summary.get("special_labels") or []):
            tpl   = (rec.get("template") or "").strip()
            qty   = max(1, int(rec.get("qty") or 1))
            lines = [str(s or "").strip() for s in (rec.get("lines") or [])][:MAX_SPECIAL_LINES]
            if not tpl:
                continue
            cur = out.setdefault(tpl, {"template": tpl, "qty": 0, "lines": []})
            cur["qty"] += qty
            if not cur["lines"] and lines:
                cur["lines"] = lines

    # 2) special_labels tábla (PN-re és – kompat – config_id-re is ránézünk)
    for r in _special_load_for_preview(db, pn, rev, ecn) or []:
        tpl   = (r.get("template") or "").strip()
        qty   = max(1, int(r.get("qty") or 1))
        lines = [str(s or "").strip() for s in (r.get("lines") or [])][:MAX_SPECIAL_LINES]
        if not tpl:
            continue
        cur = out.setdefault(tpl, {"template": tpl, "qty": 0, "lines": []})
        cur["qty"] += qty
        if not cur["lines"] and lines:
            cur["lines"] = lines

    # rendezett lista (stabil sorrend)
    return [out[k] for k in sorted(out.keys(), key=lambda s: s.lower())]
    
from uuid import uuid4
from datetime import timedelta

LOCK_TTL_SECONDS = 120  # 2 perc heartbeat nélkül -> stale

def _db_conn(db):
    # nálad néha wrapper van (db.conn), néha sima conn
    return getattr(db, "conn", db)

def _now():
    return datetime.now()

def _current_user_name():
    try:
        u = session.get("user") or {}
        return (u.get("username") or u.get("name") or "").strip() or (request.headers.get("X-User") or "unknown")
    except Exception:
        return request.headers.get("X-User") or "unknown"

def _lock_validate(db, config_id: int, token: str) -> bool:
    conn = _db_conn(db)
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT lock_token, heartbeat_at FROM bartender_project_locks WHERE config_id=%s", (config_id,))
        row = cur.fetchone()
        if not row:
            return False
        if (row.get("lock_token") or "") != (token or ""):
            return False

        # opcionális TTL check (ha lejárt, inkább invalid)
        hb = row.get("heartbeat_at")
        if hb and hb < (_now() - timedelta(seconds=LOCK_TTL_SECONDS)):
            return False
        return True
    finally:
        cur.close()

def _lock_status(db, config_id: int) -> dict | None:
    conn = _db_conn(db)
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT locked_by, lock_token, locked_at, heartbeat_at
            FROM bartender_project_locks
            WHERE config_id=%s
        """, (config_id,))
        return cur.fetchone()
    finally:
        cur.close()

def _lock_acquire(db, config_id: int, user: str) -> tuple[bool, dict]:
    """
    return: (owner, payload)
      owner=True: payload {status:'ok', owner:true, token:'...', locked_by:'...'}
      owner=False: payload {status:'locked', owner:false, locked_by:'...'}
    """
    conn = _db_conn(db)
    now = _now()
    stale_before = now - timedelta(seconds=LOCK_TTL_SECONDS)
    token = str(uuid4())

    # mysql.connector támogatja: start_transaction()
    try:
        conn.start_transaction()
    except Exception:
        pass

    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM bartender_project_locks WHERE config_id=%s FOR UPDATE", (config_id,))
        row = cur.fetchone()

        if row is None:
            cur.execute("""
                INSERT INTO bartender_project_locks
                    (config_id, locked_by, lock_token, locked_at, heartbeat_at)
                VALUES (%s,%s,%s,%s,%s)
            """, (config_id, user, token, now, now))
            conn.commit()
            return True, {"status":"ok","owner":True,"token":token,"locked_by":user}

        # ha ugyanaz a user nyitja újra: megújítjuk a tokenjét
        if (row.get("locked_by") or "") == user:
            cur.execute("""
                UPDATE bartender_project_locks
                SET lock_token=%s, heartbeat_at=%s
                WHERE config_id=%s
            """, (token, now, config_id))
            conn.commit()
            return True, {"status":"ok","owner":True,"token":token,"locked_by":user}

        # stale lock -> átvehető
        hb = row.get("heartbeat_at")
        if hb is None or hb < stale_before:
            cur.execute("""
                UPDATE bartender_project_locks
                SET locked_by=%s, lock_token=%s, locked_at=%s, heartbeat_at=%s
                WHERE config_id=%s
            """, (user, token, now, now, config_id))
            conn.commit()
            return True, {"status":"ok","owner":True,"token":token,"locked_by":user,"stolen":True}

        conn.commit()
        return False, {
            "status":"locked",
            "owner":False,
            "locked_by": row.get("locked_by"),
            "locked_at": str(row.get("locked_at") or ""),
            "heartbeat_at": str(row.get("heartbeat_at") or ""),
        }

    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        cur.close()


@bartender_bp.post("/api/bartender/lock_config")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_lock_config():
    j = request.get_json(silent=True) or {}
    try:
        config_id = int(j.get("config_id") or 0)
    except (TypeError, ValueError):
        config_id = 0

    if not config_id:
        return jsonify({"status":"error","message":"config_id kötelező"}), 400

    db = get_db()
    user = _current_user_name()
    try:
        owner, payload = _lock_acquire(db, config_id, user)
        return jsonify(payload), (200 if owner else 423)
    except Exception:
        current_app.logger.exception("lock_config failed")
        return jsonify({"status":"error","message":"Lock hiba"}), 500


@bartender_bp.post("/api/bartender/lock_heartbeat")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_lock_heartbeat():
    j = request.get_json(silent=True) or {}
    try:
        config_id = int(j.get("config_id") or 0)
    except (TypeError, ValueError):
        config_id = 0

    token = (j.get("token") or "").strip()
    if not config_id or not token:
        return jsonify({"status":"error","message":"config_id + token kötelező"}), 400

    db = get_db()
    conn = _db_conn(db)
    cur = conn.cursor()
    now = _now()
    try:
        cur.execute("""
            UPDATE bartender_project_locks
            SET heartbeat_at=%s
            WHERE config_id=%s AND lock_token=%s
        """, (now, config_id, token))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"status":"lost"}), 409
        return jsonify({"status":"ok"}), 200
    finally:
        cur.close()


@bartender_bp.post("/api/bartender/unlock_config")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_unlock_config():
    j = request.get_json(silent=True) or {}
    try:
        config_id = int(j.get("config_id") or 0)
    except (TypeError, ValueError):
        config_id = 0

    token = (j.get("token") or "").strip()
    if not config_id or not token:
        return jsonify({"status":"error","message":"config_id + token kötelező"}), 400

    db = get_db()
    conn = _db_conn(db)
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM bartender_project_locks WHERE config_id=%s AND lock_token=%s", (config_id, token))
        conn.commit()
        return jsonify({"status":"ok"}), 200
    finally:
        cur.close()


@bartender_bp.post("/api/bartender/lock_project")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_lock_project_alias():
    j = request.get_json(silent=True) or {}

    # fogadjuk el a config_id-t közvetlenül, vagy PN/REV/ECN-ből számoljuk
    try:
        config_id = int(j.get("config_id") or 0)
    except (TypeError, ValueError):
        config_id = 0

    if not config_id:
        pn  = (j.get("pn")  or "").strip()
        rev = (j.get("rev") or "").strip()
        ecn = (j.get("ecn") or "").strip()
        if pn:
            config_id = int(get_or_create_config_id(pn, rev or None, ecn or None))

    if not config_id:
        return jsonify({"status":"error","message":"config_id (vagy pn) kötelező"}), 400

    db = get_db()
    user = _current_user_name()
    owner, payload = _lock_acquire(db, config_id, user)

    # a frontendbarát formát adjuk vissza
    if owner:
        return jsonify({
            "status": "success",
            "acquired": True,
            "is_owner": True,
            "locked_by": payload.get("locked_by"),
            "lock_token": payload.get("token"),
            "config_id": config_id,
        }), 200

    return jsonify({
        "status": "success",
        "acquired": False,
        "is_owner": False,
        "locked_by": payload.get("locked_by"),
        "locked_at": payload.get("locked_at"),
        "heartbeat_at": payload.get("heartbeat_at"),
        "config_id": config_id,
    }), 200


@bartender_bp.post("/api/bartender/heartbeat_lock")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_heartbeat_lock_alias():
    j = request.get_json(silent=True) or {}

    try:
        config_id = int(j.get("config_id") or j.get("configId") or 0)
    except (TypeError, ValueError):
        config_id = 0

    token = (j.get("lock_token") or j.get("lockToken") or j.get("token") or "").strip()

    if not config_id or not token:
        return jsonify({"status":"error","message":"config_id + lock_token kötelező"}), 400

    db = get_db()
    conn = _db_conn(db)
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE bartender_project_locks
            SET heartbeat_at=%s
            WHERE config_id=%s AND lock_token=%s
        """, (_now(), config_id, token))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"status":"lost"}), 409
        return jsonify({"status":"ok"}), 200
    finally:
        cur.close()


@bartender_bp.post("/api/bartender/unlock_project")
@require_roles(MANAGER_ROLES, IT_ROLES, ETIKET_CREATOR_ROLE)
def api_unlock_project_alias():
    j = request.get_json(silent=True) or {}

    try:
        config_id = int(j.get("config_id") or j.get("configId") or 0)
    except (TypeError, ValueError):
        config_id = 0

    token = (j.get("lock_token") or j.get("lockToken") or j.get("token") or "").strip()

    if not config_id or not token:
        return jsonify({"status":"error","message":"config_id + lock_token kötelező"}), 400

    db = get_db()
    conn = _db_conn(db)
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM bartender_project_locks WHERE config_id=%s AND lock_token=%s",
            (config_id, token)
        )
        conn.commit()
        return jsonify({"status":"ok"}), 200
    finally:
        cur.close()

from collections import defaultdict

def _allowed_lines_map_from_preview(line_preview: dict[str, list[str]], specials: list[dict]) -> dict[str, set[tuple[str, ...]]]:
    """
    template -> engedélyezett lines tuple-k (aktuális preview alapján)
    - con: line_preview[tpl] nálad több "értéket" tartalmaz (["T93","T94",...]) -> mindegyik külön (val,) tuple
    - from/to/id: 1 tuple = a preview sorok
    - specials: template+lines tuple-k külön-külön engedélyezve
    """
    allowed: dict[str, set[tuple[str, ...]]] = defaultdict(set)

    for tpl, arr in (line_preview or {}).items():
        tpl = (tpl or "").strip()
        if not tpl:
            continue
        side = _side_of_template(tpl)

        if side == "con":
            for v in (arr or []):
                v = str(v or "").strip()
                if v:
                    allowed[tpl].add((v,))
        else:
            tup = tuple(str(x or "").strip() for x in (arr or []) if str(x or "").strip())
            if tup:
                allowed[tpl].add(tup)

    # specials: többféle lines ugyanahhoz a template-hez is lehet
    for rec in (specials or []):
        tpl = (rec.get("template") or "").strip()
        if not tpl:
            continue
        lines = [str(s or "").strip() for s in (rec.get("lines") or []) if str(s or "").strip()]
        allowed[tpl].add(tuple(lines))

    return allowed


def _fetch_history_cards_grouped(db, pn: str, wo: str, rev: str = "", ecn: str = "", limit: int = 200) -> list[dict]:
    cur = db.cursor(dictionary=True)
    where = ["pn=%s", "wo=%s"]
    args = [pn, wo]

    if (rev or "").strip():
        where.append("rev=%s")
        args.append(rev.strip())
    if (ecn or "").strip():
        where.append("ecn=%s")
        args.append(ecn.strip())

    sql = f"""
        SELECT
            template,
            printer_name,
            lines_json,
            SUM(printed_copies) AS printed_sum,
            COUNT(*) AS jobs,
            MAX(printed_at) AS last_printed
        FROM print_log
        WHERE {" AND ".join(where)}
        GROUP BY template, printer_name, lines_json
        ORDER BY last_printed DESC
        LIMIT %s
    """
    args.append(int(limit))

    try:
        cur.execute(sql, tuple(args))
        rows = cur.fetchall() or []
    finally:
        cur.close()

    out = []
    for r in rows:
        try:
            lines = json.loads(r.get("lines_json") or "[]")
            if not isinstance(lines, list):
                lines = []
        except Exception:
            lines = []

        out.append({
            "template": (r.get("template") or "").strip(),
            "printer_name": _norm_ws(r.get("printer_name") or ""),
            "lines": [str(x or "").strip() for x in lines if str(x or "").strip()],
            "printed_sum": int(r.get("printed_sum") or 0),
            "jobs": int(r.get("jobs") or 0),
            "last_printed": str(r.get("last_printed") or ""),
        })
    return out


def _filter_history_to_current(history_cards: list[dict],
                              templates_effective: dict[str, int],
                              printers_resolved: dict[str, str],
                              allowed_lines: dict[str, set[tuple[str, ...]]]) -> list[dict]:

    current_tpls = set((templates_effective or {}).keys())
    out = []

    for h in (history_cards or []):
        tpl = (h.get("template") or "").strip()
        if tpl not in current_tpls:
            continue

        cur_pr  = _norm_ws((printers_resolved or {}).get(tpl) or "")
        hist_pr = _norm_ws(h.get("printer_name") or "")
        if cur_pr and hist_pr and cur_pr != hist_pr:
            continue

        allowed_set = allowed_lines.get(tpl)
        if allowed_set:
            key = tuple(h.get("lines") or [])
            if key not in allowed_set:
                continue

        out.append(h)

    return out


def _norm_ws(s: str | None) -> str:
    # minden whitespace (tab, \n, dupla space) -> single space
    return " ".join(str(s or "").split()).strip()


# ===========================
# OTD API (Dashboardhoz)
# ===========================
import datetime as _dt

_OTD_TABLES = {
    "EMI": "OTD_EMI",
    "MTE": "OTD_MTE",
    "MDI": "OTD_MDI",
    "SIEMENS": "OTD_Siemens",
    "SWISSVARIAN": "OTD_Swissvarian",
}

_OTD_SELECT_COLS = """
    wo_nbr       AS `WO Nbr`,
    start_date   AS `Start Date`,
    due_date     AS `Due Date`,
    ship_date    AS `Ship Date`,
    oper         AS `Oper`,
    cell         AS `Cell`,
    dol          AS `DOL`,
    loc          AS `LOC`,
    cur_wc       AS `CUR WC`,
    part_number  AS `Part Number`,
    pr_st        AS `PR ST`,
    cust_code    AS `Cust Code`,
    qty_mfg      AS `QTY MFG`,
    hours        AS `Hours`
"""

def _iso_week_today() -> int:
    return _dt.date.today().isocalendar().week

def _otd_allowed_scopes_for_user() -> list[str]:
    """
    IT / MANAGER => minden
    egyébként: job_title / role szövegben keres EMI/MTE/MDI/SIEMENS/SWISSVARIAN kulcsszavakat
    """
    u = session.get("user") or {}

    # Elsődleges: "Egyéb beállítások" oldalon felvett OTD scope szabály.
    from services import tl_settings
    override = tl_settings.otd_scopes_for(u)
    if override:
        return [s for s in override if s in _OTD_TABLES]

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
        if k in blob:
            scopes.append(k)

    # ha nem talál semmit, ne omoljon össze a UI:
    # (ha szigorúbb kell, itt visszaadhatsz []-t)
    return scopes or list(_OTD_TABLES.keys())

def _db_main():
    """
    Próbáljuk a fő DB-t (paperless) használni: services.db.get_db.
    Ha nincs ilyen import nálad, fallback a meglévő get_db()-re.
    """
    try:
        from services.db import get_db as _get_db_main  # nálad ez a paperless szokott lenni
        return _get_db_main()
    except Exception:
        return get_db()  # bartender_db fallback (ha nálad egy DB van, ez is jó)

def _to_jsonable(v):
    if isinstance(v, (_dt.date, _dt.datetime)):
        # DATE -> YYYY-MM-DD, DATETIME -> YYYY-MM-DD HH:MM:SS
        if isinstance(v, _dt.datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        return v.strftime("%Y-%m-%d")
    return v

@bartender_bp.get("/api/otd")
@require_roles(MANAGER_ROLES, TEAMLEADER_ROLES, IT_ROLES)
def api_otd():
    """
    GET /api/otd?week=6&scope=EMI
    scope optional:
      - nincs => user scope alapján
      - scope=ALL => user scope alapján (összes engedélyezett)
      - scope=EMI/MTE/... => csak az (ha engedélyezett)
    """
    week = request.args.get("week", "").strip()
    scope = request.args.get("scope", "").strip().upper()

    try:
        week_i = int(week) if week else _iso_week_today()
    except ValueError:
        return jsonify(ok=False, error="Invalid week"), 400

    allowed = _otd_allowed_scopes_for_user()

    if scope and scope != "ALL":
        if scope not in allowed:
            return jsonify(ok=False, error=f"Scope not allowed: {scope}", allowed=allowed), 403
        scopes = [scope]
    else:
        scopes = allowed

    db = _db_main()
    conn = getattr(db, "conn", db)
    cur = conn.cursor(dictionary=True)

    data = {}
    try:
        for sc in scopes:
            tbl = _OTD_TABLES.get(sc)
            if not tbl:
                continue

            # table name whitelistből jön, safe
            sql = f"""
                SELECT {_OTD_SELECT_COLS}
                FROM {tbl}
                WHERE week = %s
                ORDER BY due_date ASC, ship_date ASC, wo_nbr ASC
            """
            cur.execute(sql, (week_i,))
            rows = cur.fetchall() or []
            # json kompatibilis
            fixed = [{k: _to_jsonable(v) for k, v in r.items()} for r in rows]
            data[sc] = fixed

        return jsonify(ok=True, week=week_i, scopes=scopes, data=data)

    finally:
        cur.close()
      
@bartender_bp.route('/api/bartender/check_label_config_exists', methods=['POST'])
def check_label_config_exists():
    data = request.get_json(silent=True) or {}
    pn  = (data.get('pn')  or '').strip()
    rev = (data.get('rev') or '').strip()
    ecn = (data.get('ecn') or '').strip()

    if not pn or not rev or not ecn:
        return jsonify({"found": False, "error": "missing_fields"}), 400

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT 1
            FROM `etiket data`
            WHERE UPPER(PN)=UPPER(%s)
              AND UPPER(REV)=UPPER(%s)
              AND UPPER(ECN)=UPPER(%s)
            LIMIT 1
        """, (pn, rev, ecn))
        found = cur.fetchone() is not None
        return jsonify({"found": found}), 200
    finally:
        cur.close()
        
@bartender_bp.post("/api/bartender/delete_saved_pn")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_delete_saved_pn():
    data = request.get_json(silent=True) or {}
    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()

    if not pn or not rev or not ecn:
        return jsonify({"status": "error", "message": "PN/REV/ECN kötelező"}), 400

    db  = get_db()
    cur = db.cursor()

    # Biztonságos: ID alapján törlünk (utolsó frissített találat)
    # MySQL/MariaDB: kell a dupla SELECT (target table workaround)
    sql = """
      DELETE FROM `etiket data`
      WHERE ID = (
        SELECT ID FROM (
          SELECT ID
          FROM `etiket data`
          WHERE PN=%s AND REV=%s AND ECN=%s
          ORDER BY COALESCE(`updated date`, `create date`) DESC, ID DESC
          LIMIT 1
        ) t
      )
    """
    cur.execute(sql, (pn, rev, ecn))
    db.commit()

    if cur.rowcount <= 0:
        cur.close()
        return jsonify({"status": "error", "message": "Nincs ilyen mentett projekt"}), 404

    cur.close()
    return jsonify({"status": "success"})

# bartender.py

from flask import request, jsonify


# ===== PRINT COUNTER + REPRINT LOG API =======================================

@bartender_bp.get("/bartender/api/printed_status", endpoint="api_printed_status_v1")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_printed_status_v1():
    """
    Query: pn, rev, ecn, key
    Válasz: { ok:true, printed:int, total_allowed:int }
    """
    pn  = (request.args.get("pn")  or "").strip()
    rev = (request.args.get("rev") or "").strip()
    ecn = (request.args.get("ecn") or "").strip()
    key = (request.args.get("key") or "").strip()

    if not (pn and rev and ecn and key):
        return jsonify({"ok": False, "error": "missing params"}), 400

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT printed, total_allowed
            FROM bartender_printed_counter
            WHERE pn=%s AND rev=%s AND ecn=%s AND card_key=%s
            LIMIT 1
        """, (pn, rev, ecn, key))
        row = cur.fetchone() or {}
        # a total_allowed-ot a frontend is kéri (fetchPrintedFromBackend);
        # enélkül mindig 0-t kapott, és csak a DOM-beli darabszámra hagyatkozhatott
        return jsonify({
            "ok": True,
            "printed": int(row.get("printed") or 0),
            "total_allowed": int(row.get("total_allowed") or 0),
        }), 200
    finally:
        try: cur.close()
        except Exception: pass


@bartender_bp.post("/bartender/api/printed_set_total")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_printed_set_total():
    """
    Body: {pn, rev, ecn, key, total}
    total_allowed = GREATEST(existing, total)
    """
    data = request.get_json(silent=True) or {}
    pn    = (data.get("pn") or "").strip()
    rev   = (data.get("rev") or "").strip()
    ecn   = (data.get("ecn") or "").strip()
    key   = (data.get("key") or "").strip()
    total = int(data.get("total") or 0)

    if not (pn and rev and ecn and key):
        return jsonify({"ok": False, "error": "missing params"}), 400
    if total < 0:
        return jsonify({"ok": False, "error": "bad total"}), 400

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO bartender_printed_counter (pn, rev, ecn, card_key, total_allowed, printed)
            VALUES (%s,%s,%s,%s,%s,0)
            ON DUPLICATE KEY UPDATE
              total_allowed = GREATEST(total_allowed, VALUES(total_allowed))
        """, (pn, rev, ecn, key, total))
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("printed_set_total failed")
        return jsonify({"ok": False, "error": "db error"}), 500
    finally:
        try: cur.close()
        except Exception: pass

    # opcionális visszaolvasás
    cur2 = db.cursor(dictionary=True)
    try:
        cur2.execute("""
            SELECT printed, total_allowed
            FROM bartender_printed_counter
            WHERE pn=%s AND rev=%s AND ecn=%s AND card_key=%s
            LIMIT 1
        """, (pn, rev, ecn, key))
        row = cur2.fetchone() or {}
        return jsonify({
            "ok": True,
            "printed": int(row.get("printed") or 0),
            "total_allowed": int(row.get("total_allowed") or total)
        }), 200
    finally:
        try: cur2.close()
        except Exception: pass


@bartender_bp.post("/bartender/api/printed_add", endpoint="api_printed_add_v1")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_printed_add_v1():
    """
    Body: {pn, rev, ecn, key, add}
    printed += add (atomikusan)
    """
    data = request.get_json(silent=True) or {}
    pn  = (data.get("pn")  or "").strip()
    rev = (data.get("rev") or "").strip()
    ecn = (data.get("ecn") or "").strip()
    key = (data.get("key") or "").strip()
    add = int(data.get("add") or 0)

    if not (pn and rev and ecn and key):
        return jsonify({"ok": False, "error": "missing params"}), 400
    if add <= 0:
        return jsonify({"ok": False, "error": "bad add"}), 400

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO bartender_printed_counter (pn, rev, ecn, card_key, printed)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              printed = printed + VALUES(printed),
              updated_at = CURRENT_TIMESTAMP
        """, (pn, rev, ecn, key, add))
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("printed_add failed")
        return jsonify({"ok": False, "error": "db error"}), 500
    finally:
        try: cur.close()
        except Exception: pass

    # visszaolvasás
    cur2 = db.cursor(dictionary=True)
    try:
        cur2.execute("""
            SELECT printed
            FROM bartender_printed_counter
            WHERE pn=%s AND rev=%s AND ecn=%s AND card_key=%s
            LIMIT 1
        """, (pn, rev, ecn, key))
        row = cur2.fetchone() or {}
        return jsonify({"ok": True, "printed": int(row.get("printed") or 0)}), 200
    finally:
        try: cur2.close()
        except Exception: pass


@bartender_bp.post("/bartender/api/reprint_add")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_reprint_add():
    """
    Body:
    {
      pn, wo, rev, ecn,
      card_key,
      template,
      printer,
      copies,
      lines: [...]
    }
    -> külön táblába logoljuk, hogy REPRINT volt.
    """
    data = request.get_json(silent=True) or {}

    pn       = (data.get("pn") or "").strip()
    wo       = (data.get("wo") or "").strip()
    rev      = (data.get("rev") or "").strip()
    ecn      = (data.get("ecn") or "").strip()
    card_key = (data.get("card_key") or data.get("key") or "").strip()
    template = (data.get("template") or "").strip()
    printer  = (data.get("printer") or "").strip()
    copies   = int(data.get("copies") or 0)
    lines    = data.get("lines") or []

    if not (pn and card_key and template):
        return jsonify({"ok": False, "error": "missing params"}), 400
    if copies <= 0:
        return jsonify({"ok": False, "error": "bad copies"}), 400
    if not isinstance(lines, list):
        lines = []

    # user
    try:
        user_name = (session.get("user") or {}).get("username") or (session.get("user") or {}).get("name")
    except Exception:
        user_name = None
    if not user_name:
        user_name = request.headers.get("X-User") or "unknown"

    lines_json = json.dumps(lines, ensure_ascii=False)

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO bartender_reprint_log
              (pn, wo, rev, ecn, card_key, template, printer, copies, lines_json, created_by, created_at)
            VALUES
              (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CURRENT_TIMESTAMP)
        """, (
            pn,
            wo or None,
            rev or None,
            ecn or None,
            card_key,
            template,
            printer or None,
            copies,
            lines_json,
            user_name
        ))
        db.commit()
        return jsonify({"ok": True}), 200
    except Exception:
        db.rollback()
        current_app.logger.exception("reprint_add failed")
        return jsonify({"ok": False, "error": "db error"}), 500
    finally:
        try: cur.close()
        except Exception: pass

# =============================================================================
# HIÁNYZÓ TEMPLATE ÉRTESÍTÉS
# =============================================================================

# =============================================================================
# TESZT MÓD (QC-blokk + automatikus email értesítések felfüggesztése)
# =============================================================================

def _bt_settings_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bartender_settings (
            k VARCHAR(64) PRIMARY KEY,
            v VARCHAR(255) NOT NULL DEFAULT '',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
    """)

def _bt_settings_get(key: str, default: str = "") -> str:
    try:
        db = get_db()
        cur = db.cursor()
        try:
            _bt_settings_table(cur)
            cur.execute("SELECT v FROM bartender_settings WHERE k=%s LIMIT 1", (key,))
            row = cur.fetchone()
            return (row[0] if row else default)
        finally:
            try: cur.close()
            except Exception: pass
    except Exception:
        current_app.logger.exception("bartender_settings olvasási hiba (k=%s)", key)
        return default

def _bt_settings_set(key: str, value: str) -> None:
    db = get_db()
    cur = db.cursor()
    try:
        _bt_settings_table(cur)
        cur.execute("""
            INSERT INTO bartender_settings (k, v) VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE v=VALUES(v)
        """, (key, value))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        try: cur.close()
        except Exception: pass

def _bt_test_mode_enabled() -> bool:
    """Teszt mód: a preview-t nem szakítja meg a QC-blokk, és az automatikus
    email értesítések (missing_template / qc_blocked) nem mennek ki."""
    return _bt_settings_get("test_mode", "0") == "1"

@bartender_bp.get("/api/bartender/test_mode")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_test_mode_get():
    return jsonify({"ok": True, "enabled": _bt_test_mode_enabled()}), 200

@bartender_bp.post("/api/bartender/test_mode")
@require_roles(MANAGER_ROLES, IT_ROLES)
def api_test_mode_set():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    try:
        _bt_settings_set("test_mode", "1" if enabled else "0")
    except Exception as exc:
        current_app.logger.exception("test_mode állítási hiba")
        return jsonify({"ok": False, "error": str(exc)}), 500
    try:
        u = session.get("user") or {}
        who = u.get("username") or u.get("name") or "unknown"
    except Exception:
        who = "unknown"
    current_app.logger.warning("Bartender TESZT MÓD %s (%s)",
                               "BEKAPCSOLVA" if enabled else "kikapcsolva", who)
    return jsonify({"ok": True, "enabled": enabled}), 200


@bartender_bp.post("/api/bartender/notify_missing_template")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_notify_missing_template():
    """
    Automatikus értesítés, ha egy PN/REV/ECN-hez nem található template.
    Az Email admin oldalon 'bartender_missing_template' page_key alatt
    konfigurálható a sablon és a fogadók (Oldal konfigurációk fül).

    Body: { pn, rev, ecn, wo, missing: ["REV"|"ECN"|"štítky"], triggered_by }

    Elérhető sablon változók:
      {{pn}}, {{rev}}, {{ecn}}, {{wo}}, {{missing}}, {{triggered_by}}, {{timestamp}}
    """
    data    = request.get_json(silent=True) or {}
    pn      = (data.get("pn")  or "").strip()
    rev     = (data.get("rev") or "").strip()
    ecn     = (data.get("ecn") or "").strip()
    wo      = (data.get("wo")  or "").strip()
    missing = data.get("missing") or []
    if not isinstance(missing, list):
        missing = [str(missing)]

    if not pn:
        return jsonify({"ok": False, "error": "pn kötelező"}), 400

    if _bt_test_mode_enabled():
        current_app.logger.info("[notify_missing_template] TESZT MÓD – email kihagyva (pn=%s)", pn)
        return jsonify({"ok": True, "skipped": "test_mode"}), 200

    try:
        u = session.get("user") or {}
        triggered_by = u.get("username") or u.get("name") or "unknown"
    except Exception:
        triggered_by = "unknown"

    missing_str = ", ".join(missing) if missing else "sablon"
    now_str     = datetime.now().strftime("%Y-%m-%d %H:%M")

    dynamic_data = {
        "pn":           pn,
        "rev":          rev or "—",
        "ecn":          ecn or "—",
        "wo":           wo  or "—",
        "missing":      missing_str,
        "triggered_by": triggered_by,
        "timestamp":    now_str,
    }

    PAGE_KEY = "bartender_missing_template"

    try:
        from services.email_core import send_for_page_key
        result = send_for_page_key(
            page_key     = PAGE_KEY,
            dynamic_data = dynamic_data,
            triggered_by = triggered_by,
        )
    except Exception as e:
        current_app.logger.exception("notify_missing_template: email hiba")
        result = {"ok": False, "error": str(e)}

    if not result.get("ok"):
        current_app.logger.warning(
            f"[notify_missing_template] Nincs '{PAGE_KEY}' konfig vagy küldési hiba: "
            f"pn={pn} rev={rev} ecn={ecn} | {result.get('error')}"
        )

    return jsonify(result), 200


# =============================================================================
# QC NEM ENGEDÉLYEZETT ÉRTESÍTÉS
# =============================================================================

@bartender_bp.post("/api/bartender/notify_qc_blocked")
@require_roles(MANAGER_ROLES, IT_ROLES, PRINTOPERATOR_ROLE, ETIKET_CREATOR_ROLE)
def api_notify_qc_blocked():
    """
    Automatikus értesítés, ha valaki QC által nem jóváhagyott
    PN/REV/ECN-hez próbál etikettet nyomtatni.

    Az Email admin oldalon 'bartender_qc_blocked' page_key alatt
    konfigurálható a sablon és a fogadók.

    Body: { pn, rev, ecn, wo, triggered_by }

    Elérhető sablon változók:
      {{pn}}, {{rev}}, {{ecn}}, {{wo}}, {{triggered_by}}, {{timestamp}}
    """
    data = request.get_json(silent=True) or {}
    pn   = (data.get("pn")  or "").strip()
    rev  = (data.get("rev") or "").strip()
    ecn  = (data.get("ecn") or "").strip()
    wo   = (data.get("wo")  or "").strip()

    if not pn:
        return jsonify({"ok": False, "error": "pn kötelező"}), 400

    if _bt_test_mode_enabled():
        current_app.logger.info("[notify_qc_blocked] TESZT MÓD – email kihagyva (pn=%s)", pn)
        return jsonify({"ok": True, "skipped": "test_mode"}), 200

    try:
        u = session.get("user") or {}
        triggered_by = u.get("username") or u.get("name") or "unknown"
    except Exception:
        triggered_by = "unknown"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    dynamic_data = {
        "pn":           pn,
        "rev":          rev or "—",
        "ecn":          ecn or "—",
        "wo":           wo  or "—",
        "triggered_by": triggered_by,
        "timestamp":    now_str,
    }

    PAGE_KEY = "bartender_qc_blocked"

    try:
        from services.email_core import send_for_page_key
        result = send_for_page_key(
            page_key     = PAGE_KEY,
            dynamic_data = dynamic_data,
            triggered_by = triggered_by,
        )
    except Exception as e:
        current_app.logger.exception("notify_qc_blocked: email hiba")
        result = {"ok": False, "error": str(e)}

    if not result.get("ok"):
        current_app.logger.warning(
            f"[notify_qc_blocked] Nincs '{PAGE_KEY}' konfig vagy küldési hiba: "
            f"pn={pn} rev={rev} ecn={ecn} | {result.get('error')}"
        )

    return jsonify(result), 200