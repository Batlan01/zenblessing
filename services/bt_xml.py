# -*- coding: utf-8 -*-

import time
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .bt_config import (
    XML_PRIMARY_PATH,
    XML_SCAN_FILE,
    LOCAL_TEMPLATE_ROOT,  # lokális/UNC sablon gyökér
)

def _escape_attr(value: str) -> str:
    """XML attribútum érték escape (idézőjelekre is)."""
    return xml_escape(value or "", {'"': "&quot;", "'": "&apos;"})

def _escape_text(value: str) -> str:
    """XML szövegtartalom escape."""
    return xml_escape(value or "")

def _resolve_btw_path(template: str) -> str:
    """
    A BarTender .btw fájl teljes elérési útját adja vissza.
    - Ha 'template' abszolút útvonal: azt használjuk.
    - Egyébként a LOCAL_TEMPLATE_ROOT + template.
    Mindig .btw kiterjesztést és backslash-t adunk vissza.
    """
    t = Path(template)
    if t.is_absolute():
        btw_path = t
    else:
        btw_path = Path(LOCAL_TEMPLATE_ROOT) / template

    if btw_path.suffix.lower() != ".btw":
        btw_path = btw_path.with_suffix(".btw")

    return str(btw_path).replace("/", "\\")

def build_print_xml(commands: list[str]) -> str:
    """Több <Print> blokkot egy XMLScript-be csomagol."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<XMLScript Version="2.0"><Command>'
        + "".join(commands) +
        '</Command></XMLScript>'
    )

def print_command_dynamic(template: str, copies: int, printer: str, substrings: dict[str, str]) -> str:
    """
    Rugalmas XML-építő bármilyen NamedSubString-hez.

    Példák a 'substrings' dict-re:
      - ID címkék: {"PN": "...", "WO": "...", "REV": "..."} vagy {"L1":"...","L2":"...","L3":"...","L4":"..."}
      - FROM .btw: {"FROM_UP":"...","FROM_DOWN":"..."}
      - TO   .btw: {"TO_UP":"...","TO_DOWN":"..."}

    A BarTender az itt megadott kulcsokkal egyező NamedSubString-eket keresi a .btw-ben.
    """
    copies = max(0, int(copies))
    btw_path = _resolve_btw_path(template)

    # NamedSubString-ek (XML-escape-elve)
    named_parts = "\n".join(
        f'  <NamedSubString Name="{_escape_attr(str(k))}"><Value>{_escape_text(str(v))}</Value></NamedSubString>'
        for k, v in (substrings or {}).items()
    )

    # Printer / Format escape-elve
    printer_xml = _escape_text(printer or "")
    format_xml = _escape_text(btw_path)

    return (
        f'<Print JobName="Job_{_escape_attr(Path(btw_path).stem)}">'
        f"\n  <Format>{format_xml}</Format>\n"
        f"  <PrintSetup>\n"
        f"    <IdenticalCopiesOfLabel>{copies}</IdenticalCopiesOfLabel>\n"
        f"    <Printer>{printer_xml}</Printer>\n"
        f"  </PrintSetup>\n"
        f"{named_parts}\n"
        f"</Print>"
    )

def print_command(template: str, copies: int, printer: str, pn: str, wo_tag: str, rev_tag: str) -> str:
    """
    Visszafelé kompatibilis segédfüggvény az egyszerű ID-címkéhez (PN/WO/REV).
    """
    return print_command_dynamic(
        template=template,
        copies=copies,
        printer=printer,
        substrings={"PN": pn, "WO": wo_tag, "REV": rev_tag}
    )

def write_print_xml(xml_text: str) -> None:
    """
    A fő és a scan XML-t is legenerálja. Kicsi késleltetés, hogy a fájlfigyelő biztosan észrevegye.
    A szülő mappákat létrehozza, régi fájlokat törli.
    """
    # Mappák biztosítása
    try:
        XML_PRIMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        XML_SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Előző fájlok törlése
    try:
        if XML_PRIMARY_PATH.exists():
            XML_PRIMARY_PATH.unlink()
    except Exception:
        pass
    try:
        if XML_SCAN_FILE.exists():
            XML_SCAN_FILE.unlink()
    except Exception:
        pass

    # Kis delay a watcher miatt
    time.sleep(0.5)

    XML_PRIMARY_PATH.write_text(xml_text, encoding="utf-8")
    XML_SCAN_FILE.write_text(xml_text, encoding="utf-8")
