# -*- coding: utf-8 -*-
from __future__ import annotations
import os
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, session, g, current_app
from werkzeug.utils import secure_filename

# Core funkciók (Excel -> preview token -> DB csere)
from services.hr_core import (
    read_excel_to_df,
    save_temp_df,
    load_temp_df,
    drop_and_replace_workers,
    sniff_db_columns,
)

# Auth/roles
from routes.auth import login_required, require_roles
from utils.roles import HR_ROLES, MANAGER_ROLES, IT_ROLES

# A projekt gyökerében lévő "templates" mappa
BP_TEMPLATES = str((Path(__file__).resolve().parents[1] / "templates"))

# Blueprint nyelvi prefixszel: /<lang>/human_resources
bp_human_resources = Blueprint(
    "human_resources",
    __name__,
    url_prefix="/<lang>/human_resources",
    template_folder=BP_TEMPLATES
)

@bp_human_resources.url_value_preprocessor
def pull_lang(endpoint, values):
    """Kiveszi a <lang> URL paramétert és g.lang-be teszi, csak HU/SK/EN-re enged."""
    lang = values.pop("lang", "hu")
    if lang not in {"hu", "sk", "en"}:
        lang = "hu"
    g.lang = lang

ALLOWED_EXT = {".xlsx", ".xls", ".csv"}


# ============= UI OLDAL =============
@bp_human_resources.get("/")
@login_required()
@require_roles(HR_ROLES | MANAGER_ROLES | IT_ROLES)
def hr_home():
    """
    HR főoldal – Emberek feltöltése Excelből.
    A sablont nyelvi alkönyvtárból tölti: templates/<lang>/hr_site.html
    """
    lang = getattr(g, "lang", "hu")
    return render_template(
        f"{lang}/hr_site.html",
        lang=lang,
        active="hr",
        user=session.get("user"),
    )


# ============= PREVIEW (feltöltés -> előnézet) =============
@bp_human_resources.post("/preview")
@login_required()
@require_roles(HR_ROLES | MANAGER_ROLES | IT_ROLES)
def hr_preview():
    """
    Fájlfeltöltés (Excel/CSV), server-side beolvasás pandas-szal,
    ideiglenes token mentése, 200 soros előnézet + oszloplista visszaadása.
    """
    file = request.files.get("file")
    if not file or file.filename.strip() == "":
        return jsonify({"ok": False, "error": "Nincs kiválasztott fájl."}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"ok": False, "error": f"Csak Excel/CSV engedélyezett: {', '.join(sorted(ALLOWED_EXT))}"}), 400

    _ = secure_filename(file.filename)  # opcionális log/trace-hez

    try:
        df = read_excel_to_df(file.stream, ext)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Beolvasási hiba: {e}"}), 400

    if df.shape[0] == 0 or df.shape[1] == 0:
        return jsonify({"ok": False, "error": "Üres fájl vagy nincs adat/fejléc."}), 400
    if df.shape[0] > 50000:
        return jsonify({"ok": False, "error": "Túl sok sor (max 50 000)."}), 400
    if df.shape[1] > 100:
        return jsonify({"ok": False, "error": "Túl sok oszlop (max 100)."}), 400

    # DB oszlopok – csak tájékoztatás a kliensnek
    db_cols = sniff_db_columns()

    # ideiglenes token létrehozása
    token = save_temp_df(df)

    # 200 soros előnézet + oszlopnevek
    preview_rows = min(200, len(df))
    preview_data = df.head(preview_rows).fillna("").astype(str).values.tolist()
    columns = list(df.columns.astype(str))

    return jsonify({
        "ok": True,
        "token": token,
        "columns": columns,
        "preview": preview_data,
        "total_rows": int(df.shape[0]),
        "db_columns": db_cols,
    })


# ============= COMMIT (DB csere) =============
@bp_human_resources.post("/commit")
@login_required()
@require_roles(HR_ROLES | MANAGER_ROLES | IT_ROLES)
def hr_commit():
    """
    A preview során mentett token alapján betölti az ideiglenes DataFrame-et,
    majd TRUNCATE + tömeges INSERT a workers táblába.
    """
    token = (request.form.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Hiányzó token (előnézet nélkül nem tölthető fel)."}), 400

    df = load_temp_df(token)
    if df is None:
        return jsonify({"ok": False, "error": "A feltöltés ideiglenes adata nem található vagy lejárt."}), 400

    try:
        affected = drop_and_replace_workers(df)
        return jsonify({"ok": True, "inserted": int(affected)})
    except Exception as e:
        # részletes stack a szerverlogba, felhasználónak rövid
        current_app.logger.exception("HR commit error")
        return jsonify({"ok": False, "error": f"Sikertelen feltöltés: {e}"}), 500
