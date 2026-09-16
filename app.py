# app.py
# -*- coding: utf-8 -*-
from datetime import timedelta
import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, session, request, redirect, url_for, flash, send_from_directory
from dotenv import load_dotenv

from config import Config
from services.cache import cache
from services.db import teardown_db
from routes import register_blueprints
from services.scheduler import start_scheduler
from utils.roles import user_can_view, role_flags_for_job

from services.notification_core import socketio, hub  # noqa: F401
from services import search_db as _search_db
from services import search_watcher as _search_watcher

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
os.chdir(BASE_DIR)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

handler = RotatingFileHandler(
    LOG_DIR / "app.log",
    maxBytes=2_000_000,
    backupCount=3,
    encoding="utf-8",
)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(handler)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    socketio.init_app(app, cors_allowed_origins="*")

    for k in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "SECRET_KEY"):
        v = os.getenv(k)
        if v:
            app.config[k] = v

    if os.getenv("MAIL_SERVER"):
        app.config["MAIL_SERVER"]   = os.getenv("MAIL_SERVER", "")
        app.config["MAIL_PORT"]     = int(os.getenv("MAIL_PORT", 587))
        app.config["MAIL_USE_TLS"]  = os.getenv("MAIL_USE_TLS", "true").lower() == "true"
        app.config["MAIL_USE_SSL"]  = os.getenv("MAIL_USE_SSL", "false").lower() == "true"
        app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME")
        app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD")
        app.config["MAIL_FROM"]     = os.getenv("MAIL_FROM")
        app.config["MAIL_ALERT_TO"] = [
            x.strip()
            for x in os.getenv("MAIL_ALERT_TO", "").split(",")
            if x.strip()
        ]
        app.config["BT_ARCHIVE_KEEP"] = 30   # projektenként megtartott snapshotok (alap: 30)
        app.config["BT_ARCHIVE_KEEP"] = 0    # 0 vagy negatív = korlátlan megőrzés, nincs törlés

    app.secret_key = app.config.get("SECRET_KEY") or os.environ.get(
        "FLASK_SECRET_KEY", "change-me-in-prod"
    )
    app.config.setdefault("SESSION_COOKIE_SECURE", False)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(days=7))

    # Cache backend env-ből: alapból SimpleCache (processzen belüli).
    # Ha fut Redis: CACHE_TYPE=RedisCache a .env-ben -> restartot túlélő,
    # több worker közt közös cache.
    _cache_type = os.getenv("CACHE_TYPE", "SimpleCache").strip()
    _cache_cfg = {
        "CACHE_TYPE": _cache_type,
        "CACHE_DEFAULT_TIMEOUT": int(os.getenv("CACHE_DEFAULT_TIMEOUT", "60")),
    }
    if _cache_type.lower() == "rediscache":
        _cache_cfg["CACHE_REDIS_HOST"] = os.getenv("CACHE_REDIS_HOST", "localhost")
        _cache_cfg["CACHE_REDIS_PORT"] = int(os.getenv("CACHE_REDIS_PORT", "6379"))
    cache.init_app(app, config=_cache_cfg)

    register_blueprints(app)
    app.teardown_appcontext(teardown_db)

    # --- Search DB init ---
    _search_db.init_app(app)

    # --- Scheduler ---
    try:
        start_scheduler(app, cache)
        app.logger.info("Scheduler started.")
    except Exception:
        app.logger.exception("Scheduler init failed")

    # --- File watcher (valós idejű index frissítés) ---
    try:
        _search_watcher.start_watcher()
        app.logger.info("File watcher started (roots: %d aktív).",
                        len([r for r in _search_db.get_roots() if r.get("active", 1)]))
    except Exception:
        app.logger.exception("File watcher init failed — indexelés csak manuálisan/schedulerrel működik")

    @app.before_request
    def _keep_session_permanent():
        if session.get("user"):
            session.permanent = True

    @app.before_request
    def _sync_lang_from_url():
        va = getattr(request, "view_args", None) or {}
        lang = va.get("lang")
        if lang in ("hu", "sk") and session.get("lang") != lang:
            session["lang"] = lang

    def root_redirect():
        user = session.get("user")
        lang = session.get("lang", "sk")
        if not user:
            return redirect(url_for("auth.login", lang="sk"))
        from routes.auth import route_for_job_title
        job_title = (user.get("job_title") or "").strip()
        target = route_for_job_title(job_title, lang)
        if target:
            return redirect(target)
        flash("Nincs megfelelő jogosultságod!", "danger")
        return redirect(url_for("auth.login", lang=lang))

    @app.context_processor
    def inject_role_helpers():
        user = session.get("user")
        jt = (user or {}).get("job_title", "")
        return {
            "can_view": lambda key: user_can_view(user, key),
            "role": role_flags_for_job(jt),
        }

    app.add_url_rule("/", "root_redirect", root_redirect, methods=["GET"])

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(
            os.path.join(app.root_path, "static"),
            "favicon.ico",
            mimetype="image/vnd.microsoft.icon",
        )

    app.logger.info(
        "Config DB -> host=%s, user=%s, name=%s",
        app.config.get("DB_HOST"),
        app.config.get("DB_USER"),
        app.config.get("DB_NAME"),
    )

    return app


if __name__ == "__main__":
    app = create_app()
    app.logger.info("Starting Flask app...")
    # Éles üzemben debug NE fusson (lassabb, és hibánál interaktív debuggert ad ki).
    # Fejlesztéshez: FLASK_DEBUG=1 a .env-ben vagy környezeti változóként.
    _debug = os.getenv("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes")
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=_debug,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )