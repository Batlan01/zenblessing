# routes/scan_injector.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from flask import Blueprint, render_template, request, session, jsonify, g
from datetime import datetime
from routes.auth import login_required, require_roles, IT_ROLES, QC_ROLES, MANAGER_ROLES, TEST_ROLES

from services.scan_core import (
    ensure_db, convert_to_slovak, compute_target,
    fetch_worker_by_rfid, fetch_worker_by_rfid_multi,
    worker_has_active_login, create_worker_login_row, ensure_worker_login_row,
    get_client_identity,
    parse_qr_and_route, complete_active_wo_with_qty,
    logout_worker_everywhere, fetch_worker_name,
    fetch_worker_last_state, fetch_related_qr_codes,
)
from services.errors import log_injector_error
from services.ssh import (   # <<< *003 WiFi, *004/*005 gombtiltás, *006/*007 watchdog, *008/*009 figyelő
    ssh_exec,
    run_script_as_root,
    get_pi_credentials,
    install_browser_key_block,
    remove_browser_key_block,
    install_kiosk_watchdog,
    remove_kiosk_watchdog,
    install_pi_command_listener,
    remove_pi_command_listener,
)
from services.notification_core import socketio  # WO esemény push a dashboardoknak


def _emit_wo_update(kind: str, payload: dict | None = None):
    """
    SocketIO push a dashboardoknak, hogy frissüljenek (polling helyett).
    Hiba esetén csendben elnyeljük – a szkennelés attól még működjön.
    """
    try:
        data = {"kind": kind}
        if payload:
            for k in ("wo", "work_id", "mode"):
                if payload.get(k) is not None:
                    data[k] = payload[k]
        socketio.emit("wo_update", data)
    except Exception:
        pass


bp_scan = Blueprint("scan", __name__, url_prefix="/<lang>/scan")

# SSH belépő a .env-ből (PI_SSH_USER / PI_SSH_PASS), alapértelmezés: user/user
_SSH_USER, _SSH_PASS = get_pi_credentials()


@bp_scan.url_value_preprocessor
def pull_lang(endpoint, values):
    g.lang = values.pop('lang', 'hu')


def _get_or_make_device():
    """
    Eszköznév + IP a kliens kérésből.
    Kérésenként CSAK EGYSZER futtatjuk le: az eredményt a flask.g-ben
    tartjuk. (A feloldás DB-t – és korábban blokkoló reverse DNS-t – érint,
    ezt fölösleges ugyanazon a kérésen belül többször megfizetni.)
    """
    cached = getattr(g, "_scan_device", None)
    if cached:
        return cached

    dev_name, dev_ip = get_client_identity(request)
    session['device_name'] = dev_name
    session['raspberry_id'] = dev_ip
    g._scan_device = (dev_name, dev_ip)
    return dev_name, dev_ip


# ============= SPECIÁLIS ADMIN PARANCSOK (*00x) =============
_SPECIAL_CMDS = {
    "*001": "Eszközazonosítás",
    "*002": "Oldal újratöltés (F5)",
    "*003": "WiFi újraindítás",
    "*004": "Home/böngésző billentyűk tiltása (alapból automatikusan tiltva)",
    "*005": "Home/böngésző billentyűk visszaengedélyezése + auto-tiltás kikapcsolása az eszközön",
    "*006": "Kiosk watchdog telepítése (automatikusan is települ)",
    "*007": "Kiosk watchdog eltávolítása + auto-telepítés tiltása ezen az eszközön",
    "*008": "*00x parancsfigyelő telepítése/frissítése (automatikusan is települ)",
    "*009": "*00x parancsfigyelő eltávolítása + auto-telepítés tiltása ezen az eszközön",
}


def _cooldown_wrap(num: str, action: str) -> str:
    """
    Közös zár a Pi-n futó *00x parancsfigyelővel (pi_cmd_listener): ha ugyanaz
    a parancs 15 mp-en belül már lefutott az eszközön (bármelyik irányból),
    nem futtatjuk le még egyszer. A figyelő ugyanezeket a /tmp/picmd_<szám>
    fájlokat használja.
    """
    f = f"/tmp/picmd_{num}"
    sep = " " if action.rstrip().endswith("&") else "; "
    return (
        f'F="{f}"; NOW=$(date +%s); '
        f'if [ -f "$F" ] && [ $((NOW - $(stat -c %Y "$F"))) -lt 15 ]; then echo SKIPPED_RECENT; '
        f'else touch "$F"; {action}{sep}fi'
    )

# A gombtiltás (udev hwdb) a services/ssh.py-ban van (install_browser_key_block),
# mert az automatikus provisioning is ugyanazt használja – alapértelmezetten
# minden eszközön tiltva vannak a böngésző-gombok, reboot után is.

def _handle_special_cmd(raw: str, dev_name: str, dev_ip: str):
    cmd = raw.strip().lower()

    if not cmd.startswith("*"):
        return None

    # *001 – eszköz neve + IP-je + a telepített komponensek állapota
    if cmd == "*001":
        msg = f"Eszközinformáció\nNév:  {dev_name}\nIP:   {dev_ip}"
        try:
            status_script = (
                'L=$(systemctl is-active pi-cmd-listener 2>/dev/null); '
                'W=$([ "$(crontab -l 2>/dev/null | grep -c kiosk_watchdog)" -gt 0 ] && echo van || echo nincs); '
                'K=$([ -f /etc/udev/hwdb.d/99-scan-disable-browser-keys.hwdb ] && echo tiltva || echo aktiv); '
                'P=$({ command -v zenity || command -v xmessage; } >/dev/null 2>&1 && echo van || echo nincs); '
                'B=$(pgrep -f chromium_kiosk >/dev/null 2>&1 && echo fut || echo nem-fut); '
                'N=$(cat /sys/class/leds/*numlock*/brightness 2>/dev/null | grep -q 1 && echo be || echo ki); '
                'echo "STATUS figyelo:${L:-nincs} watchdog:$W home-gomb:$K popup-eszkoz:$P kiosk:$B numlock:$N"'
            )
            okd, outd, errd, _rc = ssh_exec(dev_ip, _SSH_USER, _SSH_PASS, status_script, timeout=8)
            line = next((l for l in (outd or "").splitlines() if l.startswith("STATUS ")), None)
            if okd and line:
                # kulcs:érték párok külön sorban, olvashatóan
                msg += "\n" + "\n".join(line.replace("STATUS ", "").split())
            else:
                msg += "\n(Állapot SSH-n nem elérhető)"
        except Exception:
            msg += "\n(Állapot SSH-n nem elérhető)"
        return jsonify(ok=True, action="cmd_result", cmd="*001", message=msg)

    # *002 – oldal újratöltés (F5)
    if cmd == "*002":
        return jsonify(ok=True, action="reload", cmd="*002", message="Oldal újratöltés...")

    # *003 – WiFi újraindítás SSH-n keresztül (rootként fut, jelszavas sudo-val is működik)
    if cmd == "*003":
        try:
            ok, out, err, _rc = run_script_as_root(
                dev_ip, _SSH_USER, _SSH_PASS,
                _cooldown_wrap(
                    "003",
                    'nohup bash -c "sleep 2 && systemctl restart wpa_supplicant && dhclient wlan0" >/dev/null 2>&1 &',
                ),
                timeout=20,
            )
            if "SKIPPED_RECENT" in (out or ""):
                msg = f"WiFi újraindítás az imént már lefutott ({dev_name} @ {dev_ip}), nem futtattuk duplán."
            else:
                msg = f"WiFi újraindítás elküldve ({dev_name} @ {dev_ip}).\nAz eszköz pár másodperc múlva visszacsatlakozik."
        except Exception as e:
            return jsonify(ok=False, msg=f"WiFi restart hiba: {e}")
        return jsonify(ok=True, action="cmd_result", cmd="*003", message=msg)

    # *004 – billentyűzet Home/böngésző gombjainak tiltása (udev hwdb, reboot-álló)
    # Automatikusan is települ minden eszközre; ez a kézi visszakapcsolás *005 után.
    if cmd == "*004":
        try:
            ok, out = install_browser_key_block(dev_ip, _SSH_USER, _SSH_PASS)
            if not ok:
                return jsonify(ok=False, msg=f"Gombtiltás hiba: {out or 'SSH hiba'}")
            if "KEYBLOCK_ALREADY" in (out or ""):
                msg = f"A gombok már tiltva voltak ({dev_name} @ {dev_ip})."
            else:
                msg = (
                    f"Home/böngésző gombok letiltva ({dev_name} @ {dev_ip}).\n"
                    "Ha a gomb még élne: húzd ki és dugd vissza a billentyűzetet, vagy indítsd újra az eszközt."
                )
        except Exception as e:
            return jsonify(ok=False, msg=f"Gombtiltás hiba: {e}")
        return jsonify(ok=True, action="cmd_result", cmd="*004", message=msg)

    # *005 – a *004 visszavonása + az automatikus tiltás kikapcsolása ezen az eszközön
    if cmd == "*005":
        try:
            ok, out = remove_browser_key_block(dev_ip, _SSH_USER, _SSH_PASS)
            if not ok or "KEYBLOCK_REMOVED" not in (out or ""):
                return jsonify(ok=False, msg=f"Visszaengedélyezés hiba: {out or 'SSH hiba'}")
            msg = (
                f"Home/böngésző gombok újra engedélyezve ({dev_name} @ {dev_ip}).\n"
                "Az automatikus tiltás ezen az eszközön kikapcsolva (visszakapcsolás: *004)."
            )
        except Exception as e:
            return jsonify(ok=False, msg=f"Visszaengedélyezés hiba: {e}")
        return jsonify(ok=True, action="cmd_result", cmd="*005", message=msg)

    # *006 – kiosk watchdog telepítése (percenkénti cron: újraindítja a böngészőt, ha nem fut)
    if cmd == "*006":
        try:
            ok, out = install_kiosk_watchdog(dev_ip, _SSH_USER, _SSH_PASS)
            if not ok or "WATCHDOG_INSTALLED" not in (out or ""):
                return jsonify(ok=False, msg=f"Watchdog telepítés hiba: {out or 'SSH hiba'}")
            msg = (
                f"Kiosk watchdog telepítve ({dev_name} @ {dev_ip}).\n"
                "Percenként ellenőrzi a böngészőt, és újraindítja, ha nem fut."
            )
        except Exception as e:
            return jsonify(ok=False, msg=f"Watchdog telepítés hiba: {e}")
        return jsonify(ok=True, action="cmd_result", cmd="*006", message=msg)

    # *007 – kiosk watchdog eltávolítása
    if cmd == "*007":
        try:
            ok, out = remove_kiosk_watchdog(dev_ip, _SSH_USER, _SSH_PASS)
            if not ok or "WATCHDOG_REMOVED" not in (out or ""):
                return jsonify(ok=False, msg=f"Watchdog eltávolítás hiba: {out or 'SSH hiba'}")
            msg = f"Kiosk watchdog eltávolítva ({dev_name} @ {dev_ip})."
        except Exception as e:
            return jsonify(ok=False, msg=f"Watchdog eltávolítás hiba: {e}")
        return jsonify(ok=True, action="cmd_result", cmd="*007", message=msg)

    # *008 – *00x parancsfigyelő telepítése/frissítése (weboldaltól független *00x kezelés)
    if cmd == "*008":
        try:
            ok, out = install_pi_command_listener(dev_ip, _SSH_USER, _SSH_PASS)
            if not ok or "LISTENER_INSTALLED" not in (out or ""):
                return jsonify(ok=False, msg=f"Parancsfigyelő telepítés hiba: {out or 'SSH hiba'}")
            msg = (
                f"*00x parancsfigyelő telepítve ({dev_name} @ {dev_ip}).\n"
                "A *001–*005 parancsok mostantól a weboldaltól függetlenül is működnek."
            )
        except Exception as e:
            return jsonify(ok=False, msg=f"Parancsfigyelő telepítés hiba: {e}")
        return jsonify(ok=True, action="cmd_result", cmd="*008", message=msg)

    # *009 – parancsfigyelő eltávolítása (auto-telepítés tiltásával)
    if cmd == "*009":
        try:
            ok, out = remove_pi_command_listener(dev_ip, _SSH_USER, _SSH_PASS)
            if not ok or "LISTENER_REMOVED" not in (out or ""):
                return jsonify(ok=False, msg=f"Parancsfigyelő eltávolítás hiba: {out or 'SSH hiba'}")
            msg = f"*00x parancsfigyelő eltávolítva ({dev_name} @ {dev_ip})."
        except Exception as e:
            return jsonify(ok=False, msg=f"Parancsfigyelő eltávolítás hiba: {e}")
        return jsonify(ok=True, action="cmd_result", cmd="*009", message=msg)

    # ismeretlen *xxx kód
    known = ", ".join(_SPECIAL_CMDS.keys())
    return jsonify(ok=False, msg=f"Ismeretlen parancs: {cmd}  (ismert: {known})")


def _safe_last_state(worker_id):
    """Utolsó állapot lekérése – DB hiba esetén None, az oldal akkor is töltsön be."""
    if not worker_id:
        return None
    try:
        return fetch_worker_last_state(int(worker_id))
    except Exception:
        return None


# ============= HEALTH CHECK =============
@bp_scan.get("/ping")
def scan_ping():
    """Könnyű elérhetőség-ellenőrzés a kliens offline-jelzőjéhez. DB-t nem érint."""
    return jsonify(ok=True)


# ============= UI OLDAL =============
@bp_scan.get("/")
def scan_page():
    _get_or_make_device()
    lang = getattr(g, "lang", "hu")
    worker_id = session.get("worker_id")
    worker_name = fetch_worker_name(worker_id) if worker_id else None
    return render_template(
        f"{lang}/scan.html",
        lang=lang,
        active="scan",
        user=session.get("user"),
        worker_name=worker_name,
        worker_id=(worker_id if worker_name else None),
        last_state=_safe_last_state(worker_id) if worker_name else None
    )


# ============= LOGIN / QR / COMPLETE =============
@bp_scan.post("/login")
def scan_login():
    data = request.get_json(silent=True) or {}
    raw_rfid = (data.get("rfid") or "").strip()
    if not raw_rfid:
        return jsonify(ok=False, msg="Hiányzó RFID."), 400

    # --- SPECIÁLIS PARANCSOK (*001, *002, …) – RFID mezőből is működik ---
    dev_name, dev_ip = _get_or_make_device()
    cmd_response = _handle_special_cmd(raw_rfid, dev_name, dev_ip)
    if cmd_response is not None:
        return cmd_response

    # 1) SK kiosztás → karaktertérkép
    normalized = convert_to_slovak(raw_rfid)

    # 2) Csak számjegyeket engedünk tovább
    digits = "".join(ch for ch in normalized if ch.isdigit())
    if not digits:
        log_injector_error(
            dev_ip, dev_name,
            f"RFID not numeric after normalize: raw='{raw_rfid}' norm='{normalized}'",
            status="Rejected"
        )
        return jsonify(ok=False, msg="Érvénytelen RFID formátum."), 400

    # 3) Próbáljuk meg először a nyers számot, majd a targetet – EGY lekérdezéssel
    candidates = [digits]
    try:
        target = str(compute_target(int(digits)))
        if target != digits:
            candidates.append(target)
    except Exception:
        pass

    ensure_db()

    worker, used_val = fetch_worker_by_rfid_multi(candidates)

    if not worker:
        log_injector_error(dev_ip, dev_name, f"RFID not found (tried={candidates})", status="Rejected")
        return jsonify(ok=False, msg="RFID nem található."), 404

    worker_id = int(worker["id"])
    full_name = worker.get("name") or "Ismeretlen"
    session["worker_id"] = worker_id

    # Bejelentkezési sor: egy körben (ha a DB nem enné meg, marad a régi
    # kétlépcsős út – a bejelentkezés emiatt sose bukhat el).
    try:
        ensure_worker_login_row(worker_id, dev_ip, dev_name, datetime.now())
    except Exception:
        if not worker_has_active_login(worker_id, dev_ip):
            create_worker_login_row(worker_id, dev_ip, dev_name, datetime.now())

    # FIGYELEM: a last_state (WorkstationWorkorder + előzmények) NEM itt megy
    # el – az a scan oldal leglassabb lekérdezése, és a dolgozónak nem kell rá
    # várnia a bejelentkezéshez. A kliens a váltás UTÁN kéri le a /state-ről.
    # Régi kliens kedvéért: ha kifejezetten kéri, itt is visszaadjuk.
    want_state = bool(data.get("with_state"))

    return jsonify(
        ok=True,
        msg=f"Sikeres bejelentkezés: {full_name}",
        name=full_name,
        worker_id=worker_id,
        used_rfid=used_val,
        state_deferred=(not want_state),
        last_state=(_safe_last_state(worker_id) if want_state else None)
    )


@bp_scan.get("/state")
def scan_state():
    """
    A bejelentkezett dolgozó utolsó állapota (folytatható WO / utolsó lezárás).
    Külön végponton, hogy a bejelentkezés azonnal váltson: a kliens előbb
    átvált a QR panelre, és csak utána tölti be ezt.
    """
    wid = session.get("worker_id")
    if not wid:
        return jsonify(ok=False, msg="Nincs aktív bejelentkezés."), 401
    return jsonify(ok=True, last_state=_safe_last_state(wid))


@bp_scan.post("/qr")
def scan_qr():
    data = request.get_json(silent=True) or {}
    raw = (data.get("text") or "")
    if not raw.strip():
        return jsonify(ok=False, msg="Üres beolvasás."), 400

    ensure_db()
    dev_name, dev_ip = _get_or_make_device()

    # --- SPECIÁLIS PARANCSOK (*001, *002, …) – worker login NEM szükséges ---
    cmd_response = _handle_special_cmd(raw, dev_name, dev_ip)
    if cmd_response is not None:
        return cmd_response

    low = raw.strip().lower()
    if low == "calibrate":
        return jsonify(ok=True, action="calibrate", msg="Kalibrációs javaslat elkészült.")
    if low == "logout":
        wid = session.get('worker_id')
        if not wid:
            log_injector_error(dev_ip, dev_name, "logout without active session", status="Rejected")
            return jsonify(ok=False, msg="Nincs aktív bejelentkezés."), 400
        try:
            logout_worker_everywhere(wid, dev_name)
        except Exception as e:
            log_injector_error(dev_ip, dev_name, f"logout exception: {e}", status="Open")
            return jsonify(ok=False, msg=f"Hiba kijelentkezéskor: {e}"), 500
        session.pop('worker_id', None)
        return jsonify(ok=True, action="logout", msg="Kijelentkezve.")

    # STATION/PROCESS "/" → "-"
    text = raw
    if text.startswith("STATION/"):
        text = text.replace("STATION/", "STATION-", 1)
    elif text.startswith("PROCESS/"):
        text = text.replace("PROCESS/", "PROCESS-", 1)

    wid = session.get('worker_id')
    if not wid:
        return jsonify(ok=False, msg="Előbb jelentkezz be (RFID)."), 401

    try:
        u = session.get("user") or {}
        station_hint = (u.get("job_title") or u.get("jobTitle") or "").strip()

        result = parse_qr_and_route(
            text=text,
            worker_id=wid,
            raspberry_id=dev_ip,
            device_name=dev_name,
            station_hint=station_hint
        )
        if str(result.get("mode") or "") in ("started", "completed", "info"):
            _emit_wo_update("scan", result)
        return jsonify(ok=True, **result)
    except Exception as e:
        # a nyers beolvasott szöveg is kerüljön a hibatáblába, hogy
        # utólag látszódjon, mit olvasott (félre) a szkenner
        log_injector_error(
            dev_ip, dev_name, f"scan_qr exception: {e}", status="Open",
            meta={"text": raw[:300], "worker_id": wid},
        )
        return jsonify(ok=False, msg=f"Hiba: {e}"), 500


@bp_scan.post("/related")
def scan_related():
    """
    QR-megjelenítő (sk/scan): a beszkennelt WO QR alapján visszaadja az
    összes kapcsolódó workorders rekordot QR SVG-vel együtt.
    """
    data = request.get_json(silent=True) or {}
    raw = (data.get("text") or "")
    if not raw.strip():
        return jsonify(ok=False, msg="Üres beolvasás."), 400

    ensure_db()
    dev_name, dev_ip = _get_or_make_device()

    wid = session.get('worker_id')
    if not wid:
        return jsonify(ok=False, msg="Előbb jelentkezz be (RFID)."), 401

    try:
        result = fetch_related_qr_codes(raw)
        return jsonify(ok=True, **result)
    except Exception as e:
        log_injector_error(
            dev_ip, dev_name, f"scan_related exception: {e}", status="Open",
            meta={"text": raw[:300], "worker_id": wid},
        )
        return jsonify(ok=False, msg=str(e)), 500


@bp_scan.post("/complete")
def scan_complete_wo():
    data = request.get_json(silent=True) or {}
    qty = str(data.get("qty") or "").strip()
    if not qty.isdigit() or int(qty) <= 0:
        return jsonify(ok=False, msg="Érvénytelen mennyiség."), 400

    wid = session.get('worker_id')
    if not wid:
        return jsonify(ok=False, msg="Nincs aktív bejelentkezés."), 401

    dev_name, dev_ip = _get_or_make_device()
    try:
        payload = complete_active_wo_with_qty(worker_id=wid, qty=int(qty))
        if str(payload.get("mode") or "") == "completed":
            _emit_wo_update("complete", payload)
        return jsonify(ok=True, **payload)
    except Exception as e:
        log_injector_error(
            dev_ip, dev_name, f"complete exception: {e}", status="Open",
            meta={"qty": qty, "worker_id": wid},
        )
        return jsonify(ok=False, msg=f"Hiba: {e}"), 500
