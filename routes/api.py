from __future__ import annotations
from flask import Blueprint, jsonify, request, send_file, session, make_response
from io import BytesIO
from datetime import datetime, timedelta
import io
import openpyxl
import pandas as pd
# ══════════════════════════════════════════════════════════════════════════════
# EXPORT KONFIGURÁCIÓ  —  itt állítsd be a teljesítmény-küszöbértékeket
# ══════════════════════════════════════════════════════════════════════════════
EXPORT_CONFIG = {

    # ── Hatékonyság küszöbök ───────────────────────────────────────────────
    # A hatékonyság = eltelt_idő / elvárt_idő * 100
    # Példa: 80% azt jelenti, hogy az elvártnál 20%-kal gyorsabban végzett.
    #        120% azt jelenti, hogy 20%-kal tovább tartott az elvártnál.
    "hatekonysag_jo_max":   100.0,  # % alatt  → zöld  (gyorsabb vagy egyenlő az elvártnál)
    "hatekonysag_kozep_max": 120.0, # % alatt  → sárga (max 20%-kal lassabb)
                                    # % felett → piros  (több mint 20%-kal lassabb)

    # ── Különbség küszöb (óra) ─────────────────────────────────────────────
    # A különbség = elvárt_idő - eltelt_idő (pozitív = gyorsabb, negatív = lassabb)
    # Ha a különbség nagyobb mint ez, külön figyelmeztető ikon jelenik meg.
    "kulonbseg_figyelmezteto_ora": -1.0,  # -1 óránál nagyobb eltérés → figyelmeztetés

    # ── Státusz szövegek (könnyen átírható) ───────────────────────────────
    "statusz_jo":     "✅ Jó",
    "statusz_kozep":  "⚠️ Késés",
    "statusz_rossz":  "🔴 Túllépés",

    # ── Színek (Excel hex, # nélkül) ──────────────────────────────────────
    "szin_jo_hatter":      "C6EFCE",  # halvány zöld
    "szin_jo_szoveg":      "276221",  # sötét zöld
    "szin_kozep_hatter":   "FFEB9C",  # halvány sárga
    "szin_kozep_szoveg":   "9C6500",  # sötét narancs
    "szin_rossz_hatter":   "FFCCCC",  # halvány piros
    "szin_rossz_szoveg":   "9C0006",  # sötét piros

    # ── Fejléc / téma színek ──────────────────────────────────────────────
    "szin_fejlec_hatter":   "1F3864",  # sötétkék főcím
    "szin_fejlec_szoveg":   "FFFFFF",
    "szin_subfejlec_hatter":"2E75B6",  # közepes kék
    "szin_subfejlec_szoveg":"FFFFFF",
    "szin_alternalo_sor":   "EBF3FB",  # sor váltakozó háttér
    "szin_osszesito_hatter":"FFF2CC",  # sárga összesítő sor
    "szin_osszesito_szoveg":"7D6608",
}


def _export_szin(ertek, config, kulcs_hatter, kulcs_szoveg):
    """
    Visszaadja (hatter_szin, szoveg_szin) az EXPORT_CONFIG alapján.
    ertek: a hatékonyság % vagy különbség óra értéke
    kulcs_*: melyik küszöböt vizsgáljuk ('hatekonysag' vagy 'kulonbseg')
    """
    if kulcs_hatter == "hatekonysag":
        if ertek <= config["hatekonysag_jo_max"]:
            return config["szin_jo_hatter"], config["szin_jo_szoveg"]
        elif ertek <= config["hatekonysag_kozep_max"]:
            return config["szin_kozep_hatter"], config["szin_kozep_szoveg"]
        else:
            return config["szin_rossz_hatter"], config["szin_rossz_szoveg"]
    else:  # kulonbseg
        if ertek >= 0:
            return config["szin_jo_hatter"], config["szin_jo_szoveg"]
        elif ertek >= config["kulonbseg_figyelmezteto_ora"]:
            return config["szin_kozep_hatter"], config["szin_kozep_szoveg"]
        else:
            return config["szin_rossz_hatter"], config["szin_rossz_szoveg"]


def _export_statusz(hatekonysag_pct, config):
    """Státusz szöveg az EXPORT_CONFIG küszöbök alapján."""
    if hatekonysag_pct <= config["hatekonysag_jo_max"]:
        return config["statusz_jo"]
    elif hatekonysag_pct <= config["hatekonysag_kozep_max"]:
        return config["statusz_kozep"]
    else:
        return config["statusz_rossz"]

def _build_export_workbook(users_data: dict, date_label: str, mode: str, cfg: dict):
    """
    Formázott XLSX munkafüzet építő.
    users_data : day  -> {user: [[WO, PN, start, end, eff_ora, qty, comp_qty, prod,
                                  elvart, eltelt, kulonbs, statusz], ...]}
                 month-> {user: [[WO, PN, start, end, qty, comp_qty, prod,
                                  elvart, eltelt, kulonbs], ...]}
    date_label : megjelenítési dátum string (pl. "2026-04-01" vagy "2026-04")
    mode       : "day" | "month"  (day-nél van Effective Time oszlop extra)
    cfg        : EXPORT_CONFIG dict
    """
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    def _fill(color):
        return PatternFill("solid", fgColor=color)

    def _font(size=10, bold=False, color="000000"):
        return Font(name="Arial", size=size, bold=bold, color=color)

    def _border():
        s = Side(style="thin", color="BDD7EE")
        return Border(left=s, right=s, top=s, bottom=s)

    def _center():
        return Alignment(horizontal="center", vertical="center")

    def _left():
        return Alignment(horizontal="left", vertical="center")

    def _eff_szin(hatekonysag_pct):
        if hatekonysag_pct <= cfg["hatekonysag_jo_max"]:
            return cfg["szin_jo_hatter"], cfg["szin_jo_szoveg"]
        elif hatekonysag_pct <= cfg["hatekonysag_kozep_max"]:
            return cfg["szin_kozep_hatter"], cfg["szin_kozep_szoveg"]
        else:
            return cfg["szin_rossz_hatter"], cfg["szin_rossz_szoveg"]

    def _diff_szin(kulonbs_ora):
        if kulonbs_ora >= 0:
            return cfg["szin_jo_hatter"], cfg["szin_jo_szoveg"]
        elif kulonbs_ora >= cfg["kulonbseg_figyelmezteto_ora"]:
            return cfg["szin_kozep_hatter"], cfg["szin_kozep_szoveg"]
        else:
            return cfg["szin_rossz_hatter"], cfg["szin_rossz_szoveg"]

    def _statusz(hatekonysag_pct):
        if hatekonysag_pct <= cfg["hatekonysag_jo_max"]:
            return cfg["statusz_jo"]
        elif hatekonysag_pct <= cfg["hatekonysag_kozep_max"]:
            return cfg["statusz_kozep"]
        else:
            return cfg["statusz_rossz"]

    # day módban: [WO, PN, start, end, eff_ora, qty, comp_qty, prod, elvart, eltelt, kulonbs, statusz]
    # month módban: [WO, PN, start, end, qty, comp_qty, prod, elvart, eltelt, kulonbs]
    is_day = (mode == "day")

    def _sheet_title(name, used):
        """Excel lapnév: max 31 karakter, tiltott jelek nélkül, egyedi."""
        base = "".join(ch for ch in str(name or "—") if ch not in "[]:*?/\\").strip()[:31] or "—"
        title, n = base, 2
        while title.lower() in used:
            suffix = f"~{n}"
            title = base[:31 - len(suffix)] + suffix
            n += 1
        used.add(title.lower())
        return title

    wb = openpyxl.Workbook()

    # ── Összesítő lap ──────────────────────────────────────────────────────
    if is_day:
        # A "Hatékonyság" szó a Felhasználói haladás oldalon effektív/összes időt
        # jelent. Itt norma-teljesítésről van szó (elvárt/eltelt), ezért külön néven.
        sum_headers = ["Dolgozó", "WO db", "Aznapi effektív (ó)", "Elvárt idő (ó)",
                       "Eltelt idő (ó)", "Különbség (ó)", "Elvárthoz képest (%)", "Lap"]
        sum_col_widths = [26, 8, 18, 16, 16, 16, 20, 10]
    else:
        sum_headers = ["Dolgozó", "WO db", "Elvárt idő (ó)", "Eltelt idő (ó)",
                       "Különbség (ó)", "Elvárthoz képest (%)", "Lap"]
        sum_col_widths = [26, 8, 16, 16, 16, 20, 10]

    ws_sum = wb.active
    ws_sum.title = "Összesítő"
    ws_sum.sheet_view.showGridLines = False
    ws_sum.freeze_panes = "A5"

    ws_sum.merge_cells(f"A1:{get_column_letter(len(sum_headers))}1")
    ws_sum["A1"] = f"Teljesítmény összesítő  –  {date_label}"
    ws_sum["A1"].font      = _font(14, True, cfg["szin_fejlec_szoveg"])
    ws_sum["A1"].fill      = _fill(cfg["szin_fejlec_hatter"])
    ws_sum["A1"].alignment = _center()
    ws_sum.row_dimensions[1].height = 30

    from datetime import datetime as _dt
    ws_sum.merge_cells(f"A2:{get_column_letter(len(sum_headers))}2")
    ws_sum["A2"] = f"Generálva: {_dt.now().strftime('%Y-%m-%d %H:%M')}"
    ws_sum["A2"].font      = Font(name="Arial", size=9, italic=True, color="888888")
    ws_sum["A2"].alignment = _center()
    ws_sum.row_dimensions[3].height = 5

    ws_sum.row_dimensions[4].height = 22
    for ci, h in enumerate(sum_headers, 1):
        c = ws_sum.cell(4, ci, h)
        c.font = _font(10, True, cfg["szin_subfejlec_szoveg"])
        c.fill = _fill(cfg["szin_subfejlec_hatter"])
        c.alignment = _center()
        c.border = _border()

    for i, w in enumerate(sum_col_widths, 1):
        ws_sum.column_dimensions[get_column_letter(i)].width = w

    summary_rows = []

    # ── Dolgozónkénti lapok ────────────────────────────────────────────────
    used_titles = set()
    for worker, rows in users_data.items():
        sheet_title = _sheet_title(worker, used_titles)
        ws = wb.create_sheet(title=sheet_title)
        ws.sheet_view.showGridLines = False
        ws.freeze_panes = "A6"

        # Fejléc (5. sor)
        # col_defs: (fejléc, szélesség, adat_index) — az adat_index a row_data tömb indexe
        if is_day:
            col_defs = [
                ("WO szám",             10,  0), ("Part Number",    18,  1),
                ("Kezdés",              18,  2), ("Befejezés",      18,  3),
                ("Rendelési db",        13,  5), ("Elvégzett db",   13,  6),
                ("Elvárt idő (ó)",      15,  8), ("Eltelt idő (ó)", 15,  9),
                ("Aznapi effektív (ó)", 19,  4), ("Különbség (ó)",  15, 10),
                ("Státusz",             26, 11),
            ]
        else:
            col_defs = [
                ("WO szám",        10,  0), ("Part Number",    18,  1),
                ("Kezdés",         18,  2), ("Befejezés",      18,  3),
                ("Rendelési db",   13,  4), ("Elvégzett db",   13,  5),
                ("Elvárt idő (ó)", 15,  7), ("Eltelt idő (ó)", 15,  8),
                ("Különbség (ó)",  15,  9),
            ]

        ws.merge_cells(f"A1:{get_column_letter(len(col_defs))}1")
        ws["A1"] = f"{worker}  –  {date_label}"
        ws["A1"].font      = _font(13, True, cfg["szin_fejlec_szoveg"])
        ws["A1"].fill      = _fill(cfg["szin_fejlec_hatter"])
        ws["A1"].alignment = _center()
        ws.row_dimensions[1].height = 28

        # Indexek mode szerint
        if is_day:
            i_eff = 4
            i_qty, i_cqty, i_prod, i_elvart, i_eltelt, i_kulonbs = 5, 6, 7, 8, 9, 10
        else:
            i_eff = None
            i_qty, i_cqty, i_prod, i_elvart, i_eltelt, i_kulonbs = 4, 5, 6, 7, 8, 9

        elvart_ossz  = sum(float(r[i_elvart] or 0) for r in rows)
        eltelt_ossz  = sum(float(r[i_eltelt] or 0) for r in rows)
        kulonbs_ossz = sum(float(r[i_kulonbs] or 0) for r in rows)
        eff_ossz     = sum(float(r[i_eff] or 0) for r in rows) if i_eff is not None else 0.0
        # Az EXPORT_CONFIG küszöbei eltelt/elvárt arányra vannak megírva
        # (<=100% = az elvártnál gyorsabb). Korábban ez fordítva volt számolva,
        # de sehol nem jelent meg, így nem tűnt fel.
        hatekonysag  = round(eltelt_ossz / elvart_ossz * 100, 1) if elvart_ossz else 0.0

        if is_day:
            stat_labels = ["WO darab", "Aznapi effektív (ó)", "Elvárt össz. (ó)",
                           "Eltelt össz. (ó)", "Különbség (ó)"]
            stat_values = [str(len(rows)), f"{eff_ossz:.2f}", f"{elvart_ossz:.2f}",
                           f"{eltelt_ossz:.2f}", f"{kulonbs_ossz:+.2f}"]
        else:
            stat_labels = ["WO darab", "Elvárt össz. (ó)", "Eltelt össz. (ó)", "Különbség (ó)"]
            stat_values = [str(len(rows)), f"{elvart_ossz:.2f}", f"{eltelt_ossz:.2f}",
                           f"{kulonbs_ossz:+.2f}"]
        for ci, (lbl, val) in enumerate(zip(stat_labels, stat_values), 1):
            lc = ws.cell(2, ci*2-1, lbl)
            vc = ws.cell(2, ci*2, val)
            lc.font = _font(9, True, cfg["szin_subfejlec_szoveg"])
            lc.fill = _fill(cfg["szin_subfejlec_hatter"])
            lc.alignment = Alignment(horizontal="right", vertical="center")
            vc.font = _font(10, True)
            vc.fill = _fill("D6E4F0")
            vc.alignment = _center()
            for cell in [lc, vc]:
                cell.border = _border()
        ws.row_dimensions[2].height = 20
        ws.row_dimensions[3].height = 4

        ws.row_dimensions[5].height = 22
        for ci, (hdr, width, _) in enumerate(col_defs, 1):
            c = ws.cell(5, ci, hdr)
            c.font      = _font(10, True, cfg["szin_fejlec_szoveg"])
            c.fill      = _fill(cfg["szin_fejlec_hatter"])
            c.alignment = _center()
            c.border    = _border()
            ws.column_dimensions[get_column_letter(ci)].width = width

        total_cols = len(col_defs)
        data_indices = [cd[2] for cd in col_defs]
        qty_ci      = data_indices.index(i_qty)    + 1
        cqty_ci     = data_indices.index(i_cqty)   + 1
        elvart_ci   = data_indices.index(i_elvart)  + 1
        eltelt_ci   = data_indices.index(i_eltelt)  + 1
        kulonbs_ci  = data_indices.index(i_kulonbs) + 1
        eff_ci      = (data_indices.index(i_eff) + 1) if i_eff is not None else None
        elvart_excel_col = get_column_letter(elvart_ci)
        eltelt_excel_col = get_column_letter(eltelt_ci)

        for ri, row_data in enumerate(rows):
            er = ri + 6
            ws.row_dimensions[er].height = 18
            bg = cfg["szin_alternalo_sor"] if ri % 2 == 0 else "FFFFFF"

            # Folyamatban lévő sornál nincs elvárt/eltelt/különbség -> ne fessük
            # zöldre, mintha norma alatt teljesített volna.
            kulonbs_raw = row_data[i_kulonbs]
            has_diff = kulonbs_raw is not None
            diff_bg, diff_fg = _diff_szin(float(kulonbs_raw or 0)) if has_diff else (bg, "000000")

            for ci, (_, _, data_idx) in enumerate(col_defs, 1):
                c = ws.cell(er, ci)
                c.border = _border()

                if ci == kulonbs_ci:  # Különbség
                    c.value         = row_data[data_idx]
                    c.number_format = "+0.00;-0.00;0.00"
                    c.font          = _font(10, True, diff_fg) if has_diff else _font(10)
                    c.fill          = _fill(diff_bg)
                    c.alignment     = _center()
                elif data_idx in (2, 3):  # dátumok
                    c.value         = row_data[data_idx]
                    c.number_format = "YYYY-MM-DD HH:MM"
                    c.font          = _font(9)
                    c.fill          = _fill(bg)
                    c.alignment     = _center()
                elif ci in (elvart_ci, eltelt_ci) or (eff_ci and ci == eff_ci):  # idők
                    c.value         = row_data[data_idx]
                    c.number_format = "0.00"
                    c.font          = _font(10)
                    c.fill          = _fill(bg)
                    c.alignment     = _center()
                else:
                    c.value     = row_data[data_idx]
                    c.font      = _font(10)
                    c.fill      = _fill(bg)
                    c.alignment = _center() if ci > 1 else _left()

        # Összesítő sor
        tr = len(rows) + 6
        ws.row_dimensions[tr].height = 20
        ws.cell(tr, 1, "ÖSSZESEN").font      = _font(10, True, cfg["szin_osszesito_szoveg"])
        ws.cell(tr, 1).fill      = _fill(cfg["szin_osszesito_hatter"])
        ws.cell(tr, 1).alignment = _center()
        ws.cell(tr, 1).border    = _border()
        for ci in range(2, total_cols + 1):
            c = ws.cell(tr, ci)
            col_l = get_column_letter(ci)
            if ci in (qty_ci, cqty_ci):
                c.value = f"=SUM({col_l}6:{col_l}{tr-1})"
                c.number_format = "#,##0"
            elif ci in (elvart_ci, eltelt_ci) or (eff_ci and ci == eff_ci):
                c.value = f"=SUM({col_l}6:{col_l}{tr-1})"
                c.number_format = "0.00"
            elif ci == kulonbs_ci:
                c.value = f"=SUM({col_l}6:{col_l}{tr-1})"
                c.number_format = "+0.00;-0.00;0.00"
            c.font      = _font(10, True, cfg["szin_osszesito_szoveg"])
            c.fill      = _fill(cfg["szin_osszesito_hatter"])
            c.alignment = _center()
            c.border    = _border()

        # Jelmagyarázat
        lr = tr + 2
        ws.merge_cells(f"A{lr}:F{lr}")
        ws.cell(lr, 1, (
            f"🟢 Különbség ≥ 0 — Jó  |  "
            f"🟡 Különbség ≥ {cfg['kulonbseg_figyelmezteto_ora']} ó — Figyelmeztetés  |  "
            f"🔴 Különbség < {cfg['kulonbseg_figyelmezteto_ora']} ó — Túllépés"
        ))
        ws.cell(lr, 1).font = Font(name="Arial", size=8, italic=True, color="666666")

        summary_rows.append({
            "worker": worker, "wo_db": len(rows),
            "eff": eff_ossz,
            "elvart": elvart_ossz, "eltelt": eltelt_ossz,
            "kulonbs": kulonbs_ossz, "hatekon": hatekonysag,
            "sheet": sheet_title,
        })

    # ── Összesítő lap feltöltése ───────────────────────────────────────────
    for ri, row in enumerate(summary_rows, 5):
        bg = cfg["szin_alternalo_sor"] if ri % 2 == 0 else "FFFFFF"
        diff_bg, diff_fg = _diff_szin(row["kulonbs"])

        if is_day:
            row_vals = [
                row["worker"], row["wo_db"], round(row["eff"], 2),
                round(row["elvart"], 2), round(row["eltelt"], 2),
                round(row["kulonbs"], 2), row["hatekon"], row["sheet"],
            ]
            fmts = [None, "#,##0", "0.00", "0.00", "0.00", "+0.00;-0.00;0.00", "0.0", None]
            diff_ci = 6
        else:
            row_vals = [
                row["worker"], row["wo_db"],
                round(row["elvart"], 2), round(row["eltelt"], 2),
                round(row["kulonbs"], 2), row["hatekon"], row["sheet"],
            ]
            fmts = [None, "#,##0", "0.00", "0.00", "+0.00;-0.00;0.00", "0.0", None]
            diff_ci = 5

        ws_sum.row_dimensions[ri].height = 18
        for ci, (val, fmt) in enumerate(zip(row_vals, fmts), 1):
            c = ws_sum.cell(ri, ci, val)
            c.border = _border()
            if fmt: c.number_format = fmt
            norma_ci = diff_ci + 1
            if ci == diff_ci:
                c.font = _font(10, True, diff_fg); c.fill = _fill(diff_bg)
            elif ci == norma_ci and row["hatekon"]:
                nbg, nfg = _eff_szin(row["hatekon"])
                c.font = _font(10, True, nfg); c.fill = _fill(nbg)
            else:
                c.font = _font(10); c.fill = _fill(bg)
            c.alignment = _center() if ci > 1 else _left()

    # Összesítő totál sor
    if summary_rows:
        tsr = len(summary_rows) + 5
        ws_sum.row_dimensions[tsr].height = 22
        ws_sum.cell(tsr, 1, "ÖSSZESEN / ÁTLAG")
        ws_sum.cell(tsr, 1).font      = _font(10, True, cfg["szin_osszesito_szoveg"])
        ws_sum.cell(tsr, 1).fill      = _fill(cfg["szin_osszesito_hatter"])
        ws_sum.cell(tsr, 1).alignment = _center()
        ws_sum.cell(tsr, 1).border    = _border()
        total_cols_sum = (
            [(2, "#,##0", "SUM"), (3, "0.00", "SUM"), (4, "0.00", "SUM"),
             (5, "0.00", "SUM"), (6, "+0.00;-0.00;0.00", "SUM"), (7, "0.0", "AVERAGE")]
            if is_day else
            [(2, "#,##0", "SUM"), (3, "0.00", "SUM"), (4, "0.00", "SUM"),
             (5, "+0.00;-0.00;0.00", "SUM"), (6, "0.0", "AVERAGE")]
        )
        for ci, fmt, func in total_cols_sum:
            cl = get_column_letter(ci)
            c  = ws_sum.cell(tsr, ci)
            c.value         = f"={func}({cl}5:{cl}{tsr-1})"
            c.number_format = fmt
            c.font          = _font(10, True, cfg["szin_osszesito_szoveg"])
            c.fill          = _fill(cfg["szin_osszesito_hatter"])
            c.alignment     = _center()
            c.border        = _border()
        last_ci = len(sum_headers)
        ws_sum.cell(tsr, last_ci).fill   = _fill(cfg["szin_osszesito_hatter"])
        ws_sum.cell(tsr, last_ci).border = _border()

    if "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
        wb.remove(wb["Sheet"])
    return wb


# ──────────────────────────────────────────────────────────────────────────────
# WO TIMELINE EXPORT  —  időrendi ping-pong kimutatás
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_wo_timeline(cursor, start_dt_str: str, end_dt_str: str) -> dict:
    """
    Lekéri azon WO-k teljes történetét (első megjelenéstől), amelyekkel
    a megadott időtartományban foglalkoztak.

    1. lépés: megkeresi az érintett WO-kat (aznap volt activity)
    2. lépés: az összes eseményüket lekéri a legelejétől

    Visszatérés:
      {wo: {'pn': str, 'events': [
          {'station', 'worker', 'start_time', 'end_time',
           'elapsed_seconds', 'wait_seconds', 'qty', 'status', 'next_station'}
      ]}}
    """
    # 1) Az adott napon aktív WO-k azonosítása
    cursor.execute("""
        SELECT DISTINCT wo.WO
        FROM workstationworkorder wsw
        JOIN workorders wo ON wsw.work_id = wo.id
        WHERE (wsw.start_time >= %s AND wsw.start_time < %s)
           OR (wsw.end_time   >= %s AND wsw.end_time   < %s)
    """, (start_dt_str, end_dt_str, start_dt_str, end_dt_str))

    active_wos = [r["WO"] for r in (cursor.fetchall() or [])]
    if not active_wos:
        return {}

    # 2) Teljes történet lekérése ezekhez a WO-khoz (legelső eseménytől)
    placeholders = ",".join(["%s"] * len(active_wos))
    cursor.execute(f"""
        SELECT
            wo.WO,
            wo.PN,
            COALESCE(wk.name, '—')  AS worker,
            wsw.process_id          AS station,
            wsw.next_station_id     AS next_station,
            wsw.QTY                 AS qty,
            wsw.status,
            wsw.start_time,
            wsw.end_time,
            TIMESTAMPDIFF(SECOND, wsw.start_time, wsw.end_time) AS elapsed_seconds
        FROM workstationworkorder wsw
        JOIN workorders wo ON wsw.work_id = wo.id
        LEFT JOIN workers wk ON wsw.worker_id = wk.ID
        WHERE wo.WO IN ({placeholders})
        ORDER BY wo.WO ASC,
                 COALESCE(wsw.start_time, wsw.end_time) ASC,
                 wsw.id ASC
    """, active_wos)

    rows = cursor.fetchall() or []

    def _to_dt(v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return v
        try:
            return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    wo_timeline: dict = {}
    for row in rows:
        wo = row["WO"]
        pn = row["PN"]
        if wo not in wo_timeline:
            wo_timeline[wo] = {"pn": pn, "events": []}

        st = _to_dt(row.get("start_time"))
        et = _to_dt(row.get("end_time"))
        elapsed = float(row.get("elapsed_seconds") or 0)
        if elapsed == 0 and st and et:
            elapsed = max(0.0, (et - st).total_seconds())

        wo_timeline[wo]["events"].append({
            "station":         ((row.get("station")      or "").strip().upper() or "—"),
            "worker":          (row.get("worker")         or "—"),
            "start_time":       st,
            "end_time":         et,
            "elapsed_seconds":  elapsed,
            "qty":              row.get("qty") or 0,
            "status":          ((row.get("status")        or "").strip().upper()),
            "next_station":    ((row.get("next_station")  or "").strip().upper() or "—"),
        })

    # Várakozás kiszámítása: aktuális end_time → következő start_time
    for data in wo_timeline.values():
        events = data["events"]
        for i, ev in enumerate(events):
            if i < len(events) - 1:
                nxt = events[i + 1]
                if ev.get("end_time") and nxt.get("start_time"):
                    wait = (nxt["start_time"] - ev["end_time"]).total_seconds()
                    ev["wait_seconds"] = max(0.0, wait)
                else:
                    ev["wait_seconds"] = 0.0
            else:
                ev["wait_seconds"] = 0.0

    return wo_timeline


def _build_wo_timeline_workbook(wo_timeline: dict, date_label: str, cfg: dict):
    """
    Formázott XLSX munkafüzet WO időrendi útvonalhoz.
    Két lapot tartalmaz:
      1. "Összesítő"       — egy sor per WO
      2. "Részletes útvonal" — minden esemény, WO-nként csoportosítva
    """
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from datetime import datetime as _dt

    def _fill(color):
        return PatternFill("solid", fgColor=color)

    def _font(size=10, bold=False, color="000000"):
        return Font(name="Arial", size=size, bold=bold, color=color)

    def _border():
        s = Side(style="thin", color="BDD7EE")
        return Border(left=s, right=s, top=s, bottom=s)

    def _center():
        return Alignment(horizontal="center", vertical="center", wrap_text=False)

    def _left():
        return Alignment(horizontal="left", vertical="center", wrap_text=False)

    def _h(secs):
        """másodpercek → óra, 3 tizedessel"""
        return round(float(secs or 0) / 3600, 3)

    def _ts(t):
        """datetime → string, vagy '—'"""
        if isinstance(t, _dt):
            return t.strftime("%Y-%m-%d %H:%M")
        return str(t) if t else "—"

    def _wipcheck(s):
        s = (s or "").strip().upper()
        return "WIPRECIEV" in s or "VYPRISIV" in s

    wb = openpyxl.Workbook()

    # ════════════════════════════════════════════════
    # 1. ÖSSZESÍTŐ LAP
    # ════════════════════════════════════════════════
    ws_sum = wb.active
    ws_sum.title = "Összesítő"
    ws_sum.sheet_view.showGridLines = False
    ws_sum.freeze_panes = "A5"

    ws_sum.merge_cells("A1:G1")
    ws_sum["A1"] = f"WO Időrendi Összesítő  –  {date_label}"
    ws_sum["A1"].font      = _font(14, True, cfg["szin_fejlec_szoveg"])
    ws_sum["A1"].fill      = _fill(cfg["szin_fejlec_hatter"])
    ws_sum["A1"].alignment = _center()
    ws_sum.row_dimensions[1].height = 30

    ws_sum.merge_cells("A2:G2")
    ws_sum["A2"] = f"Generálva: {_dt.now().strftime('%Y-%m-%d %H:%M')}"
    ws_sum["A2"].font      = Font(name="Arial", size=9, italic=True, color="888888")
    ws_sum["A2"].alignment = _center()
    ws_sum.row_dimensions[3].height = 5

    sum_headers = ["WO", "PN", "Első megjelenés", "WIPReceive idő", "Átfutás (óra)", "Lépések", "Összes db"]
    ws_sum.row_dimensions[4].height = 22
    for ci, h in enumerate(sum_headers, 1):
        c = ws_sum.cell(4, ci, h)
        c.font      = _font(10, True, cfg["szin_subfejlec_szoveg"])
        c.fill      = _fill(cfg["szin_subfejlec_hatter"])
        c.alignment = _center()
        c.border    = _border()
    for ci, w in enumerate([14, 22, 20, 20, 15, 10, 10], 1):
        ws_sum.column_dimensions[get_column_letter(ci)].width = w

    # ════════════════════════════════════════════════
    # 2. RÉSZLETES LAP
    # ════════════════════════════════════════════════
    ws_det = wb.create_sheet("Részletes útvonal")
    ws_det.sheet_view.showGridLines = False
    ws_det.freeze_panes = "A3"

    ws_det.merge_cells("A1:J1")
    ws_det["A1"] = f"WO Részletes Útvonal  –  {date_label}"
    ws_det["A1"].font      = _font(13, True, cfg["szin_fejlec_szoveg"])
    ws_det["A1"].fill      = _fill(cfg["szin_fejlec_hatter"])
    ws_det["A1"].alignment = _center()
    ws_det.row_dimensions[1].height = 26

    det_hdrs = ["WO", "PN", "Állomás", "Dolgozó",
                "Kezdés", "Befejezés", "Töltött idő (ó)", "Várakozás (ó)",
                "Következő állomás", "Státusz"]
    det_wids = [14, 22, 12, 24, 18, 18, 16, 16, 18, 12]
    ws_det.row_dimensions[2].height = 22
    for ci, (h, w) in enumerate(zip(det_hdrs, det_wids), 1):
        c = ws_det.cell(2, ci, h)
        c.font      = _font(10, True, cfg["szin_subfejlec_szoveg"])
        c.fill      = _fill(cfg["szin_subfejlec_hatter"])
        c.alignment = _center()
        c.border    = _border()
        ws_det.column_dimensions[get_column_letter(ci)].width = w

    # ════════════════════════════════════════════════
    # SOROK FELTÖLTÉSE
    # ════════════════════════════════════════════════
    det_row = 3
    sum_row = 5
    wo_bg_cycle = ["EBF3FB", "FFFFFF"]

    for wo_idx, (wo, data) in enumerate(sorted(wo_timeline.items())):
        pn     = data.get("pn") or ""
        events = data.get("events") or []
        if not events:
            continue

        wo_bg = wo_bg_cycle[wo_idx % 2]

        # ── Összesítő értékek kiszámítása ──
        first_time   = events[0].get("start_time")
        wiprecv_time = None
        for ev in reversed(events):
            if _wipcheck(ev.get("next_station")) or _wipcheck(ev.get("station")):
                wiprecv_time = ev.get("end_time") or ev.get("start_time")
                break

        if first_time and wiprecv_time and isinstance(first_time, _dt) and isinstance(wiprecv_time, _dt):
            thru_h = round((wiprecv_time - first_time).total_seconds() / 3600, 2)
        else:
            last_t = events[-1].get("end_time") or events[-1].get("start_time")
            if first_time and last_t and isinstance(first_time, _dt) and isinstance(last_t, _dt):
                thru_h = round((last_t - first_time).total_seconds() / 3600, 2)
            else:
                thru_h = round(sum(ev.get("elapsed_seconds") or 0 for ev in events) / 3600, 2)

        total_qty = max((ev.get("qty") or 0 for ev in events), default=0)

        # Összesítő lap sor
        s_bg = cfg["szin_alternalo_sor"] if sum_row % 2 == 0 else "FFFFFF"
        ws_sum.row_dimensions[sum_row].height = 18
        sum_vals = [wo, pn, _ts(first_time), _ts(wiprecv_time), thru_h, len(events), total_qty]
        sum_fmts = [None, None, None, None, "0.00", "#,##0", "#,##0"]
        for ci, (val, fmt) in enumerate(zip(sum_vals, sum_fmts), 1):
            c = ws_sum.cell(sum_row, ci, val)
            c.font      = _font(10)
            c.fill      = _fill(s_bg)
            c.alignment = _left() if ci <= 2 else _center()
            c.border    = _border()
            if fmt:
                c.number_format = fmt
        sum_row += 1

        # ── WO csoport fejléc a részletes lapon ──
        ws_det.merge_cells(f"A{det_row}:J{det_row}")
        gc = ws_det.cell(det_row, 1,
             f"  WO: {wo}   |   PN: {pn}   |   {len(events)} esemény   |   Átfutás: {thru_h:.2f} ó")
        gc.font      = _font(10, True, cfg["szin_fejlec_szoveg"])
        gc.fill      = _fill(cfg["szin_subfejlec_hatter"])
        gc.alignment = _left()
        gc.border    = _border()
        ws_det.row_dimensions[det_row].height = 20
        det_row += 1

        # ── Esemény sorok ──
        for ev in events:
            ws_det.row_dimensions[det_row].height = 17

            status = ev.get("status") or ""

            # Sor háttérszín státusz alapján
            if "COMPLETED" in status:
                row_bg = cfg["szin_jo_hatter"]
            elif "ACTIVE" in status:
                row_bg = cfg["szin_kozep_hatter"]
            else:
                row_bg = wo_bg

            ev_vals = [
                wo,
                pn,
                ev.get("station") or "—",
                ev.get("worker")  or "—",
                _ts(ev.get("start_time")),
                _ts(ev.get("end_time")),
                _h(ev.get("elapsed_seconds")),
                _h(ev.get("wait_seconds")),
                ev.get("next_station") or "—",
                status,
            ]
            ev_fmts = [None, None, None, None, None, None, "0.000", "0.000", None, None]

            for ci, (val, fmt) in enumerate(zip(ev_vals, ev_fmts), 1):
                c = ws_det.cell(det_row, ci, val)
                c.font      = _font(9)
                c.fill      = _fill(row_bg)
                c.alignment = _left() if ci <= 4 else _center()
                c.border    = _border()
                if fmt:
                    c.number_format = fmt

            det_row += 1

        # Üres elválasztó sor WO-k között
        det_row += 1

    if "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
        wb.remove(wb["Sheet"])

    return wb


import mysql.connector
from routes.auth import login_required
from routes.teamleaders import _can_use_wo_search
from services import tl_settings
from services import wo_done
from utils.roles import has_any_role, IT_ROLES, MANAGER_ROLES, TEAMLEADER_ROLES
import os
from services import cache
from services.db import get_db
from services.ssh import (
    execute_command_on_pi,
    check_virtualenv_and_packages_on_pi,
    start_program_bg,
    stop_program_safe,
    program_status,
    tail_log,
    get_pi_credentials,
)
from utils.helpers import (
    load_devices,
    safe_float, safe_datetime,
    seconds_to_hour_decimal,
    adjust_column_width,
    format_time_difference, reload_pn_data_cache, get_assembly_data
)
from utils.roles import has_any_role, IT_ROLES
from services.tables_core import ensure_tables_meta_schema


api_bp = Blueprint('api', __name__, url_prefix='/api')

# SSH belépő a .env-ből (PI_SSH_USER / PI_SSH_PASS), alapértelmezés: user/user
USERNAME, PASSWORD = get_pi_credentials()

# ---------------- SSH-hoz kapcsolódó végpontok (frissített) ----------------



def _norm(s): return (s or "").strip().upper()
def _code_from_name(n):
    n = (n or "").strip()
    if "-" in n: n = n.split("-",1)[0]
    return _norm(n)


def _to_text(obj):
    # mindent JSON-biztos sztringgé konvertál
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", "ignore")
    if isinstance(obj, dict):
        return {k: _to_text(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_text(x) for x in obj]
    return obj

# -------- Program vezérlés (Start/Stop/Restart/Update/Log) --------

@api_bp.route('/start_program', methods=['POST'])
def start_program():
    ip = request.json.get('ip')
    if not ip:
        return jsonify({"ok": False, "error": "Missing ip"}), 400
    ok, msg = start_program_bg(ip, USERNAME, PASSWORD)
    status = program_status(ip, USERNAME, PASSWORD)
    return jsonify({"ok": bool(ok), "message": _to_text(msg), "status": _to_text(status)})

@api_bp.route('/stop_program', methods=['POST'])
def stop_program():
    ip = request.json.get('ip')
    if not ip:
        return jsonify({"ok": False, "error": "Missing ip"}), 400
    ok, msg = stop_program_safe(ip, USERNAME, PASSWORD)
    status = program_status(ip, USERNAME, PASSWORD)
    return jsonify({"ok": bool(ok), "message": _to_text(msg), "status": _to_text(status)})

@api_bp.route('/run_command', methods=['POST'])
def run_command():
    ip = request.json.get('ip')
    cmd = request.json.get('command')   # "Restart" | "Update"
    if not ip or not cmd:
        return jsonify({"ok": False, "error": "Missing ip or command"}), 400

    if cmd == "Restart":
        ok1, stop_msg = stop_program_safe(ip, USERNAME, PASSWORD)
        ok2, start_msg = start_program_bg(ip, USERNAME, PASSWORD)
        status = program_status(ip, USERNAME, PASSWORD)
        return jsonify({
            "ok": bool(ok1 and ok2),
            "result": f"Program restarted on {ip}",
            "stop_output": _to_text(stop_msg),
            "start_output": _to_text(start_msg),
            "status": _to_text(status)
        })

    if cmd == "Update":
        ok1, stop_msg = stop_program_safe(ip, USERNAME, PASSWORD)
        ok2, venv_msg = check_virtualenv_and_packages_on_pi(ip, USERNAME, PASSWORD)
        ok3, start_msg = start_program_bg(ip, USERNAME, PASSWORD)
        status = program_status(ip, USERNAME, PASSWORD)
        return jsonify({
            "ok": bool(ok1 and ok2 and ok3),
            "result": f"Program updated and restarted on {ip}",
            "stop_output": _to_text(stop_msg),
            "venv_output": _to_text(venv_msg),
            "start_output": _to_text(start_msg),
            "status": _to_text(status)
        })

    return jsonify({"ok": False, "error": "Invalid command"}), 400

@api_bp.route('/program_log', methods=['POST'])
def program_log_route():
    ip = request.json.get('ip')
    lines = int(request.json.get('lines', 200))
    if not ip:
        return jsonify({"ok": False, "error": "Missing ip"}), 400
    ok, log = tail_log(ip, USERNAME, PASSWORD, lines=lines)
    return jsonify({"ok": bool(ok), "log": _to_text(log)})

@api_bp.route('/download_file', methods=['POST'])
def download_file():
    ip = request.json.get('ip')
    if not ip:
        return jsonify({"ok": False, "error": "Missing ip"}), 400
    ok, result = check_virtualenv_and_packages_on_pi(ip, USERNAME, PASSWORD)
    txt = _to_text(result)
    if "Installed" in txt:
        return jsonify({"ok": True, "result": "Packages installed and verified successfully.", "raw": txt})
    if "Not Installed" in txt:
        return jsonify({"ok": False, "result": "Failed to install packages, please check manually.", "raw": txt})
    return jsonify({"ok": ok, "result": txt})

# -------- Devices / Egyéb meglévő endpointok --------


@api_bp.route('/program_status', methods=['POST'])
def program_status_route():
    ip = request.json.get('ip')
    if not ip:
        return jsonify({"ok": False, "error": "Missing ip"}), 400
    st = program_status(ip, USERNAME, PASSWORD)
    return jsonify({"ok": True, "status": st})


# ---------------- Egyéb meglévő végpontok (változatlan logika) ----------------

@api_bp.route('/devices')
def devices_api():
    data = load_devices()
    return jsonify(data)

@api_bp.route('/users_progress', methods=['GET'])
def users_progress_data():
    """
    Napi dolgozói statisztika.

    A számok definíciója (lásd services/workday.aggregate_workers):
        összes idő = amíg be volt jelentkezve
        effektív   = ebből az, amikor volt aktív munkarendelése
        veszteség  = ebből az, amikor nem volt
    effektív + veszteség = összes, pontosan.

    A válasz a részleteket is tartalmazza (be/kijelentkezések, munkablokkok),
    hogy a felületen vissza lehessen keresni, honnan jön egy szám.
    """
    import logging
    from services.db import get_db
    from services import workday
    _log = logging.getLogger("api.users_progress")

    selected_date = (request.args.get('date') or '').strip()
    if not selected_date:
        return jsonify({"error": "No date provided. Please select a date."}), 400

    # Opcionális állomásszűrő – ugyanaz a kör, amit a dashboard táblája mutat.
    station = (request.args.get('station') or '').strip().upper() or None
    if station and station not in workday.ASSEMBLY_STATIONS:
        station = None

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        rows = workday.fetch_day_rows(cursor, selected_date, station=station, order="ASC")
        sessions = workday.fetch_login_sessions(cursor, selected_date)

        hm = workday.fmt_dt
        results = []
        for a in workday.aggregate_workers(rows, sessions):
            results.append({
                'user': a['user'],
                'worker_id': a['worker_id'],

                # formázott értékek (visszafelé kompatibilis mezőnevek)
                'effective_time': format_time_difference(a['effective_seconds']),
                'all_time': format_time_difference(a['all_seconds']),
                'loss_waited': format_time_difference(a['loss_seconds']),

                'effective_seconds': a['effective_seconds'],
                'all_seconds': a['all_seconds'],
                'loss_seconds': a['loss_seconds'],
                'outside_seconds': a['outside_seconds'],
                'overlap_seconds': a['overlap_seconds'],
                'efficiency_pct': a['efficiency_pct'],

                'total_wo': a['rows_completed'],
                'wo_active': a['rows_active'],
                'wo_distinct': a['wo_distinct'],

                'no_login_record': a['no_login_record'],
                'assumed_logout': a['assumed_logout'],

                # ── Részletek: ebből áll össze a fenti három szám ──
                'sessions': [
                    {
                        'login': hm(x['login']),
                        'logout': hm(x['logout']) if x['logout'] else '',
                        'seconds': x['seconds'],
                        'assumed': x['assumed'],
                        'device': x['device'],
                    }
                    for x in a['sessions']
                ],
                'work_blocks': [
                    {
                        'wo': r.get('WO') or '',
                        'pn': r.get('PN') or '',
                        'station': r.get('current_station') or '',
                        'start': hm(r.get('clip_start')),
                        'end': hm(r.get('clip_end')),
                        'seconds': r.get('eff_seconds') or 0,
                        'completed': bool(r.get('is_completed')),
                        'qty': workday.qty_text(r),
                    }
                    for r in a['work_rows'] if r.get('counts_as_effective')
                ],
                # A napi sáv rétegei: bejelentkezve / ebből dolgozott / ebből állt
                'login_blocks': [
                    {'start': hm(s0), 'end': hm(e0),
                     'seconds': int((e0 - s0).total_seconds())}
                    for s0, e0 in a['login_spans']
                ],
                'active_blocks': [
                    {'start': hm(s0), 'end': hm(e0),
                     'seconds': int((e0 - s0).total_seconds())}
                    for s0, e0 in a['eff_spans']
                ],
                'idle_blocks': [
                    {'start': hm(s0), 'end': hm(e0),
                     'seconds': int((e0 - s0).total_seconds())}
                    for s0, e0 in a['loss_spans']
                ],
            })

        return jsonify(results)
    except Exception as e:
        _log.exception("[users_progress] date=%s station=%s", selected_date, station)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def _wo_norm_map(cursor, work_ids):
    """
    work_id -> {'PN', 'QTY', 'PROD_TIME'}

    A WO SAJÁT darabszámával. Korábban ez a map PN-re volt kulcsolva, így ha
    két munkarendelés ugyanarra a PN-re szólt eltérő darabszámmal, véletlen-
    szerűen az egyikük QTY-ja került minden sor elvárt idejébe.
    """
    ids = tuple({int(x) for x in work_ids if x is not None})
    if not ids:
        return {}
    placeholders = ','.join(['%s'] * len(ids))
    cursor.execute(
        "SELECT wo.ID AS work_id, wo.PN AS PN, MIN(wo.QTY) AS QTY, MIN(td.PROD) AS PROD_TIME "
        "FROM workorders wo "
        "LEFT JOIN t_dump td ON wo.PN = td.`PART.NBR` "
        "WHERE wo.ID IN (" + placeholders + ") "
        "GROUP BY wo.ID, wo.PN",
        ids,
    )
    return {
        row['work_id']: {
            'PN': row.get('PN'),
            'QTY': safe_float(row.get('QTY')),
            'PROD_TIME': safe_float(row.get('PROD_TIME')),
        }
        for row in (cursor.fetchall() or [])
    }


@api_bp.route('/users_progress_month', methods=['GET'])
def users_progress_month():
    from services.db import get_db
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    year = request.args.get('year')
    month = request.args.get('month')

    if not year or not month:
        return jsonify({"error": "Year and month must be provided."}), 400

    try:
        month_start = datetime(int(year), int(month), 1)
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        # A hónap vége KIZÁRÓLAGOS határ. Korábban (next_month - 1 nap) volt,
        # ami a hónap utolsó napjának 00:00:00-ja -> az utolsó nap gyakorlatilag
        # kimaradt az exportból.
        month_end = next_month

        effective_time_query = """
            SELECT 
                w.name AS user,
                ww.work_id,
                wo.WO,
                wo.PN,
                ww.start_time,
                ww.end_time,
                ww.QTY AS completed_qty,
                TIMESTAMPDIFF(SECOND, GREATEST(ww.start_time, %s), LEAST(ww.end_time, %s)) AS effective_time
            FROM workers w
            JOIN workstationworkorder ww ON w.ID = ww.worker_id
            LEFT JOIN workorders wo ON ww.work_id = wo.ID
            WHERE ww.status = 'Completed'
              AND ww.start_time >= %s AND ww.start_time < %s
        """
        cursor.execute(effective_time_query, (month_start, month_end, month_start, month_end))
        effective_time_results = cursor.fetchall()

        # A WO SAJÁT darabszáma kell, nem a PN-hez tartozó bármelyiké.
        wo_map = _wo_norm_map(cursor, [row.get('work_id') for row in effective_time_results])

        users_data = {}
        for row in effective_time_results:
            user = row['user']
            PN = row['PN']
            start_time = safe_datetime(row['start_time'])
            end_time = safe_datetime(row['end_time'])
            wo_info = wo_map.get(row.get('work_id')) or {}
            qty = wo_info.get('QTY', 0.0)
            prod_time = wo_info.get('PROD_TIME', 0.0)
            completed_qty = safe_float(row['completed_qty'])
            expected_time = qty * prod_time * 3600
            elapsed_time = (end_time - start_time).total_seconds() if end_time and start_time else 0
            difference = expected_time - elapsed_time

            users_data.setdefault(user, []).append([
                row["WO"], PN, start_time, end_time,
                qty, completed_qty, prod_time,
                seconds_to_hour_decimal(expected_time),
                seconds_to_hour_decimal(elapsed_time),
                seconds_to_hour_decimal(difference)
            ])

        output = BytesIO()
        label = f"{year}-{month.zfill(2)}"
        wb = _build_export_workbook(users_data, label, "month", EXPORT_CONFIG)
        wb.save(output)
        xlsx_bytes = output.getvalue()
        filename = f"Users_Progress_Month_{year}_{month}.xlsx"
        resp = make_response(xlsx_bytes)
        resp.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        resp.headers['Content-Length'] = len(xlsx_bytes)
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try: cursor.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

@api_bp.route('/users_progress_day', methods=['GET'])
def users_progress_day():
    """
    Napi Excel export.

    Pontosan ugyanazokat a sorokat exportálja, amiket a /api/users_progress
    statisztika és a dashboard táblája is a naphoz sorol (services/workday.py).
    """
    import logging
    _log = logging.getLogger("api.export_day")
    from services.db import get_db
    from services import workday

    date = (request.args.get('date') or '').strip()
    if not date:
        return jsonify({"error": "Date must be provided."}), 400

    station = (request.args.get('station') or '').strip().upper() or None
    if station and station not in workday.ASSEMBLY_STATIONS:
        station = None

    conn = None
    cursor = None
    try:
        _log.info(f"[export_day] Start: date={date} station={station or '-'}")
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        rows = workday.fetch_day_rows(cursor, date, station=station, order="ASC")
        _log.info(f"[export_day] day rows: {len(rows)}")

        wo_map = _wo_norm_map(cursor, [r.get("work_id") for r in rows])
        _log.info(f"[export_day] work orders: {len(wo_map)}")

        users_data = {}
        for r in rows:
            if not r.get("counts_as_effective"):
                continue
            info = wo_map.get(r.get("work_id")) or {}
            qty = info.get('QTY', 0.0)
            prod_time = info.get('PROD_TIME', 0.0)
            completed_qty = safe_float(r.get('done_qty'))

            # A DB szövegként tárolja az időpontokat – az értelmezett
            # változat kell, különben az Excel cella szöveg lenne, és a
            # kivonás is elszállna.
            start_time = r.get('start_dt')
            end_time = r.get('end_dt')

            if r["is_completed"] and start_time and end_time:
                expected_time = qty * prod_time * 3600
                elapsed_time = (end_time - start_time).total_seconds()
                elvart = seconds_to_hour_decimal(expected_time)
                eltelt = seconds_to_hour_decimal(elapsed_time)
                kulonbseg = seconds_to_hour_decimal(expected_time - elapsed_time)
            else:
                # Még fut: nincs értelmes "eltelt" és "különbség" – üresen hagyjuk,
                # különben a le nem zárt munka fals norma-előnyként jelenne meg.
                elvart = eltelt = kulonbseg = None

            users_data.setdefault(r.get('felhasznalo') or '—', []).append([
                r.get("WO"), r.get("PN"), start_time, end_time,
                seconds_to_hour_decimal(r.get("eff_seconds") or 0),
                qty, completed_qty, prod_time,
                elvart, eltelt, kulonbseg,
                workday.status_detail(r),
            ])

        output = BytesIO()
        wb = _build_export_workbook(users_data, date, "day", EXPORT_CONFIG)
        wb.save(output)
        xlsx_bytes = output.getvalue()
        suffix = f"_{station}" if station else ""
        filename = f"Users_Progress_Day_{date}{suffix}.xlsx"
        _log.info(f"[export_day] XLSX built: {len(xlsx_bytes)} bytes")
        resp = make_response(xlsx_bytes)
        resp.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        resp.headers['Content-Length'] = len(xlsx_bytes)
        return resp

    except Exception as e:
        _log.exception("[export_day] date=%s station=%s", date, station)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    finally:
        try:
            if cursor: cursor.close()
        except Exception: pass
        try:
            if conn: conn.close()
        except Exception: pass
        _log.info("[export_day] DB closed")


@api_bp.route('/wo_progress_export', methods=['GET'])
def wo_progress_export():
    """
    WO időrendi timeline export — dátum tartomány alapján.
    Params: start_date, end_date  (YYYY-MM-DD)
    """
    from services.db import get_db
    start_date = request.args.get('start_date', '').strip()
    end_date   = request.args.get('end_date',   '').strip()
    if not start_date or not end_date:
        return jsonify({"error": "start_date és end_date kötelező"}), 400

    conn = cursor = None
    try:
        end_adjusted = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        conn   = get_db()
        cursor = conn.cursor(dictionary=True)
        wo_timeline = _fetch_wo_timeline(cursor, start_date, end_adjusted)

        label    = f"{start_date} – {end_date}"
        wb       = _build_wo_timeline_workbook(wo_timeline, label, EXPORT_CONFIG)
        output   = BytesIO()
        wb.save(output)
        xlsx_bytes = output.getvalue()
        filename = f"WO_Timeline_{start_date}_{end_date}.xlsx"
        resp = make_response(xlsx_bytes)
        resp.headers['Content-Type']        = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        resp.headers['Content-Length']      = len(xlsx_bytes)
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            if cursor: cursor.close()
        except Exception: pass
        try:
            if conn: conn.close()
        except Exception: pass


@api_bp.route('/wo_progress_export_month', methods=['GET'])
def wo_progress_export_month():
    """
    WO időrendi timeline export — teljes hónap.
    Params: year, month
    """
    from services.db import get_db
    year  = request.args.get('year',  '').strip()
    month = request.args.get('month', '').strip()
    if not year or not month:
        return jsonify({"error": "year és month kötelező"}), 400

    conn = cursor = None
    try:
        month_start = datetime(int(year), int(month), 1)
        month_end   = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)

        conn   = get_db()
        cursor = conn.cursor(dictionary=True)
        wo_timeline = _fetch_wo_timeline(cursor,
                                          month_start.strftime("%Y-%m-%d"),
                                          month_end.strftime("%Y-%m-%d"))

        label    = f"{year}-{month.zfill(2)}"
        wb       = _build_wo_timeline_workbook(wo_timeline, label, EXPORT_CONFIG)
        output   = BytesIO()
        wb.save(output)
        xlsx_bytes = output.getvalue()
        filename = f"WO_Timeline_{year}_{month.zfill(2)}.xlsx"
        resp = make_response(xlsx_bytes)
        resp.headers['Content-Type']        = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        resp.headers['Content-Length']      = len(xlsx_bytes)
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            if cursor: cursor.close()
        except Exception: pass
        try:
            if conn: conn.close()
        except Exception: pass


def get_history_data():
    from services.db import get_db
    wo = request.args.get('wo')
    if not wo:
        return jsonify({"error": "WO parameter is missing"}), 400

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT w.name AS worker, ww.start_time, ww.end_time, ww.process_id AS station,
                   ww.next_station_id AS next_station, ww.qty
            FROM workstationworkorder ww
            JOIN workers w ON ww.worker_id = w.ID
            JOIN workorders wo ON ww.work_id = wo.ID
            WHERE wo.WO = %s
            ORDER BY ww.start_time
        """
        cursor.execute(query, (wo,))
        history_data = cursor.fetchall()
        cursor.close()
        return jsonify({"data": history_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@api_bp.route('/update_user/<int:id>', methods=['POST'])
def update_user(id):
    from services.db import get_db
    data = request.json
    conn = get_db()
    cursor = conn.cursor()
    query = """
        UPDATE workstationworkorder
        SET start_time = %s, end_time = %s, status = %s, process_id = %s, next_station_id = %s
        WHERE ID = %s
    """
    cursor.execute(query, (
        data['Start Time'],
        data['End Time'],
        data['Status'],
        data['Process ID'],
        data['Next Station ID'],
        id
    ))
    conn.commit()
    cursor.close()
    return jsonify({"message": "Record updated successfully"})

@api_bp.route('/delete_user/<int:id>', methods=['DELETE'])
def delete_user(id):
    from services.db import get_db
    conn = get_db()
    cursor = conn.cursor()
    query = "DELETE FROM workstationworkorder WHERE ID = %s"
    cursor.execute(query, (id,))
    conn.commit()
    cursor.close()
    return jsonify({"message": "Record deleted successfully"})

@api_bp.route('/delete_selected', methods=['POST'])
def delete_selected_users():
    from services.db import get_db
    data = request.json
    ids = data.get('ids', [])
    if not ids:
        return jsonify({"message": "No IDs provided"}), 400
    conn = get_db()
    cursor = conn.cursor()
    query = "DELETE FROM workstationworkorder WHERE ID IN (%s)" % ','.join(['%s'] * len(ids))
    cursor.execute(query, ids)
    conn.commit()
    cursor.close()
    return jsonify({"message": "Selected records deleted successfully"})

@api_bp.route('/export_to_excel', methods=['GET'])
def export_to_excel():
    from services.db import get_db
    conn = get_db()
    cursor = conn.cursor()
    query = """
        SELECT 
            w.ID, 
            w.workstation_id, 
            w.device_id, 
            wo.name AS worker_name, 
            CONCAT('WO: ', wo2.WO, ', PN: ', wo2.PN, '') AS work_order_data, 
            w.start_time,
            w.end_time,
            w.status,
            w.process_id,
            w.next_station_id,
            w.qty
        FROM workstationworkorder w
        LEFT JOIN workers wo ON w.worker_id = wo.ID
        LEFT JOIN workorders wo2 ON w.work_id = wo2.id
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    cursor.close()

    columns = [
        'ID', 'Workstation ID', 'Device ID', 'Worker Name', 'Work Order Data',
        'Start Time', 'End Time', 'Status', 'Process ID', 'Next Station ID', 'QTY'
    ]
    df = pd.DataFrame(rows, columns=columns)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='UsersData')
        worksheet = writer.sheets['UsersData']
        worksheet.auto_filter.ref = worksheet.dimensions
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name='Users_Data.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@api_bp.route('/get_tables', methods=['GET'])
def get_tables():
    import os
    from services.db import get_db

    print("API FILE:", __file__, flush=True)
    print("CWD:", os.getcwd(), flush=True)
    print("GET /api/get_tables HIT", flush=True)

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    def norm(s):
        return (s or "").strip().upper()

    # 1) asztalok + device
    cursor.execute("""
        SELECT 
            t.table_name,
            t.raspberry_id,
            d.device_name,
            d.device_id
        FROM tables t
        LEFT JOIN raspberrydevices d ON t.raspberry_id = d.id
        ORDER BY t.id DESC
    """)
    tables_rows = cursor.fetchall() or []

    # Debug: milyen device_name-ek vannak a táblákban?
    table_device_names = sorted({norm(r.get("device_name")) for r in tables_rows if r.get("device_name")})
    print("TABLE DEVICES:", table_device_names, flush=True)

    # 2) RAW: bejelentkezett userek (workerworkstation) – ez dönt a zöldről
    cursor.execute("""
        SELECT 
            ww.RASPBERRY_DEVICE AS raspberry_device,
            w.id AS worker_id,
            w.name AS user_name,
            ww.LOGIN_DATE AS login_date
        FROM workerworkstation ww
        JOIN workers w ON ww.WORKER_ID = w.id
        WHERE ww.LOGOUT_DATE IS NULL
           OR ww.LOGOUT_DATE = ''
           OR ww.LOGOUT_DATE = '0000-00-00 00:00:00'
    """)
    raw_logins = cursor.fetchall() or []
    print("RAW LOGINS (workerworkstation):", len(raw_logins), flush=True)

    # Debug: milyen raspberry_device értékek jönnek a workerworkstationből?
    raw_devices = sorted({norm(r.get("raspberry_device")) for r in raw_logins if r.get("raspberry_device")})
    print("RAW DEVICES:", raw_devices, flush=True)

    # Map: NORMALIZÁLT device -> legfrissebb login
    login_by_device = {}
    for r in raw_logins:
        dev_key = norm(r.get("raspberry_device"))
        if not dev_key:
            continue

        prev = login_by_device.get(dev_key)
        if not prev:
            login_by_device[dev_key] = r
        else:
            prev_dt = prev.get("login_date")
            cur_dt = r.get("login_date")
            if cur_dt and prev_dt and cur_dt > prev_dt:
                login_by_device[dev_key] = r
            elif cur_dt and not prev_dt:
                login_by_device[dev_key] = r

    # 3) Aktív ASSEMBLY munka user alapján (hover infó)
    cursor.execute("""
        SELECT
            ww.WORKER_ID AS worker_id,
            wo.WO AS WO,
            wo.PN AS PN,
            ww.START_TIME AS start_time
        FROM workstationworkorder ww
        JOIN workorders wo ON ww.WORK_ID = wo.id
        WHERE UPPER(TRIM(ww.PROCESS_ID)) = 'ASSEMBLY'
          AND DATE(ww.START_TIME) = CURDATE()
          AND (
              UPPER(TRIM(ww.STATUS)) = 'ACTIVE'
              OR ww.END_TIME IS NULL
              OR ww.END_TIME = ''
              OR ww.END_TIME = '0000-00-00 00:00:00'
          )
        ORDER BY ww.START_TIME DESC
    """)
    active_work_rows = cursor.fetchall() or []

    work_by_worker = {}
    for r in active_work_rows:
        wid = r.get("worker_id")
        if wid is None:
            continue
        prev = work_by_worker.get(wid)
        if not prev:
            work_by_worker[wid] = r
        else:
            prev_st = prev.get("start_time")
            cur_st = r.get("start_time")
            if cur_st and prev_st and cur_st > prev_st:
                work_by_worker[wid] = r
            elif cur_st and not prev_st:
                work_by_worker[wid] = r

    # 4) table_data
    table_data = []

    for r in tables_rows:
        table_name = (r.get("table_name") or "")
        raspberry_id = r.get("raspberry_id") or 0
        device_name = r.get("device_name")
        device_ip = r.get("device_id")

        if table_name.startswith("Office"):
            continue

        status = "inactive"
        user_name = None
        login_time = None
        active_work = None

        if raspberry_id != 0 and device_name:
            status = "no_user"

            # NORMALIZÁLT kulccsal keresünk
            dev_key = norm(device_name)
            login_rec = login_by_device.get(dev_key)

            if login_rec:
                status = "active"
                user_name = login_rec.get("user_name")
                wid = login_rec.get("worker_id")

                lt = login_rec.get("login_date")
                login_time = lt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(lt, "strftime") else (lt or None)

                wrec = work_by_worker.get(wid)
                if wrec:
                    st = wrec.get("start_time")
                    st_str = st.strftime("%Y-%m-%d %H:%M:%S") if hasattr(st, "strftime") else (st or None)
                    active_work = {"WO": wrec.get("WO"), "PN": wrec.get("PN"), "start_time": st_str}

        table_data.append({
            "name": device_name if device_name else table_name,
            "raw_name": table_name,
            "device_name": device_name,
            "device_ip": device_ip,
            "status": status,
            "user_name": user_name,
            "login_time": login_time,
            "active_work": active_work
        })

    cursor.close()
    return jsonify({"tables": table_data})




@api_bp.route('/get_pn_data', methods=['GET'])
def get_pn_data():
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 10))
        offset = (page - 1) * per_page

        final_data = cache.get('pn_progress_data')
        if final_data is None:
            print("Cache is empty, reloading data...")
            final_data = reload_pn_data_cache()
            if not final_data:
                return jsonify({"error": "No data available."}), 500

        paginated_data = final_data[offset:offset + per_page]
        response = {
            'data': paginated_data,
            'total': len(final_data),
            'page': page,
            'per_page': per_page,
        }
        return jsonify(response)
    except Exception as e:
        print(f"Error in /get_pn_data endpoint: {e}")
        return jsonify({"error": f"Error occurred: {e}"}), 500

@api_bp.route('/overtime_events', methods=['GET'])
def overtime_events():
    from services.db import get_db
    try:
        db = get_db()
        cursor = db.cursor(dictionary=True)
        page = request.args.get('page', 1, type=int)
        per_page = 10
        offset = (page - 1) * per_page

        query = """
        SELECT 
            wr.name AS worker_name,
            COALESCE(wo.WO, 'N/A') AS WO,
            COALESCE(wo.PN, 'N/A') AS PN,
            wo.QTY AS workorder_qty,
            ww.start_time,
            ww.process_id
        FROM workstationworkorder ww
        LEFT JOIN workers wr ON ww.worker_id = wr.ID
        LEFT JOIN workorders wo ON ww.work_id = wo.ID
        WHERE ww.process_id = 'ASSEMBLY'
        AND ww.status = 'Active'
        ORDER BY ww.start_time DESC
        LIMIT %s OFFSET %s
        """
        cursor.execute(query, (per_page, offset))
        results = cursor.fetchall()

        processed_data = []
        now = datetime.now()

        # PROD idők egyetlen lekérdezéssel (korábban soronként külön query futott)
        prod_map = {}
        pn_norms = sorted({(r['PN'] or '').strip().upper() for r in results if (r['PN'] or '').strip()})
        if pn_norms:
            try:
                ph = ",".join(["%s"] * len(pn_norms))
                prod_cursor = db.cursor()
                prod_cursor.execute(
                    f"SELECT TRIM(UPPER(`PART.NBR`)) AS PN, PROD FROM paperless.t_dump "
                    f"WHERE TRIM(UPPER(`PART.NBR`)) IN ({ph})",
                    tuple(pn_norms),
                )
                for pn_key, prod_val in prod_cursor.fetchall():
                    if pn_key not in prod_map and prod_val is not None:
                        try:
                            prod_map[pn_key] = float(prod_val)
                        except Exception:
                            pass
                prod_cursor.close()
            except Exception as e:
                print(f"[ERROR] PROD lekérdezés: {e}")

        for row in results:
            try:
                start_time = datetime.strptime(row['start_time'], '%Y-%m-%d %H:%M:%S') if row['start_time'] else None
            except (ValueError, TypeError, AttributeError):
                start_time = None

            elapsed_time = (now - start_time).total_seconds() / 3600 if start_time else None

            pn_norm = (row['PN'] or '').strip().upper()
            prod_time = prod_map.get(pn_norm)

            qty = row['workorder_qty'] if row['workorder_qty'] is not None else 'N/A'

            if prod_time and elapsed_time and elapsed_time > prod_time:
                processed_data.append({
                    'felhasznalo': row['worker_name'] or 'Ismeretlen',
                    'WO': row['WO'],
                    'PN': row['PN'],
                    'start_time': row['start_time'][:16] if row['start_time'] else '-',
                    'PROD_time': f"{prod_time:.2f}",
                    'elapsed_time': f"{elapsed_time:.2f}",
                    'QTY': qty
                })

        count_query = """
        SELECT COUNT(*) FROM workstationworkorder ww
        LEFT JOIN workorders wo ON ww.work_id = wo.ID
        WHERE ww.process_id = 'ASSEMBLY'
        """
        cursor.execute(count_query)
        total_records = cursor.fetchone()["COUNT(*)"]

        cursor.close()
        return jsonify({
            "data": processed_data,
            "total": total_records,
            "page": page,
            "per_page": per_page
        })

    except mysql.connector.Error as e:
        print(f"MariaDB hiba: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        print(f"Általános hiba: {e}")
        return jsonify({"error": str(e)}), 500

# api.py


# api.py
@api_bp.route('/assembly_data', methods=['GET'])
def assembly_data():
    """
    Dashboard – "Összeszerelési állapotok" tábla.

    Alapértelmezés: a MAI nap összes munkája az adott állomáson, beleértve a
    még futó és az előző napról átnyúló munkát is.

    Query paraméterek:
      station   : EMI | MTE | MDI | QC | TEST | SOLD | MOLD
      date      : YYYY-MM-DD, vagy 'all' (dátumszűrés nélkül)
      page      : 1-től
      per_page  : 1..1000, vagy 0 = mind (1000-es felső korláttal)

    Napra szűrve ismerjük a pontos összdarabszámot (total / total_pages).
    'all' esetén nem számolunk COUNT(*)-ot a teljes előzményen – helyette
    eggyel több sort kérünk le, és has_more jelzi, hogy van-e még.
    """
    import logging
    from services import workday
    _log = logging.getLogger("api.assembly_data")

    page = per_page = 0
    req_station = req_date = None
    try:
        page = max(request.args.get('page', 1, type=int) or 1, 1)

        raw_per_page = request.args.get('per_page', 25, type=int)
        raw_per_page = 25 if raw_per_page is None else raw_per_page
        # per_page <= 0  ->  "mind", felső korláttal
        per_page = 1000 if raw_per_page <= 0 else min(raw_per_page, 1000)
        offset = (page - 1) * per_page

        req_station = (request.args.get('station') or '').strip().upper()
        if req_station not in workday.ASSEMBLY_STATIONS:
            user_job_title = ((session.get('user', {}) or {}).get('job_title', '') or '').strip().upper()
            req_station = user_job_title if user_job_title in workday.ASSEMBLY_STATIONS else "EMI"

        req_date = (request.args.get('date') or '').strip()
        if req_date.lower() == 'all':
            req_date = None
        elif not req_date:
            # Alapértelmezés: MA. Enélkül a tábla a legutóbbi néhány sort
            # mutatta, dátumtól függetlenül.
            req_date = datetime.now().strftime('%Y-%m-%d')

        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            if req_date:
                total = workday.count_day_rows(cur, req_date, station=req_station)
                rows = workday.fetch_day_rows(
                    cur, req_date, station=req_station, limit=per_page, offset=offset
                )
                has_more = page * per_page < total
                total_pages = max(1, -(-total // per_page))
            else:
                # +1 sor: ebből tudjuk, hogy van-e még következő oldal
                rows = workday.fetch_station_rows(cur, req_station, per_page + 1, offset)
                has_more = len(rows) > per_page
                rows = rows[:per_page]
                total = None
                total_pages = None
        finally:
            try: cur.close()
            except Exception: pass

        out = []
        for r in rows:
            out.append({
                "felhasznalo": r.get("felhasznalo") or "",
                "WO": r.get("WO") or "",
                "PN": r.get("PN") or "",
                "start_time": workday.fmt_dt(r.get("start_time")),
                "end_time": workday.fmt_dt(r.get("end_time")),
                "status": r.get("status") or "",
                "current_station": r.get("current_station") or "",
                "next_station_id": r.get("next_station_id") or "",
                "done_qty": workday.as_int(r.get("done_qty")),
                "total_qty": workday.as_int(r.get("total_qty")),
                "status_detail": workday.status_detail(r),
                "qty_text": workday.qty_text(r),
                # aznapi, napra vágott munkaidő ezen a soron (mp)
                "day_seconds": workday.as_int(r.get("eff_seconds")),
            })

        return jsonify({
            "rows": out,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_more": has_more,
            "date": req_date or "all",
            "station": req_station,
        })

    except Exception as e:
        # Teljes stacktrace a szerver logba, beszédes üzenet a felületre –
        # egy csupasz "API error (500)" semmit nem árul el.
        _log.exception(
            "[assembly_data] station=%s date=%s page=%s per_page=%s",
            req_station, req_date, page, per_page,
        )
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@api_bp.route('/update_process_and_refresh', methods=['POST'])
def update_process_and_refresh():
    if 'user' not in session or session['user'].get('job_title', '') != "IT":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json
    selected_process_id = data.get('new_process_id')
    if not selected_process_id:
        return jsonify({"error": "Missing parameters"}), 400

    try:
        refreshed_data = get_assembly_data(user_job_title="IT", process_id=selected_process_id)
        return jsonify({"message": "Data refreshed successfully", "data": refreshed_data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@api_bp.route('/wo_progress_api', methods=['GET'])
def wo_progress_api():
    from services.db import get_db
    try:
        db = get_db()
        if db is None:
            return jsonify({"error": "Adatbaziskapcsolat nem elerheto"}), 500

        cursor = db.cursor(dictionary=True)

        page = request.args.get("page", default=1, type=int)
        limit = request.args.get("limit", default=50, type=int)
        offset = (page - 1) * limit

        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")

        if not start_date or not end_date:
            return jsonify({"error": "Meg kell adni a kezdeti es vegso datumot!"}), 400

        end_date_adjusted = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

        cursor.execute("""
            SELECT ws.work_id, ws.process_id, ws.next_station_id, ws.QTY, ws.end_time, ws.start_time, ws.status
            FROM workstationworkorder ws
            WHERE (ws.start_time >= %s AND ws.start_time < %s)
               OR (ws.end_time >= %s AND ws.end_time < %s)
            ORDER BY ws.start_time DESC
        """, (start_date, end_date_adjusted, start_date, end_date_adjusted))
        work_data = cursor.fetchall()

        if not work_data:
            return jsonify({
                "data": [],
                "total_records": 0,
                "page": page,
                "limit": limit,
                "total_pages": 0
            })

        work_id_list = [row["work_id"] for row in work_data]

        cursor.execute("""
            SELECT id, WO FROM workorders WHERE id IN ({})
        """.format(",".join(["%s"] * len(work_id_list))), tuple(work_id_list))
        wo_mapping = {str(row["id"]): row["WO"] for row in cursor.fetchall()}

        cursor.execute("""
            SELECT WO, PN, SUM(QTY) AS total_qty
            FROM workorders
            WHERE WO IN (%s)
            GROUP BY WO, PN
        """ % ",".join(["%s"] * len(wo_mapping.values())), list(wo_mapping.values()))
        wo_pn_qty = {}
        for row in cursor.fetchall():
            if row["WO"] not in wo_pn_qty:
                wo_pn_qty[row["WO"]] = {"total_qty": 0, "pn_list": []}
            wo_pn_qty[row["WO"]]["total_qty"] += row["total_qty"]
            wo_pn_qty[row["WO"]]["pn_list"].append(row["PN"])

        # Az összes érintett PN időadatát és SUBS sorát EGYSZER kérdezzük le
        # (korábban WO-nként és PN-enként külön query futott -> N×M lekérdezés).
        all_pns = sorted({pn for data in wo_pn_qty.values() for pn in data["pn_list"]})
        pn_time_map_all: dict = {}
        subs_rows_all: list = []
        if all_pns:
            ph = ",".join(["%s"] * len(all_pns))
            cursor.execute(f"""
                SELECT `PART.NBR` AS PN,
                       SUM(PROD) AS PROD,
                       SUM(FIQC) AS FIQC,
                       SUM(TEST) AS TEST,
                       SUM(WCUT) AS WCUT
                FROM t_dump
                WHERE `PART.NBR` IN ({ph})
                GROUP BY `PART.NBR`
            """, tuple(all_pns))
            for row in cursor.fetchall():
                pn_time_map_all[row["PN"]] = (
                    float(row["PROD"] or 0) + float(row["FIQC"] or 0)
                    + float(row["TEST"] or 0) + float(row["WCUT"] or 0)
                )

            cursor.execute(f"""
                SELECT `PART.NBR`, `SUBS`, `SUBS QTYS`
                FROM t_dump
                WHERE `PART.NBR` IN ({ph})
                AND `SUBS` IS NOT NULL
            """, tuple(all_pns))
            subs_rows_all = cursor.fetchall()

        st_time_values = {}
        for wo, data in wo_pn_qty.items():
            pn_list = data["pn_list"]
            if not pn_list:
                st_time_values[wo] = 0
                continue
            pn_set = set(pn_list)
            multiplier_map = {pn: 1.0 for pn in pn_list}
            for row in subs_rows_all:
                if row["PART.NBR"] not in pn_set:
                    continue
                subs = row["SUBS"].split("|") if row["SUBS"] else []
                qtys = row["SUBS QTYS"].split("|") if row["SUBS QTYS"] else []
                for sub, qty in zip(subs, qtys):
                    if sub in multiplier_map:
                        multiplier_map[sub] += float(qty)
            total_weighted = sum(
                pn_time_map_all.get(pn, 0.0) * multiplier_map.get(pn, 1.0)
                for pn in pn_list
            )
            st_time_values[wo] = round(total_weighted, 3)

        process_qty = {}
        work_data_sorted = sorted(work_data, key=lambda x: x["start_time"] if x["start_time"] else "9999-12-31 23:59:59")
        for row in work_data_sorted:
            work_id = str(row["work_id"])
            process_id = row["process_id"].strip().upper() if row["process_id"] else ""
            next_station = row["next_station_id"].strip().upper() if row["next_station_id"] else None
            qty_value = row["QTY"] if row["QTY"] is not None else 0
            status = row["status"].strip().upper() if "status" in row and row["status"] else ""

            # a wo_mapping már fent egyben le lett kérdezve – nincs soronkénti query
            wo = wo_mapping.get(str(work_id))
            if not wo:
                continue

            # --- itt szerepeljen a FIAS is ---
            if wo not in process_qty:
                process_qty[wo] = {
                    station: 0
                    for station in ["ASSEMBLY", "PROD", "CRIMP", "SOLD", "MOLD",
                                    "FIAS", "TEST", "QC", "SHIP", "VYPRISIV"]
                }

            prod_qty = process_qty[wo]["PROD"]
            if isinstance(prod_qty, str) and "WORKING" in prod_qty:
                prod_qty = int(prod_qty.split("/")[-1].strip()) if "/" in prod_qty else 0

            if status == "ACTIVE" and prod_qty == 0:
                process_qty[wo]["PROD"] = "WORKING"
            if status == "ACTIVE" and prod_qty > 0:
                process_qty[wo]["PROD"] = f"WORKING / {prod_qty}"
            if status == "COMPLETED":
                process_qty[wo]["PROD"] = 0

            if process_id and process_id in process_qty[wo]:
                process_qty[wo][process_id] = max(0, process_qty[wo][process_id] - qty_value)

            if next_station:
                # <<< EZ FONTOS: ha nincs még ilyen kulcs, legyen 0 >>>
                process_qty[wo].setdefault(next_station, 0)
                process_qty[wo][next_station] += qty_value

            if next_station == "ASSEMBLY":
                if "WORKING" in str(process_qty[wo]["PROD"]):
                    current_qty = int(process_qty[wo]["PROD"].split("/")[1].strip()) if "/" in process_qty[wo]["PROD"] else 0
                    new_qty = current_qty + qty_value
                    process_qty[wo]["PROD"] = f"WORKING / {new_qty}"
                else:
                    process_qty[wo]["PROD"] = f"WORKING / {qty_value}"

            if process_id == "ASSEMBLY" and next_station != "ASSEMBLY":
                if "WORKING" in str(process_qty[wo]["PROD"]):
                    current_qty = int(process_qty[wo]["PROD"].split("/")[1].strip()) if "/" in process_qty[wo]["PROD"] else 0
                    new_qty = max(0, current_qty - qty_value)
                    if new_qty == 0 and qty_value > 0:
                        process_qty[wo]["PROD"] = 0
                    else:
                        process_qty[wo]["PROD"] = f"WORKING / {new_qty}"
                elif isinstance(process_qty[wo]["PROD"], int):
                    process_qty[wo]["PROD"] = max(0, process_qty[wo]["PROD"] - qty_value)


        results = []
        for wo in process_qty:
            results.append({
                "WO": wo,
                "total_qty": wo_pn_qty.get(wo, {}).get("total_qty", 0),
                "st_time": st_time_values.get(wo, 0),
                "PROD": process_qty[wo].get("PROD", 0),
                "CRIMP": process_qty[wo].get("CRIMP", 0),
                "SOLD": process_qty[wo].get("SOLD", 0),
                "MOLD": process_qty[wo].get("MOLD", 0),
                "FIAS": process_qty[wo].get("FIAS", 0),
                "TEST": process_qty[wo].get("TEST", 0),
                "QC": process_qty[wo].get("QC", 0),
                "SHIP": process_qty[wo].get("SHIP", 0),
                "VYPRISIV": process_qty[wo].get("VYPRISIV", 0)
            })

        total_records = len(results)
        total_pages = (total_records // limit) + (1 if total_records % limit > 0 else 0)
        cursor.close()
        db.close()

        return jsonify({
            "data": results[offset:offset+limit],
            "total_records": total_records,
            "page": page,
            "limit": limit,
            "total_pages": total_pages
        })

    except Exception as e:
        print("API hiba tortent:", e)
        return jsonify({"error": str(e)}), 500


@api_bp.route('/wo_route_api', methods=['GET'])
def wo_route_api():
    """
    Egy adott WO teljes 'ping-pong' útvonalát adja vissza időrendben.
    Query param: ?wo=WO-1234
    """
    from services.db import get_db

    try:
        wo = request.args.get("wo", "").strip()
        if not wo:
            return jsonify({"error": "A 'wo' parameter kotelezo (pl. ?wo=WO-236812)"}), 400

        db = get_db()
        if db is None:
            return jsonify({"error": "Adatbaziskapcsolat nem elerheto"}), 500

        cursor = db.cursor(dictionary=True)

        # 1) Megkeressük az összes workorders sort ehhez a WO-hoz (lehet több PN is egy WO alatt)
        cursor.execute("""
            SELECT id, WO, PN
            FROM workorders
            WHERE WO = %s
        """, (wo,))
        wo_rows = cursor.fetchall()

        if not wo_rows:
            cursor.close()
            db.close()
            return jsonify({
                "wo": wo,
                "pn": None,
                "events": []
            })

        work_ids = [row["id"] for row in wo_rows]
        # Ha több PN van egy WO alatt, itt most az első PN-t tesszük ki headerbe,
        # de az eventekben is benne lesz külön PN-ként.
        pn_header = wo_rows[0]["PN"]

        # 2) Összes workstationworkorder sor ehhez a WO-hoz (idők szerint rendezve)
        cursor.execute("""
            SELECT
                wsw.work_id,
                wsw.process_id,
                wsw.next_station_id,
                wsw.QTY,
                wsw.status,
                wsw.start_time,
                wsw.end_time,
                wo.WO,
                wo.PN
            FROM workstationworkorder AS wsw
            JOIN workorders AS wo ON wsw.work_id = wo.id
            WHERE wo.WO = %s
            ORDER BY COALESCE(wsw.start_time, wsw.end_time) ASC, wsw.work_id ASC
        """, (wo,))

        rows = cursor.fetchall() or []

        events = []
        for r in rows:
            # idő: ha van start_time, azt használjuk, különben end_time
            t = r.get("start_time") or r.get("end_time")
            if isinstance(t, datetime):
                time_str = t.strftime("%Y-%m-%d %H:%M:%S")
            else:
                time_str = None

            process_id = (r.get("process_id") or "").strip().upper()
            next_station = (r.get("next_station_id") or "").strip().upper()
            status = (r.get("status") or "").strip().upper()

            # hogy a timeline jól olvasható legyen:
            # ha nincs process_id, tekintsük "START"-nak
            from_step = process_id if process_id else "START"
            # ha nincs next_station, tekintsük "END"-nek
            to_step = next_station if next_station else "END"

            qty = r.get("QTY") or 0

            events.append({
                "time": time_str,
                "from_step": from_step,
                "to_step": to_step,
                "qty": qty,
                "status": status,
                # plusz opcionális mezők, ha a frontendnek később kell
                "pn": r.get("PN"),
                "work_id": r.get("work_id"),
            })

        cursor.close()
        db.close()

        return jsonify({
            "wo": wo,
            "pn": pn_header,
            "events": events
        })

    except Exception as e:
        print("WO_ROUTE_API hiba tortent:", e)
        return jsonify({"error": str(e)}), 500
    
# api.py
# Rövid TTL cache az incoming_workorders-hez: a dashboardok másodpercenként
# pollozzák, de ugyanazt az eredményt elég ~10 mp-enként kiszámolni.
_INCOMING_CACHE: dict = {}
_INCOMING_CACHE_TTL = 10.0


@api_bp.route('/incoming_workorders', methods=['GET'])
@login_required()
def incoming_workorders():
    import time as _time

    u = session.get("user") or {}

    allowed = _incoming_allowed_stations_for_user(u)
    if not allowed:
        return jsonify({"ok": False, "error": "No allowed stations"}), 403

    station = (request.args.get("station") or allowed[0]).strip().upper()

    if station not in allowed:
        return jsonify({"ok": False, "error": "Forbidden station"}), 403

    page = request.args.get("page", 1, type=int) or 1
    per_page = request.args.get("per_page", 10, type=int) or 10
    per_page = max(1, min(per_page, 50))
    offset = (page - 1) * per_page

    cache_key = (station, page, per_page)
    hit = _INCOMING_CACHE.get(cache_key)
    if hit and (_time.monotonic() - hit[1]) < _INCOMING_CACHE_TTL:
        payload = dict(hit[0])
        payload["allowed_stations"] = allowed  # ez user-függő, ne cache-ből jöjjön
        return jsonify(payload)

    db = get_db()
    cur = db.cursor(dictionary=True)

    # közös subquery: adott station-ön COMPLETED mennyiség összegzése
    done_join_sql = """
        LEFT JOIN (
            SELECT work_id, SUM(COALESCE(QTY,0)) AS done_qty
            FROM workstationworkorder
            WHERE TRIM(UPPER(COALESCE(process_id,''))) = %s
              AND TRIM(UPPER(COALESCE(status,''))) = 'COMPLETED'
              AND end_time IS NOT NULL
              AND end_time <> ''
              AND end_time <> '0000-00-00 00:00:00'
            GROUP BY work_id
        ) done ON done.work_id = wsw.work_id
    """

    # --- összes db (pagination) ---
    # --- összes db (pagination) ---
    count_sql = f"""
        SELECT COUNT(*) AS total
        FROM (
            SELECT wsw.work_id
            FROM workstationworkorder wsw

            JOIN (
                SELECT work_id, MAX(id) AS max_id
                FROM workstationworkorder
                WHERE TRIM(UPPER(COALESCE(next_station_id,''))) = %s
                AND TRIM(UPPER(COALESCE(status,''))) = 'COMPLETED'
                AND end_time IS NOT NULL
                AND end_time <> ''
                AND end_time <> '0000-00-00 00:00:00'
                GROUP BY work_id
            ) last_send ON last_send.work_id = wsw.work_id AND last_send.max_id = wsw.id

            JOIN workorders wo ON wo.id = wsw.work_id

            {done_join_sql}

            WHERE TRIM(UPPER(COALESCE(wsw.next_station_id,''))) = %s
            AND TRIM(UPPER(COALESCE(wsw.status,''))) = 'COMPLETED'
            AND wsw.end_time IS NOT NULL
            AND wsw.end_time <> ''
            AND wsw.end_time <> '0000-00-00 00:00:00'

            -- csak akkor várakozó, ha done < total
            AND LEAST(COALESCE(done.done_qty,0), COALESCE(wo.QTY,0)) < COALESCE(wo.QTY,0)

        ) x
    """
    # paramok sorrend:
    # 1) last_send station
    # 2) done_join station
    # 3) WHERE next_station station
    cur.execute(count_sql, (station, station, station))
    total = int((cur.fetchone() or {}).get("total", 0))


    # --- lista ---
    data_sql = f"""
        SELECT
            wo.WO,
            wo.PN,
            COALESCE(wo.QTY,0) AS wo_qty,
            LEAST(COALESCE(done.done_qty,0), COALESCE(wo.QTY,0)) AS done_qty,


            wsw.process_id AS from_station,
            wsw.next_station_id AS to_station,
            wsw.QTY AS moved_qty,
            wsw.status,
            COALESCE(wsw.end_time, wsw.start_time) AS last_time

        FROM workstationworkorder wsw

        JOIN (
            SELECT work_id, MAX(id) AS max_id
            FROM workstationworkorder
            WHERE TRIM(UPPER(COALESCE(next_station_id,''))) = %s
            AND TRIM(UPPER(COALESCE(status,''))) = 'COMPLETED'
            AND end_time IS NOT NULL
            AND end_time <> ''
            AND end_time <> '0000-00-00 00:00:00'
            GROUP BY work_id
        ) last_send ON last_send.work_id = wsw.work_id AND last_send.max_id = wsw.id

        JOIN workorders wo ON wo.id = wsw.work_id

        {done_join_sql}

        WHERE TRIM(UPPER(COALESCE(wsw.next_station_id,''))) = %s
        AND TRIM(UPPER(COALESCE(wsw.status,''))) = 'COMPLETED'
        AND wsw.end_time IS NOT NULL
        AND wsw.end_time <> ''
        AND wsw.end_time <> '0000-00-00 00:00:00'

        AND LEAST(COALESCE(done.done_qty,0), COALESCE(wo.QTY,0)) < COALESCE(wo.QTY,0)


        ORDER BY COALESCE(wsw.end_time, wsw.start_time) DESC
        LIMIT %s OFFSET %s
    """
    # paramok:
    # 1) last_send station
    # 2) done_join station
    # 3) WHERE next_station station
    # 4-5) limit/offset
    cur.execute(data_sql, (station, station, station, per_page, offset))
    rows = cur.fetchall() or []


    cur.close()

    # ✅ qty_text (done/total) + a FOGADÓ állomás valódi állapota
    for r in rows:
        done = int(r.get("done_qty") or 0)
        tot  = int(r.get("wo_qty") or 0)
        r["qty_text"] = f"{done} / {tot}" if tot else f"{done} / 0"

        # A wsw.status itt mindig 'COMPLETED', mert a KÜLDŐ állomás rekordja –
        # az azt jelenti, hogy az előző állomás befejezte és átadta. A fogadó
        # állomáson viszont a WO még nincs kész, ezért abból nem lehet státuszt
        # kiírni. A valós állapot a legyártott / kért darabszámból jön.
        if tot and done >= tot:
            r["state"] = "done"          # ide elvileg nem jut el (done < total szűrő)
        elif done > 0:
            r["state"] = "in_progress"
        else:
            r["state"] = "waiting"

    total_pages = (total + per_page - 1) // per_page

    payload = {
        "ok": True,
        "station": station,
        "allowed_stations": allowed,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "data": rows
    }
    _INCOMING_CACHE[cache_key] = (payload, _time.monotonic())
    # ne nőjön korlátlanul, ha sok station/lap kombináció fordul elő
    if len(_INCOMING_CACHE) > 200:
        _INCOMING_CACHE.clear()
    return jsonify(payload)



@api_bp.route('/workorders_search', methods=['GET'])
@login_required()
def workorders_search():
    # ✅ username alapú engedély
    u = session.get("user") or {}
    if not _can_use_wo_search(u):
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"ok": True, "data": []})

    db = get_db()
    cur = db.cursor(dictionary=True)

    # WO lehet szám, de mi stringként kezeljük
    like = f"%{q}%"

    # Ha sok adat van, érdemes index: workorders(WO), workorders(PN)
    cur.execute("""
        SELECT
            WO AS wo,
            GROUP_CONCAT(DISTINCT PN ORDER BY PN SEPARATOR ' | ') AS pn_list,
            SUM(COALESCE(QTY,0)) AS total_qty
        FROM workorders
        WHERE CAST(WO AS CHAR) LIKE %s
           OR CAST(PN AS CHAR) LIKE %s
        GROUP BY WO
        ORDER BY WO DESC
        LIMIT 200
    """, (like, like))

    rows = cur.fetchall() or []
    cur.close()

    return jsonify({"ok": True, "data": rows})

@api_bp.route('/workorder_priorities', methods=['GET'])
@login_required()
def workorder_priorities():
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT work_id, wo, priority FROM workorder_priority WHERE work_id IS NOT NULL")
        rows = cur.fetchall() or []
        return jsonify({
            "by_work_id": {str(r["work_id"]): (r.get("priority") or "NORMAL") for r in rows},
            "by_wo":      {str(r["wo"]):      (r.get("priority") or "NORMAL") for r in rows if r.get("wo")}
        })
    except mysql.connector.Error as e:
        print("[workorder_priorities] DB error:", e)
        return jsonify({"by_work_id": {}, "by_wo": {}})
    finally:
        cur.close()



# ===== "Kész" jelzés (csak jelzés értékű) =====
# Bárki átállíthatja, aki a TeamLeader oldalt használhatja, és MINDENKI látja.
_WO_DONE_ROLES = MANAGER_ROLES | TEAMLEADER_ROLES | IT_ROLES


def _can_use_tl_page(u: dict) -> bool:
    return has_any_role((u or {}).get("job_title", ""), _WO_DONE_ROLES)


@api_bp.route('/workorder_done', methods=['GET'])
@login_required()
def workorder_done_map():
    """A késznek jelölt WO-k – minden bejelentkezett néző lekérheti."""
    try:
        return jsonify({"ok": True, "by_wo": wo_done.get_done_map()})
    except Exception as e:
        print("[workorder_done] hiba:", e)
        return jsonify({"ok": True, "by_wo": {}})


@api_bp.route('/set_workorder_done', methods=['POST'])
@login_required()
def set_workorder_done():
    """Body: { wo: "251796", done: true|false }"""
    u = session.get("user") or {}
    if not _can_use_tl_page(u):
        return jsonify({"error": "Forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    wo = payload.get("wo")
    done = bool(payload.get("done"))
    actor = u.get("username") or u.get("display_name") or ""

    try:
        res = wo_done.set_done(wo, done, actor=actor)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        print("[set_workorder_done] hiba:", e)
        return jsonify({"error": "DB hiba"}), 500

    return jsonify({"ok": True, **res})


@api_bp.route('/set_workorder_priority', methods=['POST'])
@login_required()
def set_workorder_priority():
    """
    Body elfogadott formák:
      - { work_id: 12345, priority: "URGENT|IMPORTANT|NORMAL" }
      - { wo: "251796", priority: "URGENT|IMPORTANT|NORMAL" }
      - { wsw_id: 1914, priority: "..." }   # workstationworkorder.id
    """
    # ✅ Prioritást csak az állíthat, akit az "Egyéb beállítások" oldalon
    #    kiválasztottak. Szabály nélkül nincs engedély.
    if not tl_settings.can_set_priority(session.get("user") or {}):
        return jsonify({"error": "Forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    print("[set_workorder_priority] payload =", payload, flush=True)

    def as_int(x):
        try:
            return int(str(x).strip())
        except Exception:
            return None

    priority = str(payload.get("priority") or "").strip().upper()
    if priority not in ("URGENT", "IMPORTANT", "NORMAL"):
        return jsonify({"error": "Bad priority"}), 400

    # 1) próbáljuk több kulcsból kiolvasni a work_id-t
    work_id = as_int(payload.get("work_id") or payload.get("workId") or payload.get("id"))
    wsw_id  = as_int(payload.get("wsw_id") or payload.get("workstationworkorder_id"))

    wo_param = str(payload.get("wo") or payload.get("WO") or "").strip()

    db = get_db()
    cur = db.cursor(dictionary=True)

    try:
        w = None

        # A) ha WO-t kaptunk, abból szerezzünk workorders.id-t
        if not work_id and wo_param:
            cur.execute("""
                SELECT id, WO, PN
                FROM workorders
                WHERE TRIM(WO) = %s
                ORDER BY id ASC
                LIMIT 1
            """, (wo_param,))
            w = cur.fetchone()
            if w:
                work_id = int(w["id"])

        # B) ha workstationworkorder.id jött, abból feloldjuk a work_id-t
        if not w and not work_id and wsw_id:
            cur.execute("SELECT work_id FROM workstationworkorder WHERE id = %s LIMIT 1", (wsw_id,))
            x = cur.fetchone()
            if x and x.get("work_id"):
                work_id = int(x["work_id"])

        # C) ha van work_id, abból lekérjük WO/PN-t
        if not w and work_id:
            cur.execute("SELECT WO, PN FROM workorders WHERE id = %s LIMIT 1", (work_id,))
            w = cur.fetchone()

        if not w or not work_id:
            return jsonify({"error": "Missing/invalid work_id (send work_id OR wo)"}), 400

        wo = (w.get("WO") or "").strip()
        pn = (w.get("PN") or "").strip()

        if not wo:
            return jsonify({"error": "Missing WO for this work_id"}), 500

        user = session.get("user", {})
        updated_by = user.get("username") or user.get("name") or user.get("id")

        cur2 = db.cursor()
        try:
            try:
                cur2.execute("""
                    INSERT INTO workorder_priority (work_id, wo, pn, priority, updated_by)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        wo = VALUES(wo),
                        pn = VALUES(pn),
                        priority = VALUES(priority),
                        updated_by = VALUES(updated_by),
                        updated_at = CURRENT_TIMESTAMP
                """, (work_id, wo, pn, priority, updated_by))
            except mysql.connector.Error as e:
                if "unknown column" in str(e).lower():
                    cur2.execute("""
                        INSERT INTO workorder_priority (work_id, wo, pn, priority)
                        VALUES (%s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            wo = VALUES(wo),
                            pn = VALUES(pn),
                            priority = VALUES(priority)
                    """, (work_id, wo, pn, priority))
                else:
                    raise

            db.commit()
            return jsonify({"ok": True, "work_id": work_id, "wo": wo, "pn": pn, "priority": priority})

        finally:
            try: cur2.close()
            except Exception: pass

    except mysql.connector.Error as e:
        print("[set_workorder_priority] DB error:", e)
        return jsonify({"error": str(e)}), 500
    finally:
        try: cur.close()
        except Exception: pass


# ---- WO kereső jogosultság (username alapú) ----
_WO_SEARCH_ALLOWED_USERNAMES = {
    # IDE olyan értéket írj, amit a session userből _get_username() ténylegesen visszaad
    # pl: "nikolas", vagy "nikolas trenčík", vagy email, stb.
    "ntrencik",
}

# előre normalizáljuk (ne kelljen mindig újra)
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
    # Elsődleges: "Egyéb beállítások" oldalon felvett szabály.
    allowed = tl_settings.can_wo_search(u)
    if allowed is not None:
        return allowed
    return _get_username(u) in _WO_SEARCH_ALLOWED_USERNAMES


# ===== Incoming (várakozó) - station engedélyek user alapján =====
from services.workday import ASSEMBLY_STATIONS as _ASSEMBLY_STATIONS
# Ugyanaz a lista, mint az összeszerelési táblánál – egy helyen karbantartva.
_INCOMING_ALLOWED_STATIONS = list(_ASSEMBLY_STATIONS)

_INCOMING_EXCEPTION_NAMES = {
    # "NIKOLAS TRENCÍK",
}
_INCOMING_EXCEPTION_JOB_TITLES = {
    "SHIFT SUPERVISOR",
    # "PRODUCTION MANAGER",
    # "PLANNER",
}

def _incoming_is_exception_user(u: dict) -> bool:
    name = str(u.get("name") or u.get("user_name") or u.get("username") or "").strip().upper()
    job  = str(u.get("job_title") or u.get("title") or "").strip().upper()
    return (name in _INCOMING_EXCEPTION_NAMES) or (job in _INCOMING_EXCEPTION_JOB_TITLES)

def _incoming_allowed_stations_for_user(u: dict) -> list[str]:
    # Elsődleges: "Egyéb beállítások" oldalon felvett állomás-szabály.
    override = tl_settings.stations_for(u)
    if override:
        return [s for s in override if s in _INCOMING_ALLOWED_STATIONS]

    # ✅ Quality Team Leader csak QC + TEST
    jt = str(u.get("job_title") or u.get("title") or "").strip().upper()
    if jt == "QUALITY TEAM LEADER":
        return ["QC", "TEST"]

    # kivételesek mindent látnak
    if _incoming_is_exception_user(u):
        return list(_INCOMING_ALLOWED_STATIONS)

    job_title = str(u.get("job_title") or u.get("title") or "").upper()

    roles = u.get("roles") or u.get("role") or ""
    roles_txt = " ".join([str(x) for x in roles]) if isinstance(roles, (list, tuple)) else str(roles)

    blob = f"{job_title} {roles_txt}".upper()

    # IT / Manager mindent láthat
    if "IT" in blob or "MANAGER" in blob:
        return list(_INCOMING_ALLOWED_STATIONS)

    # ami szerepel a job_title/roles-ben, az engedélyezett
    allowed = [s for s in _INCOMING_ALLOWED_STATIONS if s in blob]
    return allowed