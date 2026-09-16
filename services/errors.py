# services/errors.py
from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict, Any, List, Literal, Tuple

from .scan_core import db_execute  # pool-alapú, kérésenként kezelt kapcsolat

ErrorStatus = Literal["Open", "In Progress", "Closed"]


def _now_str() -> str:
    """YYYY-mm-dd HH:MM:SS formátum (MySQL DATETIME kompatibilis)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_meta(meta: Optional[Dict[str, Any]]) -> str:
    """Meta dict szelíd, egysoros megjelenítése (log célra)."""
    if not meta:
        return ""
    try:
        # kulcs=érték; vesszővel elválasztva
        parts = []
        for k, v in meta.items():
            parts.append(f"{k}={v!r}")
        return " | meta={" + ", ".join(parts) + "}"
    except Exception:
        return f" | meta={meta!r}"


def log_injector_error(
    device_ip: str,
    device_name: str,
    message: str,
    status: ErrorStatus = "Open",
    solved_at: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Új bejegyzés az `errors` táblába.

    Táblastruktúra (oszlopok szóközzel!):
      `ID` BIGINT PK AI
      `Device IP` VARCHAR(64)
      `Device Name` VARCHAR(255)
      `Error` TEXT
      `Error Raised Date` DATETIME
      `Error Solved Date` DATETIME NULL
      `Error Status` ENUM('Open','In Progress','Closed')

    Visszatér: beszúrt sor auto-increment ID-je.
    """
    msg = f"{message}{_fmt_meta(meta)}"
    raised_at = _now_str()
    # solved_at maradhat None → NULL megy a DB-be
    sql = """
    INSERT INTO `errors`
      (`Device IP`, `Device Name`, `Error`, `Error Raised Date`, `Error Solved Date`, `Error Status`)
    VALUES (%s, %s, %s, %s, %s, %s)
    """
    try:
        return int(db_execute(
            sql,
            (device_ip, device_name, msg, raised_at, solved_at, status),
            dictcur=False,
            return_lastrowid=True,
        ) or 0)
    except Exception:
        # A hibalogolás sose dobjon tovább kivételt – ha a DB épp nem elérhető,
        # ne fedje el az eredeti hibát, amit épp logolni próbáltunk.
        return 0


def mark_error_solved(error_id: int, when: Optional[str] = None) -> None:
    """
    Egy hiba lezárása. `Error Status` = 'Closed', `Error Solved Date` = NOW (vagy megadott).
    """
    solved = when or _now_str()
    db_execute(
        """
        UPDATE `errors`
           SET `Error Status` = 'Closed',
               `Error Solved Date` = %s
         WHERE `ID` = %s
        """,
        (solved, error_id),
        dictcur=False,
    )


def reopen_error(error_id: int, status: ErrorStatus = "Open") -> None:
    """
    Lezárt hiba újranyitása. `Error Solved Date` NULL-ázása, új státusz.
    """
    if status not in ("Open", "In Progress"):
        status = "Open"
    db_execute(
        """
        UPDATE `errors`
           SET `Error Status` = %s,
               `Error Solved Date` = NULL
         WHERE `ID` = %s
        """,
        (status, error_id),
        dictcur=False,
    )


def update_error_status(error_id: int, status: ErrorStatus) -> None:
    """
    Általános státuszfrissítés. 'Closed' esetén automatikusan kitölti a Solved Date-et.
    """
    if status == "Closed":
        db_execute(
            """
            UPDATE `errors`
               SET `Error Status` = 'Closed',
                   `Error Solved Date` = %s
             WHERE `ID` = %s
            """,
            (_now_str(), error_id),
            dictcur=False,
        )
    else:
        db_execute(
            """
            UPDATE `errors`
               SET `Error Status` = %s
             WHERE `ID` = %s
            """,
            (status, error_id),
            dictcur=False,
        )


def append_error_note(error_id: int, note: str, meta: Optional[Dict[str, Any]] = None) -> None:
    """
    Egyszerű „append” a `Error` mező végére időbélyeggel (audit trail-hez).
    """
    stamp = _now_str()
    addon = f"\n[{stamp}] {note}{_fmt_meta(meta)}"
    db_execute(
        """
        UPDATE `errors`
           SET `Error` = CONCAT(COALESCE(`Error`, ''), %s)
         WHERE `ID` = %s
        """,
        (addon, error_id),
        dictcur=False,
    )


def get_error(error_id: int) -> Optional[Dict[str, Any]]:
    """
    Egy bejegyzés visszaolvasása dict-ként.
    """
    row = db_execute(
        """
        SELECT `ID`, `Device IP`, `Device Name`, `Error`,
               `Error Raised Date`, `Error Solved Date`, `Error Status`
          FROM `errors`
         WHERE `ID` = %s
         LIMIT 1
        """,
        (error_id,),
        fetchone=True,
    )
    return dict(row) if row else None


def list_errors(
    limit: int = 50,
    status: Optional[ErrorStatus] = None,
    device_like: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Legutóbbi hibák listája opcionális szűrésekkel.
    """
    filters: List[str] = []
    params: List[Any] = []

    if status:
        filters.append("`Error Status` = %s")
        params.append(status)

    if device_like:
        filters.append("(`Device IP` LIKE %s OR `Device Name` LIKE %s)")
        like = f"%{device_like}%"
        params.extend([like, like])

    where = "WHERE " + " AND ".join(filters) if filters else ""
    sql = f"""
    SELECT `ID`, `Device IP`, `Device Name`, `Error`,
           `Error Raised Date`, `Error Solved Date`, `Error Status`
      FROM `errors`
      {where}
     ORDER BY `Error Raised Date` DESC, `ID` DESC
     LIMIT %s
    """
    params.append(int(max(1, min(limit, 1000))))

    rows = db_execute(sql, tuple(params)) or []
    return [dict(r) for r in rows]


def ensure_errors_table() -> None:
    """
    (Opcionális) Táblalétrehozás – futtasd deploy/boot során.
    Ha már létezik, nem csinál semmit.
    """
    db_execute("""
    CREATE TABLE IF NOT EXISTS `errors` (
      `ID` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      `Device IP` VARCHAR(64) NOT NULL,
      `Device Name` VARCHAR(255) NOT NULL,
      `Error` MEDIUMTEXT NOT NULL,
      `Error Raised Date` DATETIME NOT NULL,
      `Error Solved Date` DATETIME NULL,
      `Error Status` ENUM('Open','In Progress','Closed') NOT NULL DEFAULT 'Open',
      PRIMARY KEY (`ID`),
      KEY `idx_status_date` (`Error Status`, `Error Raised Date`),
      KEY `idx_device` (`Device IP`, `Device Name`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """, dictcur=False)
