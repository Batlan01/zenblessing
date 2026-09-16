# services/ldap_auth.py
from ldap3 import Server, Connection, ALL, Tls, NTLM, ALL_ATTRIBUTES
import ssl

AD_HOST = '10.10.2.12'
BASE_DN = 'DC=INSILCOSK,DC=local'
AD_USERNAME_ATTR = 'sAMAccountName'
AD_NAME_ATTR = 'displayName'
AD_TITLE_ATTR = 'title'

def _make_server(host: str, use_ssl: bool = False, start_tls: bool = False) -> Server:
    if use_ssl:
        # LDAPS: tipikusan 636, szerver tanúsítvány lehet self-signed; ha kell, változtasd CERT_REQUIRED-ra és add meg a CA-t
        tls = Tls(validate=ssl.CERT_NONE)  # PROD-ban érdemes validálni!
        return Server(host, use_ssl=True, get_info=ALL, tls=tls)
    else:
        # sima LDAP 389; STARTTLS-t a Connection-on kérjük majd, ha kell
        return Server(host, use_ssl=False, get_info=ALL)

def _bind_simple(server: Server, username: str, password: str, start_tls: bool = False) -> Connection | None:
    """
    SIMPLE bind domain\\user vagy user@domain formátummal.
    """
    try:
        conn = Connection(server, user=username, password=password, authentication='SIMPLE', auto_bind=False)
        if start_tls:
            if not conn.start_tls():  # STARTTLS a 389-en
                return None
        if conn.bind():
            return conn
    except Exception:
        pass
    return None

def _bind_ldaps(username: str, password: str) -> Connection | None:
    try:
        srv = _make_server(AD_HOST, use_ssl=True)
        conn = Connection(srv, user=username, password=password, authentication='SIMPLE', auto_bind=True)
        return conn if conn.bound else None
    except Exception:
        return None

def get_user_attributes(conn: Connection, username: str):
    conn.search(
        search_base=BASE_DN,
        search_filter=f'({AD_USERNAME_ATTR}={username})',
        attributes=[AD_NAME_ATTR, AD_TITLE_ATTR]
    )
    if conn.entries:
        user = conn.entries[0]
        return {
            'display_name': getattr(user, AD_NAME_ATTR).value if hasattr(user, AD_NAME_ATTR) else 'N/A',
            'job_title': getattr(user, AD_TITLE_ATTR).value if hasattr(user, AD_TITLE_ATTR) else None,
            'username': username,
        }
    return None

def authenticate_user(username: str, password: str):
    """
    1) SIMPLE bind LDAP 389 (titkosítás nélkül)
    2) SIMPLE bind LDAP 389 + STARTTLS (ha a szerver megköveteli a titkosítást)
    3) SIMPLE bind LDAPS 636
    4) (Opció) NTLM bind PyCryptodome MD4 shimmel – csak ha ragaszkodsz az NTLM-hez.
    """
    if not username or not password:
        return None

    # A legtöbb AD elfogadja mindkettőt:
    user_dn_backslash = fr'INSILCOSK\{username}'
    user_upn = f'{username}@insilcosk.local'

    # 1) SIMPLE 389
    try:
        srv = _make_server(AD_HOST, use_ssl=False)
        conn = _bind_simple(srv, user_dn_backslash, password, start_tls=False) or \
               _bind_simple(srv, user_upn,          password, start_tls=False)
        if conn and conn.bound:
            attrs = get_user_attributes(conn, username)
            conn.unbind()
            return attrs
    except Exception:
        pass

    # 2) SIMPLE 389 + STARTTLS
    try:
        srv = _make_server(AD_HOST, use_ssl=False)
        conn = _bind_simple(srv, user_dn_backslash, password, start_tls=True) or \
               _bind_simple(srv, user_upn,          password, start_tls=True)
        if conn and conn.bound:
            attrs = get_user_attributes(conn, username)
            conn.unbind()
            return attrs
    except Exception:
        pass

    # 3) LDAPS 636
    try:
        conn = _bind_ldaps(user_dn_backslash, password) or _bind_ldaps(user_upn, password)
        if conn and conn.bound:
            attrs = get_user_attributes(conn, username)
            conn.unbind()
            return attrs
    except Exception:
        pass

    # 4) Opcionális NTLM (MD4 shim szükséges)
    #    Ha szeretnéd mégis NTLM-mel használni:
    #    pip install pycryptodome
    #    és engedélyezd az alábbi blokkot (vedd ki a tripla idézőjelet).
    """
    try:
        # --- MD4 shim: a ldap3 NTLM-je hashlib.md4-et hív; itt pótoljuk ---
        import hashlib
        from Crypto.Hash import MD4 as _MD4

        _orig_new = hashlib.new

        def _new(name, data=b''):
            if name.lower() == 'md4':
                h = _MD4.new()
                if data: h.update(data)
                class _Wrap:
                    def update(self, d): h.update(d)
                    def digest(self): return h.digest()
                    def hexdigest(self): return h.hexdigest()
                return _Wrap()
            return _orig_new(name, data)

        hashlib.new = _new  # monkeypatch csak a folyamatban

        server = _make_server(AD_HOST, use_ssl=False)
        conn = Connection(server, user=user_dn_backslash, password=password, authentication=NTLM, auto_bind=True)
        if conn.bound:
            attrs = get_user_attributes(conn, username)
            conn.unbind()
            return attrs
    except Exception:
        pass
    """

    return None
