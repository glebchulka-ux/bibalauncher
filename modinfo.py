"""Иконки и названия модов: из самого jar-файла, плюс помощники для картинок."""
import io
import json
import re
import zipfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw


def square(img, size=96):
    """Обрезает картинку до квадрата по центру и приводит к нужному размеру."""
    img = img.convert("RGBA")
    w, h = img.size
    s = min(w, h)
    left, top = (w - s) // 2, (h - s) // 2
    return img.crop((left, top, left + s, top + s)).resize((size, size), Image.LANCZOS)


def round_corners(img, frac=0.22):
    """Скругляет углы квадратной картинки."""
    img = img.convert("RGBA")
    size = img.size[0]
    big = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1), radius=int(size * 4 * frac), fill=255)
    out = img.copy()
    out.putalpha(ImageChops.multiply(img.getchannel("A"), big.resize((size, size), Image.LANCZOS)))
    return out


def _image_from_zip(z, names, path):
    path = (path or "").lstrip("/")
    if not path or path not in names or z.getinfo(path).file_size > 5_000_000:
        return None
    return Image.open(io.BytesIO(z.read(path)))


def jar_info(path):
    """(название, иконка) мода из его jar: Fabric, Quilt или Forge. Любое значение может быть None."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            name = icon = None
            if "fabric.mod.json" in names:
                meta = json.loads(z.read("fabric.mod.json").decode("utf-8", "ignore"), strict=False)
                name, icon = meta.get("name") or meta.get("id"), meta.get("icon")
            elif "quilt.mod.json" in names:
                meta = json.loads(z.read("quilt.mod.json").decode("utf-8", "ignore"), strict=False)
                loader = meta.get("quilt_loader") or {}
                data = loader.get("metadata") or {}
                name, icon = data.get("name") or loader.get("id"), data.get("icon")
            elif "META-INF/mods.toml" in names:
                text = z.read("META-INF/mods.toml").decode("utf-8", "ignore")
                m = re.search(r'displayName\s*=\s*"([^"]+)"', text)
                name = m.group(1) if m else None
                m = re.search(r'logoFile\s*=\s*"([^"]+)"', text)
                icon = m.group(1) if m else None
            if isinstance(icon, dict) and icon:                  # {"128": "путь", "512": "путь"}
                icon = icon[max(icon, key=lambda k: int(k) if str(k).isdigit() else 0)]
            img = _image_from_zip(z, names, icon) if isinstance(icon, str) else None
            return (str(name) if name else None), (square(img) if img is not None else None)
    except Exception:
        return None, None


def pack_icon(path):
    """Иконка ресурспака: файл pack.png в корне архива."""
    try:
        with zipfile.ZipFile(path) as z:
            img = _image_from_zip(z, set(z.namelist()), "pack.png")
            return square(img) if img is not None else None
    except Exception:
        return None


def file_info(path):
    """(название, иконка) для мода (.jar) или ресурспака/шейдера (.zip)."""
    path = Path(path)
    if path.suffix.lower() == ".jar":
        return jar_info(path)
    return None, pack_icon(path)
