# services/hr_core.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import io
import csv
import re
import uuid
import pickle
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
from services.db import get_db

# ---------------------------------------------------------------------------
# Temp hely (preview tokenek + ide konvertált .xlsx)
# ---------------------------------------------------------------------------
TMP_DIR = Path(os.environ.get("HR_UPLOAD_TMP", "/tmp/hr_uploads"))
TMP_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Excel kompatibilitás – illegális XML vezérlőkarakterek kiszűrése
# ---------------------------------------------------------------------------
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]")  # \t,\n,\r kivétel

def _clean_cell(val) -> str:
    s = "" if val is None else str(val)
    return _ILLEGAL_XML_RE.sub("", s.replace("\ufeff", ""))

def _clean_columns(cols: List[str]) -> List[str]:
    base = [(_clean_cell(c).strip() or "col") for c in cols]
    seen: dict[str, int] = {}
    out: List[str] = []
    for c in base:
        if c in seen:
            seen[c] += 1
            out.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 1
            out.append(c)
    return out

def _safe_sheet_name(name: str = "data") -> str:
    bad = set(':\\/?*[]')
    nm = "".join(ch for ch in (name or "data") if ch not in bad).strip() or "data"
    return nm[:31]

# ---------------------------------------------------------------------------
# Fájltípus szaglás (mágikus bájtok alapján)
# ---------------------------------------------------------------------------
def _sniff_kind(raw: bytes, ext_hint: str) -> str:
    """'xlsx' | 'xls' | 'csv'"""
    if raw.startswith(b"PK\x03\x04"):
        return "xlsx"
    if raw[:8].startswith(b"\xD0\xCF\x11\xE0"):
        return "xls"
    e = (ext_hint or "").lower()
    if e in (".xlsx", ".xls"):
        return e.lstrip(".")
    return "csv"

def _excel_bytes_to_df(raw: bytes, kind: str) -> pd.DataFrame:
    bio = io.BytesIO(raw)
    if kind == "xlsx":
        df = pd.read_excel(bio, dtype=object, engine="openpyxl")
    else:  # xls -> xlrd 1.2.0 szükséges
        df = pd.read_excel(bio, dtype=object, engine="xlrd")
    df.columns = _clean_columns([_clean_cell(c) for c in df.columns])
    for c in df.columns:
        df[c] = df[c].map(_clean_cell)
    return df

# ---------------------------------------------------------------------------
# DB oszlopok (UI buborékokhoz)
# ---------------------------------------------------------------------------
def sniff_db_columns(table: str = "workers") -> List[str]:
    try:
        conn = get_db()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(f"SHOW COLUMNS FROM `{table}`;")
            return [row["Field"] for row in cur.fetchall()]
        finally:
            cur.close()
    except Exception:
        return []

# ---------------------------------------------------------------------------
# CSV -> DataFrame (robosztus olvasó: encoding + delimiter sniff)
# ---------------------------------------------------------------------------
def _pick_sep(sample_text: str) -> str:
    cands = [';', ',', '\t', '|']
    lines = [ln for ln in sample_text.split('\n') if ln.strip()][:50] or ['']
    best, score_best = ',', -1
    for sep in cands:
        counts = [ln.count(sep) for ln in lines]
        if not counts:
            continue
        avg = sum(counts)/len(counts)
        var = sum((x-avg)**2 for x in counts)/len(counts)
        score = avg - (var ** 0.5)
        if score > score_best:
            best, score_best = sep, score
    return best

def _first_row_looks_like_header(df: pd.DataFrame) -> bool:
    if df.shape[0] == 0:
        return False
    first = [_clean_cell(x) for x in df.iloc[0].tolist()]
    if all(x.strip() == "" for x in first):
        return False
    numericish = sum(1 for x in first if x.strip().replace(".", "", 1).isdigit())
    if numericish > max(1, len(first)//2):
        return False
    if len(set(x.strip() for x in first)) <= max(1, len(first)//3):
        return False
    return True

def _csv_bytes_to_dataframe(raw: bytes) -> pd.DataFrame:
    raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    encodings = ("utf-8-sig","utf-8","cp1250","iso-8859-2","windows-1250","latin-1","windows-1252")
    for enc in encodings:
        try:
            text = raw.decode(enc)
        except Exception:
            continue
        sample = text[:100_000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,|\t")
            sep = dialect.delimiter
        except Exception:
            sep = _pick_sep(text)
        try:
            reader = csv.reader(io.StringIO(text), delimiter=sep, quotechar='"', escapechar='\\')
            rows = [row for row in reader]
            if not rows:
                continue
            if len(rows) >= 2 and csv.Sniffer().has_header(sample):
                header = _clean_columns([_clean_cell(c).strip() for c in rows[0]])
                data = [[_clean_cell(x) for x in r] for r in rows[1:]]
                df = pd.DataFrame(data, columns=header)
            else:
                df = pd.DataFrame([[ _clean_cell(x) for x in r ] for r in rows])
                if len(df) >= 2 and _first_row_looks_like_header(df):
                    header = _clean_columns([_clean_cell(c).strip() for c in df.iloc[0].tolist()])
                    df = df.drop(df.index[0]).reset_index(drop=True)
                    df.columns = header
            for c in df.columns:
                df[c] = df[c].astype(str)
            return df
        except Exception:
            try:
                df = pd.read_csv(io.StringIO(text), sep=sep, engine="python",
                                 quoting=csv.QUOTE_MINIMAL, quotechar='"', escapechar='\\',
                                 on_bad_lines="skip", dtype=object)
                df.columns = _clean_columns([_clean_cell(c) for c in df.columns])
                for c in df.columns:
                    df[c] = df[c].map(_clean_cell)
                return df
            except Exception:
                pass
    # végső fallback
    text = raw.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text), delimiter=_pick_sep(text), quotechar='"', escapechar='\\')
    rows = [[_clean_cell(x) for x in r] for r in reader]
    df = pd.DataFrame(rows)
    if df.shape[1] > 0:
        df.columns = _clean_columns([_clean_cell(c) for c in df.columns])
    for c in df.columns:
        df[c] = df[c].map(_clean_cell)
    return df

# ---------------------------------------------------------------------------
# Feltöltött fájl -> egységes, tiszta XLSX + DataFrame (autodetekció)
# ---------------------------------------------------------------------------
def convert_upload_to_xlsx(file_like, ext_hint: str) -> Tuple[pd.DataFrame, Path]:
    """
    CSV/XLS/XLSX → DF + normalizált .xlsx a TMP_DIR-ben.
    """
    raw = file_like.read()
    kind = _sniff_kind(raw, ext_hint or "")
    sheet = _safe_sheet_name("data")
    tmp_xlsx = TMP_DIR / f"{uuid.uuid4().hex}.xlsx"

    if kind in ("xlsx", "xls"):
        df = _excel_bytes_to_df(raw, kind)
    else:
        df = _csv_bytes_to_dataframe(raw)
        if not df.empty:
            df = df[~(df.apply(lambda r: all((_clean_cell(x).strip()=="" for x in r)), axis=1))].reset_index(drop=True)
        df.columns = _clean_columns([_clean_cell(c) for c in df.columns])
        for c in df.columns:
            df[c] = df[c].map(_clean_cell)

    df.to_excel(tmp_xlsx, index=False, sheet_name=sheet)
    return df, tmp_xlsx

def read_excel_to_df(file_like, ext: str) -> pd.DataFrame:
    df, _ = convert_upload_to_xlsx(file_like, ext)
    return df

# ---------------------------------------------------------------------------
# Preview token (DF + audit .xlsx)
# ---------------------------------------------------------------------------
def save_temp_df(df: pd.DataFrame) -> str:
    token = uuid.uuid4().hex
    with open(TMP_DIR / f"{token}.pkl", "wb") as f:
        pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)
    try:
        clean_df = df.copy()
        clean_df.columns = _clean_columns([_clean_cell(c) for c in clean_df.columns])
        for c in clean_df.columns:
            clean_df[c] = clean_df[c].map(_clean_cell)
        clean_df.to_excel(TMP_DIR / f"{token}.xlsx", index=False, sheet_name=_safe_sheet_name("data"))
    except Exception:
        pass
    return token

def load_temp_df(token: str) -> Optional[pd.DataFrame]:
    p = TMP_DIR / f"{token}.pkl"
    if not p.is_file():
        return None
    with open(p, "rb") as f:
        obj = pickle.load(f)
    return obj if isinstance(obj, pd.DataFrame) else obj

# ---------------------------------------------------------------------------
# MAPPING: feltöltött DF → workers(id, name, rfid_tag)
# ---------------------------------------------------------------------------
def _norm_header(s: str) -> str:
    s = _clean_cell(s).lower()
    return " ".join(s.replace("_", " ").split())

_ALIAS = {
    "id": {"user id", "userid", "employee id", "emp id", "id"},
    "name": {"user name", "username", "name", "employee name", "full name", "fullname"},
    "rfid_tag": {
        "card no", "card number", "cardno", "rfid", "rfid tag", "tag",
        "badge", "badge id", "badge number", "access card", "card"
    },
}

def _is_int_like(s: str) -> bool:
    s = (_clean_cell(s) or "").strip()
    if s.startswith("+"):
        s = s[1:]
    return s.isdigit()

def _map_to_workers_schema(df: pd.DataFrame) -> pd.DataFrame:
    src_norm = {col: _norm_header(col) for col in df.columns}
    sel: dict[str, str] = {}
    for target, aliases in _ALIAS.items():
        pick = None
        for col, norm in src_norm.items():
            if norm in aliases:
                pick = col
                break
        if pick:
            sel[target] = pick

    missing = [t for t in ("id", "name", "rfid_tag") if t not in sel]
    if missing:
        seen = ", ".join(sorted(set(src_norm.values())))
        raise ValueError(
            f"Hiányzó kötelező oszlop(ok): {', '.join(missing)}. Felismert fejlécek: {seen}"
        )

    out = df[[sel["id"], sel["name"], sel["rfid_tag"]]].copy()
    out.columns = ["id", "name", "rfid_tag"]

    # tisztítás
    for c in out.columns:
        out[c] = out[c].map(_clean_cell)

    # üres sorok eldobása
    out = out[~(out[["id","name","rfid_tag"]]
                .apply(lambda r: all((str(x or "").strip()=="" for x in r)), axis=1))]

    # nem numerikus id-k eldobása + int konverzió
    out = out[out["id"].apply(_is_int_like)]
    out["id"] = out["id"].map(lambda s: int(str(s).strip().lstrip("+")))

    # rfid_tag marad sztring (vezető nullák megőrzése)
    out["rfid_tag"] = out["rfid_tag"].astype(str).str.strip()

    # --- deduplikálás ---
    # ID duplák kiszűrése (első marad)
    out = out.drop_duplicates(subset=["id"], keep="first")
    # rfid_tag duplák kiszűrése (első marad)
    out = out.drop_duplicates(subset=["rfid_tag"], keep="first").reset_index(drop=True)

    return out

# ---------------------------------------------------------------------------
# Teljes csere a workers táblában – gyors, batch-elt, FK-barát
# ---------------------------------------------------------------------------
def drop_and_replace_workers(df: pd.DataFrame, table: str = "workers") -> int:
    """
    Biztonságos csere + upsert:
      - sémára igazítás + szűrés (id int, name str, rfid_tag str)
      - FK letiltás a sessionben
      - explicit DELETE FROM + AUTO_INCREMENT reset (TRUNCATE helyett)
      - INSERT ... ON DUPLICATE KEY UPDATE (ütközés esetén frissít)
      - batch-elt beszúrás
    """
    if df is None or df.empty:
        raise ValueError("Üres DataFrame nem tölthető fel.")

    df = _map_to_workers_schema(df)  # -> id:int, name:str, rfid_tag:str
    rows = [(row.id, (row.name or "").strip(), (row.rfid_tag or "").strip())
            for row in df.itertuples(index=False)]

    conn = get_db()
    cur = conn.cursor()
    try:
        # rövid lock timeout + FK letiltás a sessionre
        try: cur.execute("SET SESSION lock_wait_timeout = 5")
        except Exception: pass
        try: cur.execute("SET SESSION FOREIGN_KEY_CHECKS = 0")
        except Exception: pass

        # teljes ürítés – DELETE (FK-mentesen), majd AI reset
        cur.execute(f"DELETE FROM `{table}`")
        try:
            cur.execute(f"ALTER TABLE `{table}` AUTO_INCREMENT = 1")
        except Exception:
            pass  # ha nincs AI, nem baj

        # upsert beszúrás
        if rows:
            sql = (
                f"INSERT INTO `{table}` (`id`,`name`,`rfid_tag`) "
                f"VALUES (%s,%s,%s) "
                f"ON DUPLICATE KEY UPDATE "
                f"`name`=VALUES(`name`), `rfid_tag`=VALUES(`rfid_tag`)"
            )
            BATCH = 200
            for i in range(0, len(rows), BATCH):
                cur.executemany(sql, rows[i:i+BATCH])

        # FK vissza
        try: cur.execute("SET SESSION FOREIGN_KEY_CHECKS = 1")
        except Exception: pass

        return len(rows)
    finally:
        try: cur.close()
        except Exception: pass

