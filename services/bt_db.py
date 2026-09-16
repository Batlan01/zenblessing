import json
from collections import Counter
from flask import g, current_app
import mysql.connector
from typing import Optional, Dict, Any, List, Tuple

from services.dbpool import get_pooled_connection

# ------------------------------------------------------------
# Központi DB kapcsolat (Flask context alatt, g.db-ben tartva)
# ------------------------------------------------------------

def _alive(db) -> bool:
    try:
        if getattr(db, "_cnx", True) is None:
            return False
        return db.is_connected()
    except Exception:
        return False


def get_db() -> mysql.connector.MySQLConnection:
    """
    Kérésenként egy kapcsolat a közös poolból (autocommit=ON).
    A kérés végén az app.teardown (services/db.teardown_db) visszaadja a poolba.
    """
    if "db" not in g or not _alive(g.db):
        try:
            g.db = get_pooled_connection(autocommit=True)
        except mysql.connector.Error as db_err:
            print(f"[DB ERROR] MySQL connection failed! Error: {db_err}")
            raise
        except Exception as e:
            print(f"[GENERAL ERROR] Database connect failed: {e}")
            raise
    return g.db

# ------------------------------------------------------------
# RÉGI LOGIKAHOZ KOMPAT: helper függvények
# ------------------------------------------------------------

def expand_templates(raw: str) -> Dict[str, int]:
    """
    Az ID_sablon mező többféle alakban is jöhet (JSON):
      - ["A.btw", "A.btw", "B.btw"]               -> {"A.btw": 2, "B.btw": 1}
      - [["A.btw", 2], ["B.btw", 3]]             -> {"A.btw": 2, "B.btw": 3}
      - [{"file":"A.btw","count":2}, ...]        -> {"A.btw": 2, ...}
      - {"A.btw": 2, "B.btw": 1}                 -> változatlan
    Bármilyen is, visszatér egy név->darab mappinggel.
    """
    if not raw:
        return {}

    try:
        val = json.loads(raw)
    except Exception:
        # nem JSON? tekintsük egyetlen fájlnévnek
        if isinstance(raw, str) and raw.lower().endswith(".btw"):
            return {raw: 1}
        return {}

    counts: Counter[str] = Counter()

    if isinstance(val, dict):
        # már mapping
        for k, v in val.items():
            try:
                counts[str(k)] += int(v) if v is not None else 1
            except Exception:
                counts[str(k)] += 1
        return dict(counts)

    if isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                counts[item] += 1
            elif isinstance(item, (list, tuple)) and len(item) >= 1:
                name = str(item[0])
                qty = 1
                if len(item) >= 2:
                    try:
                        qty = int(item[1])
                    except Exception:
                        qty = 1
                counts[name] += max(qty, 0)
            elif isinstance(item, dict):
                name = item.get("file") or item.get("name") or item.get("template") or item.get("btw")
                qty = item.get("count") or item.get("qty") or 1
                if name:
                    try:
                        qty = int(qty)
                    except Exception:
                        qty = 1
                    counts[str(name)] += max(qty, 0)
        return dict(counts)

    # bármi más formátum: nincs
    return {}
def fetch_label_row(
    conn, pn: str, rev: Optional[str] = None, ecn: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    'etiket data' rekord lekérése:
      1) PN+REV+ECN pontos találat, ha mindhárom adott
      2) ha nincs ECN: PN+REV (legfrissebb ECN szerint)
      3) ha REV sincs: PN (legfrissebb REV/ECN szerint)
    Vissza: dict a hívónak szükséges mezőkkel.
    """
    cur = conn.cursor(dictionary=True)
    try:
        if pn and rev and ecn:
            cur.execute("""
                SELECT PN, REV, ECN, summary, ID_sablon, printers
                FROM `etiket data`
                WHERE PN=%s AND REV=%s AND ECN=%s
                LIMIT 1
            """, (pn, rev, ecn))
            row = cur.fetchone()
            if row:
                return row

        if pn and rev:
            cur.execute("""
                SELECT PN, REV, ECN, summary, ID_sablon, printers
                FROM `etiket data`
                WHERE PN=%s AND REV=%s
                ORDER BY ECN DESC
                LIMIT 1
            """, (pn, rev))
            row = cur.fetchone()
            if row:
                return row

        # teljes visszafelé kompatibilitás az eredeti viselkedéssel:
        cur.execute("""
            SELECT PN, REV, ECN, summary, ID_sablon, printers
            FROM `etiket data`
            WHERE PN=%s
            ORDER BY REV DESC, ECN DESC
            LIMIT 1
        """, (pn,))
        return cur.fetchone()
    finally:
        try: cur.close()
        except Exception: pass


def fetch_label_row_by_pn(conn, pn: str, rev: Optional[str] = None, ecn: Optional[str] = None):
    return fetch_label_row(conn, pn, rev, ecn)

def fetch_qty_for_wo(conn: mysql.connector.MySQLConnection, pn: str, wo: str) -> Optional[int]:
    """
    Mennyiséget ad vissza PN+WO alapján. A pontos forrástáblák projektfüggők,
    ezért több lehetséges lekérdezést is megpróbálunk, és az első találatot adjuk.
    Ha semmi nem található, None.
    """
    # 1) workstationworkorder (tipikus saját tábla)
    candidates: List[Tuple[str, Tuple[Any, ...]]] = [
        (
            "SELECT qty FROM workstationworkorder WHERE pn=%s AND wo=%s ORDER BY id DESC LIMIT 1",
            (pn, wo),
        ),
        (
            "SELECT QTY FROM workstationworkorder WHERE PN=%s AND WO=%s ORDER BY id DESC LIMIT 1",
            (pn, wo),
        ),
        # 2) t_dump (ha a WO is része a dumpnak)
        (
            "SELECT `QTY` FROM `t_dump` WHERE LOWER(`PART.NBR`)=%s AND LOWER(`WORK ORDER`)=LOWER(%s) ORDER BY 1 DESC LIMIT 1",
            (pn.lower(), wo),
        ),
        # 3) esetleg egy másik névváltozat
        (
            "SELECT qty FROM workorders WHERE pn=%s AND wo=%s ORDER BY created_at DESC LIMIT 1",
            (pn, wo),
        ),
    ]
    cur = conn.cursor()
    try:
        for sql, params in candidates:
            try:
                cur.execute(sql, params)
                r = cur.fetchone()
                if r and r[0] is not None:
                    try:
                        return int(r[0])
                    except Exception:
                        # ha nem int, de számnak látszik
                        try:
                            return int(float(r[0]))
                        except Exception:
                            pass
            except mysql.connector.Error:
                # tábla/mező nem létezik – próbáljuk a következőt
                continue
    finally:
        try:
            cur.close()
        except Exception:
            pass
    return None

# ------------------------------------------------------------
# Normalizált séma – kis upsert/lekérdező függvények
# ------------------------------------------------------------

def get_or_create_config_id(pn: str, rev: Optional[str], ecn: Optional[str]) -> int:
    conn = get_db()
    cur = conn.cursor()
    try:
        q_sel = (
            "SELECT id FROM pn_config "
            "WHERE pn=%s AND IFNULL(rev,'')=IFNULL(%s,'') AND IFNULL(ecn,'')=IFNULL(%s,'')"
        )
        cur.execute(q_sel, (pn, rev, ecn))
        row = cur.fetchone()
        if row:
            return row[0]

        q_ins = "INSERT INTO pn_config(pn,rev,ecn) VALUES(%s,%s,%s)"
        cur.execute(q_ins, (pn, rev, ecn))
        return cur.lastrowid
    finally:
        try:
            cur.close()
        except Exception:
            pass

def upsert_id_label(
    config_id: int,
    label_count: int,
    template_id: Optional[str],
    printer_id: Optional[str],
    side_label: Optional[str],
    group_label: Optional[str],
) -> None:
    conn = get_db()
    cur = conn.cursor()
    try:
        sql = """
            INSERT INTO id_label_settings
              (config_id,label_count,template_id,printer_id,side_label,group_label)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              label_count=VALUES(label_count),
              template_id=VALUES(template_id),
              printer_id=VALUES(printer_id),
              side_label=VALUES(side_label),
              group_label=VALUES(group_label)
        """
        cur.execute(sql, (config_id, label_count, template_id, printer_id, side_label, group_label))
    finally:
        try:
            cur.close()
        except Exception:
            pass

def upsert_ft_template(
    config_id: int,
    page: int,
    group: int,
    pair: int,
    side: str,
    template_id: Optional[str],
) -> int:
    """
    Visszaadja a pair_id-t. Ha létezik, frissíti a template_id-t.
    side: 'FROM' vagy 'TO'
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        sel = (
            "SELECT id FROM ft_pair "
            "WHERE config_id=%s AND page_no=%s AND group_no=%s AND pair_no=%s AND side=%s"
        )
        cur.execute(sel, (config_id, page, group, pair, side))
        row = cur.fetchone()
        if row:
            pair_id = row[0]
            upd = "UPDATE ft_pair SET template_id=%s WHERE id=%s"
            cur.execute(upd, (template_id, pair_id))
            return pair_id

        ins = (
            "INSERT INTO ft_pair(config_id,page_no,group_no,pair_no,side,template_id) "
            "VALUES (%s,%s,%s,%s,%s,%s)"
        )
        cur.execute(ins, (config_id, page, group, pair, side, template_id))
        return cur.lastrowid
    finally:
        try:
            cur.close()
        except Exception:
            pass

def upsert_ft_line(
    pair_id: int,
    line_pos: str,  # 'top' | 'bottom'
    connector_code: Optional[str],
    free_text: Optional[str],
) -> None:
    conn = get_db()
    cur = conn.cursor()
    try:
        sel = "SELECT id FROM ft_line WHERE pair_id=%s AND line_pos=%s"
        cur.execute(sel, (pair_id, line_pos))
        row = cur.fetchone()
        if row:
            upd = "UPDATE ft_line SET connector_code=%s, free_text=%s WHERE id=%s"
            cur.execute(upd, (connector_code, free_text, row[0]))
        else:
            ins = (
                "INSERT INTO ft_line(pair_id,line_pos,connector_code,free_text) "
                "VALUES (%s,%s,%s,%s)"
            )
            cur.execute(ins, (pair_id, line_pos, connector_code, free_text))
    finally:
        try:
            cur.close()
        except Exception:
            pass

def upsert_connector_group(config_id: int, page: int, group: int) -> int:
    conn = get_db()
    cur = conn.cursor()
    try:
        sel = "SELECT id FROM connector_group WHERE config_id=%s AND page_no=%s AND group_no=%s"
        cur.execute(sel, (config_id, page, group))
        row = cur.fetchone()
        if row:
            return row[0]
        ins = "INSERT INTO connector_group(config_id,page_no,group_no) VALUES (%s,%s,%s)"
        cur.execute(ins, (config_id, page, group))
        return cur.lastrowid
    finally:
        try:
            cur.close()
        except Exception:
            pass

def upsert_connector_item(
    group_id: int,
    ord_index: int,
    code: str,
    display_name: Optional[str],
) -> None:
    conn = get_db()
    cur = conn.cursor()
    try:
        ins = (
            "INSERT INTO connector_item(group_id,ord_index,code,display_name) "
            "VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE code=VALUES(code), display_name=VALUES(display_name)"
        )
        cur.execute(ins, (group_id, ord_index, code, display_name))
    finally:
        try:
            cur.close()
        except Exception:
            pass

def fetch_config_as_ui_json(config_id: int) -> Dict[str, Any]:
    """
    Az új normalizált táblákból visszaadja a mostani UI által várt struktúrát:
      {
        "id_label": {...} | None,
        "from_to": { page_no: { group_no: { pair_no: { "from":{...}, "to":{...} }}} },
        "connectors": { page_no: { group_no: [ {ord, code, display_name}, ... ] } }
      }
    """
    conn = get_db()
    data: Dict[str, Any] = {
        "id_label": None,
        "from_to": {},
        "connectors": {},
    }

    # id_label
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM id_label_settings WHERE config_id=%s", (config_id,))
        data["id_label"] = cur.fetchone()
    finally:
        try:
            cur.close()
        except Exception:
            pass

    # ft pairs + lines
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT p.id AS pair_id, p.page_no, p.group_no, p.pair_no, p.side, p.template_id,
                   l.line_pos, l.connector_code, l.free_text
            FROM ft_pair p
            LEFT JOIN ft_line l ON l.pair_id = p.id
            WHERE p.config_id=%s
            ORDER BY p.page_no, p.group_no, p.pair_no, p.side, l.line_pos
            """,
            (config_id,),
        )
        for r in cur:
            page = data["from_to"].setdefault(r["page_no"], {})
            group = page.setdefault(r["group_no"], {})
            pair = group.setdefault(r["pair_no"], {"from": {}, "to": {}})
            side = "from" if r["side"] == "FROM" else "to"
            node = pair[side]
            if "template_id" not in node:
                node["template_id"] = r["template_id"]
            if r.get("line_pos"):
                ln = node.setdefault(r["line_pos"], {})
                ln["connector_code"] = r.get("connector_code")
                ln["text"] = r.get("free_text")
    finally:
        try:
            cur.close()
        except Exception:
            pass

    # connectors
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT g.id AS group_id, g.page_no, g.group_no,
                   i.ord_index, i.code, i.display_name
            FROM connector_group g
            LEFT JOIN connector_item i ON i.group_id = g.id
            WHERE g.config_id=%s
            ORDER BY g.page_no, g.group_no, i.ord_index
            """,
            (config_id,),
        )
        for r in cur:
            page = data["connectors"].setdefault(r["page_no"], {})
            arr = page.setdefault(r["group_no"], [])
            if r["ord_index"] is not None:
                arr.append(
                    {
                        "ord": r["ord_index"],
                        "code": r["code"],
                        "display_name": r["display_name"],
                    }
                )
    finally:
        try:
            cur.close()
        except Exception:
            pass

    return data

# --- helper: sablonok összeszámolása az új summary JSON-ból ------------------
from collections import defaultdict

# services/bt_db.py (vagy ahol a függvény van)
def template_counts_from_summary(summary):
    """
    Visszaadja: { "<file.btw>": darab/egység }
    - ID: pages[*].id_labels[*].template
    - FROM/TO: pages[*].groups[*].pairs[*].from/to.template
    - KONNEKTOR ETIKETT: summary["connector_labels"][*].template   <-- ÚJ
    """
    import json
    counts = {}

    def add(tpl, n=1):
        if not tpl:
            return
        counts[tpl] = counts.get(tpl, 0) + int(n or 1)

    try:
        data = json.loads(summary) if isinstance(summary, str) else (summary or [])
    except Exception:
        data = summary or []

    # Ha dict: új formátum {"pages":[...], "connector_labels":[...]}
    pages = data.get("pages") if isinstance(data, dict) else data
    conn_labels = (data.get("connector_labels") or []) if isinstance(data, dict) else []

    # ID + FROM/TO
    for p in (pages or []):
        for lab in (p.get("id_labels") or []):
            add((lab.get("template") or "").strip(), 1)
        for g in (p.get("groups") or []):
            for pr in (g.get("pairs") or []):
                ft = (pr.get("from") or {}).get("template") or ""
                tt = (pr.get("to")   or {}).get("template") or ""
                add(ft.strip(), 1)
                add(tt.strip(), 1)

    # KONNEKTOR CINKEK
    for cl in (conn_labels or []):
        add((cl.get("template") or "").strip(), 1)

    # kidobjuk az üreseket
    return {k: v for k, v in counts.items() if k}
