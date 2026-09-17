# services/workday.py
"""
Közös "munkanap" logika.

Ez a modul egyetlen helyen definiálja, hogy egy adott naphoz MELYIK
munkasorok tartoznak, és hogy mennyi belőlük az aznapi effektív idő.
Korábban ezt három helyen, háromféleképpen számoltuk:

  * /api/assembly_data        -> DATE(start_time) = nap        (csak aznap INDULT)
  * /api/users_progress       -> DATE(start)=nap ÉS DATE(end)=nap, csak Completed
  * /api/users_progress_day   -> start_time BETWEEN nap ÉS nap+1

Emiatt a dashboard "Összeszerelési állapotok" táblája, a "Felhasználói
haladás" statisztika és a generált Excel más-más halmazt mutatott
ugyanarra a napra. Innentől mind a három ezt a modult használja.

Szabály: egy sor akkor tartozik a naphoz, ha a [start_time, end_time)
intervalluma METSZI a napot. Így bekerül a több napon átnyúló és a még
le nem zárt (folyamatban lévő) munka is, a napra vágott idővel.
"""

from datetime import date, datetime, timedelta

# A workstationworkorder.start_time / end_time SZÖVEGKÉNT jön vissza
# ('2026-09-17 07:19:08'), és a "még nincs vége" nem SQL NULL, hanem üres
# sztring vagy a 'null' szó. A régi kód ezt még kézzel kezelte:
#     if (st.upper() == "ACTIVE") or (endt in (None, "", "null")):
# Innentől egy helyen, a to_dt() / _NO_END párossal.
EMPTY_TIME_VALUES = ("", "null", "NULL", "None", "0000-00-00", "0000-00-00 00:00:00")

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d",
)


def to_dt(value):
    """
    datetime | None – akkor is, ha az oszlop szöveget tárol.

    Ez a modul sehol nem feltételezheti, hogy az adatbázis datetime-ot ad:
    egy isinstance(x, datetime) ellenőrzés csendben kidobná az összes sort.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if text in EMPTY_TIME_VALUES:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None

# Az összeszerelési állomások sorrendje/halmaza (dashboard szűrő).
ASSEMBLY_STATIONS = ["EMI", "MTE", "MDI", "QC", "TEST", "SOLD", "MOLD"]

# Ha valaki elfelejt kijelentkezni, eddig az időpontig számoljuk az "összes időt".
# Lefedi a 6:00-14:00 és a 7:00-15:00 műszakot is.
SHIFT_FALLBACK_END = "15:00:00"

# A még le nem zárt (end_time IS NULL) sorokat csak ennyi napra visszamenőleg
# vesszük figyelembe, hogy egy elfelejtve nyitva hagyott rekord ne szennyezze
# be minden későbbi nap statisztikáját.
MAX_OPEN_DAYS = 7

# A folyamatban lévő munka is beleszámít az effektív időbe (napra vágva).
# Enélkül az a dolgozó, aki egész nap egyetlen hosszú WO-n dolgozik, 0%-on áll,
# miközben a dashboard táblája szerint reggel óta dolgozik.
COUNT_ACTIVE_AS_EFFECTIVE = True

COMPLETED_STATUS = "COMPLETED"


def day_window(date_str: str):
    """'YYYY-MM-DD' -> (nap 00:00:00, következő nap 00:00:00)"""
    day_start = datetime.strptime(date_str, "%Y-%m-%d")
    return day_start, day_start + timedelta(days=1)


def shift_fallback_end(day_start: datetime) -> datetime:
    h, m, s = (int(x) for x in SHIFT_FALLBACK_END.split(":"))
    return day_start.replace(hour=h, minute=m, second=s, microsecond=0)


# ── A napot metsző sorok kiválasztása ────────────────────────────────────────
# "Nincs vége": SQL NULL VAGY üres sztring / 'null' (szövegoszlop esetén).
# A CAST(... AS CHAR) miatt ez akkor is helyes, ha az oszlop valódi DATETIME.
_NO_END = (
    "(ww.end_time IS NULL OR CAST(ww.end_time AS CHAR) IN ('', 'null', 'NULL', "
    "'None', '0000-00-00', '0000-00-00 00:00:00'))"
)

# A %s sorrend: day_end, day_start, open_floor
# A határokat sztringként adjuk át: ISO formátumnál a szöveges összehasonlítás
# sorrendje megegyezik az időrendivel, és az index is használható marad.
_DAY_OVERLAP_WHERE = f"""
    ww.start_time IS NOT NULL
    AND CAST(ww.start_time AS CHAR) <> ''
    AND ww.start_time < %s
    AND (
        (NOT {_NO_END} AND ww.end_time >= %s)
        OR ({_NO_END} AND ww.start_time >= %s)
    )
"""

_ROW_SELECT = """
    SELECT
        ww.id                       AS row_id,
        ww.worker_id                AS worker_id,
        COALESCE(w.name, '')        AS felhasznalo,
        ww.work_id                  AS work_id,
        COALESCE(wo.WO, '')         AS WO,
        COALESCE(wo.PN, '')         AS PN,
        ww.start_time               AS start_time,
        ww.end_time                 AS end_time,
        COALESCE(ww.status, '')     AS status,
        ww.process_id               AS current_station,
        COALESCE(ww.next_station_id, '') AS next_station_id,
        COALESCE(ww.QTY, 0)         AS done_qty,
        COALESCE(wo.QTY, 0)         AS total_qty
    FROM workstationworkorder ww
    LEFT JOIN workers    w  ON w.ID  = ww.worker_id
    LEFT JOIN workorders wo ON wo.ID = ww.work_id
"""


def _sql_ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _overlap_params(day_start: datetime, day_end: datetime):
    return [
        _sql_ts(day_end),
        _sql_ts(day_start),
        _sql_ts(day_start - timedelta(days=MAX_OPEN_DAYS)),
    ]


def _where(day_start, day_end, station=None):
    sql = "WHERE " + _DAY_OVERLAP_WHERE
    params = _overlap_params(day_start, day_end)
    if station:
        sql += " AND ww.process_id = %s"
        params.append(station)
    return sql, params


def count_day_rows(cursor, date_str: str, station: str | None = None) -> int:
    """Hány sor tartozik a naphoz – a lapozáshoz kell a valódi összdarabszám."""
    day_start, day_end = day_window(date_str)
    where, params = _where(day_start, day_end, station)
    cursor.execute(
        "SELECT COUNT(*) AS c FROM workstationworkorder ww " + where, tuple(params)
    )
    row = cursor.fetchone() or {}
    return int(row.get("c") or 0)


def fetch_day_rows(
    cursor,
    date_str: str,
    station: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    order: str = "DESC",
) -> list[dict]:
    """
    A napot metsző munkasorok, mindhárom felület közös alapadata.

    Minden sort kiegészítünk a napra VÁGOTT idővel:
      clip_start / clip_end : a sor aznapi szakasza
      eff_seconds           : ennek a hossza másodpercben
      is_completed          : lezárt-e
      counts_as_effective   : beleszámít-e az effektív időbe
    """
    day_start, day_end = day_window(date_str)
    where, params = _where(day_start, day_end, station)

    sql = _ROW_SELECT + where + f" ORDER BY ww.start_time {('ASC' if order.upper() == 'ASC' else 'DESC')}, ww.id DESC"
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        params.extend([int(limit), int(offset)])

    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall() or []
    return [decorate_row(r, day_start, day_end) for r in rows]


def decorate_row(row: dict, day_start=None, day_end=None) -> dict:
    """
    Kiegészíti a nyers sort a származtatott mezőkkel.

    FONTOS: soha nem dob el sort. Ha egy időpont értelmezhetetlen, a sor
    továbbra is megjelenik a táblában, csak 0 másodperccel szerepel a
    statisztikában – egy rossz érték nem tüntethet el munkát a képernyőről.

      start_dt / end_dt      : értelmezett időpontok (vagy None)
      is_completed           : a régi tábla szabálya szerint
      clip_start / clip_end  : a sor aznapi szakasza
      eff_seconds            : ennek a hossza
      counts_as_effective    : beleszámít-e az effektív időbe
    """
    r = dict(row)
    start = to_dt(r.get("start_time"))
    end = to_dt(r.get("end_time"))
    r["start_dt"] = start
    r["end_dt"] = end

    # Ugyanaz a szabály, mint a régi táblában: ACTIVE státusz VAGY hiányzó
    # befejezés -> még fut.
    status = str(r.get("status") or "").strip().upper()
    r["is_completed"] = not (status == "ACTIVE" or end is None)

    if day_start is not None and day_end is not None and start is not None:
        if end is not None:
            effective_end = end
        else:
            # Még fut. A MAI napon "most"-ig számolunk (a túlóra is beleszámít),
            # egy korábbi napon viszont a műszak végéig – egy ott nyitva
            # felejtett sor különben teljes 24 órát írna arra a napra.
            now = datetime.now()
            effective_end = (
                min(now, day_end) if now < day_end
                else min(shift_fallback_end(day_start), day_end)
            )
        clip_start = max(start, day_start)
        clip_end = min(effective_end, day_end)
        eff = (clip_end - clip_start).total_seconds()
        r["clip_start"] = clip_start
        r["clip_end"] = clip_end
        r["eff_seconds"] = int(eff) if eff > 0 else 0
    else:
        r["clip_start"] = None
        r["clip_end"] = None
        r["eff_seconds"] = 0

    r["counts_as_effective"] = (
        r["clip_start"] is not None
        and (r["is_completed"] or COUNT_ACTIVE_AS_EFFECTIVE)
    )
    return r


def as_int(value, default: int = 0) -> int:
    """
    Biztonságos egész konverzió.

    A QTY oszlopok DECIMAL-ként ('53.00') vagy szövegként is érkezhetnek,
    amin a nyers int() ValueError-t dob – és az egész végpontot 500-ba viszi
    egyetlen rossz sor miatt.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(str(value).strip().replace(",", ".")))
    except (TypeError, ValueError):
        return default


def fmt_dt(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    Megjelenítésre szánt időpont – váratlan típustól sem dob kivételt.
    Az üres / 'null' értékből üres cella lesz, nem a "null" szó.
    """
    parsed = to_dt(value)
    if parsed is not None:
        return parsed.strftime(fmt)
    if value is None or str(value).strip() in EMPTY_TIME_VALUES:
        return ""
    return str(value)


def status_detail(row: dict) -> str:
    """A dashboard státusz-pill szövege – a táblában és az exportban ugyanaz."""
    station = str(row.get("current_station") or "").strip()
    nxt = str(row.get("next_station_id") or "").strip()
    if not row.get("is_completed"):
        return f"IN: {station}" if station else "IN: -"
    return f"Completed, sent to {nxt}" if nxt else "Completed"


def qty_text(row: dict) -> str:
    return f"{as_int(row.get('done_qty'))} / {as_int(row.get('total_qty'))}"


def merge_seconds(intervals) -> int:
    """
    Átfedő intervallumok UNIÓJÁNAK hossza másodpercben.

    Ha valaki egyszerre két WO-n dolgozik (a scan szerint párhuzamosan futnak),
    a puszta összeadás több effektív időt ad, mint amennyi a műszak hossza –
    korábban ezt egy néma "cap" takarta el a statisztika oldalon, az Excelben
    viszont nem, ezért tért el a két szám.
    """
    spans = sorted(
        (s, e) for s, e in intervals if s is not None and e is not None and e > s
    )
    total = 0.0
    cur_s = cur_e = None
    for s, e in spans:
        if cur_e is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return int(total)


def fetch_login_seconds(cursor, date_str: str) -> dict:
    """
    Dolgozónkénti "összes idő" (bejelentkezéstől kijelentkezésig).

    worker_id -> {'name': str, 'all_seconds': int, 'assumed_end': bool}
    """
    day_start, day_end = day_window(date_str)
    fallback_end = shift_fallback_end(day_start)

    cursor.execute(
        """
        SELECT w.ID            AS worker_id,
               w.name          AS name,
               MIN(ws.login_date)  AS first_login,
               MAX(ws.logout_date) AS last_logout
        FROM workerworkstation ws
        JOIN workers w ON w.ID = ws.worker_id
        WHERE ws.login_date >= %s AND ws.login_date < %s
        GROUP BY w.ID, w.name
        """,
        (_sql_ts(day_start), _sql_ts(day_end)),
    )

    now = datetime.now()
    out = {}
    for row in cursor.fetchall() or []:
        login = to_dt(row.get("first_login"))
        logout = to_dt(row.get("last_logout"))
        if login is None:
            continue

        assumed = False
        if logout is not None and logout > login:
            end = min(logout, day_end)
        else:
            # Nincs (érvényes) kijelentkezés: műszakvégig számolunk, de a mai
            # napon nem a jövőbe – legfeljebb "most"-ig.
            assumed = True
            end = fallback_end
            if now < end:
                end = max(now, login)

        seconds = int(max((end - login).total_seconds(), 0))
        out[row["worker_id"]] = {
            "name": row.get("name") or "",
            "all_seconds": seconds,
            "first_login": login,
            "last_end": end,
            "assumed_end": assumed,
        }
    return out


def fetch_station_rows(cursor, station: str, limit: int, offset: int = 0) -> list[dict]:
    """
    Dátumszűrés nélküli lista egy állomásra (date=all eset).

    Itt szándékosan NINCS COUNT(*): a teljes előzmény megszámolása a
    workstationworkorder táblán drága, és semmit nem ad hozzá. A hívó eggyel
    több sort kér, mint amennyit megjelenít, és abból tudja, van-e még.
    """
    cursor.execute(
        _ROW_SELECT
        + " WHERE ww.process_id = %s ORDER BY ww.start_time DESC, ww.id DESC LIMIT %s OFFSET %s",
        (station, int(limit), int(offset)),
    )
    return [decorate_row(r) for r in (cursor.fetchall() or [])]


def aggregate_workers(rows: list[dict], logins: dict) -> list[dict]:
    """
    Dolgozónkénti napi összesítés a fetch_day_rows() sorokból.

    Ezt használja a /api/users_progress statisztika; tiszta függvény, hogy
    ugyanaz a számítás tesztelhető legyen adatbázis nélkül is.
    """
    per_worker = {}

    def bucket(worker_id, name):
        b = per_worker.get(worker_id)
        if b is None:
            b = per_worker[worker_id] = {
                "user": name,
                "spans": [],
                "completed_seconds": 0,
                "active_seconds": 0,
                "raw_seconds": 0,
                "rows_completed": 0,
                "rows_active": 0,
                "wo_numbers": set(),
            }
        if not b["user"] and name:
            b["user"] = name
        return b

    for r in rows:
        if not r.get("counts_as_effective"):
            continue
        b = bucket(r.get("worker_id"), r.get("felhasznalo") or "")
        b["spans"].append((r["clip_start"], r["clip_end"]))
        b["raw_seconds"] += r["eff_seconds"]
        if r.get("WO"):
            b["wo_numbers"].add(str(r["WO"]))
        if r["is_completed"]:
            b["completed_seconds"] += r["eff_seconds"]
            b["rows_completed"] += 1
        else:
            b["active_seconds"] += r["eff_seconds"]
            b["rows_active"] += 1

    # Aki bejelentkezett, de aznap nem volt munkasora, az is látszódjon.
    for worker_id, info in logins.items():
        bucket(worker_id, info.get("name") or "")

    out = []
    for worker_id, b in per_worker.items():
        login_info = logins.get(worker_id) or {}
        all_time = int(login_info.get("all_seconds") or 0)

        # Átfedés = mennyivel ad többet a sorok puszta összege az uniónál.
        # (Még a bejelentkezési ablakra vágás ELŐTT, hogy csak a valódi
        # párhuzamos munkát mutassa, ne a levágás veszteségét.)
        overlap = max(b["raw_seconds"] - merge_seconds(b["spans"]), 0)

        # A munkaszakaszokat a bejelentkezési ablakra is levágjuk: egy előző
        # napról nyitva felejtett sor különben egész napnyi munkát hozna.
        spans = b["spans"]
        win_start, win_end = login_info.get("first_login"), login_info.get("last_end")
        if win_start and win_end:
            spans = [
                (max(sp, win_start), min(ep, win_end))
                for sp, ep in spans
                if min(ep, win_end) > max(sp, win_start)
            ]

        # Átfedő (párhuzamosan futó) WO-k uniója – nem egyszerű összeg.
        effective = merge_seconds(spans)

        # Nincs login rekord (pl. műszakon átnyúló munka): a mért munka legyen
        # egyben az "összes idő" is, hogy ne 0%-ot mutassunk.
        if all_time <= 0:
            all_time = effective

        capped = effective > all_time
        if capped:
            effective = all_time

        out.append({
            "worker_id": worker_id,
            "user": b["user"],
            "effective_seconds": effective,
            "all_seconds": all_time,
            "loss_seconds": max(all_time - effective, 0),
            "completed_seconds": b["completed_seconds"],
            "active_seconds": b["active_seconds"],
            "overlap_seconds": overlap,
            "rows_completed": b["rows_completed"],
            "rows_active": b["rows_active"],
            "wo_distinct": len(b["wo_numbers"]),
            "efficiency_pct": round(effective / all_time * 100, 1) if all_time > 0 else 0.0,
            "no_login_record": not bool(login_info),
            "assumed_logout": bool(login_info.get("assumed_end")),
            "capped": capped,
        })

    out.sort(key=lambda x: (x["efficiency_pct"], x["effective_seconds"]), reverse=True)
    return out
