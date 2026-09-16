# services/ad_users.py
# -*- coding: utf-8 -*-
"""
Active Directory felhasználó-kezelő szolgáltatás.

Funkciók:
  - felhasználók listázása (kereséssel)
  - egy felhasználó összes (lényeges) property-jének lekérése
  - job title (title attribútum) átírása
  - új felhasználó létrehozása (jelszóval, engedélyezett fiókkal)
  - jelszó resetelése

Az írási műveletekhez (title módosítás, létrehozás, jelszó) egy
szolgáltatás-/admin fiók szükséges, amit környezeti változókból olvasunk:

    AD_ADMIN_USER       pl. "INSILCOSK\\svc_ad"  vagy "svc_ad@insilcosk.local"
    AD_ADMIN_PASSWORD   a fiók jelszava

A jelszóval kapcsolatos műveletek (reset, létrehozáskori beállítás) az
AD-ben KIZÁRÓLAG titkosított csatornán (LDAPS 636) engedélyezettek.
"""
from __future__ import annotations

import os
import ssl
from typing import Any

from ldap3 import (
    Server,
    Connection,
    ALL,
    Tls,
    SUBTREE,
    MODIFY_REPLACE,
)
from ldap3.core.exceptions import LDAPException

# --- AD alapkonfiguráció (a ldap_auth.py-vel összhangban, env-ből felülírható) ---
AD_HOST      = os.getenv("AD_HOST", "10.10.2.12")
BASE_DN      = os.getenv("AD_BASE_DN", "DC=INSILCOSK,DC=local")
AD_DOMAIN    = os.getenv("AD_DOMAIN", "insilcosk.local")
AD_NETBIOS   = os.getenv("AD_NETBIOS", "INSILCOSK")

# Hova jöjjenek létre az új felhasználók (alapból a beépített Users konténer)
AD_USERS_OU  = os.getenv("AD_USERS_OU", f"CN=Users,{BASE_DN}")

# Admin/szolgáltatás fiók az írási műveletekhez
AD_ADMIN_USER     = os.getenv("AD_ADMIN_USER", "")
AD_ADMIN_PASSWORD = os.getenv("AD_ADMIN_PASSWORD", "")

# Mely attribútumokat kérjük le / mutatjuk meg a részleteknél
USER_DETAIL_ATTRS = [
    "sAMAccountName", "displayName", "givenName", "sn", "title",
    "department", "mail", "telephoneNumber", "mobile", "company",
    "physicalDeliveryOfficeName", "description", "manager",
    "userPrincipalName", "distinguishedName", "userAccountControl",
    "whenCreated", "whenChanged", "lastLogonTimestamp", "memberOf",
]

# Csak felhasználói fiókok (nem gépek, nem csoportok)
USER_OBJECT_FILTER = "(&(objectCategory=person)(objectClass=user))"

# Csak számítógép-fiókok
COMPUTER_OBJECT_FILTER = "(objectCategory=computer)"

# userAccountControl bit
UAC_ACCOUNTDISABLE = 0x0002


class ADError(Exception):
    """Egységes hibatípus a route réteg felé."""


# ----------------------------------------------------------------------------
# Kapcsolat
# ----------------------------------------------------------------------------
def _server(use_ssl: bool) -> Server:
    if use_ssl:
        tls = Tls(validate=ssl.CERT_NONE)  # PROD-ban érdemes valid CA-val ellenőrizni
        return Server(AD_HOST, port=636, use_ssl=True, get_info=ALL, tls=tls)
    return Server(AD_HOST, port=389, use_ssl=False, get_info=ALL)


def _admin_conn(use_ssl: bool = False) -> Connection:
    """
    Bejelentkezés az admin/szolgáltatás fiókkal.

    use_ssl=False  -> LDAP 389 (olvasás, title módosítás)
    use_ssl=True   -> LDAPS 636 (jelszó beállítás/reset, fiók létrehozás)
    """
    if not AD_ADMIN_USER or not AD_ADMIN_PASSWORD:
        raise ADError(
            "Hiányzó AD admin hitelesítő adatok. Állítsd be az "
            "AD_ADMIN_USER és AD_ADMIN_PASSWORD környezeti változókat."
        )
    try:
        conn = Connection(
            _server(use_ssl),
            user=AD_ADMIN_USER,
            password=AD_ADMIN_PASSWORD,
            authentication="SIMPLE",
            auto_bind=True,
        )
        if not conn.bound:
            raise ADError("Nem sikerült bejelentkezni az AD admin fiókkal.")
        return conn
    except ADError:
        raise
    except LDAPException as e:
        raise ADError(f"AD kapcsolódási hiba: {e}") from e


def _val(entry, attr) -> Any:
    """Biztonságos attribútum-kiolvasás egy ldap3 entry-ből."""
    try:
        a = entry[attr]
    except Exception:
        return None
    v = getattr(a, "value", None)
    return v


def _is_disabled(uac) -> bool | None:
    try:
        return bool(int(uac) & UAC_ACCOUNTDISABLE)
    except (TypeError, ValueError):
        return None


def _escape_filter(value: str) -> str:
    """RFC4515 szűrő-escape, hogy a keresés ne legyen injektálható."""
    out = []
    for ch in value or "":
        if ch == "\\":
            out.append("\\5c")
        elif ch == "*":
            out.append("\\2a")
        elif ch == "(":
            out.append("\\28")
        elif ch == ")":
            out.append("\\29")
        elif ch == "\x00":
            out.append("\\00")
        else:
            out.append(ch)
    return "".join(out)


# ----------------------------------------------------------------------------
# Listázás / lekérdezés
# ----------------------------------------------------------------------------
def list_users(query: str | None = None, limit: int = 500) -> list[dict]:
    """
    Felhasználók listázása. Ha 'query' meg van adva, akkor a
    sAMAccountName / displayName / mail mezőkben keres (részleges).
    """
    conn = _admin_conn(use_ssl=False)
    try:
        if query:
            q = _escape_filter(query.strip())
            search_filter = (
                f"(&{USER_OBJECT_FILTER}"
                f"(|(sAMAccountName=*{q}*)(displayName=*{q}*)(mail=*{q}*)))"
            )
        else:
            search_filter = USER_OBJECT_FILTER

        conn.search(
            search_base=BASE_DN,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["sAMAccountName", "displayName", "title",
                        "mail", "department", "userAccountControl"],
            size_limit=limit,
        )

        users = []
        for e in conn.entries:
            uac = _val(e, "userAccountControl")
            users.append({
                "username":     _val(e, "sAMAccountName"),
                "display_name": _val(e, "displayName"),
                "job_title":    _val(e, "title"),
                "mail":         _val(e, "mail"),
                "department":   _val(e, "department"),
                "disabled":     _is_disabled(uac),
            })

        users = [u for u in users if u["username"]]
        users.sort(key=lambda u: (u["display_name"] or u["username"] or "").lower())
        return users
    finally:
        conn.unbind()


def resolve_computer(hostname: str) -> dict | None:
    """
    Egy számítógép-fiók feloldása a hostname alapján (AD computer objektum).
    Visszaadja a gép DN-jét, az OU-ját (a DN szülő útvonala) és a csoporttagságait.

    A domainre kötött gép sAMAccountName-je 'HOSTNAME$'. A hostname-ből levágjuk
    a domain részt, ha FQDN-t kapunk (pl. PC1.insilcosk.local -> PC1).
    """
    if not hostname:
        return None
    short = hostname.split(".")[0].strip().rstrip("$")
    if not short:
        return None

    conn = _admin_conn(use_ssl=False)
    try:
        safe = _escape_filter(short)
        conn.search(
            search_base=BASE_DN,
            search_filter=(
                f"(&{COMPUTER_OBJECT_FILTER}"
                f"(|(sAMAccountName={safe}$)(cn={safe})(dNSHostName={safe}*)))"
            ),
            search_scope=SUBTREE,
            attributes=["distinguishedName", "memberOf", "operatingSystem",
                        "dNSHostName", "cn"],
        )
        if not conn.entries:
            return None

        e = conn.entries[0]
        dn = str(e.entry_dn)
        # OU = a DN szülő útvonala (az első komponens, pl. CN=PC1 levágva)
        ou = dn.split(",", 1)[1] if "," in dn else dn

        member_of = _val(e, "memberOf")
        if member_of and not isinstance(member_of, list):
            member_of = [member_of]
        groups = []
        for gdn in (member_of or []):
            cn = str(gdn).split(",", 1)[0]
            groups.append(cn[3:] if cn.upper().startswith("CN=") else cn)

        return {
            "hostname": short,
            "dn": dn,
            "ou": ou,
            "groups": groups,
            "os": str(_val(e, "operatingSystem") or ""),
            "dns_hostname": str(_val(e, "dNSHostName") or ""),
        }
    finally:
        conn.unbind()


def _find_user_dn(conn: Connection, username: str) -> str | None:
    """Megkeresi egy sAMAccountName-hez tartozó distinguishedName-et."""
    safe = _escape_filter(username.strip())
    conn.search(
        search_base=BASE_DN,
        search_filter=f"(&{USER_OBJECT_FILTER}(sAMAccountName={safe}))",
        search_scope=SUBTREE,
        attributes=["distinguishedName"],
    )
    if conn.entries:
        return str(conn.entries[0].entry_dn)
    return None


def get_user(username: str) -> dict | None:
    """Egy felhasználó részletes property-jeinek lekérése sAMAccountName alapján."""
    if not username:
        return None
    conn = _admin_conn(use_ssl=False)
    try:
        safe = _escape_filter(username.strip())
        conn.search(
            search_base=BASE_DN,
            search_filter=f"(&{USER_OBJECT_FILTER}(sAMAccountName={safe}))",
            search_scope=SUBTREE,
            attributes=USER_DETAIL_ATTRS,
        )
        if not conn.entries:
            return None

        e = conn.entries[0]
        uac = _val(e, "userAccountControl")

        member_of = _val(e, "memberOf")
        if member_of and not isinstance(member_of, list):
            member_of = [member_of]
        groups = []
        for dn in (member_of or []):
            # CN=Group Name,OU=... -> "Group Name"
            cn = str(dn).split(",", 1)[0]
            groups.append(cn[3:] if cn.upper().startswith("CN=") else cn)

        def s(attr):
            v = _val(e, attr)
            return "" if v is None else str(v)

        return {
            "username":        s("sAMAccountName"),
            "display_name":    s("displayName"),
            "given_name":      s("givenName"),
            "surname":         s("sn"),
            "job_title":       s("title"),
            "department":      s("department"),
            "company":         s("company"),
            "office":          s("physicalDeliveryOfficeName"),
            "mail":            s("mail"),
            "phone":           s("telephoneNumber"),
            "mobile":          s("mobile"),
            "description":     s("description"),
            "upn":             s("userPrincipalName"),
            "dn":              str(e.entry_dn),
            "disabled":        _is_disabled(uac),
            "when_created":    s("whenCreated"),
            "when_changed":    s("whenChanged"),
            "groups":          groups,
        }
    finally:
        conn.unbind()


# ----------------------------------------------------------------------------
# Módosítás
# ----------------------------------------------------------------------------
def update_job_title(username: str, new_title: str) -> dict:
    """A 'title' (job title) attribútum átírása."""
    if not username:
        raise ADError("Hiányzó felhasználónév.")
    new_title = (new_title or "").strip()

    conn = _admin_conn(use_ssl=False)
    try:
        dn = _find_user_dn(conn, username)
        if not dn:
            raise ADError(f"A felhasználó nem található: {username}")

        # Üres érték -> attribútum törlése (MODIFY_REPLACE üres listával)
        value = [new_title] if new_title else []
        ok = conn.modify(dn, {"title": [(MODIFY_REPLACE, value)]})
        if not ok:
            raise ADError(f"A módosítás nem sikerült: {conn.result.get('description')}")
        return {"username": username, "job_title": new_title, "dn": dn}
    finally:
        conn.unbind()


# ----------------------------------------------------------------------------
# Jelszó reset
# ----------------------------------------------------------------------------
def reset_password(username: str, new_password: str,
                   must_change: bool = True) -> dict:
    """
    Jelszó resetelése. LDAPS (636) kötelező.
    must_change=True esetén a felhasználónak a következő belépéskor
    cserélnie kell a jelszót (pwdLastSet=0).
    """
    if not username:
        raise ADError("Hiányzó felhasználónév.")
    if not new_password:
        raise ADError("Hiányzó új jelszó.")

    conn = _admin_conn(use_ssl=True)  # password műveletek csak titkosított csatornán
    try:
        dn = _find_user_dn(conn, username)
        if not dn:
            raise ADError(f"A felhasználó nem található: {username}")

        ok = conn.extend.microsoft.modify_password(dn, new_password)
        if not ok:
            raise ADError(
                f"A jelszó beállítása nem sikerült: {conn.result.get('description')} "
                f"({conn.result.get('message')})"
            )

        if must_change:
            # pwdLastSet=0 -> kötelező csere a következő belépésnél
            conn.modify(dn, {"pwdLastSet": [(MODIFY_REPLACE, [0])]})

        return {"username": username, "dn": dn}
    finally:
        conn.unbind()


# ----------------------------------------------------------------------------
# Új felhasználó létrehozása
# ----------------------------------------------------------------------------
def create_user(*, username: str, first_name: str, last_name: str,
                password: str, job_title: str = "", department: str = "",
                mail: str = "", ou: str | None = None,
                must_change: bool = True) -> dict:
    """
    Új AD felhasználó létrehozása, jelszó beállítása és fiók engedélyezése.
    LDAPS (636) szükséges a jelszó beállításához.
    """
    username = (username or "").strip()
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()

    if not username:
        raise ADError("A felhasználónév (sAMAccountName) kötelező.")
    if not first_name or not last_name:
        raise ADError("A keresztnév és a vezetéknév kötelező.")
    if not password:
        raise ADError("A kezdeti jelszó kötelező.")

    target_ou = (ou or AD_USERS_OU).strip()
    display_name = f"{first_name} {last_name}".strip()
    upn = f"{username}@{AD_DOMAIN}"
    # CN escape a vesszőhöz/speciális karakterekhez
    cn_safe = display_name.replace("\\", "\\\\").replace(",", "\\,")
    dn = f"CN={cn_safe},{target_ou}"

    attrs = {
        "objectClass": ["top", "person", "organizationalPerson", "user"],
        "sAMAccountName": username,
        "userPrincipalName": upn,
        "displayName": display_name,
        "givenName": first_name,
        "sn": last_name,
        "name": display_name,
    }
    if job_title:
        attrs["title"] = job_title
    if department:
        attrs["department"] = department
    if mail:
        attrs["mail"] = mail

    conn = _admin_conn(use_ssl=True)
    try:
        # Ne hozzunk létre duplikátumot
        if _find_user_dn(conn, username):
            raise ADError(f"Már létezik felhasználó ezzel a névvel: {username}")

        if not conn.add(dn, attributes=attrs):
            raise ADError(
                f"A felhasználó létrehozása nem sikerült: "
                f"{conn.result.get('description')} ({conn.result.get('message')})"
            )

        # Jelszó beállítása
        if not conn.extend.microsoft.modify_password(dn, password):
            # takarítás: a jelszó nélküli, letiltott fiók ne maradjon ott
            conn.delete(dn)
            raise ADError(
                f"A jelszó beállítása nem sikerült, a fiók nem jött létre: "
                f"{conn.result.get('description')} ({conn.result.get('message')})"
            )

        # Fiók engedélyezése (NORMAL_ACCOUNT = 512)
        if not conn.modify(dn, {"userAccountControl": [(MODIFY_REPLACE, [512])]}):
            raise ADError(
                f"A fiók engedélyezése nem sikerült: {conn.result.get('description')}"
            )

        if must_change:
            conn.modify(dn, {"pwdLastSet": [(MODIFY_REPLACE, [0])]})

        return {"username": username, "display_name": display_name,
                "upn": upn, "dn": dn}
    finally:
        conn.unbind()
