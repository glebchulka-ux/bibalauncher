"""Сборки: у каждой свои версия, моды, миры и настройки."""
import shutil
import uuid
from pathlib import Path

MAIN = "main"                                                    # основная сборка = обычная папка игры
COPY_PARTS = ("mods", "config", "resourcepacks", "shaderpacks", "options.txt")


def ensure_builds(cfg):
    """Гарантирует основную сборку (старая настройка version переезжает в неё)."""
    builds = cfg.setdefault("builds", [])
    if not any(b["id"] == MAIN for b in builds):
        builds.insert(0, {"id": MAIN, "name": None, "version": cfg.pop("version", ""), "ram": None, "image": None})
    cfg.setdefault("build", MAIN)
    if cfg["build"] not in {b["id"] for b in builds}:
        cfg["build"] = MAIN
    return builds


def new_build(name, version, ram=None):
    return {"id": uuid.uuid4().hex[:8], "name": name, "version": version, "ram": ram, "image": None}


def folder(build, game_dir, instances_dir):
    """Папка игры для сборки: основная — обычная .minecraft, остальные — отдельные папки."""
    return Path(game_dir) if build["id"] == MAIN else Path(instances_dir) / build["id"]


def mod_count(path):
    mods = Path(path) / "mods"
    return len(list(mods.glob("*.jar"))) if mods.is_dir() else 0


def popular(release_ids):
    """Последний патч каждой ветки (1.21.x, 1.20.x ...) — короткий список для выбора версии."""
    seen, out = set(), []
    for v in release_ids:
        minor = ".".join(v.split(".")[:2])
        if minor not in seen:
            seen.add(minor)
            out.append(v)
    return out


def copy_parts(src, dst):
    """Копирует моды, конфиги и настройки (миры не трогает)."""
    src, dst = Path(src), Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for part in COPY_PARTS:
        s = src / part
        if s.is_dir():
            shutil.copytree(s, dst / part, dirs_exist_ok=True)
        elif s.is_file():
            shutil.copy2(s, dst / part)


def remove(path):
    shutil.rmtree(path, ignore_errors=True)
