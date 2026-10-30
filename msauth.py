"""Вход через аккаунт Microsoft (лицензия Minecraft Java).
Цепочка: код устройства -> токен Microsoft -> Xbox Live -> XSTS -> Minecraft Services -> профиль."""
import re
import time

import requests

AAD = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
SCOPE = "XboxLive.signin offline_access"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
FORM_URL = "https://aka.ms/mce-reviewappid"                      # форма Mojang: одобрение приложения Azure
JSON = {"Content-Type": "application/json", "Accept": "application/json"}
XERR = {2148916233: "no_xbox", 2148916235: "country", 2148916236: "adult", 2148916237: "adult", 2148916238: "child"}


class MsError(Exception):
    """code: network, bad_client, public_client, denied, expired, cancelled, no_app, no_xbox, country,
    adult, child, no_game, rate, session, other."""

    def __init__(self, code, detail=""):
        super().__init__(code + (": " + detail if detail else ""))
        self.code, self.detail = code, detail


def valid_client_id(text):
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text or ""))


def _call(fn, url, **kw):
    try:
        return fn(url, timeout=20, **kw)
    except requests.RequestException:
        raise MsError("network") from None


def _post(url, **kw):
    return _call(requests.post, url, **kw)


def _get(url, **kw):
    return _call(requests.get, url, **kw)


def _json(r):
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _aad_error(data):
    desc = data.get("error_description") or data.get("error") or ""
    if "AADSTS700016" in desc:
        return MsError("bad_client")
    if "AADSTS7000218" in desc or "AADSTS70002" in desc or data.get("error") == "invalid_client":
        return MsError("public_client")
    return MsError("other", desc.splitlines()[0][:200] if desc else "")


# ── шаг 1: код устройства ──
def start_device_login(client_id):
    r = _post(f"{AAD}/devicecode", data={"client_id": client_id, "scope": SCOPE})
    data = _json(r)
    if r.status_code != 200 or "device_code" not in data:
        raise _aad_error(data)
    return data


def wait_for_token(client_id, device, cancelled=lambda: False):
    """Ждёт, пока пользователь введёт код на сайте. Блокирующая функция: запускать в потоке."""
    interval = int(device.get("interval", 5))
    deadline = time.time() + int(device.get("expires_in", 900))
    while time.time() < deadline:
        for _ in range(interval * 2):
            if cancelled():
                raise MsError("cancelled")
            time.sleep(0.5)
        r = _post(f"{AAD}/token", data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                        "client_id": client_id, "device_code": device["device_code"]})
        data = _json(r)
        if r.status_code == 200 and data.get("access_token"):
            return data
        err = data.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err in ("authorization_declined", "access_denied"):
            raise MsError("denied")
        if err in ("expired_token", "bad_verification_code"):
            raise MsError("expired")
        raise _aad_error(data)
    raise MsError("expired")


# ── шаги 2-5: Xbox Live, XSTS, Minecraft ──
def _skin_of(profile):
    active = next((s for s in profile.get("skins") or [] if s.get("state") == "ACTIVE"), None) or {}
    return active.get("url"), str(active.get("variant", "")).upper() == "SLIM"


def _profile(token):
    r = _get(MC_PROFILE_URL, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 404:
        raise MsError("no_game")
    if r.status_code != 200:
        raise MsError("other", f"profile {r.status_code}")
    return _json(r)


def profile_skin(token):
    """Актуальный скин аккаунта: (ссылка, тонкие ли руки)."""
    return _skin_of(_profile(token))


def minecraft_login(ms_token):
    r = _post(XBL_URL, headers=JSON, json={
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": "d=" + ms_token},
        "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
    xbl = _json(r)
    if r.status_code != 200 or "Token" not in xbl:
        raise MsError("other", f"Xbox Live {r.status_code}")
    uhs = xbl["DisplayClaims"]["xui"][0]["uhs"]

    r = _post(XSTS_URL, headers=JSON, json={
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
        "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
    xsts = _json(r)
    if r.status_code != 200 or "Token" not in xsts:
        raise MsError(XERR.get(xsts.get("XErr"), "other"), str(xsts.get("XErr", r.status_code)))

    r = _post(MC_LOGIN_URL, headers=JSON, json={"identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"})
    mc = _json(r)
    if r.status_code != 200 or "access_token" not in mc:
        if "Invalid app registration" in str(mc):
            raise MsError("no_app")
        raise MsError("rate" if r.status_code == 429 else "other", f"Minecraft {r.status_code}")
    token = mc["access_token"]
    expires = time.time() + int(mc.get("expires_in", 86400))
    profile = _profile(token)
    url, slim = _skin_of(profile)
    return {"name": profile["name"], "uuid": profile["id"], "token": token, "expires": expires,
            "skin_url": url, "slim": slim}


def login_with_token(tokens):
    acc = minecraft_login(tokens["access_token"])
    acc["refresh"] = tokens.get("refresh_token", "")
    return acc


def refresh(client_id, refresh_token):
    """Обновляет сессию без повторного ввода пароля."""
    if not client_id or not refresh_token:
        raise MsError("session")
    r = _post(f"{AAD}/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                    "refresh_token": refresh_token, "scope": SCOPE})
    data = _json(r)
    if r.status_code != 200 or not data.get("access_token"):
        if data.get("error") in ("invalid_grant", "interaction_required"):
            raise MsError("session")
        raise _aad_error(data)
    acc = minecraft_login(data["access_token"])
    acc["refresh"] = data.get("refresh_token", refresh_token)
    return acc
