"""Самообновление: проверка новой версии на GitHub (или по ссылке на update.json), скачивание и замена exe."""
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

from appinfo import EXE_NAME

HEADERS = {"User-Agent": "BibaLauncher-Updater"}


class UpdateError(Exception):
    """code: no_source, bad_source, no_release, no_asset, rate, http, network, insecure, checksum, too_small."""

    def __init__(self, code, detail=""):
        super().__init__(code + (": " + detail if detail else ""))
        self.code, self.detail = code, detail


def parse_version(text):
    """'v1.2.10-beta' -> (1, 2, 10); недостающие части считаются нулями."""
    head = re.split(r"[-+ ]", str(text or "").strip().lstrip("vV"), maxsplit=1)[0]
    nums = [int(n) for n in re.findall(r"\d+", head)]
    return tuple(nums + [0] * (3 - len(nums)))


def is_newer(remote, local):
    return parse_version(remote) > parse_version(local)


def github_slug(source):
    """'https://github.com/owner/repo/releases' или 'owner/repo' -> 'owner/repo'."""
    m = re.fullmatch(r"(?:https?://(?:www\.)?github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/.*)?", (source or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _is_manifest(source):
    if not source.startswith(("http://", "https://")):
        return False
    return urlparse(source).netloc.lower().removeprefix("www.") != "github.com"


def _get(url, **kw):
    try:
        return requests.get(url, timeout=kw.pop("timeout", 15), **kw)
    except requests.RequestException:
        raise UpdateError("network") from None


def _digest(asset, assets):
    """Контрольная сумма SHA-256: из поля digest релиза или из файла <имя>.sha256 рядом."""
    value = str(asset.get("digest") or "")
    if value.lower().startswith("sha256:"):
        return value.split(":", 1)[1].lower()
    side = next((a for a in assets if a["name"].lower() == asset["name"].lower() + ".sha256"), None)
    if side:
        try:
            m = re.search(r"\b[0-9a-fA-F]{64}\b", _get(side["browser_download_url"], headers=HEADERS).text)
            return m.group(0).lower() if m else None
        except UpdateError:
            return None
    return None


def _github(slug):
    r = _get(f"https://api.github.com/repos/{slug}/releases/latest",
             headers={**HEADERS, "Accept": "application/vnd.github+json"})
    if r.status_code == 404:
        raise UpdateError("no_release")
    if r.status_code in (403, 429):
        raise UpdateError("rate")
    if r.status_code != 200:
        raise UpdateError("http", str(r.status_code))
    data = r.json()
    assets = data.get("assets") or []
    exe = (next((a for a in assets if a["name"].lower() == EXE_NAME.lower()), None)
           or next((a for a in assets if a["name"].lower().endswith(".exe")), None))
    if exe is None:
        raise UpdateError("no_asset")
    return {"version": str(data.get("tag_name") or data.get("name") or ""), "url": exe["browser_download_url"],
            "sha256": _digest(exe, assets), "notes": data.get("body") or "",
            "page": data.get("html_url") or f"https://github.com/{slug}/releases"}


def _manifest(url):
    if not url.startswith("https://"):
        raise UpdateError("insecure")
    r = _get(url, headers=HEADERS)
    if r.status_code != 200:
        raise UpdateError("http", str(r.status_code))
    try:
        data = r.json()
    except ValueError:
        raise UpdateError("bad_source") from None
    if not isinstance(data, dict) or not data.get("version") or not data.get("url"):
        raise UpdateError("bad_source")
    return {"version": str(data["version"]), "url": data["url"], "sha256": data.get("sha256") or None,
            "notes": data.get("notes") or "", "page": data.get("page") or url}


def check(source, current):
    """None, если версия свежая; иначе словарь version, url, sha256, notes, page."""
    source = (source or "").strip()
    if not source:
        raise UpdateError("no_source")
    if _is_manifest(source):
        info = _manifest(source)
    else:
        slug = github_slug(source)
        if not slug:
            raise UpdateError("bad_source")
        info = _github(slug)
    return info if is_newer(info["version"], current) else None


def can_self_update():
    """Заменять себя можно только собранному exe на Windows."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def new_exe_path():
    exe = Path(sys.executable)
    return exe.with_name(exe.name + ".new")


def download(url, dest, sha256=None, progress=None, min_size=1_000_000):
    """Скачивает файл рядом с exe, сверяет SHA-256 (если известна) и размер."""
    if not url.startswith("https://"):
        raise UpdateError("insecure")
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    digest, got = hashlib.sha256(), 0
    try:
        with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    digest.update(chunk)
                    got += len(chunk)
                    if progress and total:
                        progress(got / total)
    except requests.RequestException:
        part.unlink(missing_ok=True)
        raise UpdateError("network") from None
    if sha256 and digest.hexdigest() != sha256.lower():
        part.unlink(missing_ok=True)
        raise UpdateError("checksum")
    if got < min_size:
        part.unlink(missing_ok=True)
        raise UpdateError("too_small")
    part.replace(dest)
    return dest


def apply_update(new_path, exe=None):
    """Windows позволяет переименовать запущенный exe: старый уходит в .old, новый встаёт на его место."""
    exe = Path(exe or sys.executable)
    old = exe.with_name(exe.name + ".old")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass
    os.replace(exe, old)
    try:
        os.replace(new_path, exe)
    except OSError:
        os.replace(old, exe)                      # откат
        raise
    return exe


def restart(exe):
    """Запускает новую копию. У onefile-сборки PyInstaller нужно сбросить служебные переменные,
    иначе новый процесс попытается жить во временной папке старого."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe)], env=env, close_fds=True, creationflags=flags, cwd=str(Path(exe).parent))


def cleanup(exe=None):
    """Удаляет хвосты прошлого обновления (.old, .new)."""
    exe = Path(exe or sys.executable)
    for suffix in (".old", ".new", ".new.part"):
        try:
            exe.with_name(exe.name + suffix).unlink(missing_ok=True)
        except OSError:
            pass
