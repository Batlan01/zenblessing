# routes/qr_injector.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import zipfile
from pathlib import Path
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, send_file,
    flash, current_app, session, g, jsonify
)
from werkzeug.utils import secure_filename

from routes.auth import login_required, require_roles, IT_ROLES, STORE_ROLES
from services.qr_core import QRProgramCore

qr_bp = Blueprint("qr_injector", __name__, url_prefix="/<lang>/qr-injector")

ALLOWED = {"txt", "xls", "xlsx"}
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED


def _is_fetch_request() -> bool:
    # A fetch általában ad: Sec-Fetch-Mode: cors / same-origin
    # illetve te is adhatsz neki X-Requested-With header-t (lásd lent).
    return (
        request.headers.get("X-Requested-With", "").lower() in ("fetch", "xmlhttprequest")
        or request.headers.get("Sec-Fetch-Mode", "").lower() in ("cors", "same-origin")
    )


def _fail(message: str, code: int = 400):
    if _is_fetch_request():
        return jsonify(error=message), code
    flash(message, "danger")
    return redirect(url_for("qr_injector.form", lang=g.lang))


def _validate_docx(path: str):
    # docx = zip; gyors sanity check
    with zipfile.ZipFile(path, "r") as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError(f"Hibás DOCX csomag (zip): {bad}")


@qr_bp.route("/", methods=["GET"])
@login_required()
@require_roles(IT_ROLES, STORE_ROLES)
def form(lang: str):
    g.lang = lang if lang in ("hu", "sk") else "hu"
    return render_template(
        f"{g.lang}/qr_injector.html",
        lang=g.lang,
        active="qr_injector",
        user=session.get("user"),
    )


@qr_bp.route("/process", methods=["POST"])
@login_required()
@require_roles(IT_ROLES, STORE_ROLES)
def process(lang: str):
    g.lang = lang if lang in ("hu", "sk") else "hu"

    f = request.files.get("wo_file")
    if not f or not f.filename:
        return _fail("Nincs kiválasztott fájl.", 400)

    if not _allowed(f.filename):
        return _fail("Csak .txt, .xls vagy .xlsx tölthető fel.", 400)

    # input mentés
    orig_name = Path(f.filename).name  # pl. WO.PRINT-LJ....TXT
    safe_name = secure_filename(orig_name)

    workdir = os.path.join(current_app.instance_path, "qr_uploads")
    os.makedirs(workdir, exist_ok=True)

    in_path = os.path.join(workdir, f"{datetime.now():%Y%m%d_%H%M%S}_{safe_name}")
    f.save(in_path)

    preview_only = bool(request.form.get("preview"))

    # A QRProgramCore konstruktora DB kapcsolatot nyit – ha a MySQL/pool nem
    # elérhető, az itt dobott kivétel korábban a try-on KÍVÜL keletkezett, így
    # nyers 500-as HTML hibaoldal ment vissza a JSON hibaüzenet helyett.
    core = None

    try:
        core = QRProgramCore(db_cfg=current_app.config.get("DB_CFG"))

        out_docx = core.process_file(in_path, enable_word_com=False, preview=preview_only)

        if not out_docx or not os.path.exists(out_docx):
            raise FileNotFoundError(f"Kimeneti DOCX nem található: {out_docx}")

        _validate_docx(out_docx)

        download_name = f"{Path(orig_name).stem}.docx"  # ugyanaz a név, csak .docx
        return send_file(
            out_docx,
            as_attachment=True,
            download_name=download_name,
            mimetype=DOCX_MIME,
        )

    except ValueError as e:
        # a feltöltött fájllal van baj (rossz formátum / nincs benne WO adat) –
        # ez felhasználói hiba, nem szerverhiba
        current_app.logger.warning("QR feldolgozás – hibás input: %s", e)
        return _fail(str(e), 400)

    except Exception as e:
        current_app.logger.exception("QR feldolgozás hiba")
        return _fail(f"Hiba történt: {e}", 500)

    finally:
        # DB kapcsolat visszaadása a poolba
        if core is not None:
            try:
                core.close()
            except Exception:
                pass
        # opcionális: ne szemeteljen (a feltöltött input és a belőle
        # előállított köztes .txt; a kimeneti .docx-et a send_file olvassa)
        for tmp_path in {in_path, os.path.splitext(in_path)[0] + ".txt"}:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
