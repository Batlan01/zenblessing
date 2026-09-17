"""
A "Felhasznaloi haladas" szamainak definicioja:

    osszes ido = amig be volt jelentkezve
    effektiv   = ebbol az, amikor volt aktiv munkarendelese
    veszteseg  = ebbol az, amikor nem volt

Az invarians: effektiv + veszteseg == osszes, MINDIG.
"""
import os, sys, datetime as dt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from services import workday as W

DAY = "2026-09-17"
# Rogzitett "most", hogy a teszt ne az oraallastol fuggjon
NOW = dt.datetime(2026, 9, 17, 13, 0, 0)
D = lambda hh, mm=0, ss=0: dt.datetime(2026, 9, 17, hh, mm, ss)
H = lambda sec: f"{sec/3600:.2f}ó"

def eq(got, want, what):
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {what}: {got}{'' if ok else f'   (várt: {want})'}")
    if not ok: sys.exit(1)

def work(wid, name, start, end, wo="1", status="Completed"):
    r = W.decorate_row(dict(
        worker_id=wid, felhasznalo=name, WO=wo, PN="P", status=status,
        start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
        end_time=end.strftime("%Y-%m-%d %H:%M:%S") if end else "",
        current_station="EMI", next_station_id="TEST", done_qty=1, total_qty=1,
    ), D(0), dt.datetime(2026, 9, 18), now=NOW)
    return r

def session(login, logout, assumed=False):
    end = logout or D(15)
    return {"login": login, "logout": logout, "clip_end": end,
            "seconds": int((end - login).total_seconds()),
            "assumed": assumed, "device": "RPi-1"}

def sessions_for(wid, name, spans):
    return {wid: {"name": name, "sessions": [session(a, b) for a, b in spans],
                  "spans": W.merge_spans([(a, b or D(15)) for a, b in spans])}}


print("── 1) Egy muszak, ket munkablokk kozotti allasidovel ──────")
rows = [work(1, "Eva", D(6, 30), D(8, 0)), work(1, "Eva", D(8, 15), D(12, 0))]
res = W.aggregate_workers(rows, sessions_for(1, "Eva", [(D(6), D(14, 30))]))[0]
print(f"   összes {H(res['all_seconds'])} = effektív {H(res['effective_seconds'])}"
      f" + veszteség {H(res['loss_seconds'])}")
eq(res["all_seconds"], int(8.5 * 3600), "összes idő (06:00–14:30)")
eq(res["effective_seconds"], int(5.25 * 3600), "effektív (1,5ó + 3,75ó)")
eq(res["loss_seconds"], int(3.25 * 3600), "veszteség")
eq(res["effective_seconds"] + res["loss_seconds"], res["all_seconds"], "INVARIÁNS")
eq(res["efficiency_pct"], 61.8, "hatékonyság")

print("\n── 2) Ebedszunetre kijelentkezik (ket munkamenet) ─────────")
# munka 10:30-12:00, de 11:00-11:30 kozott NINCS bejelentkezve
rows = [work(2, "Renáta", D(10, 30), D(12, 0))]
res = W.aggregate_workers(rows, sessions_for(2, "Renáta", [(D(6), D(11)), (D(11, 30), D(14, 30))]))[0]
print(f"   összes {H(res['all_seconds'])} = effektív {H(res['effective_seconds'])}"
      f" + veszteség {H(res['loss_seconds'])}   (kívül: {H(res['outside_seconds'])})")
eq(res["all_seconds"], int(8 * 3600), "összes idő (5ó + 3ó, a szünet nem számít)")
eq(res["effective_seconds"], int(1 * 3600), "effektív (0,5ó + 0,5ó)")
eq(res["outside_seconds"], int(0.5 * 3600), "a kijelentkezett fél óra munka külön")
eq(res["effective_seconds"] + res["loss_seconds"], res["all_seconds"], "INVARIÁNS")

print("\n── 3) Parhuzamos WO-k: az atfedes egyszer szamit ──────────")
rows = [work(3, "Tímea", D(7, 0), D(12, 0), wo="A"), work(3, "Tímea", D(8, 0), D(11, 0), wo="B")]
res = W.aggregate_workers(rows, sessions_for(3, "Tímea", [(D(6), D(14))]))[0]
print(f"   effektív {H(res['effective_seconds'])}, átfedés {H(res['overlap_seconds'])}")
eq(res["effective_seconds"], int(5 * 3600), "07:00–12:00 uniója, nem 5+3=8 óra")
eq(res["overlap_seconds"], int(3 * 3600), "a felismert átfedés")
eq(res["effective_seconds"] + res["loss_seconds"], res["all_seconds"], "INVARIÁNS")

print("\n── 4) Egesz nap egyetlen FUTO WO ─────────────────────────")
rows = [work(4, "Dávid", D(7, 0), None, status="ACTIVE")]
res = W.aggregate_workers(rows, sessions_for(4, "Dávid", [(D(6, 30), D(14, 30))]))[0]
print(f"   összes {H(res['all_seconds'])}, effektív {H(res['effective_seconds'])}, "
      f"futó WO: {res['rows_active']}")
eq(res["rows_active"], 1, "folyamatban lévő munka")
eq(res["effective_seconds"], int(6 * 3600), "07:00-tól 'most'-ig (13:00)")
eq(res["effective_seconds"] + res["loss_seconds"], res["all_seconds"], "INVARIÁNS")

print("\n── 5) Bejelentkezve, de semmi munka ──────────────────────")
res = W.aggregate_workers([], sessions_for(5, "Senki", [(D(6), D(14))]))[0]
eq(res["all_seconds"], int(8 * 3600), "összes idő")
eq(res["effective_seconds"], 0, "effektív")
eq(res["loss_seconds"], int(8 * 3600), "a teljes idő veszteség")
eq(res["efficiency_pct"], 0.0, "0%")

print("\n── 6) Reszletek a felulethez ─────────────────────────────")
rows = [work(6, "Eva", D(6, 30), D(8, 0)), work(6, "Eva", D(9, 0), D(10, 0))]
res = W.aggregate_workers(rows, sessions_for(6, "Eva", [(D(6), D(11))]))[0]
print("   munkamenetek:", [(str(x['login'])[11:16], str(x['logout'])[11:16]) for x in res['sessions']])
print("   állásidők:   ", [(str(a)[11:16], str(b)[11:16]) for a, b in res['loss_spans']])
eq(len(res["sessions"]), 1, "1 bejelentkezés")
eq([(str(a)[11:16], str(b)[11:16]) for a, b in res["loss_spans"]],
   [("06:00", "06:30"), ("08:00", "09:00"), ("10:00", "11:00")], "állásidő-szakaszok")

print("\nMINDEN TESZT OK")
