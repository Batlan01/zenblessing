"""
A 2026-09-16-i valos nap ujraszamolasa a Felhasznaloi haladas modelljevel.

    osszes ido = amig be volt jelentkezve
    effektiv   = ebbol az, amikor volt aktiv munkarendelese
    veszteseg  = ebbol az, amikor nem volt

Az adatok a kepernyokeprol es a generalt Excelbol szarmaznak.
"""
import os, sys, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from services import workday as W

NOW = dt.datetime(2026, 9, 16, 14, 30)          # rogzitett "most"
DAY_START, DAY_END = dt.datetime(2026, 9, 16), dt.datetime(2026, 9, 17)

def D(t):
    h, m, s = (int(x) for x in t.split(":"))
    return dt.datetime(2026, 9, 16, h, m, s)

def work(wid, name, start, end, wo, status="Completed"):
    return W.decorate_row(dict(
        worker_id=wid, felhasznalo=name, WO=wo, PN="x", status=status,
        start_time=D(start).strftime("%Y-%m-%d %H:%M:%S"),
        end_time=D(end).strftime("%Y-%m-%d %H:%M:%S") if end else "",
        current_station="EMI", next_station_id="TEST", done_qty=0, total_qty=0,
    ), DAY_START, DAY_END, now=NOW)

def sess(a, b):
    end = D(b) if b else NOW
    return {"login": D(a), "logout": D(b) if b else None, "clip_end": end,
            "seconds": int((end - D(a)).total_seconds()),
            "assumed": b is None, "device": "RPi"}

# Az Excel soraibol (serial datumokbol visszaszamolva) + a kepernyokep IN soraibol
rows = [
    # Csontosová Eva – a 2. es 3. sor ATFED (07:12–12:29 vs 07:32–11:16)
    work(1, "Csontosová Eva", "06:13:06", "06:30:37", "261275"),
    work(1, "Csontosová Eva", "07:12:17", "12:29:23", "261273"),
    work(1, "Csontosová Eva", "07:32:54", "11:16:18", "261125"),
    # Durcovicova Henrieta – 6 egymast koveto WO
    work(2, "Durcovicova Henrieta", "07:39:32", "09:30:24", "261543"),
    work(2, "Durcovicova Henrieta", "09:30:56", "10:48:32", "261543"),
    work(2, "Durcovicova Henrieta", "10:53:31", "11:55:53", "261543"),
    work(2, "Durcovicova Henrieta", "11:57:10", "13:35:07", "261543"),
    work(2, "Durcovicova Henrieta", "13:35:20", "13:48:48", "261543"),
    work(2, "Durcovicova Henrieta", "13:55:03", "14:02:37", "261543"),
    # Tímea Gőczeová – 2 lezart + 1 MEG FUTO
    work(3, "Tímea Gőczeová", "06:33:14", "07:31:33", "261570"),
    work(3, "Tímea Gőczeová", "09:12:49", "12:35:28", "261474"),
    work(3, "Tímea Gőczeová", "12:49:00", None, "261574", status="ACTIVE"),
    # Daridova Renáta – KIZAROLAG futo munka (a regi oldal 0%-ot mutatott ra)
    work(4, "Daridova Renáta", "07:50:00", None, "261495", status="ACTIVE"),
    work(4, "Daridova Renáta", "12:26:00", None, "261272", status="ACTIVE"),
]

sessions = {
    1: {"name": "Csontosová Eva",       "sessions": [sess("05:58:00", "14:59:30")]},
    2: {"name": "Durcovicova Henrieta", "sessions": [sess("06:29:06", "13:59:30")]},
    3: {"name": "Tímea Gőczeová",       "sessions": [sess("06:30:16", None)]},
    4: {"name": "Daridova Renáta",      "sessions": [sess("06:01:48", None)]},
}
for info in sessions.values():
    info["spans"] = W.merge_spans([(x["login"], x["clip_end"]) for x in info["sessions"]])

hm = lambda s: f"{int(s)//3600}h {int(s)%3600//60:02d}m"

print(f"{'dolgozó':22s} {'összes':>8s} {'effektív':>9s} {'veszteség':>10s} {'%':>7s}  megjegyzés")
print("-" * 80)
for a in W.aggregate_workers(rows, sessions):
    notes = []
    if a["overlap_seconds"] > 60: notes.append(f"átfedés {hm(a['overlap_seconds'])}")
    if a["rows_active"]:          notes.append(f"{a['rows_active']} futó WO")
    if a["outside_seconds"] > 60: notes.append(f"kívül {hm(a['outside_seconds'])}")
    print(f"{a['user']:22s} {hm(a['all_seconds']):>8s} {hm(a['effective_seconds']):>9s} "
          f"{hm(a['loss_seconds']):>10s} {a['efficiency_pct']:6.1f}%  {', '.join(notes)}")

    # Az egesz oldal ezen az egy egyenloségen all
    assert a["effective_seconds"] + a["loss_seconds"] == a["all_seconds"], \
        f"{a['user']}: effektív + veszteség != összes"

got = {a["user"]: a for a in W.aggregate_workers(rows, sessions)}
assert got["Daridova Renáta"]["efficiency_pct"] > 0, "a futó munka nem lehet 0%"
assert got["Csontosová Eva"]["overlap_seconds"] > 3600, "az átfedést fel kell ismerni"
assert got["Durcovicova Henrieta"]["wo_distinct"] == 1, "6 sor, 1 WO szám"

print("\nOK")
