# services/notification_core.py
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from flask import session, request
from flask_socketio import SocketIO

# Threading mode: nem kell új port, ugyanazon a 5000-en megy.
socketio = SocketIO(
    async_mode="threading",
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
    manage_session=False,
    # Python 3.13 + Werkzeug dev szerver nem tud WebSocket upgrade-et kezelni.
    # Polling módban minden funkció (értesítések, jelenlét) ugyanúgy működik.
    transports=["polling"],
)

BASE_DIR = Path(__file__).resolve().parents[1]
STATE_DIR = BASE_DIR / "instance"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "notifications_state.json"


def _now_utc() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


def _now_iso() -> str:
    return _now_utc().isoformat() + "Z"


def _parse_dt(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        # elfogadjuk: "2025-12-18T07:30" (datetime-local) vagy ISO Z
        if s.endswith("Z"):
            s = s[:-1]
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _safe_str(x: Any) -> str:
    try:
        return str(x)
    except Exception:
        return ""


def _normalize_role_words(job_title: str) -> Set[str]:
    jt = (job_title or "").strip().upper()
    for ch in ",;/|-_()":
        jt = jt.replace(ch, " ")
    return {w for w in jt.split() if w}


@dataclass
class Notice:
    notice_id: str
    message: str
    level: str = "warning"         # info/warning/success/danger
    ui: str = "toast"              # toast/modal/banner
    sticky: bool = False           # toastnál: autohide?
    toast_after_modal: bool = False
    eta_minutes: Optional[int] = None
    shutdown_at: Optional[str] = None
    created_at: str = ""
    created_by: Optional[str] = None
    active: bool = True

    # Targeting: OR logika. Ha üres -> mindenkinek.
    # target elem: {"type":"device|user|page|role", "value":"..."}
    targets: List[Dict[str, str]] = None

    def to_public(self) -> Dict[str, Any]:
        d = asdict(self)
        d["targets"] = self.targets or []
        return d


@dataclass
class MaintenanceState:
    enabled: bool = False
    message: str = ""
    level: str = "warning"
    eta_minutes: Optional[int] = None
    shutdown_at: Optional[str] = None
    updated_at: str = ""

    def to_public(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ClientInfo:
    sid: str
    user_id: str
    display_name: str
    job_title: str
    roles: List[str]
    device_name: str
    device_ip: str
    page: str
    connected_at: str

    visible: bool = True
    last_seen_at: str = ""       # heartbeat ideje
    last_active_at: str = ""     # amikor visible=True volt

    def to_public(self) -> Dict[str, Any]:
        return asdict(self)


class NotificationHub:
    def __init__(self):
        self.clients: Dict[str, ClientInfo] = {}
        self.notices: Dict[str, Notice] = {}
        self.acks: Dict[str, Dict[str, str]] = {}  # notice_id -> {user_id: iso_ts}
        self.maintenance = MaintenanceState()
        self._load_state()

    # ---------------- persistence ----------------
    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return

        for n in data.get("notices", []):
            try:
                notice = Notice(**n)
                self.notices[notice.notice_id] = notice
            except Exception:
                continue

        self.acks = data.get("acks", {}) or {}

        m = data.get("maintenance", None)
        if isinstance(m, dict):
            try:
                self.maintenance = MaintenanceState(**m)
            except Exception:
                pass

    def _save_state(self) -> None:
        try:
            payload = {
                "notices": [n.to_public() for n in self.notices.values()],
                "acks": self.acks,
                "maintenance": self.maintenance.to_public(),
                "saved_at": _now_iso(),
            }
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception:
            pass

    # ---------------- clients ----------------
    def register_client(self, sid: str, page: str = "") -> ClientInfo:
        user = session.get("user") or {}
        user_id = _safe_str(user.get("id") or user.get("username") or user.get("display_name") or f"anon:{sid}")
        display_name = _safe_str(user.get("display_name") or user.get("username") or user_id)
        job_title = _safe_str(user.get("job_title") or "")

        roles = sorted(_normalize_role_words(job_title))

        dev_name = _safe_str(session.get("device_name") or "")
        dev_ip = _safe_str(session.get("raspberry_id") or request.remote_addr or "")

        now = _now_iso()

        ci = ClientInfo(
            sid=sid,
            user_id=user_id,
            display_name=display_name,
            job_title=job_title,
            roles=roles,
            device_name=dev_name,
            device_ip=dev_ip,
            page=_safe_str(page),
            connected_at=now,
            visible=True,
            last_seen_at=now,
            last_active_at=now,
        )
        self.clients[sid] = ci
        return ci

    def unregister_client(self, sid: str) -> None:
        self.clients.pop(sid, None)

    def update_page(self, sid: str, page: str) -> None:
        if sid in self.clients:
            self.clients[sid].page = _safe_str(page)

    def touch_presence(self, sid: str, page: str = "", visible: Optional[bool] = None) -> None:
        c = self.clients.get(sid)
        if not c:
            c = self.register_client(sid, page=page or "")

        now = _now_iso()

        if page:
            c.page = _safe_str(page)

        if visible is not None:
            c.visible = bool(visible)

        c.last_seen_at = now
        if c.visible:
            c.last_active_at = now

    def list_clients(self, visible_only: bool = False, active_within_seconds: int = 30) -> List[Dict[str, Any]]:
        now = datetime.utcnow()
        out = []
        stale_sids = []

        for sid, c in self.clients.items():
            last = _parse_dt((c.last_seen_at or "").replace("Z","")) or _parse_dt((c.connected_at or "").replace("Z",""))
            age_ok = True
            if last:
                age_ok = (now - last) <= timedelta(seconds=active_within_seconds)

            # ✅ mindig szűrjünk TTL-re (különben “beragad”)
            if not age_ok:
                stale_sids.append(sid)
                continue

            if visible_only and not c.visible:
                continue

            out.append(c.to_public())

        # opcionális: takarítás
        for sid in stale_sids:
            self.clients.pop(sid, None)

        return out


    # ---------------- targeting ----------------
    def _match_target(self, notice: Notice, client: ClientInfo) -> bool:
        targets = notice.targets or []
        if not targets:
            return True

        for t in targets:
            ttype = (t.get("type") or "").strip().lower()
            val = (t.get("value") or "").strip()
            if not ttype or not val:
                continue

            if ttype == "device":
                if val == client.device_ip or val.lower() == client.device_name.lower():
                    return True

            elif ttype == "user":
                if val == client.user_id or val.lower() == client.display_name.lower():
                    return True

            elif ttype == "page":
                if val.lower() == (client.page or "").lower():
                    return True

            elif ttype == "role":
                want = _normalize_role_words(val)
                have = set(client.roles)
                if want and want.issubset(have):
                    return True

        return False

    # ---------------- notices ----------------
    def create_notice(
        self,
        message: str,
        level: str = "warning",
        ui: str = "toast",
        sticky: bool = False,
        toast_after_modal: bool = False,
        eta_minutes: Optional[int] = None,
        shutdown_at: Optional[str] = None,
        targets: Optional[List[Dict[str, str]]] = None,
        created_by: Optional[str] = None,
    ) -> Notice:
        notice_id = uuid.uuid4().hex
        dt = _parse_dt(shutdown_at)
        shutdown_iso = (dt.replace(microsecond=0).isoformat() + "Z") if dt else None

        n = Notice(
            notice_id=notice_id,
            message=message,
            level=(level or "warning").lower(),
            ui=(ui or "toast").lower(),
            sticky=bool(sticky),
            toast_after_modal=bool(toast_after_modal),
            eta_minutes=eta_minutes,
            shutdown_at=shutdown_iso,
            created_at=_now_iso(),
            created_by=created_by,
            active=True,
            targets=targets or [],
        )

        self.notices[n.notice_id] = n
        self._save_state()
        return n

    # ✅ JAVÍTÁS: stop broadcast + state mentés
    def stop_notice(self, notice_id: str) -> bool:
        n = self.notices.get(notice_id)
        if not n:
            return False
        n.active = False
        self._save_state()

        # live eltüntetés
        self.stop_notice_broadcast(notice_id)
        return True

    # ✅ JAVÍTÁS: delete broadcast + state mentés
    def delete_notice(self, notice_id: str) -> bool:
        if notice_id in self.notices:
            self.notices.pop(notice_id, None)
            self.acks.pop(notice_id, None)
            self._save_state()

            # live eltüntetés
            self.stop_notice_broadcast(notice_id)
            return True
        return False

    def list_notices(self, active_only: bool = False) -> List[Dict[str, Any]]:
        arr = []
        for n in self.notices.values():
            if active_only and not n.active:
                continue
            arr.append(n.to_public())
        arr.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return arr

    # ---------------- acks ----------------
    def record_ack(self, notice_id: str, user_id: str) -> None:
        if not notice_id:
            return
        if notice_id not in self.acks:
            self.acks[notice_id] = {}
        self.acks[notice_id][user_id] = _now_iso()
        self._save_state()

    def get_acks(self, notice_id: str) -> Dict[str, str]:
        return self.acks.get(notice_id, {}) or {}

    # ---------------- maintenance ----------------
    def set_maintenance(
        self,
        enabled: bool,
        message: str = "",
        level: str = "warning",
        eta_minutes: Optional[int] = None,
        shutdown_at: Optional[str] = None,
    ) -> None:
        dt = _parse_dt(shutdown_at)
        shutdown_iso = (dt.replace(microsecond=0).isoformat() + "Z") if dt else None

        self.maintenance.enabled = bool(enabled)
        self.maintenance.message = message or ""
        self.maintenance.level = (level or "warning").lower()
        self.maintenance.eta_minutes = eta_minutes
        self.maintenance.shutdown_at = shutdown_iso
        self.maintenance.updated_at = _now_iso()
        self._save_state()

    # ---------------- dispatch ----------------
    def dispatch_notice(self, notice: Notice) -> None:
        for sid, client in list(self.clients.items()):
            if not notice.active:
                continue
            if self._match_target(notice, client):
                socketio.emit("admin_notice", notice.to_public(), to=sid)

    def broadcast_maintenance(self) -> None:
        socketio.emit("maintenance_state", self.maintenance.to_public())

    def dispatch_active_to_client(self, sid: str) -> None:
        client = self.clients.get(sid)
        if not client:
            return

        socketio.emit("maintenance_state", self.maintenance.to_public(), to=sid)

        for n in self.notices.values():
            if n.active and self._match_target(n, client):
                socketio.emit("admin_notice", n.to_public(), to=sid)

    # ✅ JAVÍTÁS: EZ HIÁNYZOTT A CLASS-BÓL (ezért volt AttributeError)
    def stop_notice_broadcast(self, notice_id: str) -> None:
        socketio.emit("admin_notice_stop", {"notice_id": str(notice_id or "")})

    def update_presence(self, sid: str, page: str = "", visible: bool = True) -> None:
        ci = self.clients.get(sid)
        if not ci:
            return
        if page is not None:
            ci.page = _safe_str(page)
        ci.visible = bool(visible)
        ci.last_seen_at = _now_iso()


hub = NotificationHub()


# ================= Socket events =================

@socketio.on("connect")
def _on_connect(auth=None):
    sid = request.sid
    hub.register_client(sid, page="")
    hub.dispatch_active_to_client(sid)


@socketio.on("disconnect")
def _on_disconnect():
    hub.unregister_client(request.sid)


@socketio.on("page_view")
def _on_page_view(data: Dict[str, Any]):
    page = (data or {}).get("page") or ""
    hub.update_page(request.sid, page)
    hub.touch_presence(request.sid, page=page, visible=None)


@socketio.on("presence")
def _on_presence(data: Dict[str, Any]):
    page = (data or {}).get("page") or ""
    visible = bool((data or {}).get("visible", True))
    hub.touch_presence(request.sid, page=page, visible=visible)


@socketio.on("notice_ack")
def _on_ack(data: Dict[str, Any]):
    user = session.get("user") or {}
    user_id = _safe_str(user.get("id") or user.get("username") or user.get("display_name") or request.sid)
    notice_id = (data or {}).get("notice_id") or ""
    hub.record_ack(_safe_str(notice_id), user_id)

    socketio.emit("notice_ack_update", {"notice_id": notice_id, "user_id": user_id, "ts": _now_iso()})