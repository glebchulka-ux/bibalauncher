"""Клиент Modrinth API v2: поиск модов и скачивание прямо в папку игры."""
import hashlib
import io
import json
import re
from pathlib import Path

import requests
from PIL import Image

import modinfo

BASE = "https://api.modrinth.com/v2"
HEADERS = {"User-Agent": "BibaLauncher/1.0 (Minecraft launcher)"}      # Modrinth требует узнаваемый User-Agent
LOADERS = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge"}
KINDS = {                                              # что можно скачивать и в какую папку сборки это кладётся
    "mod": {"type": "mod", "folder": "mods", "loader": True},
    "shader": {"type": "shader", "folder": "shaderpacks", "loader": False},
    "resourcepack": {"type": "resourcepack", "folder": "resourcepacks", "loader": False},
    "modpack": {"type": "modpack", "folder": None, "loader": False},
}


def parse_version(vid):
    """Из id версии достаёт (версия Minecraft, загрузчик): fabric-loader-0.19.3-1.21.11 -> ('1.21.11', 'fabric')."""
    m = re.fullmatch(r"(fabric|quilt)-loader-[\w.+]+-(\d[\w.]*)", vid)
    if m:
        return m.group(2), m.group(1)
    m = re.fullmatch(r"(\d[\w.]*)-forge-[\w.+-]+", vid)
    if m:
        return m.group(1), "forge"
    m = re.fullmatch(r"neoforge-(\d+)\.(\d+)[\w.+-]*", vid)
    if m:
        return "1.%s%s" % (m.group(1), "" if m.group(2) == "0" else "." + m.group(2)), "neoforge"
    return vid, None


def pretty_version(vid):
    mc, loader = parse_version(vid)
    return f"{LOADERS[loader]} {mc}" if loader else vid


def compact(n):
    n = int(n or 0)
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def _get(path, **params):
    r = requests.get(BASE + path, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def search(query, mc, loader, limit=20, kind="mod"):
    info = KINDS[kind]
    facets = [[f"project_type:{info['type']}"]]
    if loader and info["loader"]:
        facets.append([f"categories:{loader}"])
    if mc and kind != "modpack":                       # сборки показываем для любых версий
        facets.append([f"versions:{mc}"])
    params = {"facets": json.dumps(facets), "limit": limit, "index": "relevance" if query else "downloads"}
    if query:
        params["query"] = query
    return _get("/search", **params)["hits"]


def project_versions(project, mc, loader):
    """Подходящие версии проекта: сначала релизы, внутри — самые свежие."""
    params = {}
    if mc:
        params["game_versions"] = json.dumps([mc])
    if loader:
        params["loaders"] = json.dumps([loader])
    versions = _get(f"/project/{project}/version", **params)
    versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
    versions.sort(key=lambda v: {"release": 0, "beta": 1, "alpha": 2}.get(v.get("version_type"), 3))
    return versions


def pick_file(version):
    files = version.get("files") or []
    if not files:
        raise LookupError("no files")
    return next((f for f in files if f.get("primary")), files[0])


def download_file(f, folder):
    """Скачивает файл в папку, проверяя sha512. Возвращает (имя файла, был ли скачан заново)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / Path(f["filename"]).name
    if target.exists():
        return target.name, False
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha512()
    with requests.get(f["url"], headers=HEADERS, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(part, "wb") as out:
            for chunk in r.iter_content(65536):
                out.write(chunk)
                digest.update(chunk)
    want = (f.get("hashes") or {}).get("sha512")
    if want and digest.hexdigest() != want:
        part.unlink()
        raise ValueError("hash mismatch")
    part.replace(target)
    return target.name, True


def project_info(project):
    data = _get(f"/project/{project}")
    return {"title": data.get("title") or project, "icon": data.get("icon_url")}


def fetch_icon(url, cache_dir, size=96):
    """Иконка мода: из кэша или с CDN Modrinth. Возвращает картинку со скруглёнными углами или None."""
    if not url:
        return None
    cache_dir = Path(cache_dir)
    path = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".png")
    try:
        if path.exists():
            img = Image.open(path).convert("RGBA")
        else:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            img = modinfo.square(Image.open(io.BytesIO(r.content)), size)
            cache_dir.mkdir(parents=True, exist_ok=True)
            img.save(path)
        return modinfo.round_corners(img)
    except Exception:
        return None


def download_pack(project, folder):
    """Скачивает последнюю версию сборки модов (файл .mrpack) и возвращает путь к нему."""
    for version in project_versions(project, None, None):
        files = [f for f in version.get("files") or [] if f["filename"].endswith(".mrpack")]
        if files:
            name, _ = download_file(next((f for f in files if f.get("primary")), files[0]), folder)
            return Path(folder) / name
    raise LookupError(project)


def install(project, mc, loader, folder, _seen=None, kind="mod"):
    """Ставит мод (с обязательными зависимостями вроде Fabric API), шейдер или ресурспак.
    Возвращает список словарей: file, new, project_id, title, icon."""
    seen = _seen if _seen is not None else set()
    seen.add(project)
    versions = project_versions(project, mc, loader if KINDS[kind]["loader"] else None)
    if not versions:
        raise LookupError(project)
    version = versions[0]
    done = []
    for dep in (version.get("dependencies") or []) if kind == "mod" else []:
        pid = dep.get("project_id")
        if dep.get("dependency_type") == "required" and pid and pid not in seen:
            try:
                done += install(pid, mc, loader, folder, seen)
            except LookupError:
                pass
    name, new = download_file(pick_file(version), folder)
    try:
        info = project_info(project)
    except Exception:
        info = {"title": name, "icon": None}
    done.append({"file": name, "new": new, "project_id": project, "title": info["title"], "icon": info["icon"]})
    return done
