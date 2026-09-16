# services/dbpool.py
# -*- coding: utf-8 -*-
"""
Központi MySQL connection pool az egész alkalmazásnak.

Eddig három különböző helyen nyíltak kapcsolatok (services/db.py,
services/bt_db.py, services/scan_core.py) – kérésenként újat nyitva.
Mostantól mindenki ebből a poolból kap kapcsolatot, és a close()
visszaadja a poolba (nem zárja le ténylegesen).

TELJESÍTMÉNY (fontos): minden MySQL parancs egy hálózati oda-vissza a
DB szerverrel. A kapcsolat-átvételkor korábban MINDEN lekérdezés előtt
lefutott egy ping (COM_PING) és egy rollback – vagyis egy egysoros SELECT
is 4 oda-vissza volt (ping + rollback + execute + commit). Egy scan oldali
kérés 5-8 lekérdezésével ez 20-30 felesleges kör a DB felé.
Mostantól:
  • a ping csak akkor fut, ha a kapcsolat DB_PING_INTERVAL_S-nál régebben
    volt használva (a hívók amúgy is újrapróbálják a megszakadt kapcsolatot),
  • a rollback csak akkor, ha tényleg van nyitott tranzakció.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import mysql.connector
from mysql.connector import pooling
from mysql.connector.errors import PoolError


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


DB_CFG = {
    "host": os.getenv("DB_HOST", "10.10.2.15").strip("'\""),
    "user": os.getenv("DB_USER", "root").strip("'\""),
    "password": os.getenv("DB_PASSWORD", "admin321").strip("'\""),
    "database": os.getenv("DB_NAME", "paperless").strip("'\""),
    "connection_timeout": 5,
    "charset": "utf8mb4",
    "collation": "utf8mb4_unicode_ci",
}

# Opcionális: autocommit a kapcsolat születésekor (DB_AUTOCOMMIT=1).
# Ezzel a SELECT-ek után elmarad a commit oda-vissza is (a hívók
# in_transaction alapján hagyják ki). FIGYELEM: autocommit mellett a
# több lépéses írások nem lesznek atomikusak, ezért alapból KI van kapcsolva
# – csak akkor kapcsold be, ha átnézted a többlépéses írásokat.
if _env_flag("DB_AUTOCOMMIT", False):
    DB_CFG["autocommit"] = True

_pool: Optional[pooling.MySQLConnectionPool] = None
_pool_lock = threading.Lock()

# Ha a pool pillanatnyilag kimerült, ennyit várunk, mielőtt közvetlen
# kapcsolatra esünk vissza (rövid, hogy ne lassítsa az oldalt).
_POOL_WAIT_TOTAL_S = 0.6
_POOL_WAIT_STEP_S = 0.03

# Ennyi tétlenség után ellenőrizzük pinggel a kapcsolatot. A friss (pár
# másodperce használt) kapcsolatokat fölösleges pingelni: az élő MySQL
# wait_timeout-ja tipikusan órákban mérhető, a ténylegesen megszakadt
# kapcsolatot pedig a hívók újrapróbálják (scan_core.db_execute).
_PING_INTERVAL_S = float(os.getenv("DB_PING_INTERVAL_S", "60"))

# mysql-connector felső korlátja a pool méretére
_POOL_MAX = 32


def _real_cnx(conn):
    """A poolozott burkoló mögötti valódi kapcsolat (közvetlennél önmaga)."""
    return getattr(conn, "_cnx", conn)


def get_pool() -> pooling.MySQLConnectionPool:
    """Lusta, szálbiztos pool-inicializálás."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                try:
                    size = int(os.getenv("DB_POOL_SIZE", "10"))
                except (TypeError, ValueError):
                    size = 10
                # a mysql-connector 32 fölött hibát dob – inkább vágjuk le,
                # mint hogy az egész app induláskor elszálljon
                size = max(1, min(size, _POOL_MAX))
                _pool = pooling.MySQLConnectionPool(
                    pool_name="paperless_main",
                    # Szerény pool (kevés állandó kapcsolat a MySQL felé); a
                    # csúcsterhelést a közvetlen-kapcsolat fallback fogja fel.
                    # Sok egyidejű eszköznél érdemes emelni: DB_POOL_SIZE=32.
                    pool_size=size,
                    # A session-reset a visszaadáskor szintén egy oda-vissza.
                    # Kikapcsolható: DB_POOL_RESET_SESSION=0 (a kapcsolatot a
                    # get_pooled_connection amúgy is kitakarítja átvételkor).
                    pool_reset_session=_env_flag("DB_POOL_RESET_SESSION", True),
                    **DB_CFG,
                )
    return _pool


def get_pooled_connection(autocommit: bool = False):
    """
    Kapcsolat a poolból. Ha a pool éppen kimerült, röviden vár, majd
    VÉSZMEGOLDÁSKÉNT egy közvetlen (nem-poolozott) kapcsolatot nyit – így
    egyetlen oldal sem dől el 500-zal pool-kimerülés miatt (ez pont az
    eredeti, kérésenkénti kapcsolat-nyitás viselkedése).
    A visszakapott kapcsolaton a close():
      - poolozott kapcsolatnál visszaadás a poolba,
      - közvetlen kapcsolatnál tényleges lezárás.
    """
    conn = None
    deadline = time.monotonic() + _POOL_WAIT_TOTAL_S
    while True:
        try:
            conn = get_pool().get_connection()
            break
        except PoolError:
            if time.monotonic() >= deadline:
                # pool kimerült → közvetlen kapcsolat vészmegoldásként
                conn = mysql.connector.connect(**DB_CFG)
                break
            time.sleep(_POOL_WAIT_STEP_S)
        except Exception:
            # bármi más pool-hiba esetén is inkább közvetlen kapcsolat,
            # mint 500-as oldal
            conn = mysql.connector.connect(**DB_CFG)
            break

    # Ha a kapcsolat időközben megszakadt (pl. MySQL restart), élesztjük.
    # Csak akkor pingelünk, ha a kapcsolat régebben volt használva – a friss
    # kapcsolatnál ez tiszta veszteség lenne (kérésenként 5-8 oda-vissza).
    cnx = _real_cnx(conn)
    now = time.monotonic()
    last_seen = getattr(cnx, "_pool_last_seen_ts", 0.0)
    if (now - last_seen) >= _PING_INTERVAL_S:
        try:
            conn.ping(reconnect=True, attempts=1, delay=0)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn = get_pool().get_connection()
            except Exception:
                conn = mysql.connector.connect(**DB_CFG)
            cnx = _real_cnx(conn)
    try:
        cnx._pool_last_seen_ts = now
    except Exception:
        pass

    # Védekező tisztítás: ha egy korábbi kérés beolvasatlan eredménnyel vagy
    # nyitott tranzakcióval adta vissza a kapcsolatot, az a következő oldalon
    # ("Unread result found" / "Commands out of sync") elszállna. Itt leürítjük.
    try:
        conn.consume_results()
    except Exception:
        pass
    # rollback CSAK nyitott tranzakcióra – tiszta kapcsolatnál ez egy
    # fölösleges oda-vissza volt minden egyes lekérdezés előtt.
    try:
        if conn.in_transaction:
            conn.rollback()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    if autocommit:
        # FIGYELEM: a PooledMySQLConnection burkolón a sima
        # `conn.autocommit = True` NEM jut el a szerverig – csak a burkoló
        # objektumra tenne rá egy attribútumot (a __getattr__ delegál, a
        # __setattr__ nem), és onnantól a getter is a hamis értéket adná.
        # Ezért a valódi kapcsolaton állítjuk be.
        try:
            cnx.autocommit = True
        except Exception:
            pass
    return conn
