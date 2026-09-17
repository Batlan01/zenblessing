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
    now: datetime | None = None,
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
    return [decorate_row(r, day_start, day_end, now=now) for r in rows]


def decorate_row(row: dict, day_start=None, day_end=None, now=None) -> dict:
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
            # A `now` paraméterezhető, hogy a tesztek ne az óraállástól függjenek.
            ref_now = now or datetime.now()
            effective_end = (
                min(ref_now, day_end) if ref_now < day_end
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


# ══════════════════════════════════════════════════════════════════════════
#  Intervallum-algebra
#
#  A napi számok ezen állnak vagy buknak. A definíció:
#      összes idő = amíg be volt jelentkezve
#      effektív   = ebből az, amikor volt aktív munkarendelése
#      veszteség  = ebből az, amikor NEM volt
#  Vagyis effektív + veszteség = összes, pontosan. Nem két külön mérés,
#  amit utólag egymáshoz kell igazítani.
# ══════════════════════════════════════════════════════════════════════════

def merge_spans(spans):
    """Átfedő/érintkező intervallumok uniója, rendezve."""
    clean = sorted(
        (s, e) for s, e in spans
        if s is not None and e is not None and e > s
    )
    out = []
    for s, e in clean:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def spans_seconds(spans) -> int:
    return int(sum((e - s).total_seconds() for s, e in spans))


def intersect_spans(a, b):
    """a ∩ b – mindkettő rendezett, nem átfedő listát vár (merge_spans után)."""
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if end > start:
            out.append((start, end))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def subtract_spans(a, b):
    """a \ b – az a azon részei, amiket b nem fed le."""
    out = []
    for start, end in a:
        cur = start
        for bs, be in b:
            if be <= cur or bs >= end:
                continue
            if bs > cur:
                out.append((cur, bs))
            cur = max(cur, be)
            if cur >= end:
                break
        if cur < end:
            out.append((cur, end))
    return out


def merge_seconds(intervals) -> int:
    """Átfedő intervallumok uniójának hossza másodpercben."""
    return spans_seconds(merge_spans(intervals))


# "Nincs kijelentkezés" – ugyanaz a szövegoszlop-probléma, mint az end_time-nál.
# A kódbázis máshol is így keresi az aktív loginokat (services/tables_core.py).
_NO_LOGOUT = (
    "(ws.LOGOUT_DATE IS NULL OR CAST(ws.LOGOUT_DATE AS CHAR) IN "
    "('', 'null', 'NULL', 'None', '0000-00-00', '0000-00-00 00:00:00'))"
)


def fetch_login_sessions(cursor, date_str: str, now: datetime | None = None) -> dict:
    """
    Dolgozónkénti BE/KIJELENTKEZÉSEK, tételesen.

    Korábban csak egy MIN(login)..MAX(logout) ablakot számoltunk, ami két
    műszak vagy egy ebédszünetnyi kijelentkezés esetén hazudott. Itt minden
    munkamenet külön szerepel, a napra vágva.

    worker_id -> {
        'name': str,
        'sessions': [{'login','logout','seconds','assumed','device'}],
        'spans': [(datetime, datetime)],     # a napra vágott bejelentkezések
    }
    """
    day_start, day_end = day_window(date_str)
    fallback_end = shift_fallback_end(day_start)
    now = now or datetime.now()

    cursor.execute(
        f"""
        SELECT ws.WORKER_ID       AS worker_id,
               w.name             AS name,
               ws.RASPBERRY_DEVICE AS device,
               ws.LOGIN_DATE      AS login_date,
               ws.LOGOUT_DATE     AS logout_date
        FROM workerworkstation ws
        JOIN workers w ON w.ID = ws.WORKER_ID
        WHERE ws.LOGIN_DATE IS NOT NULL
          AND CAST(ws.LOGIN_DATE AS CHAR) <> ''
          AND ws.LOGIN_DATE < %s
          AND (
              (NOT {_NO_LOGOUT} AND ws.LOGOUT_DATE >= %s)
              OR ({_NO_LOGOUT} AND ws.LOGIN_DATE >= %s)
          )
        ORDER BY ws.LOGIN_DATE
        """,
        tuple(_overlap_params(day_start, day_end)),
    )

    out = {}
    for row in cursor.fetchall() or []:
        login = to_dt(row.get("login_date"))
        if login is None:
            continue
        logout = to_dt(row.get("logout_date"))

        assumed = logout is None
        if assumed:
            # Nincs kijelentkezés: ma "most"-ig, korábbi napon a műszak végéig.
            logout = min(now, day_end) if now < day_end else min(fallback_end, day_end)

        clip_start = max(login, day_start)
        clip_end = min(logout, day_end)
        if clip_end <= clip_start:
            continue

        info = out.setdefault(row["worker_id"], {
            "name": row.get("name") or "",
            "sessions": [],
            "spans": [],
        })
        info["sessions"].append({
            "login": clip_start,
            "logout": None if assumed else clip_end,
            "clip_end": clip_end,
            "seconds": int((clip_end - clip_start).total_seconds()),
            "assumed": assumed,
            "device": row.get("device") or "",
        })
        info["spans"].append((clip_start, clip_end))

    for info in out.values():
        info["spans"] = merge_spans(info["spans"])
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


def aggregate_workers(rows: list[dict], sessions: dict) -> list[dict]:
    """
    Dolgozónkénti napi összesítés.

    A definíció – ezen múlik, hogy a számok értelmezhetők-e:

        összes idő = amíg be volt jelentkezve            (login munkamenetek uniója)
        effektív   = ebből az, amikor volt aktív WO-ja   (login ∩ munka)
        veszteség  = ebből az, amikor nem volt            (login \\ munka)

    Így effektív + veszteség = összes, mindig. Nincs többé néma "cap", és nem
    két, egymástól független mérés, ami nem jön ki egymással.

    Tiszta függvény: adatbázis nélkül tesztelhető.
    """
    by_worker = {}

    def bucket(worker_id, name):
        b = by_worker.get(worker_id)
        if b is None:
            b = by_worker[worker_id] = {
                "user": name, "rows": [], "work_spans_raw": [],
                "rows_completed": 0, "rows_active": 0, "wo_numbers": set(),
            }
        if not b["user"] and name:
            b["user"] = name
        return b

    for r in rows:
        b = bucket(r.get("worker_id"), r.get("felhasznalo") or "")
        b["rows"].append(r)
        if not r.get("counts_as_effective"):
            continue
        b["work_spans_raw"].append((r["clip_start"], r["clip_end"]))
        if r.get("WO"):
            b["wo_numbers"].add(str(r["WO"]))
        if r["is_completed"]:
            b["rows_completed"] += 1
        else:
            b["rows_active"] += 1

    # Aki bejelentkezett, de aznap nem volt munkasora, az is látszódjon.
    for worker_id, info in sessions.items():
        bucket(worker_id, info.get("name") or "")

    out = []
    for worker_id, b in by_worker.items():
        info = sessions.get(worker_id) or {}
        login_spans = list(info.get("spans") or [])
        work_spans = merge_spans(b["work_spans_raw"])

        # Párhuzamosan futó WO-k: ennyivel ad többet a puszta összeg az uniónál.
        overlap = max(
            int(sum((e - s2).total_seconds() for s2, e in b["work_spans_raw"]))
            - spans_seconds(work_spans),
            0,
        )

        no_login_record = not login_spans
        if no_login_record and work_spans:
            # Nincs bejelentkezési rekord (pl. műszakon átnyúló munka): a mért
            # munka legyen egyben a "bejelentkezett" idő is, különben 0%-ot
            # mutatnánk arra, aki bizonyíthatóan dolgozott.
            login_spans = work_spans

        eff_spans = intersect_spans(login_spans, work_spans)
        loss_spans = subtract_spans(login_spans, work_spans)
        outside_spans = subtract_spans(work_spans, login_spans)

        all_seconds = spans_seconds(login_spans)
        effective = spans_seconds(eff_spans)
        loss = spans_seconds(loss_spans)

        out.append({
            "worker_id": worker_id,
            "user": b["user"],
            "all_seconds": all_seconds,
            "effective_seconds": effective,
            "loss_seconds": loss,
            # Munka a bejelentkezett időn KÍVÜL: nem tüntetjük el, de a
            # hatékonyságba sem számít bele.
            "outside_seconds": spans_seconds(outside_spans),
            "overlap_seconds": overlap,
            "rows_completed": b["rows_completed"],
            "rows_active": b["rows_active"],
            "wo_distinct": len(b["wo_numbers"]),
            "efficiency_pct": round(effective / all_seconds * 100, 1) if all_seconds else 0.0,
            "no_login_record": no_login_record,
            "assumed_logout": any(x.get("assumed") for x in (info.get("sessions") or [])),
            "sessions": info.get("sessions") or [],
            "login_spans": login_spans,
            "eff_spans": eff_spans,
            "loss_spans": loss_spans,
            "work_rows": b["rows"],
        })

    out.sort(key=lambda x: (x["efficiency_pct"], x["effective_seconds"]), reverse=True)
    return out
