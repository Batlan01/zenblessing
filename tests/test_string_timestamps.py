"""A DB SZOVEGKENT adja az idopontokat, a 'nincs vege' pedig '' vagy 'null'."""
import os, sys, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from services import workday as W

# A 2026-09-17-i kepernyokep sorai, pontosan abban a formaban, ahogy erkeznek
RAW = [
    (1, "Daridova Renáta",      "261495", "2026-09-17 07:19:08", "",                    "ACTIVE"),
    (2, "Židek Dávid",          "261877", "2026-09-17 07:07:28", "null",                "ACTIVE"),
    (3, "Židek Dávid",          "261616", "2026-09-17 06:55:19", "2026-09-17 07:04:05", "Completed"),
    (4, "Csontosová Eva",       "261310", "2026-09-17 06:45:32", "",                    "ACTIVE"),
    (5, "Csontosová Eva",       "261273", "2026-09-17 06:36:57", "",                    "ACTIVE"),
    (6, "Tímea Gőczeová",       "261608", "2026-09-17 06:32:38", "",                    "ACTIVE"),
    (7, "Daridova Renáta",      "261272", "2026-09-17 06:25:12", "",                    "ACTIVE"),
    # tegnapi, lezart -> ma NEM tartozhat ide
    (8, "Durcovicova Henrieta", "261543", "2026-09-16 14:24:04", "2026-09-16 14:56:49", "Completed"),
]

def mkrow(t):
    rid, name, wo, start, end, status = t
    return dict(row_id=rid, worker_id=rid, felhasznalo=name, work_id=rid, WO=wo, PN="P",
                start_time=start, end_time=end, status=status,
                current_station="EMI", next_station_id="TEST", done_qty="0", total_qty="53")

class Cur:
    """Vegrehajtja a WHERE-t Pythonban, hogy a feltetel logikajat is teszteljuk."""
    def __init__(s): s.rows=[]; s.sql=""; s.p=()
    def execute(s, sql, p=()):
        s.sql, s.p = sql, p
        day_end, day_start, floor = p[0], p[1], p[2]
        def keep(r):
            st, en = r["start_time"], r["end_time"]
            if st is None or st == "": return False
            if not (st < day_end): return False
            no_end = en is None or en in W.EMPTY_TIME_VALUES
            return (en >= day_start) if not no_end else (st >= floor)
        s.rows = [mkrow(t) for t in RAW if keep(mkrow(t))]
    def fetchall(s): return [] if "COUNT(*)" in s.sql else s.rows
    def fetchone(s): return {"c": len(s.rows)}

def eq(got, want, what):
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {what}: {got}{'' if ok else f'   (várt: {want})'}")
    if not ok: sys.exit(1)

print("── A 2026-09-17-i nap a szűrővel ──────────────────────────")
c = Cur()
n = W.count_day_rows(c, "2026-09-17", station="EMI")
rows = W.fetch_day_rows(c, "2026-09-17", station="EMI")
print("   határok:", c.p[:3])
eq(n, 7, "COUNT (a régi kód 1-et adott)")
eq(len(rows), 7, "visszaadott sorok (a régi kód 0-t adott)")
eq(sum(1 for r in rows if not r["is_completed"]), 6, "ebből még futó")

print("\n── Soronként ──────────────────────────────────────────────")
for r in sorted(rows, key=lambda x: x["start_time"], reverse=True):
    print(f"   {r['felhasznalo']:22s} {r['WO']}  {W.fmt_dt(r['start_time'])}  "
          f"{W.fmt_dt(r['end_time']) or '—':19s}  {W.qty_text(r):8s}  {W.status_detail(r)}")

print("\n── Ellenőrzések ───────────────────────────────────────────")
by_wo = {r["WO"]: r for r in rows}
eq("261543" in by_wo, False, "a tegnapi lezárt sor NEM került be")
eq(W.status_detail(by_wo["261495"]), "IN: EMI", "üres vég -> IN")
eq(W.status_detail(by_wo["261877"]), "IN: EMI", "'null' vég -> IN")
eq(W.status_detail(by_wo["261616"]), "Completed, sent to TEST", "valódi vég -> Completed")
eq(W.fmt_dt(by_wo["261495"]["end_time"]), "", "üres vég -> üres cella, nem 'null'")
eq(by_wo["261616"]["eff_seconds"], 8*60+46, "lezárt sor hossza (06:55:19-07:04:05)")
eq(by_wo["261495"]["start_dt"], dt.datetime(2026,9,17,7,19,8), "start_dt értelmezve")
eq(W.qty_text(by_wo["261495"]), "0 / 53", "szöveges QTY")

print("\n── Értelmezhetetlen időpont: a sor NEM tűnhet el ──────────")
bad = W.decorate_row(dict(start_time="ez nem datum", end_time="", status="ACTIVE",
                          current_station="EMI", done_qty=1, total_qty=2),
                     dt.datetime(2026,9,17), dt.datetime(2026,9,18))
eq(bad["eff_seconds"], 0, "0 másodperc")
eq(bad["counts_as_effective"], False, "a statisztikából kimarad")
eq(W.status_detail(bad), "IN: EMI", "de a táblában látszik")

print("\n── Bejelentkezési idők szövegként ─────────────────────────")
class LoginCur(Cur):
    def execute(s, sql, p=()): s.sql, s.p = sql, p
    def fetchall(s): return [
        {"worker_id": 1, "name": "Eva",    "first_login": "2026-09-17 05:58:00", "last_logout": "2026-09-17 14:30:00"},
        {"worker_id": 2, "name": "Renáta", "first_login": "2026-09-17 06:00:00", "last_logout": ""},
    ]
out = W.fetch_login_seconds(LoginCur(), "2026-09-17")
eq(len(out), 2, "mindkét dolgozó megvan (a régi kód 0-t adott)")
eq(round(out[1]["all_seconds"]/3600, 2), 8.53, "Eva összes ideje")
eq(out[2]["assumed_end"], True, "üres kijelentkezés -> becsült")

print("\nMINDEN TESZT OK")
