# -*- coding: utf-8 -*-
"""
services/email_scheduler.py
============================
Ütemezett riport-küldő modul.

Feladata:
  - APScheduler segítségével ütemezetten Excel riportokat generál
    (Users Progress és/vagy WO Timeline) és emailben elküldi azokat
    az email_report_schedules táblában beállított fogadóknak.
  - A Flask app indításakor init_scheduler(app) hívással aktiválódik.

DB táblák (egyszer kell létrehozni):
──────────────────────────────────────────────────────────
  CREATE TABLE IF NOT EXISTS email_report_schedules (
      id              INT AUTO_INCREMENT PRIMARY KEY,
      name            VARCHAR(255)  NOT NULL,
      report_type     ENUM(
                          'users_day','users_month',
                          'wo_day','wo_month',
                          'both_day','both_month'
                      ) NOT NULL,
      frequency       ENUM('daily','weekly','monthly') NOT NULL DEFAULT 'daily',
      hour            TINYINT  NOT NULL DEFAULT 15,
      minute          TINYINT  NOT NULL DEFAULT 30,
      day_of_week     TINYINT  DEFAULT NULL,  -- 0=hétfő … 6=vasárnap (weekly)
      day_of_month    TINYINT  DEFAULT NULL,  -- 1-28 (monthly)
      active          TINYINT(1) NOT NULL DEFAULT 1,
      last_run_at     DATETIME DEFAULT NULL,
      last_status     ENUM('ok','error','never') NOT NULL DEFAULT 'never',
      last_error      TEXT     DEFAULT NULL,
      created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
  );

  CREATE TABLE IF NOT EXISTS email_report_schedule_recipients (
      id              INT AUTO_INCREMENT PRIMARY KEY,
      schedule_id     INT NOT NULL,
      recipient_type  ENUM('user','group') NOT NULL,
      recipient_id    INT NOT NULL,
      recipient_role  ENUM('to','cc') NOT NULL DEFAULT 'to',
      UNIQUE KEY uq_ersr (schedule_id, recipient_type, recipient_id, recipient_role)
  );
──────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import smtplib
import os
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("email_scheduler")

# ── Sablon renderelés  {{változó}} → érték ────────────────────────────────────
import re as _re
_TPL_RE = _re.compile(r"\{\{\s*(\w+)\s*\}\}")

def _render_tpl(template: str, data: dict) -> str:
    """{{változó}} helyettesítés — ismeretlen változók érintetlenül maradnak."""
    return _TPL_RE.sub(lambda m: str(data.get(m.group(1).strip(), m.group(0))), template)

# ── Excel export stílus konfiguráció ─────────────────────────────────────────
EXPORT_CONFIG = {
    "hatekonysag_jo_max":          100.0,
    "hatekonysag_kozep_max":       120.0,
    "kulonbseg_figyelmezteto_ora": -1.0,
    "szin_jo_hatter":       "C6EFCE", "szin_jo_szoveg":       "276221",
    "szin_kozep_hatter":    "FFEB9C", "szin_kozep_szoveg":    "9C6500",
    "szin_rossz_hatter":    "FFCCCC", "szin_rossz_szoveg":    "9C0006",
    "szin_fejlec_hatter":   "1F3864", "szin_fejlec_szoveg":   "FFFFFF",
    "szin_subfejlec_hatter":"2E75B6", "szin_subfejlec_szoveg":"FFFFFF",
    "szin_alternalo_sor":   "EBF3FB",
    "szin_osszesito_hatter":"FFF2CC", "szin_osszesito_szoveg":"7D6608",
}

# =============================================================================
# EXCEL STÍLUS SEGÉDEK
# =============================================================================

def _fill(c):   return PatternFill("solid", fgColor=c)
def _font(size=10, bold=False, color="000000"):
    return Font(name="Arial", size=size, bold=bold, color=color)
def _border():
    s = Side(style="thin", color="BDD7EE")
    return Border(left=s, right=s, top=s, bottom=s)
def _center(): return Alignment(horizontal="center", vertical="center")
def _left():   return Alignment(horizontal="left",   vertical="center")

def _safe_float(v, default=0.0):
    try: return float(v or 0)
    except: return default

def _to_dt(v):
    if v is None: return None
    if isinstance(v, datetime): return v
    try: return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S")
    except: return None

def _s2h(secs): return round(float(secs or 0) / 3600, 3)
def _ts(t):
    if isinstance(t, datetime): return t.strftime("%Y-%m-%d %H:%M")
    return str(t) if t else "—"

# =============================================================================
# USERS PROGRESS EXCEL
# =============================================================================

def _fetch_users_data(cursor, mode: str, start_dt: datetime, end_dt: datetime) -> dict:
    s, e = start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        SELECT w.name AS user, wo.WO, wo.PN, ww.start_time, ww.end_time,
               ww.QTY AS completed_qty,
               TIMESTAMPDIFF(SECOND,GREATEST(ww.start_time,%s),LEAST(ww.end_time,%s)) AS effective_time
        FROM workers w
        LEFT JOIN workstationworkorder ww ON w.ID = ww.worker_id
        LEFT JOIN workorders wo ON ww.work_id = wo.ID
        WHERE ww.status = 'Completed' AND ww.start_time BETWEEN %s AND %s
    """, (s, e, s, e))
    rows = cursor.fetchall() or []

    needed_pns = tuple({str(r["PN"]) for r in rows if r.get("PN")})
    if needed_pns:
        ph = ",".join(["%s"] * len(needed_pns))
        cursor.execute(
            f"SELECT wo.PN, wo.QTY, td.PROD AS PROD_TIME "
            f"FROM workorders wo LEFT JOIN t_dump td ON wo.PN=td.`PART.NBR` WHERE wo.PN IN ({ph})",
            needed_pns)
    else:
        cursor.execute("SELECT NULL AS PN,NULL AS QTY,NULL AS PROD_TIME WHERE 1=0")
    pn_map = {r["PN"]: {"QTY": _safe_float(r["QTY"]), "PROD_TIME": _safe_float(r["PROD_TIME"])}
              for r in cursor.fetchall()}

    users_data: dict = {}
    is_day = (mode == "day")
    for row in rows:
        user = row["user"]; pn = row["PN"]
        st = _to_dt(row["start_time"]); et = _to_dt(row["end_time"])
        qty   = pn_map.get(pn, {}).get("QTY", 0.0)
        prod  = pn_map.get(pn, {}).get("PROD_TIME", 0.0)
        cqty  = _safe_float(row["completed_qty"])
        exp   = qty * prod * 3600
        elap  = (et - st).total_seconds() if et and st else 0.0
        diff  = exp - elap
        if is_day:
            entry = [row["WO"], pn, st, et, _safe_float(row.get("effective_time")),
                     qty, cqty, prod, _s2h(exp), _s2h(elap), _s2h(diff)]
        else:
            entry = [row["WO"], pn, st, et, qty, cqty, prod, _s2h(exp), _s2h(elap), _s2h(diff)]
        users_data.setdefault(user, []).append(entry)
    return users_data


def _build_users_wb(users_data: dict, date_label: str, mode: str) -> openpyxl.Workbook:
    cfg   = EXPORT_CONFIG
    is_day = (mode == "day")
    if is_day:
        i_qty,i_cqty,i_elvart,i_eltelt,i_kulonbs = 5,6,8,9,10
    else:
        i_qty,i_cqty,i_elvart,i_eltelt,i_kulonbs = 4,5,7,8,9

    def _diff_szin(v):
        if v >= 0: return cfg["szin_jo_hatter"],    cfg["szin_jo_szoveg"]
        if v >= cfg["kulonbseg_figyelmezteto_ora"]:
            return cfg["szin_kozep_hatter"], cfg["szin_kozep_szoveg"]
        return cfg["szin_rossz_hatter"], cfg["szin_rossz_szoveg"]

    wb     = openpyxl.Workbook()
    ws_sum = wb.active; ws_sum.title = "Összesítő"
    ws_sum.sheet_view.showGridLines = False; ws_sum.freeze_panes = "A5"

    ws_sum.merge_cells("A1:F1")
    ws_sum["A1"] = f"Teljesítmény összesítő  –  {date_label}"
    ws_sum["A1"].font = _font(14,True,cfg["szin_fejlec_szoveg"])
    ws_sum["A1"].fill = _fill(cfg["szin_fejlec_hatter"]); ws_sum["A1"].alignment = _center()
    ws_sum.row_dimensions[1].height = 30
    ws_sum.merge_cells("A2:F2")
    ws_sum["A2"] = f"Generálva: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws_sum["A2"].font = Font(name="Arial",size=9,italic=True,color="888888"); ws_sum["A2"].alignment = _center()
    ws_sum.row_dimensions[3].height = 5

    for ci,(h,w) in enumerate(zip(
        ["Dolgozó","WO db","Elvárt idő (ó)","Eltelt idő (ó)","Különbség (ó)","Lap"],
        [26,8,16,16,16,10]),1):
        c = ws_sum.cell(4,ci,h)
        c.font=_font(10,True,cfg["szin_subfejlec_szoveg"]); c.fill=_fill(cfg["szin_subfejlec_hatter"])
        c.alignment=_center(); c.border=_border()
        ws_sum.column_dimensions[get_column_letter(ci)].width = w
    ws_sum.row_dimensions[4].height = 22

    summary_rows = []
    for worker, rows in users_data.items():
        ws = wb.create_sheet(title=worker[:31])
        ws.sheet_view.showGridLines = False; ws.freeze_panes = "A6"
        ws.merge_cells("A1:I1")
        ws["A1"] = f"{worker}  –  {date_label}"
        ws["A1"].font=_font(13,True,cfg["szin_fejlec_szoveg"]); ws["A1"].fill=_fill(cfg["szin_fejlec_hatter"])
        ws["A1"].alignment=_center(); ws.row_dimensions[1].height = 28

        elvart_ossz  = sum(float(r[i_elvart] or 0) for r in rows)
        eltelt_ossz  = sum(float(r[i_eltelt] or 0) for r in rows)
        kulonbs_ossz = sum(float(r[i_kulonbs] or 0) for r in rows)

        stat_labels = ["WO darab","Elvárt össz. (ó)","Eltelt össz. (ó)","Különbség (ó)"]
        stat_values = [str(len(rows)), f"{elvart_ossz:.2f}", f"{eltelt_ossz:.2f}", f"{kulonbs_ossz:+.2f}"]
        for ci,(lbl,val) in enumerate(zip(stat_labels,stat_values),1):
            lc=ws.cell(2,ci*2-1,lbl); vc=ws.cell(2,ci*2,val)
            lc.font=_font(9,True,cfg["szin_subfejlec_szoveg"]); lc.fill=_fill(cfg["szin_subfejlec_hatter"])
            lc.alignment=Alignment(horizontal="right",vertical="center")
            vc.font=_font(10,True); vc.fill=_fill("D6E4F0"); vc.alignment=_center()
            for cell in [lc,vc]: cell.border=_border()
        ws.row_dimensions[2].height=20; ws.row_dimensions[3].height=4

        if is_day:
            col_defs = [("WO szám",10,0),("Part Number",18,1),("Kezdés",18,2),("Befejezés",18,3),
                        ("Rendelési db",13,5),("Elvégzett db",13,6),
                        ("Elvárt idő (ó)",15,8),("Eltelt idő (ó)",15,9),("Különbség (ó)",15,10)]
        else:
            col_defs = [("WO szám",10,0),("Part Number",18,1),("Kezdés",18,2),("Befejezés",18,3),
                        ("Rendelési db",13,4),("Elvégzett db",13,5),
                        ("Elvárt idő (ó)",15,7),("Eltelt idő (ó)",15,8),("Különbség (ó)",15,9)]

        ws.row_dimensions[5].height=22
        for ci,(hdr,width,_) in enumerate(col_defs,1):
            c=ws.cell(5,ci,hdr); c.font=_font(10,True,cfg["szin_fejlec_szoveg"])
            c.fill=_fill(cfg["szin_fejlec_hatter"]); c.alignment=_center(); c.border=_border()
            ws.column_dimensions[get_column_letter(ci)].width=width

        data_indices=[cd[2] for cd in col_defs]
        elvart_ci  = data_indices.index(i_elvart) +1
        eltelt_ci  = data_indices.index(i_eltelt) +1
        kulonbs_ci = data_indices.index(i_kulonbs)+1
        qty_ci     = data_indices.index(i_qty)    +1
        cqty_ci    = data_indices.index(i_cqty)   +1

        for ri,row_data in enumerate(rows):
            er=ri+6; ws.row_dimensions[er].height=18
            bg=cfg["szin_alternalo_sor"] if ri%2==0 else "FFFFFF"
            d_bg,d_fg=_diff_szin(float(row_data[i_kulonbs] or 0))
            for ci,(_,_,didx) in enumerate(col_defs,1):
                c=ws.cell(er,ci); c.border=_border()
                if ci==kulonbs_ci:
                    c.value=row_data[didx]; c.number_format="+0.00;-0.00;0.00"
                    c.font=_font(10,True,d_fg); c.fill=_fill(d_bg); c.alignment=_center()
                elif didx in (2,3):
                    v=row_data[didx]
                    c.value=v.strftime("%Y-%m-%d %H:%M") if isinstance(v,datetime) else (v or "")
                    c.font=_font(9); c.fill=_fill(bg); c.alignment=_center()
                elif ci in (elvart_ci,eltelt_ci):
                    c.value=row_data[didx]; c.number_format="0.00"
                    c.font=_font(10); c.fill=_fill(bg); c.alignment=_center()
                else:
                    c.value=row_data[didx]; c.font=_font(10); c.fill=_fill(bg)
                    c.alignment=_center() if ci>1 else _left()

        tr=len(rows)+6; ws.row_dimensions[tr].height=20
        ws.cell(tr,1,"ÖSSZESEN").font=_font(10,True,cfg["szin_osszesito_szoveg"])
        ws.cell(tr,1).fill=_fill(cfg["szin_osszesito_hatter"]); ws.cell(tr,1).alignment=_center(); ws.cell(tr,1).border=_border()
        for ci in range(2,len(col_defs)+1):
            c=ws.cell(tr,ci); col_l=get_column_letter(ci)
            if ci in (qty_ci,cqty_ci): c.value=f"=SUM({col_l}6:{col_l}{tr-1})"; c.number_format="#,##0"
            elif ci in (elvart_ci,eltelt_ci): c.value=f"=SUM({col_l}6:{col_l}{tr-1})"; c.number_format="0.00"
            elif ci==kulonbs_ci: c.value=f"=SUM({col_l}6:{col_l}{tr-1})"; c.number_format="+0.00;-0.00;0.00"
            c.font=_font(10,True,cfg["szin_osszesito_szoveg"]); c.fill=_fill(cfg["szin_osszesito_hatter"])
            c.alignment=_center(); c.border=_border()

        summary_rows.append({"worker":worker,"wo_db":len(rows),
                              "elvart":elvart_ossz,"eltelt":eltelt_ossz,
                              "kulonbs":kulonbs_ossz,"sheet":worker[:31]})

    for ri,row in enumerate(summary_rows,5):
        bg=cfg["szin_alternalo_sor"] if ri%2==0 else "FFFFFF"
        d_bg,d_fg=_diff_szin(row["kulonbs"])
        ws_sum.row_dimensions[ri].height=18
        for ci,(val,fmt) in enumerate(zip(
            [row["worker"],row["wo_db"],round(row["elvart"],2),round(row["eltelt"],2),round(row["kulonbs"],2),row["sheet"]],
            [None,"#,##0","0.00","0.00","+0.00;-0.00;0.00",None]),1):
            c=ws_sum.cell(ri,ci,val); c.border=_border()
            if fmt: c.number_format=fmt
            c.font=_font(10,True,d_fg) if ci==5 else _font(10)
            c.fill=_fill(d_bg) if ci==5 else _fill(bg)
            c.alignment=_left() if ci==1 else _center()

    if summary_rows:
        tsr=len(summary_rows)+5; ws_sum.row_dimensions[tsr].height=22
        ws_sum.cell(tsr,1,"ÖSSZESEN / ÁTLAG").font=_font(10,True,cfg["szin_osszesito_szoveg"])
        ws_sum.cell(tsr,1).fill=_fill(cfg["szin_osszesito_hatter"]); ws_sum.cell(tsr,1).alignment=_center(); ws_sum.cell(tsr,1).border=_border()
        for ci,fmt in [(2,"#,##0"),(3,"0.00"),(4,"0.00"),(5,"+0.00;-0.00;0.00")]:
            cl=get_column_letter(ci); c=ws_sum.cell(tsr,ci)
            c.value=f"=SUM({cl}5:{cl}{tsr-1})"; c.number_format=fmt
            c.font=_font(10,True,cfg["szin_osszesito_szoveg"]); c.fill=_fill(cfg["szin_osszesito_hatter"])
            c.alignment=_center(); c.border=_border()
        ws_sum.cell(tsr,6).fill=_fill(cfg["szin_osszesito_hatter"]); ws_sum.cell(tsr,6).border=_border()

    if "Sheet" in wb.sheetnames and len(wb.sheetnames)>1: wb.remove(wb["Sheet"])
    return wb


# =============================================================================
# WO TIMELINE EXCEL
# =============================================================================

def _fetch_wo_data(cursor, start_str: str, end_str: str) -> dict:
    # 1) Az adott napon aktív WO-k azonosítása
    cursor.execute("""
        SELECT DISTINCT wo.WO
        FROM workstationworkorder wsw
        JOIN workorders wo ON wsw.work_id = wo.id
        WHERE (wsw.start_time >= %s AND wsw.start_time < %s)
           OR (wsw.end_time   >= %s AND wsw.end_time   < %s)
    """, (start_str, end_str, start_str, end_str))

    active_wos = [r["WO"] for r in (cursor.fetchall() or [])]
    if not active_wos:
        return {}

    # 2) Teljes történet lekérése (legelső eseménytől)
    ph = ",".join(["%s"] * len(active_wos))
    cursor.execute(f"""
        SELECT wo.WO, wo.PN,
               COALESCE(wk.name,'—') AS worker,
               wsw.process_id AS station, wsw.next_station_id AS next_station,
               wsw.QTY AS qty, wsw.status, wsw.start_time, wsw.end_time,
               TIMESTAMPDIFF(SECOND,wsw.start_time,wsw.end_time) AS elapsed_seconds
        FROM workstationworkorder wsw
        JOIN workorders wo ON wsw.work_id=wo.id
        LEFT JOIN workers wk ON wsw.worker_id=wk.ID
        WHERE wo.WO IN ({ph})
        ORDER BY wo.WO ASC, COALESCE(wsw.start_time,wsw.end_time) ASC, wsw.id ASC
    """, active_wos)

    wo_tl: dict = {}
    for row in (cursor.fetchall() or []):
        wo=row["WO"]; pn=row["PN"]
        if wo not in wo_tl: wo_tl[wo]={"pn":pn,"events":[]}
        st=_to_dt(row.get("start_time")); et=_to_dt(row.get("end_time"))
        elap=_safe_float(row.get("elapsed_seconds"))
        if elap==0 and st and et: elap=max(0.0,(et-st).total_seconds())
        wo_tl[wo]["events"].append({
            "station":     ((row.get("station")      or "").strip().upper() or "—"),
            "worker":       (row.get("worker")        or "—"),
            "start_time":   st, "end_time": et,
            "elapsed_seconds": elap, "qty": row.get("qty") or 0,
            "status":      ((row.get("status")       or "").strip().upper()),
            "next_station":((row.get("next_station") or "").strip().upper() or "—"),
        })

    for data in wo_tl.values():
        evs=data["events"]
        for i,ev in enumerate(evs):
            nxt=evs[i+1] if i<len(evs)-1 else None
            ev["wait_seconds"]=max(0.0,(nxt["start_time"]-ev["end_time"]).total_seconds()) \
                if nxt and ev.get("end_time") and nxt.get("start_time") else 0.0
    return wo_tl


def _build_wo_wb(wo_tl: dict, date_label: str) -> openpyxl.Workbook:
    cfg=EXPORT_CONFIG
    def _wipcheck(s): s=(s or "").strip().upper(); return "WIPRECIEV" in s or "VYPRISIV" in s

    wb=openpyxl.Workbook(); ws_sum=wb.active; ws_sum.title="Összesítő"
    ws_sum.sheet_view.showGridLines=False; ws_sum.freeze_panes="A5"
    ws_sum.merge_cells("A1:G1")
    ws_sum["A1"]=f"WO Időrendi Összesítő  –  {date_label}"
    ws_sum["A1"].font=_font(14,True,cfg["szin_fejlec_szoveg"]); ws_sum["A1"].fill=_fill(cfg["szin_fejlec_hatter"])
    ws_sum["A1"].alignment=_center(); ws_sum.row_dimensions[1].height=30
    ws_sum.merge_cells("A2:G2")
    ws_sum["A2"]=f"Generálva: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws_sum["A2"].font=Font(name="Arial",size=9,italic=True,color="888888"); ws_sum["A2"].alignment=_center()
    ws_sum.row_dimensions[3].height=5
    for ci,(h,w) in enumerate(zip(
        ["WO","PN","Első megjelenés","WIPReceive idő","Átfutás (óra)","Lépések","Összes db"],
        [14,22,20,20,15,10,10]),1):
        c=ws_sum.cell(4,ci,h); c.font=_font(10,True,cfg["szin_subfejlec_szoveg"])
        c.fill=_fill(cfg["szin_subfejlec_hatter"]); c.alignment=_center(); c.border=_border()
        ws_sum.column_dimensions[get_column_letter(ci)].width=w
    ws_sum.row_dimensions[4].height=22

    ws_det=wb.create_sheet("Részletes útvonal"); ws_det.sheet_view.showGridLines=False; ws_det.freeze_panes="A3"
    ws_det.merge_cells("A1:J1")
    ws_det["A1"]=f"WO Részletes Útvonal  –  {date_label}"
    ws_det["A1"].font=_font(13,True,cfg["szin_fejlec_szoveg"]); ws_det["A1"].fill=_fill(cfg["szin_fejlec_hatter"])
    ws_det["A1"].alignment=_center(); ws_det.row_dimensions[1].height=26
    for ci,(h,w) in enumerate(zip(
        ["WO","PN","Állomás","Dolgozó","Kezdés","Befejezés","Töltött idő (ó)","Várakozás (ó)","Következő állomás","Státusz"],
        [14,22,12,24,18,18,16,16,18,12]),1):
        c=ws_det.cell(2,ci,h); c.font=_font(10,True,cfg["szin_subfejlec_szoveg"])
        c.fill=_fill(cfg["szin_subfejlec_hatter"]); c.alignment=_center(); c.border=_border()
        ws_det.column_dimensions[get_column_letter(ci)].width=w
    ws_det.row_dimensions[2].height=22

    det_row=3; sum_row=5; bg_cycle=["EBF3FB","FFFFFF"]
    for idx,(wo,data) in enumerate(sorted(wo_tl.items())):
        pn=data.get("pn") or ""; events=data.get("events") or []
        if not events: continue
        wo_bg=bg_cycle[idx%2]
        first_t=events[0].get("start_time"); wip_t=None
        for ev in reversed(events):
            if _wipcheck(ev.get("next_station")) or _wipcheck(ev.get("station")):
                wip_t=ev.get("end_time") or ev.get("start_time"); break
        if first_t and wip_t: thru=round((wip_t-first_t).total_seconds()/3600,2)
        else:
            last_t=events[-1].get("end_time") or events[-1].get("start_time")
            thru=round((last_t-first_t).total_seconds()/3600,2) if first_t and last_t else 0.0
        total_qty=max((ev.get("qty") or 0 for ev in events),default=0)

        s_bg=cfg["szin_alternalo_sor"] if sum_row%2==0 else "FFFFFF"
        ws_sum.row_dimensions[sum_row].height=18
        for ci,(val,fmt) in enumerate(zip([wo,pn,_ts(first_t),_ts(wip_t),thru,len(events),total_qty],
                                          [None,None,None,None,"0.00","#,##0","#,##0"]),1):
            c=ws_sum.cell(sum_row,ci,val); c.font=_font(10); c.fill=_fill(s_bg)
            c.alignment=_left() if ci<=2 else _center(); c.border=_border()
            if fmt: c.number_format=fmt
        sum_row+=1

        ws_det.merge_cells(f"A{det_row}:J{det_row}")
        gc=ws_det.cell(det_row,1,f"  WO: {wo}   |   PN: {pn}   |   {len(events)} esemény   |   Átfutás: {thru:.2f} ó")
        gc.font=_font(10,True,cfg["szin_fejlec_szoveg"]); gc.fill=_fill(cfg["szin_subfejlec_hatter"])
        gc.alignment=_left(); gc.border=_border(); ws_det.row_dimensions[det_row].height=20; det_row+=1

        for ev in events:
            ws_det.row_dimensions[det_row].height=17; st=ev.get("status") or ""
            rb=(cfg["szin_jo_hatter"] if "COMPLETED" in st else cfg["szin_kozep_hatter"] if "ACTIVE" in st else wo_bg)
            for ci,(val,fmt) in enumerate(zip(
                [wo,pn,ev.get("station") or "—",ev.get("worker") or "—",
                 _ts(ev.get("start_time")),_ts(ev.get("end_time")),
                 _s2h(ev.get("elapsed_seconds")),_s2h(ev.get("wait_seconds")),
                 ev.get("next_station") or "—",st],
                [None,None,None,None,None,None,"0.000","0.000",None,None]),1):
                c=ws_det.cell(det_row,ci,val); c.font=_font(9); c.fill=_fill(rb)
                c.alignment=_left() if ci<=4 else _center(); c.border=_border()
                if fmt: c.number_format=fmt
            det_row+=1
        det_row+=1

    if "Sheet" in wb.sheetnames and len(wb.sheetnames)>1: wb.remove(wb["Sheet"])
    return wb


# =============================================================================
# EMAIL KÜLDÉS (MELLÉKLETTEL)
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


def _send_with_attachments(
    to_addresses: list[str],
    cc_addresses: list[str],
    subject: str,
    body_html: str,
    attachments: list[tuple[str, bytes]],
) -> None:
    cfg = _smtp_cfg()
    from_addr = (f"{cfg['from_name']} <{cfg['from_email']}>"
                 if cfg["from_name"] and cfg["from_email"] else cfg["from_email"] or cfg["username"])

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addresses)
    if cc_addresses:
        msg["Cc"] = ", ".join(cc_addresses)
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    for fname, data in attachments:
        part = MIMEApplication(data, Name=fname)
        part["Content-Disposition"] = f'attachment; filename="{fname}"'
        msg.attach(part)

    all_rec = list(to_addresses) + list(cc_addresses or [])
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as srv:
        srv.ehlo(); srv.starttls(); srv.ehlo()
        srv.login(cfg["username"], cfg["password"])
        srv.sendmail(cfg["from_email"] or cfg["username"], all_rec, msg.as_string())


# =============================================================================
# FOGADÓK FELOLDÁSA A DB-BŐL
# =============================================================================

def _resolve_schedule_recipients(cursor, schedule_id: int) -> tuple[list[str], list[str]]:
    cursor.execute(
        "SELECT recipient_type, recipient_id, recipient_role "
        "FROM email_report_schedule_recipients WHERE schedule_id=%s", (schedule_id,))
    rows = cursor.fetchall() or []
    to_set: set = set(); cc_set: set = set()
    for row in rows:
        rtype = row.get("recipient_type"); rid = row.get("recipient_id")
        role  = row.get("recipient_role") or "to"
        if rtype == "user":
            cursor.execute("SELECT email FROM email_users WHERE id=%s AND active=1 LIMIT 1", (rid,))
            r = cursor.fetchone()
            if r:
                em = r.get("email") or ""; (to_set if role=="to" else cc_set).add(em.strip()) if em else None
        elif rtype == "group":
            cursor.execute(
                "SELECT u.email FROM email_users u JOIN email_group_members m ON m.user_id=u.id "
                "WHERE m.group_id=%s AND u.active=1", (rid,))
            for gr in (cursor.fetchall() or []):
                em = gr.get("email") or ""; (to_set if role=="to" else cc_set).add(em.strip()) if em else None
    return sorted(to_set), sorted(cc_set)


# =============================================================================
# RIPORT FUTTATÁSA
# =============================================================================

REPORT_TYPE_LABELS = {
    "users_day":   "Napi Users Progress",
    "users_month": "Havi Users Progress",
    "wo_day":      "Napi WO Timeline",
    "wo_month":    "Havi WO Timeline",
    "both_day":    "Napi Users Progress + WO Timeline",
    "both_month":  "Havi Users Progress + WO Timeline",
}


def _run_schedule(app, schedule_id: int) -> None:
    """Egy ütemezés futtatása. Háttérszálból hívódik."""
    with app.app_context():
        from services.bt_db import get_db  # a projekt saját DB kapcsolója
        conn = None; cursor = None
        status = "ok"; error_msg = None

        try:
            conn   = get_db()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT id,name,report_type,frequency,subject_template,body_template "
                "FROM email_report_schedules WHERE id=%s AND active=1 LIMIT 1",
                (schedule_id,))
            sched = cursor.fetchone()
            if not sched:
                log.warning(f"[sched {schedule_id}] nem található vagy inaktív")
                return

            rtype   = sched["report_type"]
            name    = sched["name"]
            mode    = "month" if "month" in rtype else "day"
            now     = datetime.now()

            # Időtartomány
            if mode == "day":
                # Hétvégén (szombat=5, vasárnap=6) ne fusson
                if now.weekday() in (5, 6):
                    log.info(f"[sched {schedule_id}] '{name}' — hétvége, kihagyva.")
                    return

                # Hétfőn (weekday=0) a péntek adatait küldjük (3 nappal korábbi)
                days_back = 3 if now.weekday() == 0 else 1
                ref      = now - timedelta(days=days_back)
                start_dt = ref.replace(hour=0,  minute=0,  second=0,  microsecond=0)
                end_dt   = ref.replace(hour=23, minute=59, second=59, microsecond=0)
                label    = ref.strftime("%Y-%m-%d")
                log.info(f"[sched {schedule_id}] napi mód, ref nap: {label} ({days_back} nappal korábbi)")
            else:
                ref      = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
                start_dt = ref
                end_dt   = (ref.replace(day=28) + timedelta(days=4)).replace(day=1)
                label    = ref.strftime("%Y-%m")

            start_s = start_dt.strftime("%Y-%m-%d %H:%M:%S")
            end_s   = end_dt.strftime("%Y-%m-%d %H:%M:%S")

            log.info(f"[sched {schedule_id}] '{name}' fut | {rtype} | {label}")

            attachments: list[tuple[str, bytes]] = []
            report_names: list[str] = []

            # ── Users Progress ─────────────────────────────────────────────
            if "users" in rtype or "both" in rtype:
                users_data = _fetch_users_data(cursor, mode, start_dt, end_dt)
                if users_data:
                    wb = _build_users_wb(users_data, label, mode)
                    buf = BytesIO(); wb.save(buf)
                    fname = f"Users_Progress_{label}.xlsx"
                    attachments.append((fname, buf.getvalue()))
                    report_names.append(f"Users Progress – {label} ({len(users_data)} dolgozó)")
                    log.info(f"[sched {schedule_id}]   users: {len(users_data)} dolgozó")
                else:
                    log.warning(f"[sched {schedule_id}]   users: nincs adat")

            # ── WO Timeline ────────────────────────────────────────────────
            if "wo" in rtype or "both" in rtype:
                wo_tl = _fetch_wo_data(cursor, start_s, end_s)
                if wo_tl:
                    wb = _build_wo_wb(wo_tl, label)
                    buf = BytesIO(); wb.save(buf)
                    fname = f"WO_Timeline_{label}.xlsx"
                    attachments.append((fname, buf.getvalue()))
                    report_names.append(f"WO Timeline – {label} ({len(wo_tl)} WO)")
                    log.info(f"[sched {schedule_id}]   wo: {len(wo_tl)} WO")
                else:
                    log.warning(f"[sched {schedule_id}]   wo: nincs adat")

            if not attachments:
                raise ValueError("Nincs generálható riport (üres adathalmaz)")

            # Fogadók feloldása
            to_addr, cc_addr = _resolve_schedule_recipients(cursor, schedule_id)
            if not to_addr:
                raise ValueError("Nincs aktív fogadó ehhez az ütemezéshez")

            mode_hu  = "Napi" if mode == "day" else "Havi"
            rep_list = "".join(f"<li>{r}</li>" for r in report_names)

            # Sablon változók
            tpl_vars = {
                "mode":       mode_hu,
                "date":       label,
                "name":       name,
                "report_list": rep_list,
                "generated":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            }

            # Tárgy — sablon vagy alapértelmezett
            raw_subject = (sched.get("subject_template") or "").strip()
            subject = _render_tpl(raw_subject, tpl_vars) if raw_subject \
                else f"[Factory] {mode_hu} teljesítmény riport – {label}"

            # Törzs — sablon vagy alapértelmezett HTML
            raw_body = (sched.get("body_template") or "").strip()
            if raw_body:
                body = _render_tpl(raw_body, tpl_vars)
            else:
                body = f"""
<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#333">
  <h2 style="color:#1F3864">{mode_hu} teljesítmény riport  –  {label}</h2>
  <p>Ütemezés neve: <b>{name}</b></p>
  <ul>{rep_list}</ul>
  <hr style="border:none;border-top:1px solid #ddd;margin:20px 0">
  <p style="font-size:11px;color:#888">Automatikusan generálva: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</body></html>"""

            _send_with_attachments(to_addr, cc_addr, subject, body, attachments)
            log.info(f"[sched {schedule_id}] email elküldve → {to_addr}")

        except Exception as exc:
            status    = "error"
            error_msg = str(exc)
            log.exception(f"[sched {schedule_id}] HIBA: {exc}")
        finally:
            try:
                if cursor:
                    cursor.execute(
                        "UPDATE email_report_schedules SET last_run_at=%s, last_status=%s, last_error=%s WHERE id=%s",
                        (datetime.now(), status, error_msg, schedule_id))
                    conn.commit()
                    cursor.close()
            except Exception as e:
                log.error(f"[sched {schedule_id}] DB update hiba: {e}")


# =============================================================================
# SCHEDULER INTEGRÁCIÓ  —  a meglévő services/scheduler.py példányát használja
# =============================================================================

def _cron_kwargs(row: dict) -> dict:
    freq = row.get("frequency") or "daily"
    h    = int(row.get("hour")   or 15)
    m    = int(row.get("minute") or 30)
    if freq == "daily":
        return {"hour": h, "minute": m}
    elif freq == "weekly":
        dow = int(row.get("day_of_week") or 0)
        return {"day_of_week": dow, "hour": h, "minute": m}
    else:  # monthly
        dom = int(row.get("day_of_month") or 1)
        return {"day": dom, "hour": h, "minute": m}


def _get_scheduler():
    """A meglévő scheduler példányt kéri le a services.scheduler modulból."""
    from services.scheduler import get_scheduler
    return get_scheduler()


def load_schedules_into(scheduler, app) -> None:
    """
    Betölti a DB-ben lévő aktív ütemezéseket a megadott scheduler példányba.
    A services/scheduler.py start_scheduler()-je hívja közvetlenül.
    """
    # Régi report job-ok törlése
    for job in list(scheduler.get_jobs()):
        if job.id.startswith("report_"):
            job.remove()

    from services.bt_db import get_db
    try:
        with app.app_context():
            conn   = get_db()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM email_report_schedules WHERE active=1")
            rows = cursor.fetchall() or []
            cursor.close()
    except Exception as e:
        log.error(f"load_schedules_into DB hiba: {e}")
        return

    for row in rows:
        sid = int(row["id"])
        kw  = _cron_kwargs(row)
        scheduler.add_job(
            func             = _run_schedule,
            trigger          = CronTrigger(**kw),
            args             = [app, sid],
            id               = f"report_{sid}",
            name             = row.get("name") or f"Schedule {sid}",
            replace_existing = True,
            misfire_grace_time = 300,
        )
        log.info(f"  Email riport betöltve: #{sid} '{row.get('name')}' ({row.get('frequency')} {kw})")


def reload_schedules(app) -> None:
    """
    Mentés/törlés után az email_core.py hívja — újratölti a job-okat
    a meglévő futó scheduler példányba.
    """
    scheduler = _get_scheduler()
    if scheduler is None:
        log.warning("reload_schedules: scheduler még nem fut")
        return
    load_schedules_into(scheduler, app)


def run_now(app, schedule_id: int) -> None:
    """Azonnali futtatás (manuális trigger a UI-ból)."""
    import threading
    threading.Thread(target=_run_schedule, args=(app, schedule_id), daemon=True).start()