# services/scan_core.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import platform
import socket
import threading
import time
from datetime import datetime
from typing import Optional

import mysql.connector
import netifaces as ni

from services.dbpool import DB_CFG, get_pool, get_pooled_connection

# Connection pool: kérésenként kapunk/visszaadunk egy kapcsolatot,
# így a párhuzamos kérések (több raspberry egyszerre) nem akadnak össze.
# (A pool a services/dbpool.py-ban van, az egész app közösen használja.)



def get_client_ip(req) -> str:
    """
    Flask request-ből kiszedi a kliens (raspberry) IP-t.
    Proxy mögött is működik (X-Forwarded-For / X-Real-IP).
    """
    ip = req.headers.get("X-Forwarded-For") or req.headers.get("X-Real-IP") or req.remote_addr
    if not ip:
        return "0.0.0.0"
    ip = ip.split(",", 1)[0].strip()
    if ip.startswith("::ffff:"):
        ip = ip.replace("::ffff:", "", 1)
    return ip


# IP -> eszköznév cache: a feloldás (DB + esetleg lassú reverse DNS) minden
# kérésnél lefutna, ezért az eredményt rövid ideig memóriában tartjuk.
_DEVICE_NAME_CACHE: dict[str, tuple[str, float]] = {}
_DEVICE_NAME_TTL_OK = 600.0       # ismert név: 10 perc
_DEVICE_NAME_TTL_UNKNOWN = 300.0  # RPI-UNKNOWN: 5 perc (hátha közben felveszik a táblába)

# Reverse DNS a HÁTTÉRBEN: a socket.gethostbyaddr() a libc resolvert hívja,
# aminek nincs használható timeoutja – ha az eszközhöz nincs PTR rekord, vagy
# a nameserver nem válaszol, ez a hívás MÁSODPERCEKIG blokkol. Mivel minden
# scan-kérés (login, QR, complete) áthalad az eszköz-azonosításon, ez volt a
# bejelentkezés fő késleltetője. Ezért a kérés soha nem várja meg: azonnal
# visszakapja a DB-ből ismert nevet vagy az RPI-UNKNOWN-t, a reverse DNS pedig
# külön szálon fut és utólag frissíti a cache-t.
_RDNS_PENDING: set[str] = set()
_RDNS_LOCK = threading.Lock()


def _rdns_worker(device_ip: str) -> None:
    try:
        host, _, _ = socket.gethostbyaddr(device_ip)
        if host:
            _DEVICE_NAME_CACHE[device_ip] = (str(host), time.monotonic())
    except Exception:
        pass
    finally:
        with _RDNS_LOCK:
            _RDNS_PENDING.discard(device_ip)


def _start_rdns_lookup(device_ip: str) -> None:
    """Reverse DNS indítása háttérszálon (IP-nként egyszerre csak egy)."""
    with _RDNS_LOCK:
        if device_ip in _RDNS_PENDING:
            return
        _RDNS_PENDING.add(device_ip)
    try:
        threading.Thread(
            target=_rdns_worker, args=(device_ip,),
            name=f"rdns-{device_ip}", daemon=True,
        ).start()
    except Exception:
        with _RDNS_LOCK:
            _RDNS_PENDING.discard(device_ip)


def resolve_device_name_by_ip(device_ip: str) -> str:
    """
    IP -> hostname feloldás (cache-elve, NEM blokkoló):
    1) RaspberryDevices táblából (device_id -> device_name)
    2) fallback: reverse DNS – háttérszálon, az eredmény a következő
       kérésnél már a cache-ből jön (a mostani kérést nem lassítja)
    """
    now = time.monotonic()
    hit = _DEVICE_NAME_CACHE.get(device_ip)
    if hit:
        name, ts = hit
        ttl = _DEVICE_NAME_TTL_UNKNOWN if name == "RPI-UNKNOWN" else _DEVICE_NAME_TTL_OK
        if now - ts < ttl:
            return name

    name = "RPI-UNKNOWN"
    try:
        row = db_execute(
            "SELECT device_name FROM RaspberryDevices WHERE device_id=%s LIMIT 1",
            (device_ip,),
            fetchone=True,
        )
        if row and row.get("device_name"):
            name = str(row["device_name"])
    except Exception:
        pass

    _DEVICE_NAME_CACHE[device_ip] = (name, now)

    if name == "RPI-UNKNOWN":
        _start_rdns_lookup(device_ip)

    return name


def get_client_identity(req) -> tuple[str, str]:
    """
    Visszaadja: (device_name, device_ip) a kliens (raspberry) alapján.
    """
    ip = get_client_ip(req)
    name = resolve_device_name_by_ip(ip)
    return name, ip



def ensure_db():
    """
    Visszafelé kompatibilis belépő: bemelegíti a poolt.
    FIGYELEM: már nem ad vissza kapcsolatot – aki SQL-t akar futtatni,
    az a db_execute()-ot használja (az kérésenként kezeli a kapcsolatot).
    """
    get_pool()
    return None


# Megszakadt kapcsolatra utaló MySQL hibakódok – ilyenkor egyszer újrapróbáljuk
_RETRYABLE_ERRNOS = {2006, 2013, 2055}  # server gone / lost connection / broken pipe


def db_execute(
    query: str,
    params: tuple | list = (),
    fetchone: bool = False,
    dictcur: bool = True,
    return_lastrowid: bool = False,
):
    """
    Biztonságos végrehajtó: pool-ból vett kapcsolat, buffered kurzor,
    garantált fetch/close/visszaadás. Megszakadt kapcsolatnál egyszer
    automatikusan újrapróbálja friss kapcsolattal.
    """
    # A commit is egy hálózati oda-vissza a DB felé: csak akkor küldjük el,
    # ha tényleg van nyitott tranzakció (DB_AUTOCOMMIT=1 mellett nincs).
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        conn = None
        cur = None
        try:
            conn = get_pooled_connection()
            cur = conn.cursor(dictionary=dictcur, buffered=True)
            cur.execute(query, params or ())
            if cur.with_rows:
                data = cur.fetchone() if fetchone else cur.fetchall()
            elif return_lastrowid:
                data = cur.lastrowid or 0
            else:
                data = None
            try:
                needs_commit = bool(conn.in_transaction)
            except Exception:
                needs_commit = True
            if needs_commit:
                conn.commit()
            return data
        except mysql.connector.Error as e:
            last_err = e
            if attempt == 1 and getattr(e, "errno", None) in _RETRYABLE_ERRNOS:
                continue
            raise
        finally:
            try:
                if cur is not None:
                    cur.close()
            except Exception:
                pass
            try:
                if conn is not None:
                    conn.close()  # pooled kapcsolatnál ez visszaadás a poolba
            except Exception:
                pass
    raise last_err  # elvileg nem érünk ide


# --- ESZKÖZ AZONOSSÁG ---
def get_device_identity() -> tuple[str, str]:
    dev_name = socket.gethostname()
    if platform.system() == 'Windows':
        dev_ip = socket.gethostbyname(dev_name)
    else:
        try:
            dev_ip = ni.ifaddresses('wlan0')[ni.AF_INET][0]['addr']
        except Exception:
            dev_ip = socket.gethostbyname(dev_name)
    return dev_name, dev_ip


# =======================
#  BILLENTYŰ/SCANNER NORMALIZÁLÁS
# =======================
_ACCENT_STRIP = str.maketrans({
    "á": "a", "ä": "a", "à": "a", "â": "a", "Á": "A", "Ä": "A", "À": "A", "Â": "A",
    "č": "c", "Č": "C", "ç": "c", "Ç": "C",
    "ď": "d", "Ď": "D",
    "é": "e", "ě": "e", "ë": "e", "è": "e", "ê": "e", "É": "E", "Ě": "E", "Ë": "E", "È": "E", "Ê": "E",
    "í": "i", "ï": "i", "ì": "i", "î": "i", "Í": "I", "Ï": "I", "Ì": "I", "Î": "I",
    "ĺ": "l", "ľ": "l", "Ł": "L", "ł": "l", "Ľ": "L", "Ĺ": "L",
    "ň": "n", "ń": "n", "Ñ": "N", "Ň": "N", "Ń": "N",
    "ó": "o", "ô": "o", "ö": "o", "ò": "o", "õ": "o", "Ó": "O", "Ô": "O", "Ö": "O", "Ò": "O", "Õ": "O",
    "ŕ": "r", "ř": "r", "Ř": "R", "Ŕ": "R",
    "š": "s", "ś": "s", "ß": "ss", "Š": "S", "Ś": "S",
    "ť": "t", "Ť": "T",
    "ú": "u", "ů": "u", "ü": "u", "ù": "u", "û": "u", "Ú": "U", "Ů": "U", "Ü": "U", "Ù": "U", "Û": "U",
    "ý": "y", "Ý": "Y",
    "ž": "z", "ź": "z", "Ž": "Z", "Ź": "Z",
})

_SYM_NORMALIZE = {
    "–": "-", "—": "-", "−": "-",
    "／": "/", "∕": "/", "｜": "|",
    "‒": "-", "‐": "-",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "´": "'",
}

_SK_KB_BACKMAP = {
    "ˇ": "`", "§": "'", "÷": "/", "×": "*", "¸": ",", "·": ".",
}


def _strip_accents(s: str) -> str:
    return s.translate(_ACCENT_STRIP)


def _normalize_symbols(s: str) -> str:
    for k, v in _SYM_NORMALIZE.items():
        s = s.replace(k, v)
    for k, v in _SK_KB_BACKMAP.items():
        s = s.replace(k, v)
    return s


def _maybe_fix_w_pipes(s: str) -> str:
    """
    Ha a QR-ben nincs '|' de láthatóan kulcs-érték párok vannak és 'w' választ el,
    akkor csak a szeparátor szerepű 'w'-ket cseréljük '|' karakterre.
    """
    if "|" in s or "w" not in s:
        return s
    if not re.search(r'[A-Z_]+-', s):
        return s
    s2 = re.sub(r'(?<=\w)w(?=[A-Z_]+-)', '|', s)
    return s2


def normalize_scanner_text(raw: str) -> str:
    if not raw:
        return raw
    s = "".join(ch for ch in raw if ord(ch) >= 32 or ch in ("\r", "\n", "\t"))
    s = s.strip()
    s = _strip_accents(s)
    s = _normalize_symbols(s)

    if s.startswith("STATION/"):
        s = s.replace("STATION/", "STATION-", 1)
    if s.startswith("PROCESS/"):
        s = s.replace("PROCESS/", "PROCESS-", 1)

    s = _maybe_fix_w_pipes(s)
    s = re.sub(r"[ \t]+", " ", s)
    return s


# =======================
#  RFID normalizálás
# =======================
_SHIFT_MAP = {
    '+': '1', 'ľ': '2', 'š': '3', 'č': '4', 'ť': '5',
    'ž': '6', 'ý': '7', 'á': '8', 'í': '9', 'é': '0',
    '=': '-', '%': '=', 'Q': 'q', 'W': 'w', 'E': 'e',
    'R': 'r', 'T': 't', 'Z': 'z', 'U': 'u', 'I': 'i',
    'O': 'o', 'P': 'p', 'ú': '[', 'ä': ']', 'ň': '\\',
    'A': 'a', 'S': 's', 'D': 'd', 'F': 'f', 'G': 'g',
    'H': 'h', 'J': 'j', 'K': 'k', 'L': 'l', 'ô': ';',
    '§': "'", 'Y': 'y', 'X': 'x', 'C': 'c', 'V': 'v',
    'B': 'b', 'N': 'n', 'M': 'm', '?': ',', ':': '.',
    '_': '/', 'ˇ': '`', '!': '1', '"': '2', '§': '3',
    '$': '4', '%': '5', '/': '6', '&': '7', '(': '8',
    ')': '9', '=': '0', '_': '-'
}


def convert_to_slovak(text: str) -> str:
    return "".join(_SHIFT_MAP.get(ch, ch) for ch in text)


def compute_target(uid_str: int) -> int:
    # 32 bites decimál → bájtok (big-endian)
    n = int(uid_str)  # fontos: NE használd a lstrip('0')-t
    b0 = (n >> 24) & 0xFF
    b1 = (n >> 16) & 0xFF  # facility
    b2 = (n >> 8) & 0xFF
    b3 = n & 0xFF

    facility = b1
    lower16 = (b2 << 8) | b3

    mapped_fac = facility
    delta = 0

    # facility=34: két altípus a b2 felső bájt alapján
    if facility == 34:
        if b2 >= 0x68:  # pl. 0x68xx (mint a 'niko' kártyánál)
            mapped_fac = 203
            delta = -13395

    return mapped_fac * 100000 + (lower16 + delta)


# =======================
#  Segédek WO-hoz
# =======================
def _is_parent_wid(work_id: int) -> bool:
    r = db_execute("SELECT PN, master_pn FROM Workorders WHERE ID=%s", (work_id,), fetchone=True)
    if not r:
        return False
    return str(r["PN"] or "") == str(r["master_pn"] or "")


def _get_parent_wid(child_wid: int) -> Optional[int]:
    """
    Visszaadja a parent WO ID-ját (ahol PN==master_pn) ugyanarra a WO-ra,
    amelyhez a child (child_wid) tartozik.
    """
    row = db_execute(
        """
        SELECT p.ID
        FROM Workorders AS c
        JOIN Workorders AS p
          ON p.WO = c.WO
         AND p.PN = p.master_pn
        WHERE c.ID = %s
        LIMIT 1
        """,
        (child_wid,),
        fetchone=True
    )
    return int(row["ID"]) if row else None


def _child_counts_for_parent(parent_wid: int) -> tuple[int, int]:
    """
    Visszaadja: (összes SUB darab, abból hány SUB teljesült 'Completed' státusszal ÉS next_station_id ~ VYPRISIV).
    """
    p = db_execute(
        "SELECT WO, PN AS parent_pn FROM Workorders WHERE ID=%s",
        (parent_wid,),
        fetchone=True,
    )
    if not p:
        return 0, 0
    wo, parent_pn = p["WO"], p["parent_pn"]

    total_row = db_execute(
        """
        SELECT COUNT(*) AS cnt
        FROM Workorders
        WHERE WO=%s AND master_pn=%s AND PN<>master_pn
        """,
        (wo, parent_pn),
        fetchone=True,
    )
    total = int(total_row["cnt"]) if total_row else 0
    if total == 0:
        return 0, 0

    done_row = db_execute(
        """
        SELECT COUNT(DISTINCT wsw.work_id) AS done
        FROM WorkstationWorkorder wsw
        JOIN Workorders c ON c.ID = wsw.work_id
        WHERE c.WO=%s
          AND c.master_pn=%s
          AND c.PN<>c.master_pn
          AND wsw.status='Completed'
          AND (
                UPPER(COALESCE(wsw.next_station_id,''))='VYPRISIV'
             OR UPPER(COALESCE(wsw.next_station_id,'')) LIKE '%%VYPRISIV%%'
          )
        """,
        (wo, parent_pn),
        fetchone=True,
    )
    done = int(done_row["done"]) if done_row else 0
    return total, done


def _auto_close_parent_if_ready_vyprisiv(
    child_wid: int,
    worker_id: int,
    raspberry_id: str,
    device_name: str
) -> Optional[str]:
    """
    Ha a parent összes SUB-ja Completed @ VYPRISIV, beszúr egy Completed sort a parentnak is
    (start=end=now, next_station_id='VYPRISIV'). Visszaadja a parent WO számát, ha autó-lezárt.
    """
    parent = _get_parent_wid(child_wid)
    if not parent:
        return None

    total, done = _child_counts_for_parent(parent)
    if total == 0 or done < total:
        return None

    # Már lezárt parent?
    exists = db_execute(
        "SELECT 1 FROM WorkstationWorkorder WHERE work_id=%s AND status='Completed' LIMIT 1",
        (parent,),
        fetchone=True,
    )
    if exists:
        prow = db_execute(
            "SELECT WO FROM Workorders WHERE ID=%s",
            (parent,),
            fetchone=True,
        )
        return prow["WO"] if prow else None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # (opcionális) örököljünk metaadatot az utolsó child-sorból
    meta = db_execute(
        """
        SELECT workstation_id, device_id
        FROM WorkstationWorkorder
        WHERE work_id=%s
        ORDER BY end_time DESC, start_time DESC
        LIMIT 1
        """,
        (child_wid,),
        fetchone=True,
    )
    workstation_id = (meta["workstation_id"] if meta and meta.get("workstation_id") else raspberry_id)
    device_id      = (meta["device_id"]      if meta and meta.get("device_id")      else device_name)

    # Parent auto-complete beszúrás
    db_execute(
        """
        INSERT INTO WorkstationWorkorder
          (workstation_id, device_id, worker_id, work_id, start_time, end_time, status, QTY, next_station_id)
        VALUES (%s,%s,%s,%s,%s,%s,'Completed', NULL, 'VYPRISIV')
        """,
        (workstation_id, device_id, worker_id, parent, now, now),
    )

    prow = db_execute(
        "SELECT WO FROM Workorders WHERE ID=%s",
        (parent,),
        fetchone=True,
    )
    return prow["WO"] if prow else None



# =======================
#  WORKER & LOGIN
# =======================
def fetch_worker_by_rfid(rfid: str):
    return db_execute(
        "SELECT id, name FROM Workers WHERE rfid_tag=%s",
        (rfid,),
        fetchone=True,
    )


def fetch_worker_by_rfid_multi(rfids) -> tuple[Optional[dict], Optional[str]]:
    """
    Több RFID-jelölt (nyers szám + compute_target) keresése EGY lekérdezéssel.
    Korábban jelöltenként külön kérdeztük le a DB-t – a bejelentkezés így
    kétszer fizette meg a hálózati oda-vissza időt.
    Visszaad: (worker sor vagy None, a ténylegesen talált rfid vagy None).
    A jelöltek sorrendje számít: az első találó jelölt nyer.
    """
    cands = [str(r) for r in (rfids or []) if str(r or "").strip()]
    if not cands:
        return None, None

    placeholders = ",".join(["%s"] * len(cands))
    rows = db_execute(
        f"SELECT id, name, rfid_tag FROM Workers WHERE rfid_tag IN ({placeholders})",
        tuple(cands),
    ) or []
    if not rows:
        return None, None

    by_tag = {str(r.get("rfid_tag")): r for r in rows}
    for rid in cands:
        row = by_tag.get(rid)
        if row:
            return row, rid
    return None, None


def fetch_worker_name(worker_id: int) -> str | None:
    row = db_execute(
        "SELECT name FROM Workers WHERE id=%s",
        (worker_id,),
        fetchone=True,
    )
    return row['name'] if row else None


def fetch_worker_last_state(worker_id: int) -> Optional[dict]:
    """
    A dolgozó utolsó tevékenysége a DB-ből, F5 / szerver restart utáni
    visszaállításhoz.
    - Ha van Active sora, azt adjuk vissza (folytatható munka).
    - Különben a legutóbb lezárt (Completed) sort.
    - Ha egyik sincs, None.
    """
    row = db_execute(
        """
        SELECT wsw.work_id, wsw.status, wsw.QTY AS done_qty,
               wsw.process_id, wsw.next_station_id,
               w.WO, w.PN, w.ECN, w.REV, w.QTY AS wo_qty,
               COALESCE(w.HIERARCHY,'') AS HIERARCHY
        FROM WorkstationWorkorder AS wsw
        JOIN Workorders AS w ON w.ID = wsw.work_id
        WHERE wsw.worker_id=%s
          AND wsw.status IN ('Active','Completed')
        ORDER BY (wsw.status='Active') DESC,
                 COALESCE(wsw.end_time, wsw.start_time) DESC,
                 wsw.start_time DESC
        LIMIT 1
        """,
        (worker_id,),
        fetchone=True,
    )
    if not row:
        return None

    active = (str(row["status"]) == "Active")
    cur = str(row.get("process_id") or "").strip()
    nxt = str(row.get("next_station_id") or "").strip()
    hierarchy = row["HIERARCHY"] if (row["HIERARCHY"] or "") != "N/A" else ""

    state = {
        "mode": "resume_active" if active else "resume_completed",
        "work_id": int(row["work_id"]),
        "wo": row["WO"],
        "pn": row["PN"],
        "ecn": row["ECN"],
        "rev": row["REV"],
        "expected_qty": (str(row["wo_qty"]) if row["wo_qty"] is not None else None),
        "entered_qty": (None if active or row["done_qty"] is None else str(row["done_qty"])),
        "current_station": (cur if cur and cur != "N/A" else None),
        "next_station": (nxt if nxt and nxt != "N/A" else None),
        "hierarchy": hierarchy,
    }

    if active:
        state["message"] = (
            f"WO {row['WO']} je stále aktívne – pokračuj v práci, "
            f"alebo naskenuj WO QR na ukončenie."
        )
    else:
        done = state["entered_qty"]
        qty_txt = f" ({done} ks)" if done not in (None, "") else ""
        state["message"] = f"Posledná dokončená práca: WO {row['WO']}{qty_txt}."

    # összes eddig elküldött mennyiség ehhez a WO sorhoz (nem csak az utolsó)
    try:
        state["history"] = fetch_wo_send_history(int(row["work_id"]))
    except Exception:
        state["history"] = {"items": [], "total": 0}

    return state


def worker_has_active_login(worker_id: int, raspberry_ip: str) -> bool:
    r = db_execute(
        "SELECT 1 FROM WorkerWorkstation "
        "WHERE worker_id=%s AND Raspberry_Device=%s AND logout_date IS NULL LIMIT 1",
        (worker_id, raspberry_ip),
        fetchone=True,
    )
    return bool(r)


def create_worker_login_row(worker_id: int, raspberry_ip: str, device_name: str, when: datetime):
    db_execute(
        "INSERT INTO WorkerWorkstation "
        "(Worker_id, Workstation_ID, Raspberry_Device, Login_Date) "
        "VALUES (%s,%s,%s,%s)",
        (worker_id, raspberry_ip, device_name, when.strftime("%Y-%m-%d %H:%M:%S")),
    )


def ensure_worker_login_row(worker_id: int, raspberry_ip: str, device_name: str, when: datetime) -> bool:
    """
    Bejelentkezési sor létrehozása, ha még nincs nyitott ezen az eszközön.
    Egy körben (INSERT ... SELECT ... WHERE NOT EXISTS), hogy a login ne
    fizessen két külön DB oda-vissza időt. Egyben versenyhelyzet-biztos is:
    ugyanaz a kártya kétszer olvasva sem hoz létre két nyitott sort.
    Visszaad: True, ha most jött létre új sor.
    """
    db_execute(
        """
        INSERT INTO WorkerWorkstation
              (Worker_id, Workstation_ID, Raspberry_Device, Login_Date)
        SELECT %s, %s, %s, %s
          FROM DUAL
         WHERE NOT EXISTS (
               SELECT 1 FROM (
                   SELECT 1 FROM WorkerWorkstation
                    WHERE worker_id=%s
                      AND Raspberry_Device=%s
                      AND logout_date IS NULL
                    LIMIT 1
               ) AS existing_login
         )
        """,
        (
            worker_id, raspberry_ip, device_name, when.strftime("%Y-%m-%d %H:%M:%S"),
            worker_id, raspberry_ip,
        ),
    )
    return True


def logout_worker_everywhere(worker_id: int, device_name: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # csak NULL-t vagy zero-datetime-et zárunk le
    db_execute(
        """
        UPDATE WorkerWorkstation
           SET logout_date=%s
         WHERE worker_id=%s
           AND (logout_date IS NULL OR logout_date = '0000-00-00 00:00:00')
        """,
        (now, worker_id),
    )
    # minden aktív WO lezárása
    db_execute(
        "UPDATE WorkstationWorkorder "
        "SET status='Completed', end_time=%s "
        "WHERE worker_id=%s AND status='Active'",
        (now, worker_id),
    )


def auto_logout_open_logins(idle_minutes: int = 15) -> dict:
    """
    Műszakvégi automatikus kijelentkeztetés (ütemezett hívás a schedulerből).
    Minden nyitva maradt bejelentkezést lezár, KIVÉVE azokat a dolgozókat,
    akiknek idle_minutes percen belül volt scan-aktivitásuk (túlórázók védelme,
    az ő aktív WO-jukat nem zárjuk le mennyiség nélkül).
    Visszaad: {"logged_out": [worker_id, ...], "skipped": [worker_id, ...]}
    """
    rows = db_execute(
        """
        SELECT DISTINCT worker_id
        FROM WorkerWorkstation
        WHERE logout_date IS NULL OR logout_date = '0000-00-00 00:00:00'
        """,
    ) or []

    result = {"logged_out": [], "skipped": []}
    now = datetime.now()

    for r in rows:
        try:
            wid = int(r["worker_id"])
        except Exception:
            continue
        try:
            if idle_minutes > 0:
                act = db_execute(
                    """
                    SELECT GREATEST(
                             COALESCE(MAX(start_time), '1970-01-01'),
                             COALESCE(MAX(end_time),   '1970-01-01')
                           ) AS last_ts
                    FROM WorkstationWorkorder
                    WHERE worker_id=%s
                    """,
                    (wid,),
                    fetchone=True,
                )
                last_ts = act.get("last_ts") if act else None
                if last_ts is not None:
                    if isinstance(last_ts, str):
                        try:
                            last_ts = datetime.strptime(last_ts[:19], "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            last_ts = None
                    if last_ts and (now - last_ts).total_seconds() < idle_minutes * 60:
                        result["skipped"].append(wid)
                        continue

            logout_worker_everywhere(wid, "AUTO-LOGOUT")
            result["logged_out"].append(wid)
        except Exception:
            # egy dolgozó hibája ne állítsa meg a többiek kijelentkeztetését
            pass

    return result


# =======================
#  QR FELDOLGOZÁS
# =======================
def _is_active(work_id: int) -> bool:
    r = db_execute(
        "SELECT 1 FROM WorkstationWorkorder WHERE work_id=%s AND status='Active' LIMIT 1",
        (work_id,),
        fetchone=True,
    )
    return bool(r)


def _start_wo(worker_id: int, raspberry_id: str, device_name: str, work_id: int, hierarchy: str) -> dict:
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_execute(
        "INSERT INTO WorkstationWorkorder "
        "(workstation_id, device_id, worker_id, work_id, start_time, status) "
        "VALUES (%s,%s,%s,%s,%s,'Active')",
        (raspberry_id, device_name, worker_id, work_id, start_time),
    )
    row = db_execute(
        "SELECT WO, QTY, ECN, REV FROM Workorders WHERE ID=%s",
        (work_id,),
        fetchone=True,
    )
    wo, qty, ecn, rev = (row['WO'], row['QTY'], row['ECN'], row['REV']) if row else ("?", "?", "?", "?")
    msg = f"WO {wo} started.\n{hierarchy}\nECN: {ecn}\nQTY: {qty}\nREV: {rev}\nScan STATION-QR."
    return {"mode": "started", "work_id": work_id, "wo": wo, "message": msg}


def _complete_prepare(work_id: int, hierarchy: str) -> dict:
    """
    Kézi lezárás előkészítése.
    Itt már NEM tiltjuk a TOP-level WO-kat, ugyanúgy mehet rá a qty kérés,
    mint bármelyik SUB-ra.
    """
    prow = db_execute(
        "SELECT WO, QTY, ECN, REV FROM Workorders WHERE ID=%s",
        (work_id,),
        fetchone=True,
    )

    wo, qty, ecn, rev = ("?", "?", "?", "?")
    if prow:
        wo, qty, ecn, rev = prow["WO"], prow["QTY"], prow["ECN"], prow["REV"]

    msg = (
        f"Preparing to complete WO {wo}.\n"
        f"{hierarchy}\n"
        f"ECN: {ecn}\n"
        f"QTY: {qty}\n"
        f"REV: {rev}\n"
        f"Please enter finished quantity."
    )

    return {
        "mode": "need_qty",
        "work_id": work_id,
        "wo": wo,
        "message": msg,
    }



def _get_active_wid_for_worker_wo(worker_id: int, wo_value: str) -> tuple[Optional[int], str]:
    """
    Megnézi, hogy a workernek fut-e AKTÍV sora ugyanarra a WO-ra.
    Visszaad: (work_id vagy None, hierarchy_txt)
    """
    row = db_execute(
        """
        SELECT wsw.work_id, COALESCE(w.HIERARCHY,'') AS HIERARCHY
        FROM WorkstationWorkorder AS wsw
        JOIN Workorders AS w ON w.ID = wsw.work_id
        WHERE wsw.worker_id=%s
          AND wsw.status='Active'
          AND w.WO=%s
        ORDER BY wsw.start_time DESC
        LIMIT 1
        """,
        (worker_id, wo_value),
        fetchone=True
    )
    if not row:
        return None, ""
    return int(row["work_id"]), (row["HIERARCHY"] or "")


def _find_work_id_by(fields_sql: str, params: tuple) -> int | None:
    row = db_execute(f"SELECT ID FROM Workorders WHERE {fields_sql}", params, fetchone=True)
    return int(row['ID']) if row else None


def _find_unique_work_id_fallback(wo_value: str, pn_value: str = "") -> Optional[int]:
    """
    Utolsó esély a WO azonosítására, ha a HIERARCHY/master_pn alapú keresés
    nem talált semmit (pl. sérült/hiányos QR beolvasás miatt).
    Csak akkor ad vissza ID-t, ha a találat EGYÉRTELMŰ (pontosan 1 sor),
    így rossz WO-t nem indítunk el véletlenül.
    """
    if not wo_value:
        return None
    if pn_value:
        rows = db_execute(
            "SELECT ID FROM Workorders WHERE PN=%s AND WO=%s LIMIT 2",
            (pn_value, wo_value),
        ) or []
        if len(rows) == 1:
            return int(rows[0]["ID"])
        if len(rows) > 1:
            # PN+WO önmagában nem egyértelmű: tipikusan a qr_core által
            # duplán beírt sor az oka (ugyanaz a PN egyszer HIERARCHY nélkül,
            # egyszer HIERARCHY-val). Ha a HIERARCHY-s sorok közt már
            # egyértelmű a találat, azt indítjuk.
            rows = db_execute(
                "SELECT ID FROM Workorders "
                "WHERE PN=%s AND WO=%s AND COALESCE(HIERARCHY,'') NOT IN ('','N/A') "
                "LIMIT 2",
                (pn_value, wo_value),
            ) or []
            if len(rows) == 1:
                return int(rows[0]["ID"])
            return None
    rows = db_execute(
        "SELECT ID FROM Workorders WHERE WO=%s LIMIT 2",
        (wo_value,),
    ) or []
    if len(rows) == 1:
        return int(rows[0]["ID"])
    return None


def fetch_wo_send_history(work_id: int) -> dict:
    """
    Az adott WO sor (work_id) ÖSSZES lezárt (Completed) tétele:
    mennyiség + hova lett továbbküldve + mikor.
    A scan oldal ebből mutatja az eddig elküldött darabszámokat,
    nem csak a legutolsót.
    """
    rows = db_execute(
        """
        SELECT wsw.QTY AS qty,
               COALESCE(NULLIF(wsw.next_station_id,''),'?') AS station,
               wsw.end_time
        FROM WorkstationWorkorder wsw
        WHERE wsw.work_id=%s
          AND wsw.status='Completed'
          AND wsw.QTY IS NOT NULL
        ORDER BY COALESCE(wsw.end_time, wsw.start_time) ASC
        """,
        (work_id,),
    ) or []

    items = []
    total = 0
    for r in rows:
        try:
            q = int(r["qty"])
        except Exception:
            continue
        if q <= 0:
            continue
        total += q
        t = r.get("end_time")
        try:
            t_txt = t.strftime("%d.%m. %H:%M") if t else ""
        except Exception:
            t_txt = str(t or "")
        items.append({"qty": q, "station": str(r.get("station") or "?"), "time": t_txt})
    return {"items": items, "total": total}


def _attach_send_history(payload: dict, work_id) -> dict:
    """History hozzáfűzése a válaszhoz – hiba esetén üres, sose dobjon."""
    try:
        payload["history"] = fetch_wo_send_history(int(work_id))
    except Exception:
        payload["history"] = {"items": [], "total": 0}
    return payload


def _route_to_wid(
    wid: int,
    hierarchy: str,
    worker_id: int,
    raspberry_id: str,
    device_name: str,
    station_hint: str,
) -> dict:
    """
    Közös útvonal, ha a QR-ből megvan a work_id:
    - ha már aktív → lezárás előkészítése (qty kérés),
    - különben indítás + AUTO STATION beállítás.
    Mindkét ág megkapja a küldési előzményeket (history).
    """
    if _is_active(wid):
        return _attach_send_history(_complete_prepare(wid, hierarchy), wid)

    resp = _start_wo(worker_id, raspberry_id, device_name, wid, hierarchy)

    # AUTO STATION beállítás (STATION scan kihagyása)
    auto_station = resolve_station_for_device(raspberry_id, station_hint)
    if auto_station:
        db_execute(
            "UPDATE WorkstationWorkorder SET process_id=%s WHERE work_id=%s AND status='Active'",
            (auto_station, int(resp["work_id"])),
        )
        # UX: már nem STATION-t kérünk, hanem PROCESS-t
        resp["message"] = resp["message"].replace("Scan STATION-QR.", "Scan PROCESS-QR.")
        resp["auto_station"] = auto_station

    return _attach_send_history(resp, wid)


def _extract_hierarchy_values(h: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    pn = re.search(r'PN:\s*(\S+)', h)
    s1 = re.search(r'SUB1:\s*(\S+)', h)
    s2 = re.search(r'SUB2:\s*(\S+)', h)
    return (
        pn.group(1) if pn else None,
        s1.group(1) if s1 else None,
        s2.group(1) if s2 else None,
    )


def parse_qr_and_route(
    text: str,
    worker_id: int,
    raspberry_id: str,   # nálad ez a device IP
    device_name: str,
    station_hint: str = ""
) -> dict:
    # kiosztás-független normalizálás
    text = normalize_scanner_text(text)

    # --- WO ---
    if text.startswith("WO"):
        parts = [p for p in text.split("|") if "-" in p]
        kv: dict[str, str] = {}
        for p in parts:
            key, *rest = p.split("-", 1)
            kv[key] = rest[0] if rest else ""

        wo_value  = (kv.get("WO") or "").strip()
        pn_value  = (kv.get("PN") or "").strip()
        master_pn = (kv.get("MASTER_PN") or "").strip()

        h_key     = next((k for k in kv.keys() if k.startswith("HIERARCHY")), None)
        hierarchy = (kv.get(h_key) or "").strip() if h_key else ""

        if master_pn == pn_value and master_pn:
            pn_value = master_pn  # parent PN a QR-ben

        # --- LEZÁRÁSI ÚJRASZKENNELÉS: ha a dolgozónak MÁR VAN AKTÍV sora
        # erre a WO+PN-re, mindig oda routolunk. A workorders táblában
        # ugyanaz a PN több sorban is szerepelhet (flat/N-A + HIERARCHY-s,
        # ill. több fa-pozíció), és az újraszkennelés e nélkül egy MÁSIK
        # duplikátum-sorra oldódhat fel → lezárás helyett új munkát indítana.
        if wo_value and pn_value:
            arow = db_execute(
                """
                SELECT wsw.work_id, COALESCE(w.HIERARCHY,'') AS HIERARCHY
                FROM WorkstationWorkorder AS wsw
                JOIN Workorders AS w ON w.ID = wsw.work_id
                WHERE wsw.worker_id=%s
                  AND wsw.status='Active'
                  AND w.WO=%s
                  AND w.PN=%s
                ORDER BY wsw.start_time DESC
                LIMIT 1
                """,
                (worker_id, wo_value, pn_value),
                fetchone=True,
            )
            if arow:
                h_active = arow["HIERARCHY"] if (arow["HIERARCHY"] or "") != "N/A" else ""
                return _route_to_wid(
                    int(arow["work_id"]), h_active or hierarchy,
                    worker_id, raspberry_id, device_name, station_hint,
                )

        # --- részletes routing HIERARCHY alapján (ha van)
        if hierarchy:
            pn_h, s1, s2 = _extract_hierarchy_values(hierarchy)

            # ELSŐKÉNT a QR saját PN-je + a teljes HIERARCHY lánc: ez minden
            # mélységben (SUB3, SUB4, ...) a pontos sort találja meg. A régi
            # s1/s2-alapú keresés csak 2 szintig működött, mélyebb QR-eknél
            # a szülő sorára routolt.
            wid = None
            if pn_value:
                wid = _find_work_id_by(
                    "PN=%s AND WO=%s AND HIERARCHY LIKE %s",
                    (pn_value, wo_value, hierarchy),
                )
            if not wid and s2:
                wid = _find_work_id_by("PN=%s AND WO=%s AND HIERARCHY LIKE %s", (s2, wo_value, hierarchy))
            if not wid and s1 and not s2:
                wid = _find_work_id_by("PN=%s AND WO=%s AND HIERARCHY LIKE %s", (s1, wo_value, hierarchy))
            if not wid and pn_value:
                # fallback PN + master_pn
                wid = _find_work_id_by("PN=%s AND WO=%s AND master_pn=%s", (pn_value, wo_value, master_pn))

            if not wid:
                # utolsó esély: egyértelmű PN+WO / WO találat (sérült QR ellen)
                # – a szkennelt sor SAJÁT PN-jével, ne a lánc közbülső elemével
                wid = _find_unique_work_id_fallback(wo_value, pn_value or s2 or s1)

            if wid:
                return _route_to_wid(wid, hierarchy, worker_id, raspberry_id, device_name, station_hint)

            raise RuntimeError(
                f"WO {wo_value or '?'} sa nenašlo podľa HIERARCHY (PN: {pn_value or '?'}). "
                f"Naskenuj QR kód znova, alebo daj vedieť team leaderovi."
            )

        # --- HIERARCHY nélkül: standalone vagy parent
        if pn_value and pn_value == master_pn:
            wid = _find_work_id_by("PN=%s AND WO=%s AND master_pn=%s", (pn_value, wo_value, master_pn))
            if wid:
                return _route_to_wid(wid, hierarchy, worker_id, raspberry_id, device_name, station_hint)

        # utolsó esély: egyértelmű PN+WO / WO találat (sérült/hiányos QR ellen)
        wid = _find_unique_work_id_fallback(wo_value, pn_value)
        if wid:
            return _route_to_wid(wid, hierarchy, worker_id, raspberry_id, device_name, station_hint)

        if not wo_value:
            raise RuntimeError(
                "Neúplné načítanie QR kódu (chýba číslo WO). Naskenuj QR kód znova."
            )
        raise RuntimeError(
            f"WO {wo_value} sa nenašlo v databáze (PN: {pn_value or '?'}). "
            f"Naskenuj QR kód znova, alebo daj vedieť team leaderovi."
        )

    # --- PROCESS ---
    if text.startswith("PROCESS"):
        parts = [p for p in text.split("|") if "-" in p]
        kv: dict[str, str] = {}
        for p in parts:
            key, *rest = p.split("-", 1)
            kv[key] = rest[0] if rest else ""
        process_id = (kv.get("PROCESS") or "").strip()

        row = db_execute(
            "SELECT work_id, process_id FROM WorkstationWorkorder "
            "WHERE worker_id=%s AND status='Active' "
            "ORDER BY start_time DESC LIMIT 1",
            (worker_id,),
            fetchone=True,
        )
        if not row:
            raise RuntimeError("Előbb WO-t kell szkennelni.")
        if not row['process_id'] or row['process_id'] in ("", "N/A"):
            return {"mode": "info", "message": "Előbb STATION-QR szükséges (nincs process_id beállítva)."}

        db_execute(
            "UPDATE WorkstationWorkorder SET next_station_id=%s "
            "WHERE work_id=%s AND status='Active'",
            (process_id, int(row['work_id'])),
        )

        d = db_execute(
            "SELECT WO, PN, HIERARCHY FROM Workorders WHERE ID=%s",
            (int(row['work_id']),),
            fetchone=True,
        )
        htxt = "" if not d or (d['HIERARCHY'] or '') == "N/A" else (d['HIERARCHY'] or '')
        next_station_value = process_id or "N/A"

        return {
            "mode": "info",
            "message": (
                f"WO: {d['WO'] if d else '?'}\n"
                f"PN: {d['PN'] if d else '?'}\n"
                f"{htxt}\n"
                f"Erre az állomásra lett küldve: {next_station_value}\n"
                f"Szkenneld a WO QR kódjat a befejezeshez"
            )
        }


    # --- STATION ---
    if text.startswith("STATION"):
        parts = [p for p in text.split("|") if "-" in p]
        kv: dict[str, str] = {}
        for p in parts:
            key, *rest = p.split("-", 1)
            kv[key] = rest[0] if rest else ""
        station = (kv.get("STATION") or "").strip()

        row = db_execute(
            "SELECT work_id FROM WorkstationWorkorder "
            "WHERE worker_id=%s AND status='Active' "
            "ORDER BY start_time DESC LIMIT 1",
            (worker_id,),
            fetchone=True,
        )
        if not row:
            return {"mode": "info", "message": "Előbb WO-t kell szkennelni."}

        db_execute(
            "UPDATE WorkstationWorkorder SET process_id=%s "
            "WHERE work_id=%s AND status='Active'",
            (station, int(row['work_id'])),
        )
        d = db_execute(
            "SELECT WO, PN, HIERARCHY FROM Workorders WHERE ID=%s",
            (int(row['work_id']),),
            fetchone=True,
        )
        htxt = "" if not d or (d['HIERARCHY'] or '') == "N/A" else (d['HIERARCHY'] or '')
        return {
            "mode": "info",
            "message": f"WO: {d['WO'] if d else '?'}\nPN: {d['PN'] if d else '?'}\n{htxt}\nSent to station {station}. Now scan PROCESS-QR."
        }

    # fallback
    return {"mode": "info", "message": "Ismeretlen QR formátum."}



def _coerce_qty(val) -> int:
    """Laza, de biztonságos QTY koerció. Üres/rossz => 0. Tizedeseket levágjuk."""
    if val is None:
        return 0
    s = str(val).strip().replace(',', '.')
    # csak az eleji előjeles számot engedjük
    m = re.match(r'^\s*([+-]?\d+(?:\.\d+)?)', s)
    if not m:
        return 0
    try:
        return max(0, int(float(m.group(1))))
    except Exception:
        return 0


def complete_active_wo_with_qty(worker_id: int, qty) -> dict:
    # QTY koerció – sose dobjon kivételt a hívó felé
    qty = _coerce_qty(qty)

    r = db_execute(
        "SELECT work_id FROM WorkstationWorkorder "
        "WHERE worker_id=%s AND status='Active' ORDER BY start_time DESC LIMIT 1",
        (worker_id,),
        fetchone=True,
    )
    if not r:
        # ne dobjunk RuntimeError-t -> visszaüzenet, hogy a kliens JSON-t kapjon
        return {"mode": "error", "message": "Nincs aktív WO a dolgozóhoz."}

    work_id = int(r['work_id'])

    # top-level WO kézi zárása tiltott, ha vannak SUB-jai (a SUB-ok lezárása zárja le automatikusan)
    try:
        if _is_parent_wid(work_id) and _wo_has_children(work_id):
            prow = db_execute("SELECT WO FROM Workorders WHERE ID=%s", (work_id,), fetchone=True)
            return _attach_send_history({
                "mode": "blocked",
                "work_id": work_id,
                "wo": (prow['WO'] if prow else '?'),
                "message": "Top-level WO kézi lezárása tiltott, mert vannak SUB-ok. Zárd le a SUB WO-kat.",
            }, work_id)
    except Exception:
        pass

    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # UPDATE – védetten
    try:
        db_execute(
            "UPDATE WorkstationWorkorder "
            "SET status='Completed', end_time=%s, QTY=%s "
            "WHERE work_id=%s AND status='Active'",
            (end_time, qty, work_id),
        )
    except Exception as e:
        return {"mode": "error", "message": f"DB hiba a lezáráskor: {e}"}

    # státusz összerakás
    row = db_execute(
        "SELECT WO, QTY, ECN, REV, HIERARCHY FROM Workorders WHERE ID=%s",
        (work_id,),
        fetchone=True,
    )
    if row:
        wo, wo_qty, ecn, rev, hierarchy = row['WO'], row['QTY'], row['ECN'], row['REV'], (row['HIERARCHY'] or '')
    else:
        wo, wo_qty, ecn, rev, hierarchy = "?", "?", "?", "?", ""

    # auto-close parent – védetten, hogy ne okozzon 500-at
    parent_wo = None
    try:
        device_name, raspberry_id = get_device_identity()
        parent_wo = _auto_close_parent_if_ready_vyprisiv(
            child_wid=work_id,
            worker_id=worker_id,
            raspberry_id=raspberry_id,
            device_name=device_name
        )
    except Exception as e:
        # lenyeljük – csak infót adunk vissza
        parent_wo = None

    msg = (
        f"WO {wo} completed.\n{hierarchy}\nECN: {ecn}\nQTY in WO: {wo_qty}\nREV: {rev}"
        + (f"\nParent WO {parent_wo} auto-completed @ VYPRISIV." if parent_wo else "")
    )
    return _attach_send_history({
        "mode": "completed",
        "work_id": work_id,
        "wo": wo,
        "parent_auto_closed": bool(parent_wo),
        "message": msg
    }, work_id)



def _wo_has_children(parent_wid: int) -> bool:
    """
    Igaz, ha a megadott parent WO-hoz (PN==master_pn) tartozik legalább egy SUB sor (PN!=master_pn).
    """
    row = db_execute(
        """
        SELECT c.WO, p.PN AS parent_pn
        FROM Workorders AS p
        JOIN Workorders AS c ON c.WO = p.WO
        WHERE p.ID=%s
        LIMIT 1
        """,
        (parent_wid,),
        fetchone=True,
    )
    if not row:
        return False
    wo, parent_pn = row["WO"], row["parent_pn"]
    r = db_execute(
        """
        SELECT COUNT(*) AS cnt
        FROM Workorders
        WHERE WO=%s AND master_pn=%s AND PN<>master_pn
        """,
        (wo, parent_pn),
        fetchone=True,
    )
    return bool(r and int(r["cnt"]) > 0)


def _station_from_table_name(table_name: str) -> str:
    """
    Pl: 'EMI - 1' -> 'EMI'
        'MTE-2'   -> 'MTE'
    """
    s = (table_name or "").strip()
    if not s:
        return ""
    base = s.split("-", 1)[0].strip()
    return base.upper().replace(" ", "_")


def resolve_station_for_device(device_ip: str, fallback_job_title: str = "") -> str:
    """
    1) tables + raspberrydevices mapping alapján
    2) fallback: job_title
    """
    # 1) tables + raspberrydevices
    row = db_execute(
        """
        SELECT t.table_name
        FROM tables t
        JOIN raspberrydevices r ON r.id = t.raspberry_id
        WHERE r.device_id = %s
        LIMIT 1
        """,
        (device_ip,),
        fetchone=True,
    )
    if row and row.get("table_name"):
        st = _station_from_table_name(str(row["table_name"]))
        if st:
            return st

    # 2) fallback: job_title
    jt = (fallback_job_title or "").strip()
    return jt.upper().replace(" ", "_")


# ==================== Kapcsolódó QR kódok (scan oldal, QR-megjelenítő) ====================

def _parse_wo_qr_fields(text: str) -> dict[str, str]:
    """
    A beolvasott WO QR string (pipe-elválasztott kulcs-érték párok)
    mezőinek kinyerése: WO, PN, REV, ECN, MASTER_PN, HIERARCHY.
    """
    kv: dict[str, str] = {}
    for p in text.split("|"):
        if "-" not in p:
            continue
        key, _, val = p.partition("-")
        kv[key.strip()] = val.strip()

    h_key = next((k for k in kv if k.startswith("HIERARCHY")), None)
    return {
        "wo":        kv.get("WO", ""),
        "pn":        kv.get("PN", ""),
        "rev":       kv.get("REV", ""),
        "ecn":       kv.get("ECN", ""),
        "master_pn": kv.get("MASTER_PN", ""),
        "hierarchy": kv.get(h_key, "") if h_key else "",
    }


def _build_wo_qr_string(r: dict) -> str:
    """
    Egy workorders sorból visszaállítja a QR string-et a meglévő
    struktúrában (qr_core.query_recursive_subs formátuma), így az itt
    megjelenített kódok ugyanúgy beolvashatók, mint a nyomtatottak.
    """
    def v(key, default="N/A"):
        x = r.get(key)
        return default if x in (None, "", "None") else str(x)

    parts = [
        f"WO-{v('WO')}",
        f"GRP-{v('GRP')}",
        f"PN-{v('PN')}",
        f"MASTER_PN-{v('MASTER_PN', v('PN'))}",
        f"REV-{v('REV')}",
        f"CLL-{v('CELL')}",
        f"QTY-{v('QTY')}",
        f"ECN-{v('ECN')}",
        f"TIME_WCUT-{v('TIME_WCUT')}",
        f"TIME_PROD-{v('TIME_PROD')}",
        f"TIME_TEST-{v('TIME_TEST')}",
        f"TIME_FIQC-{v('TIME_FIQC')}",
    ]
    h = r.get("HIERARCHY") or ""
    if h and h != "N/A":
        parts.append(f"HIERARCHY-{h}")
    return "|".join(parts)


def _hierarchy_chain(h: str) -> list:
    """
    A HIERARCHY string PN-láncát adja vissza felülről lefelé:
    'PN: A SUB1: B SUB2: C' -> ['A', 'B', 'C']. Üres / N/A -> [].
    Az utolsó elem maga a rekord PN-je, az előtte lévő a közvetlen szülője.
    """
    if not h or h == "N/A":
        return []
    chain = []
    m = re.search(r'PN:\s*(\S+)', h)
    if m:
        chain.append(m.group(1))
    for sm in re.finditer(r'SUB\d+:\s*(\S+)', h):
        chain.append(sm.group(1))
    return chain


def _edge_qty_lookup(parent_pns: set) -> dict:
    """
    A t_dump-ból a szülő PN-ek SUBS / SUBS QTYS listái alapján visszaadja,
    hogy egy (szülő, gyerek) élhez mekkora SUBS QTY tartozik.
    Ez a QR kód-példányszám szorzója: a BATCH QTY (workorders.QTY) NEM az.
    Visszatérés: {(parent_pn, child_pn): qty_int}
    """
    edge: dict = {}
    pns = [p for p in parent_pns if p]
    if not pns:
        return edge
    placeholders = ",".join(["%s"] * len(pns))
    rows = db_execute(
        f"SELECT `PART.NBR` AS PN, SUBS, `SUBS QTYS` AS QTYS "
        f"FROM t_dump WHERE `PART.NBR` IN ({placeholders})",
        tuple(pns),
    ) or []
    for r in rows:
        subs = str(r.get("SUBS") or "")
        qtys = str(r.get("QTYS") or "")
        if not subs or subs == "None":
            continue
        sl = subs.split("|")
        ql = qtys.split("|") if qtys and qtys != "None" else []
        for i, s in enumerate(sl):
            s = s.strip()
            if not s:
                continue
            try:
                q = int(float(ql[i])) if i < len(ql) else 1
            except (ValueError, TypeError):
                q = 1
            edge[(str(r["PN"]), s)] = max(1, q)
    return edge


def _qr_svg(data: str) -> str:
    """QR kód SVG-ként (skálázható, éles marad bármilyen cellméretben)."""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    svg = img.to_string(encoding="unicode")
    # fix width/height mm-ben ne legyen – a cella mérete (CSS) skálázza
    svg = re.sub(r'\s(?:width|height)="[^"]*"', "", svg, count=2)
    return svg


# /related válasz-cache WO-nként: a ~44 QR SVG generálása a legdrágább
# lépés, és a WO fa a generálás után nem változik. Rövid TTL, hogy egy
# újragenerált WO se ragadjon be sokáig.
_RELATED_CACHE: dict = {}
_RELATED_CACHE_TTL = 180.0   # másodperc
_RELATED_CACHE_MAX = 50      # ennyi WO-t tartunk memóriában


def fetch_related_qr_codes(text: str) -> dict:
    """
    A beszkennelt WO QR alapján visszaadja az ÖSSZES kapcsolódó
    workorders rekordot (teljes WO fa: parent + minden SUB), soronként
    a visszaállított QR stringgel és SVG képpel. WO-nként cache-elve.
    """
    text = normalize_scanner_text(text)
    if not text.startswith("WO"):
        raise RuntimeError("Toto nie je WO QR kód. Naskenuj QR kód z pracovného lístka.")

    f = _parse_wo_qr_fields(text)
    wo_value = f["wo"]

    if wo_value:
        hit = _RELATED_CACHE.get(wo_value)
        if hit and (time.monotonic() - hit[0]) < _RELATED_CACHE_TTL:
            return hit[1]

    # elsődleges kulcs a WO szám; sérült QR esetén PN+REV+ECN alapján
    # próbáljuk egyértelműen azonosítani a WO-t
    if not wo_value and f["pn"]:
        rows = db_execute(
            "SELECT DISTINCT WO FROM Workorders WHERE PN=%s AND REV=%s AND ECN=%s LIMIT 2",
            (f["pn"], f["rev"], f["ecn"]),
        ) or []
        if len(rows) == 1:
            wo_value = str(rows[0]["WO"])

    if not wo_value:
        raise RuntimeError("Neúplné načítanie QR kódu (chýba číslo WO). Naskenuj QR kód znova.")

    rows = db_execute(
        """
        SELECT ID, WO, PN, QTY, MLT_STATUS,
               TIME_FIQC, TIME_TEST, TIME_PROD, TIME_WCUT,
               ECN, REV, CELL, `GROUP` AS GRP,
               MASTER_PN, COALESCE(HIERARCHY,'') AS HIERARCHY
        FROM Workorders
        WHERE WO=%s
        """,
        (wo_value,),
    ) or []

    if not rows:
        raise RuntimeError(
            f"WO {wo_value} sa nenašlo v databáze (PN: {f['pn'] or '?'}). "
            f"Naskenuj QR kód znova, alebo daj vedieť team leaderovi."
        )

    # rendezés fa-sorrendbe (DFS pre-order): a MASTER mindig elöl, utána a
    # szülő után rögtön a saját SUB-jai, a testvérek PN szerint. Minden
    # nem-master sor kulcsát a masterrel kezdjük, így a szintek helyesen
    # egymásba ágyazódnak (a HIERARCHY lánc a master-t nem tartalmazza).
    def sort_chain(r):
        h = r["HIERARCHY"] if r["HIERARCHY"] != "N/A" else ""
        pn = str(r["PN"] or "")
        master = str(r["MASTER_PN"] or "")
        ch = _hierarchy_chain(h)
        if pn == master and not ch:
            return []                 # master – mindig legelöl
        if ch:
            return [master] + ch      # master + level1..leveln
        return [master, pn]           # level-1 flat (N/A) sub

    rows.sort(key=sort_chain)

    # a beszkennelt (parent) sor adatai a státusz sorhoz
    head = rows[0]
    for r in rows:
        if (r["PN"] or "") == (f["master_pn"] or f["pn"]) and (r["HIERARCHY"] in ("", "N/A")):
            head = r
            break

    # 1. menet: szerep / szint / szülő meghatározása soronként
    metas = []
    for r in rows:
        h = r["HIERARCHY"] if r["HIERARCHY"] != "N/A" else ""
        pn = str(r["PN"] or "")
        master = str(r["MASTER_PN"] or "")
        chain = _hierarchy_chain(h)

        if pn == master and not chain:
            role, depth, parent_pn = "master", 0, ""
        elif chain:
            # a HIERARCHY lánc a level-1 subtól indul (a master nincs benne),
            # így a valós fa-szint = a lánc hossza. Az utolsó elem maga a PN,
            # az előtte lévő a közvetlen szülő.
            depth = len(chain)
            parent_pn = chain[-2] if len(chain) >= 2 else master
            role = "sub"
        else:
            # flat (N/A) SUB sor: nincs lánc, közvetlen szülője a master
            role, depth, parent_pn = "sub", 1, master
        metas.append((r, pn, role, depth, parent_pn))

    # 2. menet: kód-példányszám a t_dump SUBS QTY-jából (él-mennyiség).
    # A BATCH QTY (workorders.QTY) NEM szorzó: a WO-ban szereplő PN 1x
    # generálódik, és csak ha a SUBS QTY > 1, akkor lesz annyi példány.
    edge = _edge_qty_lookup({m[4] for m in metas if m[4]})

    items = []
    for r, pn, role, depth, parent_pn in metas:
        copies = 1
        if role != "master" and parent_pn:
            copies = edge.get((parent_pn, pn), 1)
        copies = max(1, min(copies, 999))   # védőkorlát

        svg = _qr_svg(_build_wo_qr_string(r))
        for n in range(1, copies + 1):
            items.append({
                "pn":  pn,
                "qty": str(r["QTY"] or ""),
                "depth": depth,
                "parent_pn": parent_pn,
                "role": role,
                "idx": n,
                "of": copies,
                "sub": depth,   # visszafelé kompatibilitás
                "svg": svg,
            })

    result = {
        "wo":    str(head["WO"] or wo_value),
        "pn":    str(head["PN"] or ""),
        "rev":   str(head["REV"] or ""),
        "ecn":   str(head["ECN"] or ""),
        "qty":   str(head["QTY"] or ""),
        "count": len(items),
        "items": items,
    }

    # cache-be (legrégebbi kidobása, ha megtelt)
    if wo_value:
        if len(_RELATED_CACHE) >= _RELATED_CACHE_MAX:
            oldest = min(_RELATED_CACHE, key=lambda k: _RELATED_CACHE[k][0])
            _RELATED_CACHE.pop(oldest, None)
        _RELATED_CACHE[wo_value] = (time.monotonic(), result)

    return result
