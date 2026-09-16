# routes/search_injector.py
from __future__ import annotations
import logging, os, string
from flask import Blueprint, g, jsonify, render_template, request, session
from services import search_db, search_indexer, search_watcher

log = logging.getLogger(__name__)
bp_search = Blueprint("search", __name__, url_prefix="/<lang>/search")


@bp_search.url_value_preprocessor
def pull_lang(endpoint, values):
    g.lang = values.pop("lang", "hu")


@bp_search.get("/")
def search_page():
    lang = getattr(g, "lang", "hu")
    return render_template(f"{lang}/search.html", lang=lang, active="search",
                           user=session.get("user"), stats=search_db.get_stats())


@bp_search.get("/api")
def search_api():
    q     = (request.args.get("q")     or "").strip()
    ext   = (request.args.get("ext")   or "").strip().lower().lstrip(".")
    drive = (request.args.get("drive") or "").strip()
    type_ = (request.args.get("type")  or "").strip()
    try:    limit = min(int(request.args.get("limit", 200)), 500)
    except: limit = 200
    if len(q) < 2:
        return jsonify(ok=True, results=[], total=0)
    try:
        results = search_db.search_files(
            q=q, ext_filter=ext or None,
            drive_filter=drive or None,
            type_filter=type_ or None,
            limit=limit,
        )
        return jsonify(ok=True, results=results, total=len(results))
    except Exception as e:
        log.exception("search_api hiba")
        return jsonify(ok=False, msg=str(e)), 500


@bp_search.get("/status")
def index_status():
    try:
        stats = search_db.get_stats()
        stats["is_running"]      = search_indexer.is_running()
        stats["watcher_running"] = search_watcher.is_running()
        return jsonify(ok=True, **stats)
    except Exception as e:
        log.exception("index_status hiba")
        return jsonify(ok=False, msg=str(e), is_running=False, watcher_running=False,
                       total=0, files=0, dirs=0, status="error",
                       progress="", last_index="-", current_dir=""), 500


@bp_search.get("/filters")
def get_filters():
    try:
        return jsonify(ok=True, exts=search_db.get_ext_list(), drives=search_db.get_drives())
    except Exception as e:
        return jsonify(ok=False, msg=str(e)), 500


@bp_search.get("/admin/browse")
def admin_browse():
    path = (request.args.get("path") or "").strip()
    try:
        if not path:
            drives = []
            for letter in string.ascii_uppercase:
                d = f"{letter}:\\"
                if os.path.exists(d):
                    drives.append({"name": f"{letter}:", "path": d, "type": "drive"})
            return jsonify(ok=True, items=drives, current=path, parent=None)

        is_unc = path.startswith("\\\\")

        # UNC path normalizálás
        if is_unc:
            parts = path.lstrip("\\").split("\\")
            if len(parts) >= 2:
                path = "\\\\" + "\\".join(p for p in parts if p)
            if path.count("\\") == 2:
                path = path + "\\"

        # Helyi útvonalnál ellenőrzés, UNC-nél kihagyjuk (isdir megbízhatatlan)
        if not is_unc and not os.path.exists(path):
            return jsonify(ok=False, msg="Nem létező mappa."), 400

        items = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=True):
                            items.append({"name": entry.name, "path": entry.path, "type": "dir"})
                    except OSError:
                        continue
        except PermissionError:
            return jsonify(ok=False, msg="Hozzáférés megtagadva."), 403
        except OSError as e:
            if getattr(e, 'winerror', None) == 67:
                return jsonify(ok=False, msg=f"A megosztás nem található: {path}\n\nUNC formátum: \\\\szerver\\share_neve"), 400
            return jsonify(ok=False, msg=str(e)), 400

        items.sort(key=lambda x: x["name"].lower())

        norm = path.rstrip("\\/")
        if is_unc:
            parts = norm.lstrip("\\").split("\\")
            if len(parts) <= 1:
                parent = None
            elif len(parts) == 2:
                parent = "\\\\" + parts[0] + "\\"
            else:
                parent = "\\\\" + "\\".join(parts[:-1])
        else:
            parent = str(os.path.dirname(norm))
            if os.path.normpath(parent) == os.path.normpath(path):
                parent = ""

        return jsonify(ok=True, items=items, current=path, parent=parent)

    except Exception as e:
        log.exception("browse hiba")
        return jsonify(ok=False, msg=str(e)), 500


@bp_search.get("/admin")
def admin_page():
    lang = getattr(g, "lang", "hu")
    return render_template(f"{lang}/search_admin.html", lang=lang, active="search",
                           user=session.get("user"), stats=search_db.get_stats(),
                           roots=search_db.get_roots())


@bp_search.post("/admin/root/add")
def admin_root_add():
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify(ok=False, msg="Hiányzó útvonal."), 400
    try:
        rid = search_db.add_root(
            path=path,
            priority=int(data.get("priority", 5)),
            recursive=bool(data.get("recursive", True)),
            label=str(data.get("label", "")),
        )
        # Watcher újraindítása hogy az új mappát is figyelje
        try:
            search_watcher.restart_watcher()
        except Exception as e:
            log.warning("watcher restart hiba root add után: %s", e)
        return jsonify(ok=True, id=rid)
    except Exception as e:
        return jsonify(ok=False, msg=str(e)), 500


@bp_search.post("/admin/root/toggle")
def admin_root_toggle():
    data = request.get_json(silent=True) or {}
    try:
        search_db.toggle_root(int(data["id"]), bool(data["active"]))
        try:
            search_watcher.restart_watcher()
        except Exception as e:
            log.warning("watcher restart hiba toggle után: %s", e)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, msg=str(e)), 500


@bp_search.post("/admin/root/delete")
def admin_root_delete():
    data = request.get_json(silent=True) or {}
    try:
        search_db.delete_root(int(data["id"]))
        try:
            search_watcher.restart_watcher()
        except Exception as e:
            log.warning("watcher restart hiba delete után: %s", e)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, msg=str(e)), 500


@bp_search.post("/admin/reindex")
def admin_reindex():
    try:
        started = search_indexer.run_full_index_async()
        if not started:
            return jsonify(ok=False, msg="Indexelés már fut.")
        return jsonify(ok=True, msg="Indexelés elindítva.")
    except Exception as e:
        log.exception("admin_reindex hiba")
        return jsonify(ok=False, msg=str(e)), 500


@bp_search.post("/admin/reindex/root")
def admin_reindex_root():
    try:
        data = request.get_json(silent=True) or {}
        root_id = int(data.get("id", 0))
        if not root_id:
            return jsonify(ok=False, msg="Hiányzó root id."), 400
        if search_indexer.is_running():
            return jsonify(ok=False, msg="Indexelés már fut.")
        roots = [r for r in search_db.get_roots() if r["id"] == root_id]
        if not roots:
            return jsonify(ok=False, msg="Root nem található."), 404
        search_indexer.run_full_index_async(roots=roots)
        return jsonify(ok=True, msg=f"Indexelés elindítva: {roots[0]['path']}")
    except Exception as e:
        log.exception("admin_reindex_root hiba")
        return jsonify(ok=False, msg=str(e)), 500


@bp_search.post("/admin/reindex/incremental")
def admin_reindex_incremental():
    result = search_indexer.run_incremental(since_seconds=900)
    return jsonify(result)


@bp_search.post("/admin/clear")
def admin_clear():
    try:
        deleted = search_db.clear_index()
        return jsonify(ok=True, msg=f"{deleted:,} bejegyzés törölve.")
    except Exception as e:
        log.exception("clear hiba")
        return jsonify(ok=False, msg=str(e)), 500


@bp_search.post("/admin/watcher/restart")
def admin_watcher_restart():
    """Watcher manuális újraindítása az admin felületről."""
    try:
        search_watcher.restart_watcher()
        return jsonify(ok=True, running=search_watcher.is_running())
    except Exception as e:
        log.exception("watcher restart hiba")
        return jsonify(ok=False, msg=str(e)), 500