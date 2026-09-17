import os, sys, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from services import workday as W

D = lambda h, m, day=16: dt.datetime(2026, 9, day, h, m)

class FakeCursor:
    """Nagyon egyszerű álcursor: visszaadja a sorokat, és eltárolja a paramétereket."""
    def __init__(self, rows, count=None):
        self.rows, self.count = rows, count
        self.last_sql = self.last_params = None
    def execute(self, sql, params=()):
        self.last_sql, self.last_params = sql, params
        self._is_count = "COUNT(*)" in sql
    def fetchall(self):
        return [] if self._is_count else self.rows
    def fetchone(self):
        return {"c": self.count} if self._is_count else (self.rows[0] if self.rows else None)

def row(rid, wid, name, start, end, status, station="EMI", nxt="TEST", wo="1", pn="P", done=0, tot=0):
    return dict(row_id=rid, worker_id=wid, felhasznalo=name, work_id=rid, WO=wo, PN=pn,
                start_time=start, end_time=end, status=status, current_station=station,
                next_station_id=nxt, done_qty=done, total_qty=tot)

print("── 1) Átfedő (párhuzamos) WO-k ─────────────────────────────")
# Csontosová Eva esete: 07:12–12:29 és 07:32–11:16 ÁTFEDNEK
rows = [
    row(1, 10, "Eva", D(6,13), D(6,30), "Completed"),
    row(2, 10, "Eva", D(7,12), D(12,29), "Completed"),
    row(3, 10, "Eva", D(7,32), D(11,16), "Completed"),
]
cur = FakeCursor(rows)
got = W.fetch_day_rows(cur, "2026-09-16")
naiv = sum(r["eff_seconds"] for r in got)
unio = W.merge_seconds([(r["clip_start"], r["clip_end"]) for r in got])
print(f"   naiv összeg : {naiv/3600:.2f} ó   <- ezt írta az Excel")
print(f"   unió        : {unio/3600:.2f} ó   <- ennyi időt töltött valójában munkával")
assert unio < naiv and unio == (D(6,30)-D(6,13)).seconds + (D(12,29)-D(7,12)).seconds

print("── 2) Előző napról átnyúló + még futó munka ────────────────")
rows = [
    row(4, 20, "Renáta", D(15,0,15), None, "ACTIVE"),          # tegnap 15:00 óta fut
    row(5, 21, "Tímea",  D(22,0,15), D(2,30), "Completed"),    # éjszakai, átnyúló
]
cur = FakeCursor(rows)
got = W.fetch_day_rows(cur, "2026-09-16")
for r in got:
    print(f"   {r['felhasznalo']:8s} {str(r['clip_start'])[11:16]}–{str(r['clip_end'])[11:16]} "
          f"= {r['eff_seconds']/3600:5.2f} ó  completed={r['is_completed']}")
assert len(got) == 2, "az átnyúló munkának is látszania kell"
# Egy korabbi napon nyitva felejtett sor nem irhat 24 orat arra a napra
assert got[0]["eff_seconds"] <= 15 * 3600, "nyitott sor a muszak vegeig szamol"
assert got[1]["clip_start"] == D(0,0) and got[1]["clip_end"] == D(2,30)

print("── 3) A lekérdezés paraméterei ─────────────────────────────")
cur = FakeCursor([], count=42)
print("   count =", W.count_day_rows(cur, "2026-09-17", station="EMI"))
print("   params =", cur.last_params)
# A hatarokat sztringkent adjuk at: az idopont-oszlopok szoveget tarolnak,
# ISO formatumnal a szoveges osszehasonlitas sorrendje = idorend.
assert cur.last_params[0] == "2026-09-18 00:00:00"   # day_end (kizarolagos)
assert cur.last_params[1] == "2026-09-17 00:00:00"   # day_start
assert cur.last_params[2] == "2026-09-10 00:00:00"   # nyitott sorok also hatara
assert cur.last_params[3] == "EMI"

print("── 4) Státusz / QTY szöveg ─────────────────────────────────")
r_done = row(6, 30, "X", D(9,0), D(10,0), "Completed", nxt="TEST", done=5, tot=5)
r_done["is_completed"] = True
r_run = row(7, 30, "X", D(11,0), None, "ACTIVE", done=0, tot=53)
r_run["is_completed"] = False
print("  ", W.status_detail(r_done), "|", W.qty_text(r_done))
print("  ", W.status_detail(r_run), "|", W.qty_text(r_run))
assert W.status_detail(r_done) == "Completed, sent to TEST"
assert W.status_detail(r_run) == "IN: EMI" and W.qty_text(r_run) == "0 / 53"

print("── 5) Be/kijelentkezesek, elfelejtett kijelentkezes ──────")
class LoginCursor(FakeCursor):
    def execute(s2, sql, p=()): s2.last_sql, s2.last_params = sql, p
    def fetchall(s2):
        return [
            {"worker_id": 10, "name": "Eva", "device": "RPi-7",
             "login_date": D(5, 58), "logout_date": D(14, 30)},
            {"worker_id": 20, "name": "Renáta", "device": "RPi-1",
             "login_date": D(6, 0), "logout_date": None},
        ]
# "most" a KOVETKEZO nap -> a lezaratlan munkamenet a muszak vegeig (15:00) szamol
out = W.fetch_login_sessions(LoginCursor([]), "2026-09-16", now=dt.datetime(2026, 9, 17, 10, 0))
for wid, v in out.items():
    total = W.spans_seconds(v["spans"])
    print(f"   {v['name']:8s} {total/3600:5.2f} ó  munkamenet={len(v['sessions'])} "
          f"feltételezett_vég={v['sessions'][0]['assumed']}")
assert abs(W.spans_seconds(out[10]["spans"]) - 8.533 * 3600) < 60
# korabbi nap + nincs kijelentkezes -> a muszak vegeig (15:00), nem ejfelig
assert out[20]["sessions"][0]["assumed"]
assert abs(W.spans_seconds(out[20]["spans"]) - 9 * 3600) < 60

print("── 6) Nyers ertekek biztonsagos kezelese ───────────────────")
# Ezek buktattak korabban 500-ba az egesz vegpontot egyetlen rossz soron
for raw, want in [(5, 5), ("5", 5), ("53.00", 53), (dt.timezone, 0),
                  (None, 0), ("", 0), ("12,5", 12), ("abc", 0)]:
    got = W.as_int(raw)
    print(f"   as_int({raw!r:12}) = {got}")
    assert got == want, (raw, got, want)

print("   fmt_dt(datetime) =", repr(W.fmt_dt(D(9, 5))))
print("   fmt_dt(None)     =", repr(W.fmt_dt(None)))
print("   fmt_dt('szoveg') =", repr(W.fmt_dt("2026-09-17 09:05")))
assert W.fmt_dt(D(9, 5)) == "2026-09-16 09:05:00"
assert W.fmt_dt(None) == "" and W.fmt_dt("") == "" and W.fmt_dt("null") == ""

r = row(1, 1, "X", D(9, 0), None, "ACTIVE", done="0", tot="53.00")
r["is_completed"] = False
print("   qty_text decimal szovegbol:", W.qty_text(r))
assert W.qty_text(r) == "0 / 53"

print("── 7) date=all ag: nincs COUNT(*), +1 sor a has_more-hoz ───")
class StationCursor(FakeCursor):
    def execute(s2, sql, params=()):
        s2.last_sql, s2.last_params = sql, params
        s2._is_count = "COUNT(*)" in sql
        assert not s2._is_count, "a date=all ag NEM szamolhat COUNT(*)-ot"
sc = StationCursor([row(1, 1, "X", D(9, 0), None, "ACTIVE")])
got = W.fetch_station_rows(sc, "EMI", 26, 0)
print("   params:", sc.last_params)
assert sc.last_params == ("EMI", 26, 0)
assert got[0]["is_completed"] is False
assert not hasattr(W, "count_station_rows"), "a dragа COUNT(*) helper torolve"

print("\nMINDEN TESZT OK")
