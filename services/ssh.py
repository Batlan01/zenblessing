# services/ssh.py
from __future__ import annotations
import base64
import os
from pathlib import Path
from typing import Tuple, Optional

import paramiko

DEFAULT_TIMEOUT = 12


def get_pi_credentials() -> Tuple[str, str]:
    """
    Raspberry SSH belépő. A .env-ben felülírható:
      PI_SSH_USER=...
      PI_SSH_PASS=...
    (alapértelmezés a korábbi beégetett user/user, hogy semmi ne törjön el)
    """
    return os.getenv("PI_SSH_USER", "user"), os.getenv("PI_SSH_PASS", "user")


def get_kiosk_url() -> str:
    """A kiosk böngésző kezdőoldala. A .env-ben felülírható: KIOSK_URL=..."""
    return os.getenv("KIOSK_URL", "http://10.10.2.14:5000/sk/scan/")


def get_kiosk_scale() -> str:
    """
    A kiosk böngésző nagyítása (device scale factor). ALAPBÓL 'auto' → a Pi
    az indításkor a SAJÁT képernyő-felbontásából számolja (lásd a launch
    scriptet), így minden eszköz a saját kijelzőjéhez igazodik, kézi
    beállítás nélkül. A .env-ben KIOSK_SCALE=<szám> fix értékre kényszeríthető
    (0.5–3.0), pl. ha egy adott gépet külön akarsz állítani.

    Miért kell egyáltalán: a böngésző --incognito módban indul, így a kézi
    Ctrl +/- nagyítás SOHA nem marad meg (reboot / watchdog után visszaáll).
    A launch parancsba égetett érték viszont minden induláskor érvényes.
    """
    raw = (os.getenv("KIOSK_SCALE", "auto") or "auto").strip().lower()
    if raw == "auto":
        return "auto"
    try:
        val = float(raw)
        if 0.5 <= val <= 3.0:
            return repr(val)
    except (TypeError, ValueError):
        pass
    return "auto"


def get_kiosk_ref_width() -> int:
    """
    Referencia-képernyőszélesség (px) az auto-scale-hez: KIOSK_REF_WIDTH,
    alap 1920. A Pi ehhez normalizál:  scale = felbontás_szélesség / ref.
    Így az 1920 széles kijelzők érintetlenek (scale 1.0), a kisebbek
    (pl. 1024) arányosan kicsinyítve mutatják a teljes elrendezést.
    """
    try:
        w = int(os.getenv("KIOSK_REF_WIDTH", "1920"))
        return w if 640 <= w <= 7680 else 1920
    except (TypeError, ValueError):
        return 1920


# A Pi-n futó bash részlet, ami a device-scale-factort kiszámolja:
# ha SCALE_OVR="auto", a `xrandr` aktuális (*) felbontásából, különben a fix
# értéket használja. Csak bash egész-aritmetika (nincs awk/bc, nincs idézőjel-
# gubanc). A hívó a {scale} és {ref_width} mezőket tölti ki.
def _scale_bash_snippet(scale: str, ref_width: int) -> str:
    return f"""SCALE_OVR="{scale}"
if [ "$SCALE_OVR" = "auto" ]; then
  CUR=$(xrandr 2>/dev/null | grep -F "*" | head -1)
  RES=$(echo "$CUR" | grep -oE "[0-9]+x[0-9]+" | head -1 | cut -dx -f1)
  RW={ref_width}
  if [ -n "$RES" ] && [ "$RES" -gt 0 ] 2>/dev/null && [ "$RW" -gt 0 ]; then
    X=$((RES*1000/RW))
    [ "$X" -lt 500 ] && X=500
    [ "$X" -gt 3000 ] && X=3000
    SCALE=$(printf "%d.%03d" $((X/1000)) $((X%1000)))
  else
    SCALE=1.0
  fi
else
  SCALE="$SCALE_OVR"
fi"""

def _decode(b: Optional[bytes]) -> str:
    if b is None:
        return ""
    try:
        return b.decode("utf-8", "ignore")
    except Exception:
        return str(b)

def ssh_exec(ip: str, username: str, password: str, command: str,
             timeout: int = DEFAULT_TIMEOUT) -> Tuple[bool, str, str, int]:
    """
    Egyetlen parancs futtatása SSH-n. Mindig str-t ad vissza (nincs bytes).
    Visszatér: (ok, stdout_str, stderr_str, return_code)
    """
    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(ip, username=username, password=password,
                       timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = _decode(stdout.read())
        err = _decode(stderr.read())
        rc = stdout.channel.recv_exit_status() if stdout and stdout.channel else 0
        ok = (rc == 0)
        return ok, out, err, rc
    except Exception as e:
        return False, "", f"{type(e).__name__}: {e}", 255
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass

def execute_command_on_pi(ip: str, username: str, password: str, command: str) -> Tuple[bool, str]:
    """
    Backward compatible wrapper: (ok, output_str). stderr hozzácsapva, ha van.
    """
    ok, out, err, _ = ssh_exec(ip, username, password, command)
    msg = out if out.strip() else err
    return ok, msg


def run_script_as_root(
    ip: str, username: str, password: str, script: str, timeout: int = 30
) -> Tuple[bool, str, str, int]:
    """
    Teljes script futtatása rootként, EGYETLEN sudo hívással.
    A scriptet base64-ben visszük át (quoting-biztos), a sudo a jelszót
    stdin-ről kapja (-S), így az olyan Pi-ken is működik, ahol a sudo
    jelszót kér (nincs NOPASSWD) – NOPASSWD esetén a jelszó-sort a sudo
    egyszerűen nem olvassa el.
    A scriptben NEM kell (és nem is szabad) sudo-t használni.
    """
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    pw_sq = password.replace("'", "'\\''")
    cmd = (
        "T=/tmp/.pi_admin_$$.sh"
        f" && echo {b64} | base64 -d > $T"
        f" && printf '%s\\n' '{pw_sq}' | sudo -S -p '' bash $T"
        "; rc=$?; rm -f $T; exit $rc"
    )
    return ssh_exec(ip, username, password, cmd, timeout=timeout)

def start_program_bg(ip: str, username: str, password: str) -> Tuple[bool, str]:
    """
    Böngésző indítása kiosk módban a scan oldalra.
    (NÉV maradt backward kompatibilitás miatt, de most már a Chromiumot jelenti.)
    """
    url = get_kiosk_url()
    scale_block = _scale_bash_snippet(get_kiosk_scale(), get_kiosk_ref_width())

    cmd = f"""/bin/bash -lc '
set +e

# kézi stop flag törlése – a watchdog újra felügyelheti a böngészőt
rm -f /tmp/kiosk_manual_stop

export DISPLAY=:0
export XAUTHORITY=/home/{username}/.Xauthority

# böngésző bináris kiválasztása
if command -v chromium-browser >/dev/null 2>&1; then
    BROWSER=$(command -v chromium-browser)
elif command -v chromium >/dev/null 2>&1; then
    BROWSER=$(command -v chromium)
else
    echo NO_CHROMIUM
    exit 0
fi

echo "USING_BROWSER=$BROWSER"

# külön kiosk profil könyvtár
PROFILE_DIR="/home/{username}/.config/chromium_kiosk"
mkdir -p "$PROFILE_DIR"

# előző kiosk / chromium példányok lelövése
pkill -f "$BROWSER.*chromium_kiosk" >/dev/null 2>&1 || true
pkill -f "$BROWSER" >/dev/null 2>&1 || true
sleep 1

# device-scale-factor: auto a felbontásból (vagy fix KIOSK_SCALE)
{scale_block}
echo "KIOSK_SCALE=$SCALE"

# indulás – logoljuk egy fájlba
"$BROWSER" \\
  --user-data-dir="$PROFILE_DIR" \\
  --password-store=basic \\
  --kiosk \\
  --incognito \\
  --force-device-scale-factor=$SCALE \\
  --high-dpi-support=1 \\
  --noerrdialogs \\
  --disable-infobars \\
  --no-first-run \\
  --disable-session-crashed-bubble \\
  "{url}" > /tmp/raspi_browser.log 2>&1 &

echo STARTED
'"""

    ok, out = execute_command_on_pi(ip, username, password, cmd)
    # itt a caller logolhat, csak visszaadjuk
    return ok, out.strip()

def stop_program_safe(ip: str, username: str, password: str) -> Tuple[bool, str]:
    """
    Böngésző (Chromium) leállítása; többször is hívható, nem gond ha épp nem fut.
    """
    cmd = r"""/bin/bash -lc '
set +e

# jelezzük a watchdognak, hogy ez szándékos leállítás (reboot törli a flaget)
touch /tmp/kiosk_manual_stop

if pgrep -f "chromium" >/dev/null 2>&1; then
    pkill -f "chromium" >/dev/null 2>&1 || true
    sleep 1
    if pgrep -f "chromium" >/dev/null 2>&1; then
        pkill -9 -f "chromium" >/dev/null 2>&1 || true
        sleep 1
    fi
fi

echo "browser stopped"
'"""

    ok, out = execute_command_on_pi(ip, username, password, cmd)
    return ok, out.strip()


def program_status(ip: str, username: str, password: str) -> str:
    """
    Böngésző státusz – csak a kiosk profil számít futónak.
    """
    cmd = r"""/bin/bash -lc '
set +e
if pgrep -f "chromium_kiosk" >/dev/null 2>&1; then
  echo "Browser Running"
else
  echo "Browser Disabled"
fi
'"""
    ok, out = execute_command_on_pi(ip, username, password, cmd)
    return out.strip() if ok else "Unknown"


def tail_log(ip: str, username: str, password: str, lines: int = 200) -> Tuple[bool, str]:
    """
    Böngésző log olvasása (/tmp/raspi_browser.log).
    """
    lines = max(1, min(int(lines), 2000))
    cmd = f"""/bin/bash -lc 'tail -n {lines} /tmp/raspi_browser.log 2>/dev/null || echo "No log"'"""
    return execute_command_on_pi(ip, username, password, cmd)

def install_kiosk_watchdog(
    ip: str, username: str, password: str, respect_optout: bool = False
) -> Tuple[bool, str]:
    """
    Percenkénti cron watchdog telepítése a Pi-re: ha a chromium_kiosk nem fut
    (pl. valaki kilépett a böngészőből), automatikusan újraindítja.
    A kézi Stop gombot tiszteletben tartja (/tmp/kiosk_manual_stop flag),
    reboot után a flag törlődik, így a watchdog magától elindítja a kioskot.
    Idempotens: többszöri futtatásra sem duplázza a cron sort.

    respect_optout=True (automatikus telepítésnél): ha az eszközön a *007
    paranccsal kikapcsolták a watchdogot (opt-out flag), nem telepíti újra.
    Kézi *006-nál respect_optout=False: telepít és törli az opt-out flaget.
    """
    url = get_kiosk_url()
    scale_block = _scale_bash_snippet(get_kiosk_scale(), get_kiosk_ref_width())
    wd_path = f"/home/{username}/kiosk_watchdog.sh"
    optout_flag = f"/home/{username}/.kiosk_watchdog_disabled"

    if respect_optout:
        pre = f'if [ -f {optout_flag} ]; then echo WATCHDOG_OPTOUT; exit 0; fi'
    else:
        pre = f'rm -f {optout_flag}'

    cmd = f"""/bin/bash -lc '{pre}
cat > {wd_path} <<"WDEOF"
#!/bin/bash
# Kiosk watchdog – percenkenti cron. Ha nem fut a chromium_kiosk, ujrainditja.
# Kezi leallitas (Stop gomb) eseten a /tmp/kiosk_manual_stop flag miatt nem indit ujra.
[ -f /tmp/kiosk_manual_stop ] && exit 0
pgrep -f chromium_kiosk >/dev/null 2>&1 && exit 0
export DISPLAY=:0
export XAUTHORITY=/home/{username}/.Xauthority
if command -v chromium-browser >/dev/null 2>&1; then BROWSER=$(command -v chromium-browser)
elif command -v chromium >/dev/null 2>&1; then BROWSER=$(command -v chromium)
else exit 0
fi
PROFILE_DIR="/home/{username}/.config/chromium_kiosk"
mkdir -p "$PROFILE_DIR"
{scale_block}
"$BROWSER" --user-data-dir="$PROFILE_DIR" --password-store=basic --kiosk --incognito --force-device-scale-factor=$SCALE --high-dpi-support=1 --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble "{url}" >> /tmp/raspi_browser.log 2>&1 &
WDEOF
chmod +x {wd_path}
( crontab -l 2>/dev/null | grep -v kiosk_watchdog ; echo "* * * * * {wd_path}" ) | crontab -
echo WATCHDOG_INSTALLED'
"""
    # hosszabb timeout: van Pi, ami lassan épít SSH kapcsolatot
    ok, out, err, _rc = ssh_exec(ip, username, password, cmd, timeout=30)
    return ok, (out if out.strip() else err)


def remove_kiosk_watchdog(ip: str, username: str, password: str) -> Tuple[bool, str]:
    """
    A kiosk watchdog cron + script eltávolítása a Pi-ről.
    Opt-out flaget is letesz, hogy az automatikus telepítés ne rakja vissza.
    """
    wd_path = f"/home/{username}/kiosk_watchdog.sh"
    optout_flag = f"/home/{username}/.kiosk_watchdog_disabled"
    cmd = (
        "/bin/bash -lc '( crontab -l 2>/dev/null | grep -v kiosk_watchdog ) | crontab - ; "
        f"rm -f {wd_path} ; touch {optout_flag} ; echo WATCHDOG_REMOVED'"
    )
    ok, out, err, _rc = ssh_exec(ip, username, password, cmd, timeout=30)
    return ok, (out if out.strip() else err)


# =======================
#  Böngésző-billentyűk (Home stb.) tiltása – udev hwdb
# =======================
# c0223 = AC Home (az inkognitó lapot nyitó gomb), c0221 = AC Search,
# c022a = AC Bookmarks, c018a = AL Mail. Kernel szinten tilt, X11/Wayland
# alatt is él, és a hwdb-ben marad, így ÚJRAINDÍTÁS UTÁN IS érvényes.
_HWDB_REMOTE_FILE = "/etc/udev/hwdb.d/99-scan-disable-browser-keys.hwdb"
_HWDB_RULE_TEXT = (
    "evdev:input:*\n"
    " KEYBOARD_KEY_c0223=reserved\n"
    " KEYBOARD_KEY_c0221=reserved\n"
    " KEYBOARD_KEY_c022a=reserved\n"
    " KEYBOARD_KEY_c018a=reserved\n"
)
_KEYBLOCK_OPTOUT_FLAG = "/etc/scan-keyblock.disabled"


def install_browser_key_block(
    ip: str, username: str, password: str, respect_optout: bool = False
) -> Tuple[bool, str]:
    """
    A Home/böngésző gombok tiltásának telepítése (udev hwdb szabály).
    Az automatikus provisioning is ezt hívja, így minden eszközön
    alapértelmezetten tiltva vannak ezek a gombok, reboot után is.

    respect_optout=True: ha az eszközön *005-tel kikapcsolták a tiltást
    (opt-out flag), nem telepíti újra. Kézi *004-nél respect_optout=False:
    telepít és törli az opt-out flaget.
    Idempotens: ha a szabály már fent van, nem futtatja újra a lassú
    hwdb-frissítést (KEYBLOCK_ALREADY).
    """
    if respect_optout:
        pre = f"if [ -f {_KEYBLOCK_OPTOUT_FLAG} ]; then echo KEYBLOCK_OPTOUT; exit 0; fi\n"
    else:
        pre = f"rm -f {_KEYBLOCK_OPTOUT_FLAG}\n"

    script = (
        "set -e\n"
        + pre
        + f"if grep -q c0223 {_HWDB_REMOTE_FILE} 2>/dev/null; then echo KEYBLOCK_ALREADY; exit 0; fi\n"
        + f"mkdir -p {os.path.dirname(_HWDB_REMOTE_FILE)}\n"
        + f"cat > {_HWDB_REMOTE_FILE} <<'HWDBEOF'\n{_HWDB_RULE_TEXT}HWDBEOF\n"
        + "systemd-hwdb update\n"
        + "udevadm trigger --sysname-match='event*'\n"
        + "echo KEYBLOCK_INSTALLED\n"
    )
    # a systemd-hwdb update lassú lehet a Pi-n, ezért hosszabb timeout
    ok, out, err, _rc = run_script_as_root(ip, username, password, script, timeout=45)
    return ok, (out if out.strip() else err)


def remove_browser_key_block(ip: str, username: str, password: str) -> Tuple[bool, str]:
    """
    A gombtiltás visszavonása. Opt-out flaget is letesz, hogy az automatikus
    provisioning ne rakja vissza ezen az eszközön.
    """
    script = (
        f"rm -f {_HWDB_REMOTE_FILE}\n"
        "systemd-hwdb update\n"
        "udevadm trigger --sysname-match='event*'\n"
        f"touch {_KEYBLOCK_OPTOUT_FLAG}\n"
        "echo KEYBLOCK_REMOVED\n"
    )
    ok, out, err, _rc = run_script_as_root(ip, username, password, script, timeout=45)
    return ok, (out if out.strip() else err)


# =======================
#  *00x parancsfigyelő (pi_cmd_listener) telepítése a Pi-kre
# =======================
_LISTENER_LOCAL_PATH = Path(__file__).resolve().parent.parent / "pi_files" / "pi_cmd_listener.py"
_LISTENER_REMOTE_PATH = "/usr/local/bin/pi_cmd_listener.py"
_LISTENER_UNIT_NAME = "pi-cmd-listener.service"
_LISTENER_UNIT_PATH = f"/etc/systemd/system/{_LISTENER_UNIT_NAME}"
_LISTENER_OPTOUT_FLAG = "/etc/pi-cmd-listener.disabled"


def install_pi_command_listener(
    ip: str, username: str, password: str, respect_optout: bool = False
) -> Tuple[bool, str]:
    """
    A *00x parancsfigyelő (pi_files/pi_cmd_listener.py) telepítése/frissítése
    a Pi-re systemd service-ként. A daemon a /dev/input eszközöket figyeli,
    így a *001..*005 parancsok a weboldaltól függetlenül is működnek.
    Idempotens: újratelepítés = frissítés + service restart.
    """
    try:
        src = _LISTENER_LOCAL_PATH.read_bytes()
    except OSError as e:
        return False, f"pi_cmd_listener.py nem olvasható: {e}"

    # NumLock alapértelmezett bekapcsolva tartása (.env: PI_NUMLOCK_FORCE=off kapcsolja ki)
    numlock_force = (os.getenv("PI_NUMLOCK_FORCE", "on") or "").strip().lower() not in (
        "off", "0", "false", "none", "disabled"
    )
    unit = (
        "[Unit]\n"
        "Description=Scan kiosk *00x parancsfigyelo\n"
        "After=multi-user.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart=/usr/bin/python3 {_LISTENER_REMOTE_PATH}\n"
        f"Environment=KIOSK_USER={username}\n"
        f"Environment=KIOSK_URL={get_kiosk_url()}\n"
        f"Environment=NUMLOCK_FORCE={'on' if numlock_force else 'off'}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )

    b64_script = base64.b64encode(src).decode("ascii")
    b64_unit = base64.b64encode(unit.encode("utf-8")).decode("ascii")

    if respect_optout:
        pre = f"if [ -f {_LISTENER_OPTOUT_FLAG} ]; then echo LISTENER_OPTOUT; exit 0; fi\n"
    else:
        pre = f"rm -f {_LISTENER_OPTOUT_FLAG}\n"

    script = (
        "set -e\n"
        + pre
        + f"echo {b64_script} | base64 -d > {_LISTENER_REMOTE_PATH}\n"
        + f"echo {b64_unit} | base64 -d > {_LISTENER_UNIT_PATH}\n"
        + "systemctl daemon-reload\n"
        + f"systemctl enable {_LISTENER_UNIT_NAME} >/dev/null 2>&1 || true\n"
        + f"systemctl restart {_LISTENER_UNIT_NAME}\n"
        + "echo LISTENER_INSTALLED\n"
    )
    ok, out, err, _rc = run_script_as_root(ip, username, password, script, timeout=40)
    return ok, (out if out.strip() else err)


def remove_pi_command_listener(ip: str, username: str, password: str) -> Tuple[bool, str]:
    """
    A parancsfigyelő leállítása + eltávolítása. Opt-out flaget is letesz,
    hogy az automatikus telepítés ne rakja vissza.
    """
    script = (
        f"systemctl disable --now {_LISTENER_UNIT_NAME} >/dev/null 2>&1\n"
        f"rm -f {_LISTENER_REMOTE_PATH} {_LISTENER_UNIT_PATH}\n"
        "systemctl daemon-reload\n"
        f"touch {_LISTENER_OPTOUT_FLAG}\n"
        "echo LISTENER_REMOVED\n"
    )
    ok, out, err, _rc = run_script_as_root(ip, username, password, script, timeout=30)
    return ok, (out if out.strip() else err)


def check_virtualenv_and_packages_on_pi(ip: str, username: str, password: str) -> Tuple[bool, str]:
    """
    Venv ellenőrzés/telepítés + repo frissítés + fájlok a helyükre.
    """
    script = r"""/bin/bash -lc '
set -e
# git
if ! command -v git >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y git
fi
# venv
if [ ! -d "/home/user/myenv" ]; then
  python3 -m venv /home/user/myenv
fi
source /home/user/myenv/bin/activate
pip install --upgrade pip
pip install ttkbootstrap mysql-connector Pillow netifaces

# repo
cd /home/user
if [ ! -d "QR-Code-Project" ]; then
  git clone https://github.com/SilcoBat/QR-Code-Project.git || echo "Git clone failed"
else
  cd QR-Code-Project && git pull || echo "Git pull failed"
fi

# fájlok
if [ -d "/home/user/QR-Code-Project" ]; then
  cp /home/user/QR-Code-Project/V3.7.py /home/user/Desktop/ || echo "V3.7.py copy failed"
  cp /home/user/QR-Code-Project/logo.png /home/user/ || echo "logo.png copy failed"
  echo "Installed"
else
  echo "QR-Code-Project directory not found"
fi
'"""
    return execute_command_on_pi(ip, username, password, script)
