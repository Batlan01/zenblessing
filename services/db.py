import mysql.connector
from flask import g

from services.dbpool import get_pooled_connection


def _alive(db) -> bool:
    """Igaz, ha a (pooled) kapcsolat még használható."""
    try:
        # PooledMySQLConnection: explicit close() után a belső _cnx None
        if getattr(db, "_cnx", True) is None:
            return False
        return db.is_connected()
    except Exception:
        return False


def get_db():
    """
    Kérésenként egy kapcsolat a közös poolból (g.db-ben tartva).
    A korábbi viselkedéssel kompatibilis: autocommit=ON.
    A teardown_db a kérés végén visszaadja a poolba.
    """
    if 'db' not in g or not _alive(g.db):
        try:
            g.db = get_pooled_connection(autocommit=True)
        except mysql.connector.Error as db_err:
            print(f"[DB ERROR] MySQL connection failed! Error: {db_err}")
            raise
        except Exception as e:
            print(f"[GENERAL ERROR] Database connect failed: {e}")
            raise
    return g.db


def teardown_db(exception):
    db = g.pop('db', None)
    if db is None:
        return
    # Ha a route már explicit close()-olta (visszaadta a poolba), ne zárjuk újra:
    # a dupla visszaadás felesleges új kapcsolatot nyitna / hibát dobna.
    if getattr(db, "_cnx", True) is None:
        return
    try:
        db.close()  # pooled kapcsolat: visszaadás a poolba
    except Exception as e:
        print(f"[DB ERROR] Failed to close DB connection: {e}")
