# services/scheduler.py
# -*- coding: utf-8 -*-
import os

from apscheduler.schedulers.background import BackgroundScheduler
from utils.helpers import reload_pn_data_cache

# Globális scheduler példány — az email_scheduler is ezt használja
_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler | None:
    """Visszaadja a futó scheduler példányt (email_scheduler használja)."""
    return _scheduler


def start_scheduler(app, cache):
    global _scheduler

    _scheduler = BackgroundScheduler(timezone="Europe/Bratislava", daemon=True)

    # ── Meglévő job: PN cache újratöltés ──────────────────────────────────
    _scheduler.add_job(
        lambda: reload_with_context(app, cache),
        "interval",
        minutes=2,
        id="pn_cache_reload",
        replace_existing=True,
    )

    _scheduler.start()

    # ── Email riport ütemezések betöltése DB-ből ───────────────────────────
    try:
        from services.email_scheduler import load_schedules_into
        load_schedules_into(_scheduler, app)
        app.logger.info("Email report schedules loaded.")
    except Exception:
        app.logger.exception("Email scheduler init failed — riport küldés nem aktív")

    # ── Műszakvégi automatikus kijelentkeztetés ────────────────────────────
    try:
        _register_auto_logout(_scheduler, app)
    except Exception:
        app.logger.exception("Auto-logout schedule init failed")

    # ── Raspberry watchdog + parancsfigyelő automatikus telepítése ────────
    try:
        _register_pi_agents(_scheduler, app)
    except Exception:
        app.logger.exception("PI agents schedule init failed")


def reload_with_context(app, cache):
    with app.app_context():
        reload_pn_data_cache(cache)


# =======================
#  Műszakvégi auto-logout
# =======================
# .env beállítások:
#   AUTO_LOGOUT_TIMES=15:31          (több műszakhoz vesszővel: "15:31,23:31")
#   AUTO_LOGOUT_TIMES=off            (kikapcsolás)
#   AUTO_LOGOUT_IDLE_MIN=15          (ennyi percen belüli scan-aktivitásnál NEM jelentkeztet ki)

def _register_auto_logout(sched: BackgroundScheduler, app):
    raw = (os.getenv("AUTO_LOGOUT_TIMES", "15:31") or "").strip()
    if not raw or raw.lower() in ("off", "none", "disabled", "0"):
        app.logger.info("Auto-logout kikapcsolva (AUTO_LOGOUT_TIMES=%r).", raw)
        return

    for t in raw.split(","):
        t = t.strip()
        if not t:
            continue
        try:
            hh, mm = t.split(":")
            hh, mm = int(hh), int(mm)
        except Exception:
            app.logger.error("Auto-logout: érvénytelen időpont: %r (HH:MM kell)", t)
            continue
        sched.add_job(
            lambda: _auto_logout_job(app),
            "cron",
            hour=hh,
            minute=mm,
            id=f"auto_logout_{hh:02d}{mm:02d}",
            replace_existing=True,
        )
        app.logger.info("Auto-logout ütemezve: %02d:%02d", hh, mm)


def _auto_logout_job(app):
    from services.scan_core import auto_logout_open_logins

    try:
        idle_min = int(os.getenv("AUTO_LOGOUT_IDLE_MIN", "15") or 0)
    except Exception:
        idle_min = 15

    with app.app_context():
        try:
            res = auto_logout_open_logins(idle_minutes=idle_min)
        except Exception:
            app.logger.exception("Auto-logout job hiba")
            return

        if res.get("skipped"):
            app.logger.info("Auto-logout: kihagyva (aktív munka): %s", res["skipped"])
        if not res.get("logged_out"):
            return

        app.logger.info("Auto-logout: kijelentkeztetve: %s", res["logged_out"])

        # a nyitva hagyott scan oldalak visszaállítása RFID képernyőre
        try:
            from services.notification_core import socketio
            socketio.emit("scan_force_logout", {"worker_ids": res["logged_out"]})
        except Exception:
            app.logger.exception("Auto-logout: socket értesítés nem ment ki")


# =======================
#  Raspberry watchdog + *00x parancsfigyelő automatikus telepítése
# =======================
# .env beállítások:
#   PI_AGENTS_AUTO=on               (off = automatikus telepítés kikapcsolva)
#   PI_AGENTS_TIME=05:00            (napi futás időpontja, HH:MM)
# Szerver induláskor is lefut egyszer (~2 perccel a start után), így új eszköz
# vagy frissített figyelő script a következő restartnál magától felkerül.

def _register_pi_agents(sched: BackgroundScheduler, app):
    auto = (os.getenv("PI_AGENTS_AUTO", "on") or "").strip().lower()
    if auto in ("off", "0", "false", "none", "disabled"):
        app.logger.info("PI agents automatikus telepítés kikapcsolva (PI_AGENTS_AUTO=%r).", auto)
        return

    t = (os.getenv("PI_AGENTS_TIME", "05:00") or "05:00").strip()
    try:
        hh, mm = t.split(":")
        hh, mm = int(hh), int(mm)
    except Exception:
        app.logger.error("PI_AGENTS_TIME érvénytelen: %r (HH:MM kell) — 05:00 lesz", t)
        hh, mm = 5, 0

    sched.add_job(
        lambda: _pi_agents_job(app),
        "cron",
        hour=hh,
        minute=mm,
        id="pi_agents_daily",
        replace_existing=True,
    )

    # induláskor is fusson egyszer, pár perccel a start után
    from datetime import datetime, timedelta
    sched.add_job(
        lambda: _pi_agents_job(app),
        "date",
        run_date=datetime.now() + timedelta(minutes=2),
        id="pi_agents_boot",
        replace_existing=True,
    )
    app.logger.info("PI agents telepítés ütemezve: induláskor +2 perc, naponta %02d:%02d.", hh, mm)


def _pi_agents_job(app):
    from services.pi_provision import ensure_pi_agents_on_all_devices

    with app.app_context():
        try:
            res = ensure_pi_agents_on_all_devices()
        except Exception:
            app.logger.exception("PI agents telepítés hiba")
            return
        if res.get("ok"):
            app.logger.info("PI agents OK: %s", "; ".join(res["ok"]))
        if res.get("optout"):
            app.logger.info("PI agents kihagyva (opt-out): %s", "; ".join(res["optout"]))
        if res.get("failed"):
            app.logger.warning("PI agents HIBA: %s", "; ".join(res["failed"]))