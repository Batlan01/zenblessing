# services/database_viewer_core.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple, Sequence
import re, threading

from sqlalchemy import create_engine, text, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError, OperationalError

CHARSET   = "utf8mb4"
COLLATION = "utf8mb4_general_ci"
INIT_SQL  = f"SET NAMES {CHARSET} COLLATE {COLLATION}"

@dataclass
class DBCreds:
    host: str
    port: int
    user: str
    password: str
    charset: str = CHARSET
    collation: str = COLLATION

class EngineRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._engines: Dict[str, Engine] = {}
        self._db_names: Dict[str, str] = {}
        self._creds: Dict[str, DBCreds] = {}

    def _build_url(self, creds: DBCreds, dbname: Optional[str] = None) -> str:
        # mysql+mysqlconnector://user:pass@host:port/db?charset=utf8mb4&collation=utf8mb4_general_ci
        base = f"mysql+mysqlconnector://{creds.user}:{creds.password}@{creds.host}:{creds.port}"
        qs   = f"?charset={creds.charset}&collation={creds.collation}"
        return f"{base}/{dbname}{qs}" if dbname else f"{base}/{qs}"

    def _make_engine(self, url: str) -> Engine:
        eng = create_engine(
            url,
            pool_size=5, max_overflow=2,
            pool_pre_ping=True, pool_recycle=1800, pool_timeout=30,
            future=True,
            # connect_args={"init_command": INIT_SQL},  # <-- ELTÁVOLÍTVA
        )

        # Minden új DBAPI kapcsolatnál lefut – driverfüggetlen (PyMySQL, mysql-connector-python, mysqlclient)
        @event.listens_for(eng, "connect")
        def _init_connection(dbapi_conn, conn_record):
            cur = dbapi_conn.cursor()
            try:
                # Amit eddig init_command-ban futtattunk:
                cur.execute(INIT_SQL)
                # Ide tehetsz további session-szintű beállításokat is, pl.:
                # cur.execute("SET time_zone = '+00:00'")
                # cur.execute("SET SESSION sql_mode='STRICT_TRANS_TABLES'")
            finally:
                cur.close()

        return eng

    def create_or_update(self, session_id: str, creds: DBCreds) -> None:
        with self._lock:
            old = self._engines.pop(session_id, None)
            if old:
                try: old.dispose()
                except Exception: pass
            self._creds[session_id] = creds
            self._engines[session_id] = self._make_engine(self._build_url(creds))
            self._db_names.pop(session_id, None)

    def set_database(self, session_id: str, dbname: str) -> None:
        with self._lock:
            creds = self._creds.get(session_id)
            if not creds:
                raise RuntimeError("Nincs aktív DB kapcsolat. Jelentkezz be előbb.")
            old = self._engines.pop(session_id, None)
            if old:
                try: old.dispose()
                except Exception: pass
            self._engines[session_id] = self._make_engine(self._build_url(creds, dbname))
            self._db_names[session_id] = dbname

    def get_engine(self, session_id: str) -> Engine:
        with self._lock:
            eng = self._engines.get(session_id)
            if not eng:
                raise RuntimeError("Nincs aktív DB kapcsolat ehhez a munkamenethez.")
            return eng

    def get_current_db(self, session_id: str) -> Optional[str]:
        with self._lock:
            return self._db_names.get(session_id)

    def logout(self, session_id: str) -> None:
        with self._lock:
            eng = self._engines.pop(session_id, None)
            if eng:
                try: eng.dispose()
                except Exception: pass
            self._db_names.pop(session_id, None)
            self._creds.pop(session_id, None)

engine_registry = EngineRegistry()

# engedjük: betűk/számok/._$/ szóköz és kötőjel; TILOS: backtick, pontosvessző, NUL
_identifier_rx = re.compile(r"^[A-Za-z0-9_.$ \-]+$")

def sanitize_identifier(name: str) -> str:
    name = (name or "").strip()
    # tiltott karakterek, amelyek megtörnék a backtick idézést vagy több parancsot engednének át
    if not name or "`" in name or ";" in name or "\x00" in name:
        raise ValueError("Érvénytelen azonosító.")
    if not _identifier_rx.match(name):
        raise ValueError("Érvénytelen azonosító.")
    return name


def test_connection(session_id: str) -> Tuple[bool, Optional[str]]:
    try:
        eng = engine_registry.get_engine(session_id)
        with eng.connect() as conn:
            # az event miatt amúgy is lefut az INIT_SQL, de ártani nem árt
            conn.exec_driver_sql(INIT_SQL)
            conn.exec_driver_sql("SELECT 1")
        return True, None
    except (OperationalError, SQLAlchemyError) as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)

# ---------- Meta ----------
def list_databases(session_id: str) -> List[str]:
    eng = engine_registry.get_engine(session_id)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rows = conn.execute(text("SHOW DATABASES")).all()
    return [r[0] for r in rows]

def list_tables(session_id: str, dbname: str) -> List[str]:
    dbname = sanitize_identifier(dbname)
    engine_registry.set_database(session_id, dbname)
    eng = engine_registry.get_engine(session_id)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rows = conn.execute(text("SHOW FULL TABLES")).all()
    return [r[0] for r in rows]

def get_columns(session_id: str, dbname: str, table: str):
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    eng = engine_registry.get_engine(session_id)
    sql = text("""
        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tbl
        ORDER BY ORDINAL_POSITION
    """)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rows = conn.execute(sql, {"db": dbname, "tbl": table}).mappings().all()
    return [dict(r) for r in rows]

def describe_table(session_id: str, dbname: str, table: str):
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    eng = engine_registry.get_engine(session_id)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rows = conn.execute(text(f"DESCRIBE `{table}`")).mappings().all()
    return [dict(r) for r in rows]

def get_indexes(session_id: str, dbname: str, table: str):
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    eng = engine_registry.get_engine(session_id)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rows = conn.execute(text(f"SHOW INDEX FROM `{table}`")).mappings().all()
    return [dict(r) for r in rows]

def show_create_table(session_id: str, dbname: str, table: str) -> str:
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    eng = engine_registry.get_engine(session_id)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        row = conn.execute(text(f"SHOW CREATE TABLE `{table}`")).first()
    return (row[1] if row and len(row) > 1 else (row[0] if row else "")) or ""

# ---------- WHERE builder ----------
_ALLOWED_OPS = {"=", "!=", "<", "<=", ">", ">=", "LIKE", "IN", "BETWEEN", "IS NULL", "IS NOT NULL"}

def _build_where_multiple(wheres: Optional[Sequence[Dict[str, Any]]], logic: str = "AND") -> Tuple[str, Dict[str, Any]]:
    if not wheres:
        return "", {}
    logic = "OR" if str(logic).upper().startswith("OR") else "AND"
    parts: List[str] = []
    params: Dict[str, Any] = {}
    seq = 0
    for cond in wheres:
        col = sanitize_identifier((cond.get("column") or "").strip())
        op  = (cond.get("op") or "").strip().upper()
        if op not in _ALLOWED_OPS:
            raise ValueError(f"Érvénytelen operátor: {op}")
        if op in {"IS NULL", "IS NOT NULL"}:
            parts.append(f"`{col}` {op}")
            continue
        if op == "IN":
            vals = cond.get("value")
            keys = []
            for v in (vals or []):
                k = f"w{seq}"; seq += 1
                params[k] = v
                keys.append(f":{k}")
            if keys:
                parts.append(f"`{col}` IN ({','.join(keys)})")
            else:
                parts.append("1=0")
            continue
        if op == "BETWEEN":
            vals = cond.get("value") or [None, None]
            k1, k2 = f"w{seq}", f"w{seq+1}"; seq += 2
            params[k1], params[k2] = vals[0], vals[1]
            parts.append(f"`{col}` BETWEEN :{k1} AND :{k2}")
            continue
        val = cond.get("value")
        if op == "LIKE" and isinstance(val, str) and "%" not in val:
            val = f"%{val}%"
        k = f"w{seq}"; seq += 1
        params[k] = val
        parts.append(f"`{col}` {op} :{k}")
    clause = "WHERE " + f" {logic} ".join(parts)
    return clause, params

# ---------- Count / Distinct / Aggregate ----------
def row_count(session_id: str, dbname: str, table: str, wheres=None, logic="AND") -> int:
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    eng = engine_registry.get_engine(session_id)
    where_sql, params = _build_where_multiple(wheres, logic)
    sql = text(f"SELECT COUNT(*) FROM `{table}` {where_sql}")
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        n = conn.execute(sql, params).scalar_one()
    return int(n or 0)

def distinct_values(session_id: str, dbname: str, table: str, column: str, limit=100, offset=0, wheres=None, logic="AND"):
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table); column = sanitize_identifier(column)
    limit  = max(1, min(int(limit or 100), 1000)); offset = max(0, int(offset or 0))
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    where_sql, params = _build_where_multiple(wheres, logic)
    eng = engine_registry.get_engine(session_id)
    sql = text(f"SELECT DISTINCT `{column}` AS val FROM `{table}` {where_sql} ORDER BY val LIMIT :limit OFFSET :offset")
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rs = conn.execute(sql, {**params, "limit": limit, "offset": offset})
        vals = [r[0] for r in rs.fetchall()]
    return {"values": vals, "limit": limit, "offset": offset, "count": len(vals)}

_ALLOWED_AGG = {"COUNT", "MIN", "MAX", "AVG", "SUM"}
def aggregate(session_id: str, dbname: str, table: str, func: str, column: Optional[str], wheres=None, logic="AND"):
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    func   = (func or "").upper()
    if func not in _ALLOWED_AGG:
        raise ValueError("Nem támogatott aggregáció.")
    col_sql = "*" if func == "COUNT" and not column else f"`{sanitize_identifier(column or '*')}`"
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    where_sql, params = _build_where_multiple(wheres, logic)
    eng = engine_registry.get_engine(session_id)
    sql = text(f"SELECT {func}({col_sql}) FROM `{table}` {where_sql}")
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        val = conn.execute(sql, params).scalar()
    return val

# ---------- Preview ----------
def preview_table(session_id: str, dbname: str, table: str, limit=50, offset=0, orders=None, wheres=None, logic="AND"):
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    limit  = max(1, min(int(limit or 50), 1000))
    offset = max(0, int(offset or 0))
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)

    order_sql = ""
    if orders:
        parts = []
        for o in orders:
            c = sanitize_identifier((o.get("column") or "").strip())
            d = "DESC" if str(o.get("dir") or "ASC").upper().startswith("D") else "ASC"
            parts.append(f"`{c}` {d}")
        if parts:
            order_sql = " ORDER BY " + ", ".join(parts)

    where_sql, params = _build_where_multiple(wheres, logic)
    eng = engine_registry.get_engine(session_id)
    sql = text(f"SELECT * FROM `{table}` {where_sql}{order_sql} LIMIT :limit OFFSET :offset")
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rs = conn.execute(sql, {"limit": limit, "offset": offset, **params})
        rows = rs.fetchall()
        cols = rs.keys()
    data = [dict(zip(cols, r)) for r in rows]
    return {"columns": list(cols), "rows": data, "limit": limit, "offset": offset, "count": len(data),
            "orders": orders or [], "logic": "OR" if str(logic).upper().startswith("OR") else "AND"}

# ---------- SQL futtató ----------
_READONLY_PREFIXES = ("SELECT", "SHOW", "DESCRIBE", "EXPLAIN", "WITH")
def run_sql(session_id: str, dbname: str, sql_text: str, readonly: bool = True) -> Dict[str, Any]:
    dbname = sanitize_identifier(dbname)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    sql_text = (sql_text or "").strip()
    if not sql_text:
        raise ValueError("Üres SQL.")
    first = sql_text.split(None, 1)[0].upper()
    if readonly and all(not first.startswith(pref) for pref in _READONLY_PREFIXES):
        raise ValueError("Csak olvasási parancsok engedélyezettek ebben a módban (SELECT/SHOW/DESCRIBE/EXPLAIN).")

    eng = engine_registry.get_engine(session_id)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rs = conn.exec_driver_sql(sql_text)
        if rs.returns_rows:
            rows = rs.fetchall(); cols = rs.keys()
            data = [dict(zip(cols, r)) for r in rows]
            return {"columns": list(cols), "rows": data, "rowcount": len(data), "message": "OK"}
        else:
            affected = rs.rowcount
            return {"columns": [], "rows": [], "rowcount": affected, "message": f"Affected rows: {affected}"}

# ---------- PK + módosítások ----------
def get_primary_keys(session_id: str, dbname: str, table: str) -> List[str]:
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    eng = engine_registry.get_engine(session_id)
    sql = text("""
      SELECT COLUMN_NAME
      FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
      WHERE TABLE_SCHEMA=:db AND TABLE_NAME=:tbl AND CONSTRAINT_NAME='PRIMARY'
      ORDER BY ORDINAL_POSITION
    """)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rows = conn.execute(sql, {"db": dbname, "tbl": table}).all()
    return [r[0] for r in rows]

def update_cell(session_id: str, dbname: str, table: str, pk_values: Dict[str, Any], column: str, new_value: Any) -> int:
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table); column = sanitize_identifier(column)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    pks = get_primary_keys(session_id, dbname, table)
    if not pks: raise ValueError("A táblának nincs primer kulcsa (nem biztonságos update).")
    where_parts=[]; params={"new": new_value}
    for i, pk in enumerate(pks):
        if pk not in pk_values: raise ValueError(f"Hiányzik PK érték: {pk}")
        key=f"p{i}"; where_parts.append(f"`{pk}` = :{key}"); params[key]=pk_values[pk]
    sql = text(f"UPDATE `{table}` SET `{column}`=:new WHERE " + " AND ".join(where_parts))
    eng = engine_registry.get_engine(session_id)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rs = conn.execute(sql, params)
        conn.commit()
        return rs.rowcount or 0

def insert_row(session_id: str, dbname: str, table: str, data: Dict[str, Any]) -> int:
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    cols = [sanitize_identifier(c) for c in data.keys()]
    placeholders = [f":v{i}" for i,_ in enumerate(cols)]
    params = {f"v{i}": data[c] for i,c in enumerate(cols)}
    sql = text(f"INSERT INTO `{table}` ({', '.join('`'+c+'`' for c in cols)}) VALUES ({', '.join(placeholders)})")
    eng = engine_registry.get_engine(session_id)
    with eng.connect() as conn:
        conn.exec_driver_sql(INIT_SQL)
        rs = conn.execute(sql, params)
        conn.commit()
        return rs.rowcount or 0

def delete_rows(session_id: str, dbname: str, table: str, pk_list: List[Dict[str, Any]]) -> int:
    dbname = sanitize_identifier(dbname); table = sanitize_identifier(table)
    if engine_registry.get_current_db(session_id) != dbname:
        engine_registry.set_database(session_id, dbname)
    pks = get_primary_keys(session_id, dbname, table)
    if not pks: raise ValueError("A táblának nincs primer kulcsa (nem biztonságos törlés).")
    eng = engine_registry.get_engine(session_id)
    total=0
    with eng.begin() as conn:  # transaction
        conn.exec_driver_sql(INIT_SQL)
        for row in pk_list:
            where_parts=[]; params={}
            for i, pk in enumerate(pks):
                key=f"p{i}"; where_parts.append(f"`{pk}`=:{key}"); params[key]=row[pk]
            rs = conn.execute(text(f"DELETE FROM `{table}` WHERE "+ " AND ".join(where_parts)), params)
            total += (rs.rowcount or 0)
    return total
