# services/pi_provision.py
# -*- coding: utf-8 -*-
"""
Raspberry eszközök automatikus felkészítése (provisioning).

A scheduler hívja (induláskor + naponta): végigmegy a RaspberryDevices
táblában lévő összes eszközön, és SSH-n telepíti/frissíti:
  - a kiosk watchdogot (percenkénti cron, újraindítja a böngészőt, ha nem fut),
  - a *00x parancsfigyelőt (pi_cmd_listener systemd service),
  - a Home/böngésző gombok tiltását (udev hwdb – reboot után is érvényes).

Így nem kell eszközönként kézzel beütni a *004/*006/*008 parancsokat.
Az eszközön *005/*007/*009-cel letett opt-out flageket tiszteletben tartja.

.env:
  PI_KEYBLOCK_AUTO=off  → a gombtiltás automatikus telepítése kikapcsolva.
  PI_AGENTS_EXCLUDE=10.10.40.43,SIL-LAP-03047.INSILCOSK.local
      → vesszővel elválasztott IP-k vagy eszköznevek, amiket a telepítő kihagy
        (pl. laptopok, amik a RaspberryDevices táblában vannak, de nem Pi-k).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from services.scan_core import db_execute
from services.ssh import (
    get_pi_credentials,
    install_browser_key_block,
    install_kiosk_watchdog,
    install_pi_command_listener,
)


def list_raspberry_devices() -> List[Dict[str, Any]]:
    """Eszközlista a DB-ből: [{'ip': ..., 'name': ...}, ...]"""
    try:
        rows = db_execute("SELECT device_id, device_name FROM RaspberryDevices") or []
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        ip = str(r.get("device_id") or "").strip()
        if ip:
            out.append({"ip": ip, "name": str(r.get("device_name") or "").strip()})
    return out


def _state(ok: bool, out: str, ok_markers: tuple, optout_marker: str) -> str:
    txt = out or ""
    if optout_marker in txt:
        return "optout"
    if ok and any(m in txt for m in ok_markers):
        return "ok"
    return f"hiba({(txt.strip() or 'SSH')[:120]})"


def ensure_pi_agents_on_all_devices() -> Dict[str, List[str]]:
    """
    Watchdog + parancsfigyelő + gombtiltás telepítése minden ismert eszközre.
    Egy eszköz hibája nem állítja meg a többit. Összegzést ad vissza a loghoz.
    """
    user, pw = get_pi_credentials()
    keyblock_auto = (os.getenv("PI_KEYBLOCK_AUTO", "on") or "").strip().lower() not in (
        "off", "0", "false", "none", "disabled"
    )
    exclude = {
        x.strip().lower()
        for x in (os.getenv("PI_AGENTS_EXCLUDE", "") or "").split(",")
        if x.strip()
    }
    summary: Dict[str, List[str]] = {"ok": [], "optout": [], "failed": []}

    for dev in list_raspberry_devices():
        ip, name = dev["ip"], dev["name"]
        if ip.lower() in exclude or (name and name.lower() in exclude):
            continue
        label = f"{name or ip} ({ip})" if name else ip
        try:
            ok_w, out_w = install_kiosk_watchdog(ip, user, pw, respect_optout=True)
            ok_l, out_l = install_pi_command_listener(ip, user, pw, respect_optout=True)
            if keyblock_auto:
                ok_k, out_k = install_browser_key_block(ip, user, pw, respect_optout=True)
            else:
                ok_k, out_k = True, "KEYBLOCK_SKIPPED"
        except Exception as e:
            summary["failed"].append(f"{label}: {e}")
            continue

        wd = _state(ok_w, out_w, ("WATCHDOG_INSTALLED",), "WATCHDOG_OPTOUT")
        ls = _state(ok_l, out_l, ("LISTENER_INSTALLED",), "LISTENER_OPTOUT")
        kb = _state(ok_k, out_k, ("KEYBLOCK_INSTALLED", "KEYBLOCK_ALREADY", "KEYBLOCK_SKIPPED"), "KEYBLOCK_OPTOUT")
        entry = f"{label}: watchdog={wd}, listener={ls}, keyblock={kb}"

        if wd.startswith("hiba") or ls.startswith("hiba") or kb.startswith("hiba"):
            summary["failed"].append(entry)
        elif "optout" in (wd, ls, kb):
            summary["optout"].append(entry)
        else:
            summary["ok"].append(entry)

    return summary
