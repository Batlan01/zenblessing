# routes/vnc.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
import atexit
from functools import wraps

from flask import (
    Blueprint, abort, jsonify, redirect,
    render_template, request, session, url_for,
)

from services.db import get_db

log = logging.getLogger(__name__)

vnc_bp = Blueprint("vnc", __name__, url_prefix="/<lang>/vnc")

# ── Konfiguráció ──────────────────────────────────────────────────────────────
VNC_PORT   = 5900          # minden Pi-n ugyanez
WS_PORT_BASE = 6080        # ws_port = WS_PORT_BASE + db_id  (pl. id=1 → 6081)
NOVNC_PATH = r"C:\Users\ntrencik_adm\Documents\Flask Webserver 2026-2-3\static\novnc"

# ── DB betöltés ───────────────────────────────────────────────────────────────
def _load_devices_from_db() -> dict[str, dict]:
    """
    Betölti az eszközöket a raspberrydevices táblából.
    Visszatér: { device_key: { name, host, vnc_port, ws_port, icon, location } }
    """
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT id, DEVICE_ID, DEVICE_NAME FROM raspberrydevices ORDER BY id")
        rows = cur.fetchall()
        cur.close()
    except Exception:
        log.exception("Nem sikerült betölteni a raspberrydevices táblát")
        return {}

    devices = {}
    for row in rows:
        db_id       = int(row["id"])
        host        = (row["DEVICE_ID"] or "").strip()
        name        = (row["DEVICE_NAME"] or "").strip() or host
        key         = f"pi-{db_id}"          # egyedi kulcs: pi-1, pi-2, …

        devices[key] = {
            "name":     name,
            "host":     host,
            "vnc_port": VNC_PORT,
            "ws_port":  WS_PORT_BASE + db_id,  # 6081, 6082, … (egyedi!)
            "icon":     "fa-desktop",
            "location": "",                    # nincs külön oszlop
        }

    log.info("VNC: %d eszköz betöltve az adatbázisból", len(devices))
    return devices


def get_devices() -> dict[str, dict]:
    """
    Mindig friss adatot kér a DB-ből.
    Ha cache kell, itt lehet bevezetni (pl. 60mp TTL).
    """
    return _load_devices_from_db()


# ── Websockify folyamat-kezelő ────────────────────────────────────────────────
_procs: dict[str, subprocess.Popen] = {}
_lock  = threading.Lock()


def _port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_bridge(device_id: str, dev: dict) -> bool:
    """Elindítja a websockify bridge-et. True ha sikeresen fut."""
    with _lock:
        proc = _procs.get(device_id)
        if proc and proc.poll() is None:
            return True

        if not _port_open(dev["host"], dev["vnc_port"]):
            log.warning("VNC nem érhető el: %s:%s", dev["host"], dev["vnc_port"])
            return False


        cmd = [
            "websockify",
            f"0.0.0.0:{dev['ws_port']}",
            f"{dev['host']}:{dev['vnc_port']}",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _procs[device_id] = proc
            time.sleep(0.5)
            ok = proc.poll() is None
            if ok:
                log.info("websockify elindult: %s → :%s", device_id, dev["ws_port"])
            return ok
        except FileNotFoundError:
            log.error("websockify nincs telepítve – pip install websockify")
            return False


def _stop_bridge(device_id: str) -> None:
    with _lock:
        proc = _procs.pop(device_id, None)
        if proc and proc.poll() is None:
            proc.terminate()


def _device_status(device_id: str, dev: dict) -> dict:
    vnc_ok  = _port_open(dev["host"], dev["vnc_port"])
    proc    = _procs.get(device_id)
    ws_live = bool(proc and proc.poll() is None)
    return {
        "online":        vnc_ok,
        "vnc_reachable": vnc_ok,
        "ws_running":    ws_live,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────
def _login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            lang = kwargs.get("lang", "sk")
            return redirect(url_for("auth.login", lang=lang))
        return f(*args, **kwargs)
    return wrapper


# ── Oldalak ───────────────────────────────────────────────────────────────────
@vnc_bp.route("/")
@_login_required
def dashboard(lang: str):
    devices = get_devices()
    return render_template(
        "hu/vnc.html",
        devices=devices,
        lang=lang,
        active="vnc",
        user=session.get("user"),
    )


@vnc_bp.route("/view/<device_id>")
@_login_required
def viewer(lang: str, device_id: str):
    devices = get_devices()
    dev = devices.get(device_id)
    if not dev:
        abort(404)

    started     = _start_bridge(device_id, dev)
    server_host = request.host.split(":")[0]
    novnc_url = (
    f"/static/novnc/vnc.html"
    f"?host={server_host}&port={dev['ws_port']}"
    f"&autoconnect=true&resize=scale"
)

    return render_template(
        "hu/vnc_viewer.html",
        device=dev,
        device_id=device_id,
        novnc_url=novnc_url,
        ws_ready=started,
        lang=lang,
        active="vnc",
        user=session.get("user"),
    )


# ── API ───────────────────────────────────────────────────────────────────────
@vnc_bp.route("/api/status")
@_login_required
def api_status(lang: str):
    devices = get_devices()
    return jsonify({
        did: _device_status(did, dev)
        for did, dev in devices.items()
    })


@vnc_bp.route("/api/connect/<device_id>", methods=["POST"])
@_login_required
def api_connect(lang: str, device_id: str):
    devices = get_devices()
    dev = devices.get(device_id)
    if not dev:
        return jsonify({"ok": False, "error": "ismeretlen eszköz"}), 404
    ok = _start_bridge(device_id, dev)
    return jsonify({"ok": ok})


@vnc_bp.route("/api/disconnect/<device_id>", methods=["POST"])
@_login_required
def api_disconnect(lang: str, device_id: str):
    _stop_bridge(device_id)
    return jsonify({"ok": True})


@vnc_bp.route("/api/diag/<device_id>")
@_login_required
def api_diag(lang: str, device_id: str):
    """
    Diagnosztika (a kapcsolat-logikát NEM érinti): megmondja, hol akad el.
    Böngészőből: /<lang>/vnc/api/diag/<device_id>
    """
    import shutil
    devices = get_devices()
    dev = devices.get(device_id)
    if not dev:
        return jsonify({"ok": False, "error": "ismeretlen eszköz"}), 404

    ws_port = int(dev["ws_port"])
    proc = _procs.get(device_id)
    ws_listening = _port_open("127.0.0.1", ws_port, timeout=1.0)
    pi_ok = _port_open(dev["host"], dev["vnc_port"])
    wsk = shutil.which("websockify") is not None

    if ws_listening and pi_ok:
        hint = ("Szerver oldalon OK (bridge figyel, Pi elérhető). Ha a böngésző mégis "
                f"'Failed to connect', az a böngésző→szerver WebSocket a {ws_port} porton "
                "– valószínűleg TŰZFAL, vagy a böngésző más gépről nem éri el a portot.")
    elif not wsk:
        hint = "A websockify nincs telepítve a szerveren (pip install websockify)."
    elif not pi_ok:
        hint = f"A Pi VNC ({dev['host']}:{dev['vnc_port']}) nem elérhető a szerverről."
    elif not ws_listening:
        hint = "A bridge nem figyel a porton (a websockify nem indult el / azonnal leállt)."
    else:
        hint = "Ismeretlen állapot."

    return jsonify({
        "device":               device_id,
        "pi_host":              dev["host"],
        "vnc_port":             dev["vnc_port"],
        "ws_port":              ws_port,
        "websockify_installed": wsk,
        "pi_vnc_reachable":     pi_ok,
        "ws_port_listening":    ws_listening,
        "proc_alive":           bool(proc and proc.poll() is None),
        "hint":                 hint,
    })


# ── Cleanup ───────────────────────────────────────────────────────────────────
@atexit.register
def _cleanup_all():
    for did in list(_procs):
        _stop_bridge(did)