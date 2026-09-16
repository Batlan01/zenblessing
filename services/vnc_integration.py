# ══════════════════════════════════════════════════════════════════
#  VNC modul integrálása a meglévő Flask appba
# ══════════════════════════════════════════════════════════════════

# ── 1. Csomagok telepítése ─────────────────────────────────────────
#
#   pip install websockify
#   sudo apt install novnc          # vagy: pip install novnc
#
#   A noVNC alapértelmezett helye apt után: /usr/share/novnc
#   Ha másik helyen van, módosítsd a routes/vnc.py-ban: NOVNC_PATH

# ── 2. routes/__init__.py – blueprint regisztrálása ────────────────
#
# Keresd meg a register_blueprints() funkciót és add hozzá:
#
#   from routes.vnc import vnc_bp
#   app.register_blueprint(vnc_bp)
#
# Példa:
#
#   def register_blueprints(app):
#       from routes.main   import main_bp
#       from routes.auth   import auth_bp
#       from routes.vnc    import vnc_bp        # <-- ez az új sor
#       app.register_blueprint(main_bp)
#       app.register_blueprint(auth_bp)
#       app.register_blueprint(vnc_bp)          # <-- és ez

# ── 3. base.html sidebar – link hozzáadása ─────────────────────────
#
# A <ul class="nav flex-column gap-1"> blokkba add hozzá:
#
#   {% if user and can_view('vnc') %}
#   <li class="nav-item">
#     <a class="nav-link {% if active=='vnc' %}active{% endif %}"
#        href="{{ url_for('vnc.dashboard', lang=current_lang) }}">
#       <i class="fa-solid fa-display"></i><span>VNC</span>
#     </a>
#   </li>
#   {% endif %}

# ── 4. utils/roles.py – jogosultság (ha van ilyen logika) ──────────
#
# Ha a can_view() egy dict/set alapján dolgozik, add hozzá a 'vnc' kulcsot
# a megfelelő szerepkörökhöz. Pl.:
#
#   ROLE_PERMISSIONS = {
#       "admin":    {"database", "notifications", "devices", "vnc", ...},
#       "operator": {"devices", "vnc"},
#       ...
#   }

# ── 5. Fájlok elhelyezése ──────────────────────────────────────────
#
#   routes/vnc.py                        ← blueprint
#   templates/vnc/dashboard.html         ← eszköz lista
#   templates/vnc/viewer.html            ← fullscreen VNC nézet

# ── 6. Eszközök konfigurálása ──────────────────────────────────────
#
# routes/vnc.py tetején a DEVICES dict-ben add meg az eszközöket.
# Minden eszköznek egyedi ws_port kell (pl. 6080, 6081, 6082…)
#
#   DEVICES = {
#       "pi-gepsor1": {
#           "name":     "Gépsor 1 – Kezelő",
#           "host":     "192.168.1.101",   # Raspberry IP
#           "vnc_port": 5900,              # VNC port az eszközön
#           "ws_port":  6080,              # websockify port (egyedi!)
#           "icon":     "fa-desktop",      # Font Awesome ikon
#           "location": "Csarnok A",
#       },
#       ...
#   }

# ── 7. URL-ek ──────────────────────────────────────────────────────
#
#   /hu/vnc/           → eszközlista dashboard
#   /hu/vnc/view/<id>  → fullscreen VNC viewer
#   /hu/vnc/api/status → JSON státusz
#
#   /sk/vnc/           → ugyanaz szlovák route-tal

# ── 8. Raspberry Pi oldalon (x11vnc) ──────────────────────────────
#
# Ha még nincs VNC szerver a Pi-n:
#   sudo apt install x11vnc
#   x11vnc -display :0 -forever -loop -noxdamage -repeat -rfbauth ~/.vnc/passwd -rfbport 5900
#
#   Vagy autostart-hoz /etc/systemd/system/x11vnc.service fájlba.