"""A 2026-09-16-i valos adatok (kepernyokep + Excel) ujraszamolasa."""
import os, sys, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from services import workday as W

def D(t):  # "HH:MM:SS" -> datetime 2026-09-16
    h, m, s = (int(x) for x in t.split(":"))
    return dt.datetime(2026, 9, 16, h, m, s)

def mk(rid, wid, name, start, end, wo, completed=True):
    r = dict(row_id=rid, worker_id=wid, felhasznalo=name, work_id=rid, WO=wo, PN="x",
             start_time=D(start), end_time=D(end) if end else None,
             status="Completed" if completed else "ACTIVE",
             current_station="EMI", next_station_id="TEST", done_qty=0, total_qty=0)
    r["clip_start"] = r["start_time"]
    r["clip_end"] = r["end_time"] or D("12:47:00")
    r["eff_seconds"] = int((r["clip_end"] - r["clip_start"]).total_seconds())
    r["is_completed"] = completed
    r["counts_as_effective"] = True
    return r

# Az Excel sorai (serial datumokbol visszaszamolva) + a kepernyokep IN soraival
rows = [
    # Csontosová Eva – a 2. és 3. sor ATFED (07:12–12:29 vs 07:32–11:16)
    mk(1, 1, "Csontosová Eva", "06:13:06", "06:30:37", "261275"),
    mk(2, 1, "Csontosová Eva", "07:12:17", "12:29:23", "261273"),
    mk(3, 1, "Csontosová Eva", "07:32:54", "11:16:18", "261125"),
    # Durcovicova Henrieta – 6 egymast koveto WO
    mk(4, 2, "Durcovicova Henrieta", "07:39:32", "09:30:24", "261543"),
    mk(5, 2, "Durcovicova Henrieta", "09:30:56", "10:48:32", "261543"),
    mk(6, 2, "Durcovicova Henrieta", "10:53:31", "11:55:53", "261543"),
    mk(7, 2, "Durcovicova Henrieta", "11:57:10", "13:35:07", "261543"),
    mk(8, 2, "Durcovicova Henrieta", "13:35:20", "13:48:48", "261543"),
    mk(9, 2, "Durcovicova Henrieta", "13:55:03", "14:02:37", "261543"),
    # Tímea Gőczeová – 2 lezart + 1 MEG FUTO (12:49 ota)
    mk(10, 3, "Tímea Gőczeová", "06:33:14", "07:31:33", "261570"),
    mk(11, 3, "Tímea Gőczeová", "09:12:49", "12:35:28", "261474"),
    mk(12, 3, "Tímea Gőczeová", "12:49:00", None, "261574", completed=False),
    # Daridova Renáta – KIZAROLAG futo munka (a regi oldal 0%-ot mutatott ra)
    mk(13, 4, "Daridova Renáta", "07:50:00", None, "261495", completed=False),
    mk(14, 4, "Daridova Renáta", "12:26:00", None, "261272", completed=False),
]

logins = {
    1: {"name": "Csontosová Eva",       "all_seconds": int(9*3600+1*60+30),  "first_login": D("05:58:00"), "last_end": D("14:59:30"), "assumed_end": False},
    2: {"name": "Durcovicova Henrieta", "all_seconds": int(7*3600+30*60+24), "first_login": D("06:29:06"), "last_end": D("13:59:30"), "assumed_end": False},
    3: {"name": "Tímea Gőczeová",       "all_seconds": int(8*3600+29*60+44), "first_login": D("06:30:16"), "last_end": D("15:00:00"), "assumed_end": True},
    4: {"name": "Daridova Renáta",      "all_seconds": int(8*3600+58*60+12), "first_login": D("06:01:48"), "last_end": D("15:00:00"), "assumed_end": True},
}

def hm(sec): return f"{int(sec)//3600}h {int(sec)%3600//60:02d}m"

# A REGI logika: csak lezart sorok, egyszeru osszeadas, majd nemа cap
print(f"{'dolgozó':22s} {'RÉGI eff':>10s} {'ÚJ eff':>10s} {'összes':>9s} {'RÉGI %':>7s} {'ÚJ %':>6s}  megjegyzés")
print("-" * 92)
for a in W.aggregate_workers(rows, logins):
    wid = a["worker_id"]
    old_eff = sum(r["eff_seconds"] for r in rows if r["worker_id"] == wid and r["is_completed"])
    all_t = logins[wid]["all_seconds"]
    old_eff_capped = min(old_eff, all_t)
    old_pct = round(old_eff_capped / all_t * 100, 1) if all_t else 0.0
    notes = []
    if a["overlap_seconds"] > 60: notes.append(f"átfedés {hm(a['overlap_seconds'])}")
    if a["rows_active"]:          notes.append(f"{a['rows_active']} futó WO")
    if a["capped"]:               notes.append("levágva")
    print(f"{a['user']:22s} {hm(old_eff_capped):>10s} {hm(a['effective_seconds']):>10s} "
          f"{hm(all_t):>9s} {old_pct:6.1f}% {a['efficiency_pct']:5.1f}%  {', '.join(notes)}")

got = {a["user"]: a for a in W.aggregate_workers(rows, logins)}
assert got["Daridova Renáta"]["efficiency_pct"] > 0, "a futó munka ne legyen 0%"
assert got["Csontosová Eva"]["overlap_seconds"] > 3600, "az átfedést fel kell ismerni"
assert not got["Csontosová Eva"]["capped"], "az unió után már nincs szükség levágásra"
assert got["Durcovicova Henrieta"]["wo_distinct"] == 1, "6 sor, 1 WO szám"
print("\nOK")
