"""Тема оформления: вся палитра выводится из одного цвета, картинки перекрашиваются под него."""
import base64
import colorsys
import io

from PIL import Image, ImageChops, ImageDraw, ImageOps

from assets_data import DATA

DEFAULT = "#F10317"
PRESETS = [("#F10317", "Красный"), ("#FF6A00", "Оранжевый"), ("#F5B400", "Жёлтый"), ("#1FB85A", "Зелёный"),
           ("#00B8D9", "Бирюзовый"), ("#2F6BFF", "Синий"), ("#8B5CF6", "Фиолетовый"), ("#EC4899", "Розовый")]
BASE_HUE = colorsys.rgb_to_hsv(0xF1 / 255, 0x03 / 255, 0x17 / 255)[0]     # оттенок исходных картинок


def asset(name):
    return Image.open(io.BytesIO(base64.b64decode(DATA[name]))).convert("RGBA")


def _rgb(code):
    code = code.lstrip("#")
    return tuple(int(code[i:i + 2], 16) for i in (0, 2, 4))


def _hex(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(1, max(0, s)), min(1, max(0, v)))
    return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


class Theme:
    def __init__(self):
        self.apply(DEFAULT, 16)

    def apply(self, accent, radius, bg=None):
        r, g, b = _rgb(accent)
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        s, v = max(s, 0.55), max(v, 0.80)                 # акцент всегда яркий
        self.accent = _hex(h, s, v)
        self.hi = _hex(h, s - 0.15, 1.0)
        self.dark = _hex(h, s, v * 0.82)
        self.active = _hex(h, min(s, 0.95), 0.28)
        self.sidebar = _hex(h, 0.45, 0.036)
        self.page = _hex(h, 0.55, 0.060)
        self.card = _hex(h, 0.20, 0.050)
        self.edge = _hex(h, 0.32, 0.165)
        self.input = _hex(h, 0.35, 0.066)
        self.input_edge = _hex(h, 0.28, 0.180)
        self.hover = _hex(h, 0.40, 0.105)
        if bg:                                            # свой цвет фона: поверхности берутся от него (всегда тёмные)
            hb, sb, vb = colorsys.rgb_to_hsv(*(c / 255 for c in _rgb(bg)))
            v0, s0 = min(max(vb, 0.03), 0.22), min(sb, 0.8)
            self.page = _hex(hb, s0, v0)
            self.sidebar = _hex(hb, s0, v0 * 0.62)
            self.card = _hex(hb, s0 * 0.8, v0 * 0.82)
            self.edge = _hex(hb, s0 * 0.8, min(v0 * 2.4 + 0.02, 0.55))
            self.input = _hex(hb, s0, v0 * 1.15)
            self.input_edge = _hex(hb, s0 * 0.8, min(v0 * 2.6 + 0.02, 0.58))
            self.hover = _hex(hb, s0, v0 * 1.7)
        self.page_rgb = _rgb(self.page)
        d = (h - BASE_HUE + 0.5) % 1.0 - 0.5                # разница оттенков, от -0.5 до 0.5
        self.shift = 0 if abs(d) < 0.004 else round((d % 1.0) * 256) % 256
        self.rad = int(radius)                            # скругление кнопок и полей
        self.card_rad = self.rad + 4                      # скругление карточек
        self.small_rad = max(self.rad - 6, 3)


th = Theme()


def hue_shift(img, shift):
    """Поворачивает оттенок картинки, не трогая прозрачность."""
    if not shift:
        return img
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    h, s, v = rgba.convert("RGB").convert("HSV").split()
    h = h.point(lambda x: (x + shift) % 256)
    out = Image.merge("HSV", (h, s, v)).convert("RGB").convert("RGBA")
    out.putalpha(alpha)
    return out


BADGES = {"edit": (744, 36), "reset": (696, 36)}          # центры значков на баннере 780x235
BADGE_R = 20


def _badges(w, h, photo):
    """Круглые значки на баннере: карандаш (сменить фон) и крестик (вернуть стандартный)."""
    s, big = w / 780, 3
    keys = ["edit"] + (["reset"] if photo is not None else [])
    ov = Image.new("RGBA", (w * big, h * big), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for key in keys:
        cx, cy = BADGES[key]
        x, y, r = cx * s * big, cy * s * big, BADGE_R * s * big
        d.ellipse((x - r, y - r, x + r, y + r), fill=(8, 4, 5, 185), outline=(255, 255, 255, 80), width=2 * big)
    ov = ov.resize((w, h), Image.LANCZOS)
    for key in keys:
        cx, cy = BADGES[key]
        size = round(22 * s)
        icon = asset("icons/pencil.png" if key == "edit" else "icons/close.png").resize((size, size), Image.LANCZOS)
        ov.alpha_composite(icon, (round(cx * s - size / 2), round(cy * s - size / 2)))
    return ov


def hero_image(page_rgb, shift, photo=None):
    """Баннер главной: своё фото или стандартная картинка, плавно уходит в цвет страницы."""
    w, h = 936, 282
    if photo is not None:
        img = ImageOps.fit(photo.convert("RGB"), (w, h), Image.LANCZOS)
        left = 0.0
    else:
        img = hue_shift(asset("hero_raw.jpg"), shift).convert("RGB")
        left = 0.22
    col = Image.new("L", (1, h))
    col.putdata([round(255 * min(1, max(0, (0.90 - y / h) / 0.40))) for y in range(h)])
    row = Image.new("L", (w, 1))
    row.putdata([round(255 * (min(1, x / (left * w)) ** 0.8 if left else 1)) for x in range(w)])
    mask = ImageChops.multiply(col.resize((w, h)), row.resize((w, h)))
    img = Image.composite(img, Image.new("RGB", (w, h), page_rgb), mask)
    return Image.alpha_composite(img.convert("RGBA"), _badges(w, h, photo)).convert("RGB")


def cover_tile(size=128):
    """Обложка сборки по умолчанию: тёмная плитка со значком слоёв."""
    tile = Image.new("RGBA", (size, size), _rgb(th.active) + (255,))
    icon = asset("icons/layers.png").resize((size // 2, size // 2), Image.LANCZOS)
    solid = Image.new("RGBA", icon.size, _rgb(th.hi) + (255,))
    solid.putalpha(icon.getchannel("A"))
    tile.alpha_composite(solid, (size // 4, size // 4))
    return tile
