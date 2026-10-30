"""Скины: превью, голова для аватара, скин с Ely.by и локальный сервер для оффлайн-режима."""
import base64
import hashlib
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageOps


def valid_skin(img):
    w, h = img.size
    return w >= 64 and w % 64 == 0 and h in (w, w // 2)


def looks_slim(img):
    """У тонких (slim) скинов крайний столбец руки прозрачный."""
    if img.size[1] < img.size[0]:
        return False
    k = img.size[0] // 64
    return img.convert("RGBA").crop((47 * k, 20 * k, 48 * k, 32 * k)).getchannel("A").getextrema()[1] == 0


def placeholder_skin():
    """Простая фигурка, пока скин не выбран."""
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    skin, shirt, pants = (201, 154, 122, 255), (47, 164, 214, 255), (60, 64, 110, 255)
    d.rectangle((0, 0, 31, 15), fill=skin)
    d.rectangle((16, 16, 39, 31), fill=shirt)
    d.rectangle((40, 16, 55, 31), fill=skin)
    d.rectangle((0, 16, 15, 31), fill=pants)
    d.rectangle((16, 48, 31, 63), fill=pants)
    d.rectangle((32, 48, 47, 63), fill=skin)
    return im


def _side(skin, back, slim, k):
    """Рисует персонажа спереди или сзади (16x32 клетки)."""
    legacy = skin.size[1] < skin.size[0]
    slim = slim and not legacy
    cv = Image.new("RGBA", (16 * k, 32 * k), (0, 0, 0, 0))

    def put(box, x, y, flip=False):
        im = skin.crop(tuple(v * k for v in box))
        if flip:
            im = ImageOps.mirror(im)
        cv.alpha_composite(im, (x * k, y * k))

    aw = 3 if slim else 4
    xl, xr = (1 if slim else 0), 12                     # руки слева и справа на экране
    if not back:
        put((8, 8, 16, 16), 4, 0)
        put((20, 20, 28, 32), 4, 8)
        put((44, 20, 44 + aw, 32), xl, 8)               # правая рука игрока
        put((4, 20, 8, 32), 4, 20)                      # правая нога
        if legacy:
            put((44, 20, 44 + aw, 32), xr, 8, True)
            put((4, 20, 8, 32), 8, 20, True)
        else:
            put((36, 52, 36 + aw, 64), xr, 8)
            put((20, 52, 24, 64), 8, 20)
            put((20, 36, 28, 48), 4, 8)
            put((44, 36, 44 + aw, 48), xl, 8)
            put((52, 52, 52 + aw, 64), xr, 8)
            put((4, 36, 8, 48), 4, 20)
            put((4, 52, 8, 64), 8, 20)
        put((40, 8, 48, 16), 4, 0)
    else:
        put((24, 8, 32, 16), 4, 0)
        put((32, 20, 40, 32), 4, 8)
        put((52, 20, 52 + aw, 32), xr, 8)               # правая рука игрока справа на экране
        put((12, 20, 16, 32), 8, 20)
        if legacy:
            put((52, 20, 52 + aw, 32), xl, 8, True)
            put((12, 20, 16, 32), 4, 20, True)
        else:
            la = (43 if slim else 44, 52, 46 if slim else 48, 64)
            put(la, xl, 8)
            put((28, 52, 32, 64), 4, 20)
            put((32, 36, 40, 48), 4, 8)
            put((51 if slim else 52, 36, 54 if slim else 56, 48), xr, 8)
            put((59 if slim else 60, 52, 62 if slim else 64, 64), xl, 8)
            put((12, 36, 16, 48), 8, 20)
            put((12, 52, 16, 64), 4, 20)
        put((56, 8, 64, 16), 4, 0)
    return cv


def preview(skin, slim=False, scale=7):
    """Картинка «вид спереди + вид сзади»."""
    skin = skin.convert("RGBA")
    k = skin.size[0] // 64
    out = Image.new("RGBA", (35 * k, 32 * k), (0, 0, 0, 0))
    out.alpha_composite(_side(skin, False, slim, k), (0, 0))
    out.alpha_composite(_side(skin, True, slim, k), (19 * k, 0))
    return out.resize((35 * scale, 32 * scale), Image.NEAREST)


def head_icon(skin, size=84):
    """Круглый аватар из головы скина."""
    skin = skin.convert("RGBA")
    k = skin.size[0] // 64
    head = skin.crop((8 * k, 8 * k, 16 * k, 16 * k))
    head.alpha_composite(skin.crop((40 * k, 8 * k, 48 * k, 16 * k)))
    head = head.resize((size, size), Image.NEAREST)
    big = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(head, (0, 0), big.resize((size, size), Image.LANCZOS))
    return out


def save_skin(img, folder, stem):
    """Сохраняет скин в библиотеку (перекодируя PNG, чтобы убрать лишние данные)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = "".join(c for c in stem if c.isalnum() or c in "-_ ")[:40].strip() or "skin"
    name, n = f"{stem}.png", 2
    while (folder / name).exists():
        name, n = f"{stem}-{n}.png", n + 1
    img.convert("RGBA").save(folder / name, "PNG")
    return name


def fetch_skin_url(url, timeout=10):
    """Скачивает PNG скина по ссылке (например, с textures.minecraft.net)."""
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGBA")


def fetch_ely_skin(nick, timeout=10):
    """Скин игрока с Ely.by. Возвращает (картинка, slim?) или (None, False)."""
    r = requests.get(f"http://skinsystem.ely.by/textures/{nick}", timeout=timeout)
    if r.status_code != 200:
        return None, False
    skin = (r.json() or {}).get("SKIN") or {}
    if not skin.get("url"):
        return None, False
    png = requests.get(skin["url"], timeout=timeout)
    png.raise_for_status()
    img = Image.open(io.BytesIO(png.content)).convert("RGBA")
    return img, (skin.get("metadata") or {}).get("model") == "slim"


class SkinServer:
    """Крошечный сервер в формате Yggdrasil: через authlib-injector отдаёт игре локальный скин."""

    def __init__(self, name, uuid, png_path, slim=False):
        self.name, self.uuid, self.slim = name, uuid.replace("-", "").lower(), slim
        self.data = Path(png_path).read_bytes()
        self.hash = hashlib.sha256(self.data).hexdigest()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj).encode())

            def do_GET(self):
                path = self.path.split("?")[0].rstrip("/")
                if path == "":
                    return self._json(outer.metadata())
                if path.startswith("/textures/"):
                    return self._send(200, outer.data, "image/png")
                if path.startswith("/sessionserver/session/minecraft/profile/"):
                    uid = path.rsplit("/", 1)[1].replace("-", "").lower()
                    return self._json(outer.profile()) if uid == outer.uuid else self._send(204)
                if path.endswith("/hasJoined"):
                    return self._json(outer.profile())
                self._json({"error": "NotFound"}, 404)

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size) if size else b""
                path = self.path.split("?")[0].rstrip("/")
                if path.endswith("/profiles/minecraft"):
                    try:
                        names = json.loads(body or b"[]")
                    except ValueError:
                        names = []
                    return self._json([{"id": outer.uuid, "name": outer.name}] if outer.name in names else [])
                if path.endswith("/session/minecraft/join"):
                    return self._send(204)
                self._json({"error": "NotFound"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def metadata(self):
        return {"meta": {"serverName": "Biba Launcher", "implementationName": "BibaLauncher", "implementationVersion": "1.0"},
                "skinDomains": ["127.0.0.1"]}

    def profile(self):
        skin = {"url": f"http://127.0.0.1:{self.port}/textures/{self.hash}.png"}
        if self.slim:
            skin["metadata"] = {"model": "slim"}
        textures = {"timestamp": int(time.time() * 1000), "profileId": self.uuid, "profileName": self.name,
                    "textures": {"SKIN": skin}}
        value = base64.b64encode(json.dumps(textures).encode()).decode()
        return {"id": self.uuid, "name": self.name, "properties": [{"name": "textures", "value": value}]}

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
