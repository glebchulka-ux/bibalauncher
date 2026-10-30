#!/usr/bin/env python3
"""Biba Launcher — лаунчер Minecraft: Microsoft и Ely.by, сборки, моды с Modrinth, скины, автообновление."""
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox

import customtkinter as ctk
import minecraft_launcher_lib as mll
import requests
from PIL import Image

import builds as bd
import modinfo
import modrinth as mr
import msauth as ms
import skins
import updater as up
from appinfo import NAME, UPDATE_REPO, VERSION
from i18n import LANGS, lang, set_lang, tr
from theme import BADGE_R, BADGES, DEFAULT, PRESETS, asset, cover_tile, hero_image, hue_shift, th

if sys.platform.startswith("win"):          # своя иконка на панели задач
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("biba.launcher")
    except Exception:
        pass

ROOT = Path.home() / ".biba-launcher"                  # настройки, сессия, сборки, логи
_OLD_ROOT = Path.home() / ".glebster"                  # прежнее имя: переносим данные при первом запуске
if _OLD_ROOT.is_dir() and not ROOT.exists():
    try:
        _OLD_ROOT.rename(ROOT)
    except OSError:
        pass
CACHE, SKINS_DIR = ROOT / "cache", ROOT / "skins"
INSTANCES_DIR, COVERS_DIR, BG_DIR = ROOT / "instances", ROOT / "covers", ROOT / "backgrounds"
GAME_DIR = Path(mll.utils.get_minecraft_directory())   # обычная папка .minecraft
INJECTOR_JAR, CONFIG, LOG = ROOT / "authlib-injector.jar", ROOT / "config.json", ROOT / "game.log"
AUTH = "https://authserver.ely.by/auth"
INJECTOR_META = (
    "https://authlib-injector.yushi.moe/artifact/latest.json",
    "https://bmclapi2.bangbang93.com/mirrors/authlib-injector/artifact/latest.json",
)
TEXT, DIM, GREEN, ERR = "#F2F2F2", "#87888C", "#1F9442", "#FF6B6B"


# ───────────────────────── настройки ─────────────────────────

def load_cfg():
    try:
        return json.loads(CONFIG.read_text("utf-8"))
    except Exception:
        return {}


def save_cfg(cfg):
    ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def log_error(text):
    """Ошибки интерфейса пишутся в ~/.biba-launcher/error.log (в exe нет консоли)."""
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        with open(ROOT / "error.log", "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


# ───────────────────────── Ely.by ─────────────────────────

class ElyError(Exception):
    def __init__(self, msg, two_factor=False, network=False):
        super().__init__(msg)
        self.two_factor, self.network = two_factor, network


def ely_call(endpoint, payload):
    try:
        r = requests.post(f"{AUTH}/{endpoint}", json=payload, timeout=15)
    except requests.RequestException:
        raise ElyError(tr("Нет связи с Ely.by. Проверьте интернет."), network=True) from None
    if r.status_code == 200:
        return r.json() if r.content else {}
    try:
        msg = r.json().get("errorMessage", "")
    except ValueError:
        msg = ""
    low = msg.lower()
    if "invalid credentials" in low:
        msg = tr("Неверный логин, пароль или код 2FA.")
    raise ElyError(msg or tr("Ely.by ответил ошибкой {code}.", code=r.status_code), two_factor="two factor" in low)


def _account(data):
    p = data["selectedProfile"]
    return {"name": p["name"], "uuid": p["id"], "token": data["accessToken"]}


def ely_login(client_token, login, password, totp=""):
    # Если включена 2FA, код передаётся в поле пароля как «пароль:код»
    data = ely_call("authenticate", {
        "username": login,
        "password": f"{password}:{totp}" if totp else password,
        "clientToken": client_token,
        "requestUser": False,
    })
    return _account(data)


def ely_refresh(account, client_token):
    data = ely_call("refresh", {"accessToken": account["token"], "clientToken": client_token})
    return _account(data)


def ensure_injector():
    """authlib-injector подменяет серверы авторизации Mojang (на Ely.by или на локальный сервер скина)."""
    if INJECTOR_JAR.exists():
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    error = None
    for url in INJECTOR_META:
        try:
            meta = requests.get(url, timeout=15)
            meta.raise_for_status()
            meta = meta.json()
            jar = requests.get(meta["download_url"], timeout=60)
            jar.raise_for_status()
            want = (meta.get("checksums") or {}).get("sha256")
            if want and hashlib.sha256(jar.content).hexdigest() != want:
                raise ValueError("checksum")
            INJECTOR_JAR.write_bytes(jar.content)
            return
        except Exception as e:
            error = e
    raise RuntimeError(tr("Не удалось скачать authlib-injector: {e}", e=error))


def offline_uuid(name):
    h = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return h.hex()


# ───────────────────────── версии и папки ─────────────────────────

def fetch_versions():
    if hasattr(mll.utils, "get_version_list"):
        items = mll.utils.get_version_list()
    else:
        items = mll.utils.get_available_versions(str(GAME_DIR))
    return [{"id": v["id"], "type": v["type"]} for v in items]


def installed_ids():
    try:
        return {v["id"] for v in mll.utils.get_installed_versions(str(GAME_DIR))}
    except Exception:
        return set()


def install_fabric_loader(mc, callback):
    if hasattr(mll, "fabric"):
        mll.fabric.install_fabric(mc, str(GAME_DIR), callback=callback)
    else:
        mll.mod_loader.get_mod_loader("fabric").install(mc, str(GAME_DIR), callback=callback)


def open_folder(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        webbrowser.open(path.as_uri())


# ───────────────────────── виджеты ─────────────────────────

def load_icon(name, color, size):
    """Белая иконка, перекрашенная в нужный цвет."""
    mask = asset(f"icons/{name}.png")
    solid = Image.new("RGBA", mask.size, color)
    solid.putalpha(mask.getchannel("A"))
    return ctk.CTkImage(solid, size=(size, size))


def label(parent, text="", size=13, color=DIM, bold=False, **kw):
    return ctk.CTkLabel(parent, text=text, text_color=color, anchor="w", width=0,
                        font=ctk.CTkFont(size=size, weight="bold" if bold else "normal"), **kw)


def button(parent, text, command, primary=False, **kw):
    opts = dict(text=text, command=command, height=40, corner_radius=th.rad, font=ctk.CTkFont(size=14))
    if primary:
        opts.update(fg_color=th.accent, hover_color=th.hi, text_color="#FFFFFF")
    else:
        opts.update(fg_color=th.input, hover_color=th.hover, text_color=TEXT, border_width=1,
                    border_color=th.input_edge)
    opts.update(kw)
    return ctk.CTkButton(parent, **opts)


def entry(parent, placeholder, **kw):
    opts = dict(placeholder_text=placeholder, height=40, corner_radius=th.rad, fg_color=th.input,
                border_color=th.input_edge, text_color=TEXT)
    opts.update(kw)
    return ctk.CTkEntry(parent, **opts)


def segmented(parent, values, command, **kw):
    opts = dict(values=values, command=command, fg_color=th.input, selected_color=th.dark,
                selected_hover_color=th.accent, unselected_color=th.input, unselected_hover_color=th.hover,
                text_color=TEXT, corner_radius=th.rad)
    opts.update(kw)
    return ctk.CTkSegmentedButton(parent, **opts)


def scroll_list(parent, width, height):
    return ctk.CTkScrollableFrame(parent, width=width, height=height, fg_color="transparent",
                                  scrollbar_button_color=th.edge, scrollbar_button_hover_color=th.dark)


def card(parent, **kw):
    opts = dict(fg_color=th.card, border_width=1, border_color=th.edge, corner_radius=th.card_rad)
    opts.update(kw)
    return ctk.CTkFrame(parent, **opts)


def clickable(widget, fn):
    widget.bind("<Button-1>", lambda _e: fn(), add="+")
    for child in widget.winfo_children():
        clickable(child, fn)


def clear(frame):
    for w in frame.winfo_children():
        w.destroy()


# ───────────────────────── окно аккаунта ─────────────────────────

def ms_message(err):
    """Понятный текст для кода ошибки входа через Microsoft."""
    texts = {
        "network": tr("Нет связи с Microsoft. Проверьте интернет."),
        "bad_client": tr("Приложение с таким Client ID не найдено. Проверьте ID."),
        "public_client": tr("В приложении Azure включите «Allow public client flows» (Authentication → Advanced settings)."),
        "denied": tr("Вход отклонён."),
        "expired": tr("Время ввода кода вышло. Попробуйте ещё раз."),
        "no_app": tr("Mojang не одобрил это приложение (Invalid app registration). Подайте его на одобрение через «Форма Mojang»."),
        "no_xbox": tr("У этого аккаунта нет профиля Xbox. Войдите на xbox.com и создайте его."),
        "country": tr("Xbox Live недоступен в стране этого аккаунта."),
        "adult": tr("Для этого аккаунта нужна проверка возраста на странице Xbox."),
        "child": tr("Это детский аккаунт: родитель должен добавить его в семейную группу Microsoft."),
        "no_game": tr("На этом аккаунте нет Minecraft Java Edition."),
        "rate": tr("Слишком много попыток. Подождите немного."),
        "session": tr("Сессия Microsoft истекла. Войдите заново."),
    }
    return texts.get(err.code) or tr("Ошибка Microsoft: {e}", e=err.detail or err.code)


def update_message(err):
    """Понятный текст для кода ошибки обновления."""
    texts = {
        "no_source": tr("Укажите источник обновлений: репозиторий GitHub (владелец/репозиторий) или ссылку на update.json."),
        "bad_source": tr("Не понял источник обновлений. Нужен формат владелец/репозиторий или ссылка на update.json."),
        "no_release": tr("Релизов не найдено. Репозиторий должен быть публичным и содержать хотя бы один релиз."),
        "no_asset": tr("В релизе нет файла .exe."),
        "rate": tr("GitHub временно ограничил запросы. Попробуйте позже."),
        "network": tr("Нет связи с сервером обновлений. Проверьте интернет."),
        "insecure": tr("Ссылка на обновление не защищена (нужен https), пропускаю."),
        "checksum": tr("Контрольная сумма не совпала. Обновление отклонено."),
        "too_small": tr("Скачанный файл слишком мал. Обновление отклонено."),
    }
    return texts.get(err.code) or tr("Ошибка обновления: {e}", e=err.detail or err.code)


class AccountDialog(ctk.CTkToplevel):
    def __init__(self, app, tab=None):
        super().__init__(app, fg_color=th.page)
        self.app, self.busy = app, False
        self.ms_busy, self._cancel, self._code = False, False, ""
        self.title(tr("Аккаунт"))
        self.geometry("420x610")
        self.resizable(False, False)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(200, self._ready)

        label(self, tr("Аккаунт"), 22, TEXT, True).pack(anchor="w", padx=28, pady=(24, 14))
        self.tab_names = ["Microsoft", "Ely.by", tr("Оффлайн")]
        self.tabs = segmented(self, self.tab_names, self._tab)
        self.tabs.pack(fill="x", padx=28)
        self.boxes = [self._build_ms(), self._build_ely(), self._build_offline()]
        if tab is None:
            tab = 0 if app.cfg["account"].get("type") == "microsoft" else 1
        self.tabs.set(self.tab_names[tab])
        self._tab(self.tab_names[tab])

    def _ready(self):
        try:
            self.lift()
            self.focus()
            self.grab_set()
        except Exception:
            pass

    def _close(self):
        self._cancel = True                 # останавливаем ожидание кода Microsoft
        self.destroy()

    def _tab(self, value):
        for box in self.boxes:
            box.pack_forget()
        self.boxes[self.tab_names.index(value)].pack(fill="both", expand=True, padx=28, pady=(20, 0))

    # Microsoft
    def _build_ms(self):
        box = ctk.CTkFrame(self, fg_color="transparent")
        acc = self.app.cfg["account"]
        if acc.get("type") == "microsoft":
            label(box, tr("Вы вошли как {name}", name=acc["name"]), 15, TEXT).pack(anchor="w")
            label(box, tr("Лицензия Minecraft проверена, можно заходить на любые серверы."), 13,
                  wraplength=340, justify="left").pack(anchor="w", pady=(4, 18))
            button(box, tr("Выйти из Microsoft"), self._ms_logout).pack(fill="x")
            return box
        label(box, tr("Client ID приложения Azure"), 14, TEXT).pack(anchor="w", pady=(0, 6))
        self.ms_id = entry(box, "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self.ms_id.insert(0, self.app.cfg.get("ms_client_id", ""))
        self.ms_id.pack(fill="x")
        label(box, tr("Нужно приложение Azure, одобренное Mojang. Без одобрения Microsoft ответит «Invalid app registration»."),
              12, wraplength=340, justify="left").pack(anchor="w", pady=(8, 10))
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(anchor="w", fill="x")
        button(row, tr("Как получить"), self._ms_help, width=140, height=34).pack(side="left")
        button(row, tr("Форма Mojang"), lambda: webbrowser.open(ms.FORM_URL), width=140, height=34
               ).pack(side="left", padx=8)
        self.ms_btn = button(box, tr("Войти через Microsoft"), self._ms_start, primary=True)
        self.ms_btn.pack(fill="x", pady=(16, 0))
        self.ms_code = label(box, "", 30, TEXT, True)
        self.ms_code.pack(anchor="w", pady=(14, 0))
        self.ms_copy = button(box, tr("Скопировать код"), self._ms_copy, width=160, height=32)   # появится вместе с кодом
        self.ms_msg = label(box, "", 13, DIM, wraplength=340, justify="left")
        self.ms_msg.pack(fill="x", pady=(10, 0))
        return box

    def _ms_help(self):
        messagebox.showinfo(
            tr("Как получить Client ID"),
            tr("1. Откройте portal.azure.com → Microsoft Entra ID → App registrations → New registration.\n"
               "2. Тип аккаунтов: только личные учётные записи Microsoft (Personal Microsoft accounts only).\n"
               "3. Authentication → Advanced settings → Allow public client flows: Yes.\n"
               "4. Скопируйте Application (client) ID и вставьте его сюда.\n"
               "5. Отправьте этот ID на одобрение Mojang: кнопка «Форма Mojang»."), parent=self)

    def _ms_start(self):
        if self.ms_busy:                                   # повторное нажатие = отмена
            self._cancel = True
            return
        cid = self.ms_id.get().strip()
        if not ms.valid_client_id(cid):
            self.ms_msg.configure(text=tr("Вставьте Client ID приложения: он выглядит как xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx."),
                                  text_color=ERR)
            return
        self.app.cfg["ms_client_id"] = cid
        save_cfg(self.app.cfg)
        self.ms_busy, self._cancel = True, False
        self.ms_btn.configure(text=tr("Отмена"))
        self.ms_msg.configure(text=tr("Связываюсь с Microsoft…"), text_color=DIM)
        threading.Thread(target=self._ms_worker, args=(cid,), daemon=True).start()

    def _ms_worker(self, cid):
        try:
            device = ms.start_device_login(cid)
            self.app.ui(self._ms_show_code, device)
            tokens = ms.wait_for_token(cid, device, lambda: self._cancel)
            self.app.ui(self._ms_text, tr("Проверяю лицензию Minecraft…"))
            acc = ms.login_with_token(tokens)
            if self._cancel:
                raise ms.MsError("cancelled")
        except ms.MsError as e:
            self.app.ui(self._ms_failed, e)
        except Exception as e:
            self.app.ui(self._ms_failed, ms.MsError("other", str(e)))
        else:
            self.app.ui(self._ms_done, acc)

    def _ms_text(self, text):
        if self.winfo_exists():
            self.ms_msg.configure(text=text, text_color=DIM)

    def _ms_show_code(self, device):
        if not self.winfo_exists():
            return
        url = device.get("verification_uri") or "https://www.microsoft.com/link"
        self._code = device["user_code"]
        self.ms_code.configure(text=self._code)
        self.ms_copy.pack(anchor="w", pady=(6, 0), before=self.ms_msg)
        self.ms_msg.configure(text=tr("Открыт браузер: {url}. Введите там этот код и войдите в аккаунт Microsoft.", url=url),
                              text_color=DIM)
        webbrowser.open(url)

    def _ms_copy(self):
        self.clipboard_clear()
        self.clipboard_append(self._code)

    def _ms_failed(self, err):
        if not self.winfo_exists():
            return
        self.ms_busy = False
        self.ms_btn.configure(text=tr("Войти через Microsoft"))
        self.ms_code.configure(text="")
        self.ms_copy.pack_forget()
        self.ms_msg.configure(text="" if err.code == "cancelled" else ms_message(err), text_color=ERR)

    def _ms_done(self, acc):
        self.app.set_account({"type": "microsoft", **acc})
        if self.winfo_exists():
            self.destroy()

    def _ms_logout(self):
        self.app.logout_ms()
        self.destroy()

    # Ely.by
    def _build_ely(self):
        box = ctk.CTkFrame(self, fg_color="transparent")
        acc = self.app.cfg["account"]
        if acc.get("type", "ely") == "ely":
            label(box, tr("Вы вошли как {name}", name=acc["name"]), 15, TEXT).pack(anchor="w")
            label(box, tr("Ник и скин берутся с Ely.by."), 13).pack(anchor="w", pady=(4, 18))
            button(box, tr("Выйти из Ely.by"), self._logout).pack(fill="x")
            return box
        self.login = entry(box, tr("Ник или e-mail"))
        self.login.pack(fill="x", pady=(0, 10))
        self.password = entry(box, tr("Пароль"), show="•")
        self.password.pack(fill="x", pady=(0, 10))
        self.totp = entry(box, tr("Код из приложения 2FA"))     # появится, только если нужен
        self.login_btn = button(box, tr("Войти через Ely.by"), self._do_login, primary=True)
        self.login_btn.pack(fill="x", pady=(4, 0))
        self.msg = label(box, "", 13, ERR, wraplength=340, justify="left")
        self.msg.pack(fill="x", pady=(10, 0))
        for w in (self.login, self.password, self.totp):
            w.bind("<Return>", lambda _e: self._do_login(), add="+")
        return box

    def _do_login(self):
        if self.busy:
            return
        login, pw, code = self.login.get().strip(), self.password.get(), self.totp.get().strip()
        if not login or not pw:
            self.msg.configure(text=tr("Введите ник или e-mail и пароль."))
            return
        self.busy = True
        self.login_btn.configure(state="disabled", text=tr("Входим…"))
        self.msg.configure(text="")
        threading.Thread(target=self._login_worker, args=(login, pw, code), daemon=True).start()

    def _login_worker(self, login, pw, code):
        try:
            acc = ely_login(self.app.cfg["client_token"], login, pw, code)
        except ElyError as e:
            self.app.ui(self._failed, e)
        except Exception as e:
            self.app.ui(self._failed, ElyError(tr("Ошибка: {e}", e=e)))
        else:
            self.app.ui(self._done, acc)

    def _failed(self, err):
        if not self.winfo_exists():
            return
        self.busy = False
        self.login_btn.configure(state="normal", text=tr("Войти через Ely.by"))
        if err.two_factor:
            self.totp.pack(fill="x", pady=(0, 10), before=self.login_btn)
            self.msg.configure(text=tr("Включена двухфакторная защита. Введите код."))
        else:
            self.msg.configure(text=str(err))

    def _done(self, acc):
        self.app.set_account({"type": "ely", **acc})
        if self.winfo_exists():
            self.destroy()

    def _logout(self):
        self.app.logout_ely()
        self.destroy()

    # Оффлайн
    def _build_offline(self):
        box = ctk.CTkFrame(self, fg_color="transparent")
        acc = self.app.cfg["account"]
        current = self.app.cfg.get("offline_name") or (acc["name"] if acc.get("type") == "offline" else "GlebSter")
        label(box, tr("Ник для игры без аккаунта"), 14, TEXT).pack(anchor="w", pady=(0, 8))
        self.nick = entry(box, tr("Ник"))
        self.nick.insert(0, current)
        self.nick.pack(fill="x")
        label(box, tr("Подходит для одиночной игры и серверов без проверки лицензии."), 13,
              wraplength=340, justify="left").pack(anchor="w", pady=(8, 16))
        button(box, tr("Играть оффлайн"), self._offline, primary=True).pack(fill="x")
        self.off_msg = label(box, "", 13, ERR, wraplength=340, justify="left")
        self.off_msg.pack(fill="x", pady=(10, 0))
        return box

    def _offline(self):
        nick = self.nick.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nick):
            self.off_msg.configure(text=tr("Ник: 3–16 символов, латиница, цифры и _."))
            return
        self.app.set_account({"type": "offline", "name": nick})
        self.destroy()


class BuildDialog(ctk.CTkToplevel):
    """Создание и редактирование сборки."""

    def __init__(self, app, bid=None):
        super().__init__(app, fg_color=th.page)
        self.app, self.bid = app, bid
        self.build = next((b for b in app.cfg["builds"] if b["id"] == bid), None)
        self.is_main = bid == bd.MAIN
        self.cover_img, self.cover_clear = None, False
        self.ram_value = (self.build or {}).get("ram") or app.cfg["ram"]
        heading = tr("Сборка") if self.build else tr("Новая сборка")
        self.title(heading)
        self.geometry("460x690")
        self.resizable(False, False)
        self.transient(app)
        self.after(200, self._ready)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28, pady=18)
        label(body, heading, 22, TEXT, True).pack(anchor="w")
        if not self.is_main:
            label(body, tr("Название"), 14, TEXT).pack(anchor="w", pady=(14, 6))
            self.name = entry(body, tr("Например: Выживание с модами"))
            self.name.pack(fill="x")
            if self.build:
                self.name.insert(0, self.build["name"])
        if self.build is None:
            label(body, tr("Версия Minecraft"), 14, TEXT).pack(anchor="w", pady=(14, 6))
            releases = bd.popular([v["id"] for v in app.versions if v["type"] == "release"])
            self.version = ctk.CTkComboBox(body, values=releases, height=40, corner_radius=th.rad,
                                           fg_color=th.input, border_color=th.input_edge, button_color=th.edge,
                                           button_hover_color=th.accent, dropdown_fg_color=th.card,
                                           dropdown_hover_color=th.hover, dropdown_text_color=TEXT, text_color=TEXT)
            self.version.set(app.latest or (releases[0] if releases else ""))
            self.version.pack(fill="x")
            label(body, tr("Загрузчик модов"), 14, TEXT).pack(anchor="w", pady=(14, 6))
            self.loaders = [tr("Без модов"), "Fabric"]
            self.loader = segmented(body, self.loaders, lambda _v: None)
            self.loader.set(self.loaders[1])
            self.loader.pack(anchor="w")
        else:
            ver = self.build.get("version")
            label(body, tr("Версия: {v}", v=mr.pretty_version(ver) if ver else "—"), 14, TEXT).pack(anchor="w", pady=(14, 0))
            label(body, tr("Сменить версию можно на вкладке «Версии»."), 12).pack(anchor="w")

        self.ram_lbl = label(body, "", 14, TEXT)
        self.ram_lbl.pack(anchor="w", pady=(14, 6))
        self.ram = ctk.CTkSlider(body, from_=1, to=16, number_of_steps=15, width=360, progress_color=th.accent,
                                 button_color=th.hi, button_hover_color=th.hi, fg_color=th.edge,
                                 command=self._ram_changed)
        self.ram.set(self.ram_value)
        self.ram.pack(anchor="w")
        self._ram_changed(self.ram_value)

        label(body, tr("Обложка"), 14, TEXT).pack(anchor="w", pady=(14, 6))
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(anchor="w", fill="x")
        self.cover_lbl = ctk.CTkLabel(row, text="", width=0)
        self.cover_lbl.pack(side="left")
        button(row, tr("Выбрать картинку…"), self._pick_cover, width=170, height=36).pack(side="left", padx=(12, 8))
        button(row, tr("Убрать"), self._clear_cover, width=90, height=36).pack(side="left")
        self._show_cover()

        self.err = label(body, "", 13, ERR, wraplength=380, justify="left")
        self.err.pack(fill="x", pady=(10, 0))
        button(body, tr("Сохранить") if self.build else tr("Создать"), self._save, primary=True).pack(fill="x", pady=(8, 0))
        if self.build:
            more = ctk.CTkFrame(body, fg_color="transparent")
            more.pack(fill="x", pady=(10, 0))
            button(more, tr("Дублировать"), self._duplicate, width=120, height=36).pack(side="left")
            button(more, tr("Открыть папку"), self._open, width=130, height=36).pack(side="left", padx=8)
            if not self.is_main:
                button(more, tr("Удалить"), self._delete, width=90, height=36, text_color=ERR).pack(side="left")

    def _ready(self):
        try:
            self.lift()
            self.focus()
            self.grab_set()
        except Exception:
            pass

    def _ram_changed(self, value):
        self.ram_value = int(round(value))
        self.ram_lbl.configure(text=tr("Память: {n} ГБ", n=self.ram_value))

    def _show_cover(self):
        img = self.cover_img
        if img is None and self.build and self.build.get("image") and not self.cover_clear:
            try:
                img = Image.open(COVERS_DIR / self.build["image"])
            except Exception:
                img = None
        if img is None:
            img = cover_tile(128)
        self._cov = ctk.CTkImage(modinfo.round_corners(modinfo.square(img, 128)), size=(64, 64))
        self.cover_lbl.configure(image=self._cov)

    def _pick_cover(self):
        path = filedialog.askopenfilename(parent=self, title=tr("Выберите картинку для обложки"),
                                          filetypes=[(tr("Изображения"), "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not path:
            return
        try:
            self.cover_img = Image.open(path).convert("RGBA")
        except Exception:
            self.err.configure(text=tr("Не удалось открыть картинку."))
            return
        self.cover_clear = False
        self._show_cover()

    def _clear_cover(self):
        self.cover_img, self.cover_clear = None, True
        self._show_cover()

    def _save(self):
        app = self.app
        if self.build is None:
            mc = self.version.get().strip()
            if not mc:
                self.err.configure(text=tr("Выберите версию Minecraft."))
                return
            if app.all_ids and mc not in app.all_ids and mc not in installed_ids():
                self.err.configure(text=tr("Версия {v} не найдена.", v=mc))
                return
            build = bd.new_build(self.name.get().strip() or tr("Новая сборка"), mc, self.ram_value)
            if self.cover_img is not None:
                app.save_cover(build, self.cover_img)
            app.add_build(build, fabric=self.loader.get() == self.loaders[1])
        else:
            fields = {"ram": self.ram_value}
            if not self.is_main:
                fields["name"] = self.name.get().strip() or self.build["name"]
            if self.cover_img is not None:
                fields["image"] = app.save_cover(self.build, self.cover_img)
            elif self.cover_clear:
                app.drop_cover(self.build)
                fields["image"] = None
            app.update_build(self.bid, **fields)
        self.destroy()

    def _duplicate(self):
        self.app.duplicate_build(self.bid)
        self.destroy()

    def _open(self):
        open_folder(bd.folder(self.build, GAME_DIR, INSTANCES_DIR))

    def _delete(self):
        if self.app.delete_build(self.bid):
            self.destroy()


# ───────────────────────── главное окно ─────────────────────────

class App(ctk.CTk):
    def __init__(self):
        cfg = load_cfg()
        set_lang(cfg.get("lang", "ru"))
        th.apply(cfg.get("accent", DEFAULT), cfg.get("radius", 16))
        super().__init__(fg_color=th.page)
        self.title(NAME)
        self.geometry("1000x640")
        self.resizable(False, False)

        self.cfg = cfg
        for key, value in (("client_token", uuid.uuid4().hex), ("ram", 4), ("java", ""), ("accent", DEFAULT),
                           ("radius", 16), ("lang", "ru"), ("background", None), ("ms_client_id", ""), ("after_launch", "hide"), ("auto_update", True), ("update_source", ""),
                           ("account", {"type": "offline", "name": "GlebSter"})):
            self.cfg.setdefault(key, value)
        bd.ensure_builds(self.cfg)
        save_cfg(self.cfg)

        self.q = queue.Queue()      # действия для главного потока
        self.msg = None             # последний статус из фонового потока
        self.prog = None            # последний прогресс из фонового потока
        self.busy = False
        self.versions, self.all_ids, self.latest = [], set(), ""
        self.vfilter, self.page = 0, "home"
        self.mods_tab, self.mods_key, self.mods_kind = 0, None, "mod"
        self.updating = False
        self.skin_img, self.skin_slim = None, False
        self._photo = self._load_photo()

        self._build_ui()
        self.go("home")
        self._set_icon()
        self.refresh_skin()
        self.after(60, self._pump)
        threading.Thread(target=self._load_versions, daemon=True).start()
        if up.can_self_update():
            up.cleanup()                 # убираем хвосты прошлого обновления
        self.after(4000, self._update_tick)

    def _load_photo(self):
        name = self.cfg.get("background")
        if not name:
            return None
        try:
            return Image.open(BG_DIR / name).convert("RGB")
        except Exception:
            return None

    def report_callback_exception(self, exc, val, tb):
        log_error("".join(traceback.format_exception(exc, val, tb)))

    # ── сборка и пересборка интерфейса ──
    def _build_ui(self):
        self.nav, self.pages, self.status_labels = {}, {}, []
        self._build_sidebar()
        content = ctk.CTkFrame(self, fg_color=th.page, corner_radius=0)
        content.pack(side="right", fill="both", expand=True)
        for key, build in (("home", self._build_home), ("builds", self._build_builds),
                           ("versions", self._build_versions), ("mods", self._build_mods),
                           ("skins", self._build_skins), ("settings", self._build_settings)):
            frame = ctk.CTkFrame(content, fg_color=th.page, corner_radius=0)
            frame.place(x=0, y=0, relwidth=1, relheight=1)
            self.pages[key] = frame
            build(frame)
        self.refresh_account()
        self.refresh_version_labels()

    def rebuild(self):
        """Пересобирает весь интерфейс (после смены цвета, скругления или языка)."""
        th.apply(self.cfg["accent"], self.cfg["radius"])
        set_lang(self.cfg["lang"])
        self.configure(fg_color=th.page)
        for w in self.winfo_children():
            w.destroy()
        self._build_ui()
        self.go(self.page)
        self._set_icon()

    def _later(self, fn, *args):
        self.after(40, lambda: fn(*args))       # отложенно: нельзя ломать виджет прямо в его обработчике

    def _set_icon(self):
        def apply():
            try:
                CACHE.mkdir(parents=True, exist_ok=True)
                img = hue_shift(asset("icon.png"), th.shift)
                if sys.platform.startswith("win"):
                    path = CACHE / f"icon_{th.shift}.ico"
                    if not path.exists():
                        img.save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
                    self.iconbitmap(str(path))
                else:
                    path = CACHE / f"icon_{th.shift}.png"
                    img.save(path)
                    self._icon = tk.PhotoImage(file=str(path))
                    self.iconphoto(True, self._icon)
            except Exception:
                pass
        self.after(250, apply)

    # ── обновление интерфейса из фоновых потоков ──
    def ui(self, fn, *args):
        self.q.put((fn, args))

    def say(self, text, err=False):
        self.msg = (text, err)

    def _pump(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                try:
                    fn(*args)
                except tk.TclError:       # виджет уже пересобран
                    pass
                except Exception:
                    log_error(traceback.format_exc())
        except queue.Empty:
            pass
        if self.msg:
            text, err = self.msg
            self.msg = None
            for lbl in self.status_labels:
                try:
                    lbl.configure(text=text, text_color=ERR if err else DIM)
                except tk.TclError:
                    pass
        if self.prog is not None:
            try:
                self.bar.set(self.prog)
            except tk.TclError:
                pass
            self.prog = None
        self.after(60, self._pump)

    # ── боковая панель ──
    def _build_sidebar(self):
        side = ctk.CTkFrame(self, width=220, fg_color=th.sidebar, corner_radius=0)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        head = ctk.CTkFrame(side, fg_color="transparent")
        head.pack(fill="x", padx=24, pady=(28, 26))
        self._wordmark(head, 20).pack(anchor="w")
        label(head, tr("для Minecraft"), 12).pack(anchor="w", padx=2)

        for key, text, icon in (("home", tr("Главная"), "home"), ("builds", tr("Сборки"), "layers"),
                                ("versions", tr("Версии"), "gamepad"), ("mods", tr("Моды"), "cube"),
                                ("skins", tr("Скины"), "user"), ("settings", tr("Настройки"), "gear")):
            row = ctk.CTkFrame(side, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=2)
            bar = ctk.CTkFrame(row, width=4, height=44, fg_color="transparent", corner_radius=2)
            bar.pack(side="left")
            dim, hot = load_icon(icon, DIM, 22), load_icon(icon, th.hi, 22)
            btn = ctk.CTkButton(row, text="  " + text, image=dim, compound="left", anchor="w", height=44,
                                corner_radius=th.rad, border_spacing=14, font=ctk.CTkFont(size=15),
                                fg_color="transparent", hover_color=th.hover, text_color=DIM,
                                command=lambda k=key: self.go(k))
            btn.pack(side="left", fill="x", expand=True, padx=(8, 0))
            self.nav[key] = (bar, btn, dim, hot)

        chip = ctk.CTkFrame(side, fg_color="transparent")
        chip.pack(side="bottom", fill="x", padx=18, pady=22)
        self.avatar_lbl = ctk.CTkLabel(chip, text="", width=0)
        self.avatar_lbl.pack(side="left")
        info = ctk.CTkFrame(chip, fg_color="transparent")
        info.pack(side="left", padx=(12, 0))
        self.acc_name = label(info, "", 15, TEXT)
        self.acc_name.pack(anchor="w")
        self.acc_state = label(info, "", 12)
        self.acc_state.pack(anchor="w")
        ctk.CTkLabel(chip, image=load_icon("chevron", DIM, 16), text="", width=0).pack(side="right")
        self._set_avatar()
        clickable(chip, self.open_account)

    @staticmethod
    def _wordmark(parent, size):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        label(row, "Biba", size, TEXT, True).pack(side="left")
        label(row, "Launcher", size, th.accent, True).pack(side="left", padx=(max(size // 4, 4), 0))
        return row

    @staticmethod
    def _title(page, title, sub):
        label(page, title, 28, TEXT, True).place(x=40, y=30)
        label(page, sub, 13).place(x=42, y=72)

    def _set_avatar(self):
        if self.skin_img is not None:
            img = skins.head_icon(self.skin_img, 84)
        else:
            img = hue_shift(asset("avatar.png"), th.shift)
        self._avatar = ctk.CTkImage(img, size=(42, 42))
        self.avatar_lbl.configure(image=self._avatar)

    def go(self, key):
        self.page = key
        self.pages[key].tkraise()
        for k, (bar, btn, dim, hot) in self.nav.items():
            on = k == key
            bar.configure(fg_color=th.accent if on else "transparent")
            btn.configure(fg_color=th.active if on else "transparent", text_color=TEXT if on else DIM,
                          image=hot if on else dim)
        if key == "versions":
            self.render_versions()
        elif key == "builds":
            self.render_builds()
        elif key == "mods":
            self.render_mods()
        elif key == "skins":
            self.render_skins()

    # ── главная ──
    def _build_home(self, page):
        self.banner_lbl = ctk.CTkLabel(page, image=self._banner_image(), text="", width=0, fg_color=th.page)
        self.banner_lbl.place(x=0, y=0)
        self.banner_lbl.bind("<Button-1>", self._banner_click, add="+")
        head = ctk.CTkFrame(page, fg_color="transparent")
        head.place(x=52, y=206)
        self._wordmark(head, 46).pack(anchor="w")
        label(head, tr("Твой лаунчер Minecraft"), 18).pack(anchor="w", padx=4)

        self.play_btn = button(page, "  " + tr("Играть"), self.do_play, primary=True, width=290, height=62,
                               image=load_icon("play", "#FFFFFF", 22), compound="left",
                               font=ctk.CTkFont(size=19, weight="bold"))
        self.play_btn.place(x=56, y=332)
        self.build_btn = button(page, "", lambda: self.go("builds"), width=230, height=48,
                                image=load_icon("layers", TEXT, 20), compound="left", font=ctk.CTkFont(size=15))
        self.build_btn.place(x=364, y=339)
        self.build_info = label(page, "", 12)
        self.build_info.place(x=368, y=396)

        c1 = card(page, width=325, height=118)
        c1.place(x=56, y=424)
        self._thumb = ctk.CTkImage(hue_shift(asset("thumb.png"), th.shift), size=(116, 87))
        ctk.CTkLabel(c1, image=self._thumb, text="", width=0).place(x=16, y=15)
        label(c1, tr("Последняя версия"), 13).place(x=150, y=18)
        self.latest_lbl = label(c1, "—", 26, TEXT, True)
        self.latest_lbl.place(x=148, y=40)
        label(c1, "●", 12, GREEN).place(x=150, y=86)
        label(c1, tr("Стабильная"), 12, TEXT).place(x=168, y=86)
        ctk.CTkLabel(c1, image=load_icon("chevron", DIM, 16), text="", width=0).place(x=292, y=51)
        clickable(c1, self.pick_latest)

        c2 = card(page, width=325, height=118)
        c2.place(x=399, y=424)
        ctk.CTkLabel(c2, image=load_icon("news", th.accent, 26), text="", width=0).place(x=20, y=22)
        label(c2, tr("Новости"), 16, TEXT).place(x=62, y=20)
        label(c2, tr("Пока нет новостей"), 12).place(x=62, y=52)

        self.bar = ctk.CTkProgressBar(page, width=668, height=6, corner_radius=3, fg_color=th.input,
                                      progress_color=th.accent)
        self.bar.set(0)
        self.bar.place(x=56, y=566)
        self.status = label(page, "", 13, wraplength=668, justify="left")
        self.status.place(x=56, y=580)
        self.status_labels.append(self.status)

    # фон главной: карандаш на баннере меняет его на своё фото
    def _banner_image(self):
        self._hero = ctk.CTkImage(hero_image(th.page_rgb, th.shift, self._photo), size=(780, 235))
        return self._hero

    def _banner_click(self, e):
        k = 780 / (self.banner_lbl.winfo_width() or 780)          # клик -> координаты чертежа 780x235
        x, y = e.x * k, e.y * k
        for key, (cx, cy) in BADGES.items():
            if key == "reset" and self._photo is None:
                continue
            if (x - cx) ** 2 + (y - cy) ** 2 <= (BADGE_R + 3) ** 2:
                if key == "edit":
                    self.choose_background()
                else:
                    self.reset_background()
                return

    def choose_background(self):
        path = filedialog.askopenfilename(parent=self, title=tr("Выберите фото для фона"),
                                          filetypes=[(tr("Изображения"), "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not path:
            return
        try:
            img = Image.open(path)
            img.load()
            img = img.convert("RGB")
        except Exception:
            messagebox.showerror(tr("Фон"), tr("Не удалось открыть картинку."), parent=self)
            return
        img.thumbnail((1800, 1800))
        BG_DIR.mkdir(parents=True, exist_ok=True)
        img.save(BG_DIR / "background.jpg", "JPEG", quality=92)
        self.cfg["background"] = "background.jpg"
        save_cfg(self.cfg)
        self._photo = img
        self.banner_lbl.configure(image=self._banner_image())

    def reset_background(self):
        try:
            (BG_DIR / (self.cfg.get("background") or "background.jpg")).unlink(missing_ok=True)
        except OSError:
            pass
        self.cfg["background"] = None
        save_cfg(self.cfg)
        self._photo = None
        self.banner_lbl.configure(image=self._banner_image())

    # ── версии ──
    def _build_versions(self, page):
        self._title(page, tr("Версии"), tr("Версия выбранной сборки. Недостающие файлы скачаются при запуске."))
        self.filter_names = [tr("Релизы"), tr("Снапшоты"), tr("Установленные")]
        self.seg = segmented(page, self.filter_names, self._set_filter)
        self.seg.set(self.filter_names[self.vfilter])
        self.seg.place(x=40, y=108)
        self.search = entry(page, tr("Поиск"), width=200, height=32)
        self.search.place(x=540, y=108)
        self.search.bind("<KeyRelease>", lambda _e: self.render_versions(), add="+")
        self.vlist = scroll_list(page, 690, 410)
        self.vlist.place(x=34, y=160)

    def _set_filter(self, value):
        self.vfilter = self.filter_names.index(value)
        self.render_versions()

    def render_versions(self):
        clear(self.vlist)
        installed, chosen = installed_ids(), self.build_version()
        query = self.search.get().strip().lower()
        kinds = {"release": tr("релиз"), "snapshot": tr("снапшот"), "old_beta": tr("бета"), "old_alpha": tr("альфа")}
        rows = []
        for v in self.versions:
            if self.vfilter == 0 and v["type"] != "release":
                continue
            if self.vfilter == 1 and v["type"] != "snapshot":
                continue
            if self.vfilter == 2 and v["id"] not in installed:
                continue
            if query and query not in v["id"].lower():
                continue
            rows.append(v)
        if not rows:
            label(self.vlist, tr("Ничего не найдено.") if self.versions else tr("Загружаю список версий…"),
                  14).pack(anchor="w", padx=8, pady=8)
        for v in rows[:40]:
            note = kinds.get(v["type"], tr("своя")) + (tr(", установлена") if v["id"] in installed else "")
            on = v["id"] == chosen
            ctk.CTkButton(self.vlist, text=f"{v['id']}      {note}", anchor="w", height=42,
                          corner_radius=th.rad, font=ctk.CTkFont(size=14), text_color=TEXT, border_width=1,
                          fg_color=th.active if on else th.card, border_color=th.accent if on else th.edge,
                          hover_color=th.hover, command=lambda vid=v["id"]: self.select_version(vid)
                          ).pack(fill="x", pady=3)
        if len(rows) > 40:
            label(self.vlist, tr("Показаны первые 40 из {n}. Уточните поиск.", n=len(rows)), 12
                  ).pack(anchor="w", padx=8, pady=8)

    # сборка = версия + моды + миры; все настройки версии относятся к выбранной сборке
    def cur_build(self):
        return next(b for b in self.cfg["builds"] if b["id"] == self.cfg["build"])

    def build_version(self):
        return self.cur_build().get("version") or ""

    def build_name(self, b):
        return b["name"] or tr("Основная")

    def build_summary(self, b):
        ver = b.get("version")
        return tr("{ver} · модов: {n} · {ram} ГБ", ver=mr.pretty_version(ver) if ver else "—",
                  n=bd.mod_count(bd.folder(b, GAME_DIR, INSTANCES_DIR)), ram=b.get("ram") or self.cfg["ram"])

    def mods_dir(self):
        return bd.folder(self.cur_build(), GAME_DIR, INSTANCES_DIR) / "mods"

    def update_build(self, bid, **fields):
        for b in self.cfg["builds"]:
            if b["id"] == bid:
                b.update(fields)
        save_cfg(self.cfg)
        self.refresh_version_labels()
        self.mods_key = None
        if self.page == "builds":
            self.render_builds()
        elif self.page == "mods":
            self.render_mods()
        elif self.page == "versions":
            self.render_versions()

    def select_version(self, vid):
        self.update_build(self.cfg["build"], version=vid)

    def pick_latest(self):
        if self.latest:
            self.select_version(self.latest)
            self.say(tr("Выбрана версия {v}.", v=self.latest))

    def refresh_version_labels(self):
        b = self.cur_build()
        name = self.build_name(b)
        self.build_btn.configure(text="  " + (name if len(name) <= 18 else name[:17] + "…"))
        self.build_info.configure(text=self.build_summary(b))
        self.latest_lbl.configure(text=self.latest or "—")

    def _load_versions(self):
        try:
            online = fetch_versions()
        except Exception:
            online = []
        try:
            local = [{"id": v["id"], "type": v.get("type", "custom")}
                     for v in mll.utils.get_installed_versions(str(GAME_DIR))]
        except Exception:
            local = []
        known = {v["id"] for v in online}
        self.ui(self._set_versions, online + [v for v in local if v["id"] not in known])

    def _set_versions(self, items):
        self.versions = items
        self.all_ids = {v["id"] for v in items}
        releases = [v["id"] for v in items if v["type"] == "release"]
        self.latest = releases[0] if releases else ""
        build = self.cur_build()
        if not build.get("version") and self.latest:
            build["version"] = self.latest
            save_cfg(self.cfg)
        self.refresh_version_labels()
        if self.page == "versions":
            self.render_versions()

    # ── моды, шейдеры, ресурспаки и сборки модов (Modrinth) ──
    KIND_KEYS = ["mod", "shader", "resourcepack", "modpack"]

    def _build_mods(self, page):
        self._title(page, tr("Моды"), tr("Моды, шейдеры, ресурспаки и сборки скачиваются с Modrinth."))
        self._ph_icon = ctk.CTkImage(modinfo.round_corners(cover_tile(96)), size=(48, 48))
        info = card(page, width=700, height=64)
        info.place(x=40, y=102)
        self.mod_info_lbl = label(info, "", 13, TEXT, wraplength=470, justify="left")
        self.mod_info_lbl.place(x=18, y=12)
        self.fabric_btn = button(info, tr("Установить Fabric"), self.install_fabric, primary=True, width=190, height=36)
        self.kind_names = [tr("Моды"), tr("Шейдеры"), tr("Ресурспаки"), tr("Сборки модов")]
        self.kind_seg = segmented(page, self.kind_names, self._kind_picked)
        self.kind_seg.set(self.kind_names[self.KIND_KEYS.index(self.mods_kind)])
        self.kind_seg.place(x=40, y=178)
        self.mods_names = ["Modrinth", tr("Установленные")]
        self.mod_tabs = segmented(page, self.mods_names, self._mods_tab)
        self.mod_tabs.set(self.mods_names[self.mods_tab])
        self.mod_tabs.place(x=520, y=178)
        self.mod_query = entry(page, tr("Поиск"), width=430, height=36)
        self.mod_query.bind("<Return>", lambda _e: self.mods_search(True), add="+")
        self.mod_find = button(page, tr("Найти"), lambda: self.mods_search(True), primary=True, width=120, height=36)
        self.mods_list = scroll_list(page, 690, 300)
        self.mods_list.place(x=34, y=268)
        self.mods_status = label(page, "", 13, wraplength=700, justify="left")
        self.mods_status.place(x=40, y=584)
        self.status_labels.append(self.mods_status)
        self.mods_key = None

    def mods_ctx(self):
        vid = self.build_version()
        mc, loader = mr.parse_version(vid) if vid else ("", None)
        return vid, mc, loader

    def content_dir(self, kind):
        """Папка выбранной сборки для этого типа файлов: mods, shaderpacks или resourcepacks."""
        return bd.folder(self.cur_build(), GAME_DIR, INSTANCES_DIR) / mr.KINDS[kind]["folder"]

    def _kind_picked(self, value):
        self.mods_kind = self.KIND_KEYS[self.kind_names.index(value)]
        self.mods_key = None
        self.render_mods()

    def _mods_tab(self, value):
        self.mods_tab = self.mods_names.index(value)
        self.render_mods()

    def render_mods(self):
        kind = self.mods_kind
        vid, mc, loader = self.mods_ctx()
        name = self.build_name(self.cur_build())
        name = name if len(name) <= 22 else name[:21] + "…"
        if kind == "modpack":
            text = tr("Сборка модов установится как новая сборка: со своей версией, модами и настройками.")
        elif not vid:
            text = tr("Сначала выберите версию игры.")
        elif kind == "shader":
            text = tr("Сборка «{b}»: Minecraft {mc}. Шейдерам нужен мод Iris (Fabric) или OptiFine.", b=name, mc=mc)
        elif kind == "resourcepack":
            text = tr("Сборка «{b}»: Minecraft {mc}. Ресурспаки включаются в настройках игры.", b=name, mc=mc)
        elif loader:
            text = tr("Сборка «{b}»: Minecraft {mc}, загрузчик {loader}", b=name, mc=mc, loader=mr.LOADERS[loader])
        else:
            text = tr("Сборка «{b}»: Minecraft {mc} без загрузчика. Нужен Fabric.", b=name, mc=mc)
        self.mod_info_lbl.configure(text=text)
        if kind == "mod" and vid and not loader:
            self.fabric_btn.place(x=490, y=14)
        else:
            self.fabric_btn.place_forget()
        if self.mods_tab == 0:
            self.mod_query.place(x=40, y=222)
            self.mod_find.place(x=484, y=222)
            self.mods_search(False)
        else:
            self.mod_query.place_forget()
            self.mod_find.place_forget()
            self.render_installed()

    def mods_search(self, force):
        kind = self.mods_kind
        vid, mc, loader = self.mods_ctx()
        if not vid and kind != "modpack":
            clear(self.mods_list)
            return
        key = (self.mod_query.get().strip(), mc, loader or "fabric", kind)
        if key == self.mods_key and not force:
            return
        self.mods_key = key
        self.say(tr("Ищу на Modrinth…"))
        threading.Thread(target=self._search_worker, args=(key,), daemon=True).start()

    def _search_worker(self, key):
        try:
            hits = mr.search(key[0], key[1], key[2], kind=key[3])
        except Exception as e:
            self.say(tr("Не удалось связаться с Modrinth: {e}", e=e), True)
            return
        self.ui(self._show_hits, key, hits)

    def _show_hits(self, key, hits):
        if key != self.mods_key or self.mods_tab != 0:
            return
        clear(self.mods_list)
        if not hits:
            label(self.mods_list, tr("Ничего не найдено."), 14).pack(anchor="w", padx=8, pady=8)
        icons = []
        for hit in hits:
            row = card(self.mods_list)
            row.pack(fill="x", pady=4)
            row.grid_columnconfigure(1, weight=1)
            icon = ctk.CTkLabel(row, text="", width=0, image=self._ph_icon)
            icon.grid(row=0, column=0, padx=(14, 4), pady=12)
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.grid(row=0, column=1, sticky="w", padx=10, pady=10)
            label(text, hit["title"], 15, TEXT, True).pack(anchor="w")
            label(text, f"{hit['author']} · " + tr("{n} загрузок", n=mr.compact(hit["downloads"])), 12
                  ).pack(anchor="w")
            desc = hit.get("description", "")
            label(text, desc if len(desc) <= 80 else desc[:79] + "…", 12, TEXT).pack(anchor="w", pady=(4, 0))
            btn = button(row, tr("Установить") if key[3] == "modpack" else tr("Скачать"), None, primary=True,
                         width=120, height=36)
            btn.configure(command=lambda b=btn, h=hit: self.install_item(h, b))
            btn.grid(row=0, column=2, padx=16)
            icons.append((hit.get("icon_url"), icon))
        self._load_icons(icons)
        self.say(tr("Найдено: {n}", n=len(hits)))

    def _load_icons(self, items):
        def work():
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(mr.fetch_icon, url, CACHE / "icons"): lbl for url, lbl in items if url}
                for fut in as_completed(futures):
                    img = fut.result()
                    if img is not None:
                        self.ui(self._put_icon, futures[fut], img)
        threading.Thread(target=work, daemon=True).start()

    def _put_icon(self, lbl, img):
        lbl.configure(image=ctk.CTkImage(img, size=(48, 48)))

    def install_item(self, hit, btn):
        if self.mods_kind == "modpack":
            self.install_modpack(hit, btn)
        else:
            self.install_mod(hit, btn)

    def install_mod(self, hit, btn):
        kind = self.mods_kind
        vid, mc, loader = self.mods_ctx()
        btn.configure(state="disabled", text=tr("Скачиваю…"))
        threading.Thread(target=self._install_worker,
                         args=(hit, btn, mc, loader or "fabric", self.content_dir(kind), self.cfg["build"], kind),
                         daemon=True).start()

    def _install_worker(self, hit, btn, mc, loader, folder, bid, kind):
        try:
            done = mr.install(hit["project_id"], mc, loader, folder, kind=kind)
        except LookupError:
            self.ui(self._mod_done, btn, False,
                    tr("Для {mc} ({loader}) у этого мода нет подходящей версии.", mc=mc, loader=mr.LOADERS[loader])
                    if kind == "mod" else tr("Для Minecraft {mc} нет подходящей версии.", mc=mc))
        except Exception as e:
            self.ui(self._mod_done, btn, False, tr("Не удалось скачать: {e}", e=e))
        else:
            self._index_add(bid, done)
            self.ui(self._mod_done, btn, True, tr("Установлено: {names}", names=", ".join(d["file"] for d in done)))

    def _mod_done(self, btn, ok, text):
        btn.configure(text=tr("Установлено") if ok else tr("Скачать"), state="disabled" if ok else "normal")
        self.say(text, not ok)

    # сборка модов (.mrpack) ставится как новая сборка
    def install_modpack(self, hit, btn):
        if self.busy:
            self.say(tr("Подождите, пока закончится текущая операция."), True)
            return
        self.busy = True
        btn.configure(state="disabled", text=tr("Устанавливаю…"))
        threading.Thread(target=self._modpack_worker, args=(hit, btn), daemon=True).start()

    def _modpack_worker(self, hit, btn):
        build = icon = None
        try:
            if not hasattr(mll, "mrpack"):
                raise RuntimeError(tr("Обновите библиотеку: pip install -U minecraft-launcher-lib"))
            self.say(tr("Скачиваю сборку «{name}»…", name=hit["title"]))
            pack = mr.download_pack(hit["project_id"], CACHE / "packs")
            build = bd.new_build(hit["title"], "", self.cfg["ram"])
            gdir = bd.folder(build, GAME_DIR, INSTANCES_DIR)
            gdir.mkdir(parents=True, exist_ok=True)
            icon = mr.fetch_icon(hit.get("icon_url"), CACHE / "icons")
            self.say(tr("Устанавливаю сборку «{name}»…", name=hit["title"]))
            mll.mrpack.install_mrpack(str(pack), str(GAME_DIR), modpack_directory=str(gdir),
                                      callback=self._install_callback(), mrpack_install_options={"optionalFiles": []})
            build["version"] = mll.mrpack.get_mrpack_launch_version(str(pack))
        except LookupError:
            error = tr("У этой сборки нет файла .mrpack для установки.")
        except Exception as e:
            error = tr("Не удалось установить сборку: {e}", e=e)
        else:
            error = ""
        if error and build is not None:
            bd.remove(bd.folder(build, GAME_DIR, INSTANCES_DIR))          # не оставляем недоделанную папку
            build = None
        self.busy = False
        self.ui(self._modpack_done, build, icon, btn, error)

    def _modpack_done(self, build, icon, btn, error):
        if build is None:
            btn.configure(state="normal", text=tr("Установить"))
            self.say(error, True)
            return
        if icon is not None:
            self.save_cover(build, icon)
        self.add_build(build, fabric=False)
        btn.configure(text=tr("Установлено"))
        self.say(tr("Сборка «{name}» установлена и выбрана. Нажмите «Играть».", name=build["name"]))

    # список скачанных файлов запоминает названия и иконки с Modrinth
    @staticmethod
    def _index_path(bid):
        return CACHE / f"modindex_{bid}.json"

    def _index_load(self, bid):
        try:
            return json.loads(self._index_path(bid).read_text("utf-8"))
        except Exception:
            return {}

    def _index_add(self, bid, done):
        index = self._index_load(bid)
        for d in done:
            index[d["file"]] = {"title": d["title"], "icon": d["icon"]}
        CACHE.mkdir(parents=True, exist_ok=True)
        self._index_path(bid).write_text(json.dumps(index, ensure_ascii=False), "utf-8")

    def render_installed(self):
        clear(self.mods_list)
        kind = self.mods_kind
        if kind == "modpack":
            label(self.mods_list, tr("Сборки модов устанавливаются как отдельные сборки. Смотрите вкладку «Сборки»."),
                  14, wraplength=640, justify="left").pack(anchor="w", padx=8, pady=8)
            return
        folder = self.content_dir(kind)
        button(self.mods_list, tr("Открыть папку"), lambda: open_folder(folder), width=210
               ).pack(anchor="w", pady=(0, 8))
        files = sorted(folder.glob("*.jar" if kind == "mod" else "*.zip")) if folder.is_dir() else []
        if not files:
            label(self.mods_list, tr("Пока ничего нет."), 14).pack(anchor="w", padx=8, pady=8)
        items = []
        for path in files:
            row = card(self.mods_list)
            row.pack(fill="x", pady=4)
            row.grid_columnconfigure(1, weight=1)
            icon = ctk.CTkLabel(row, text="", width=0, image=self._ph_icon)
            icon.grid(row=0, column=0, padx=(14, 4), pady=10)
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.grid(row=0, column=1, sticky="w", padx=10)
            title = label(text, path.name, 14, TEXT, True)
            title.pack(anchor="w")
            label(text, f"{path.name}   {path.stat().st_size // 1024} " + tr("КБ"), 12).pack(anchor="w")
            button(row, tr("Удалить"), lambda p=path: self.delete_mod(p), width=100, height=32
                   ).grid(row=0, column=2, padx=16)
            items.append((path, icon, title))
        if items:
            threading.Thread(target=self._mod_meta_worker, args=(items, self.cfg["build"]), daemon=True).start()

    def _mod_meta_worker(self, items, bid):
        index = self._index_load(bid)
        for path, icon, title in items:
            info = index.get(path.name) or {}
            name = info.get("title")
            img = mr.fetch_icon(info["icon"], CACHE / "icons") if info.get("icon") else None
            if img is None or not name:                       # файл положили вручную: читаем данные из самого файла
                file_name, file_img = modinfo.file_info(path)
                name = name or file_name
                if img is None and file_img is not None:
                    img = modinfo.round_corners(file_img)
            self.ui(self._put_mod, icon, title, name, img)

    def _put_mod(self, icon, title, name, img):
        if name:
            title.configure(text=name)
        if img is not None:
            icon.configure(image=ctk.CTkImage(img, size=(48, 48)))

    def delete_mod(self, path):
        if messagebox.askyesno(tr("Удалить"), tr("Удалить файл {name}?", name=path.name), parent=self):
            try:
                path.unlink()
            except OSError as e:
                self.say(tr("Ошибка: {e}", e=e), True)
            self.render_installed()

    def install_fabric(self):
        vid, mc, loader = self.mods_ctx()
        if mc and not loader:
            self._start_fabric(mc, self.cfg["build"])

    def _start_fabric(self, mc, bid):
        if self.busy:
            self.say(tr("Подождите, пока закончится текущая операция."), True)
            return
        self.busy = True
        self.fabric_btn.configure(state="disabled")
        self.say(tr("Устанавливаю Fabric для {mc}…", mc=mc))
        threading.Thread(target=self._fabric_worker, args=(mc, bid), daemon=True).start()

    def _fabric_worker(self, mc, bid):
        try:
            install_fabric_loader(mc, self._install_callback())
            found = [i for i in installed_ids() if i.startswith("fabric-loader-") and i.endswith("-" + mc)]
            newest = max(found, key=lambda i: (GAME_DIR / "versions" / i / f"{i}.json").stat().st_mtime)
            self.ui(self._fabric_done, newest, bid)
        except Exception as e:
            self.say(tr("Ошибка: {e}", e=e), True)
        finally:
            self.busy = False
            self.ui(lambda: self.fabric_btn.configure(state="normal"))

    def _fabric_done(self, vid, bid):
        if vid not in {v["id"] for v in self.versions}:
            self.versions.append({"id": vid, "type": "custom"})
        self.update_build(bid, version=vid)
        self.say(tr("Fabric установлен. Теперь можно скачивать моды."))

    # ── сборки ──
    def _build_builds(self, page):
        self._title(page, tr("Сборки"), tr("Свои наборы модов. У каждой сборки отдельные моды, миры и настройки."))
        button(page, "  " + tr("Создать сборку"), self.new_build_dialog, primary=True, width=190, height=40,
               image=load_icon("plus", "#FFFFFF", 18), compound="left").place(x=550, y=26)
        self.blist = scroll_list(page, 690, 440)
        self.blist.place(x=34, y=112)
        self.builds_status = label(page, "", 13, wraplength=700, justify="left")
        self.builds_status.place(x=40, y=584)
        self.status_labels.append(self.builds_status)

    def _cover(self, b):
        img = None
        if b.get("image"):
            try:
                img = Image.open(COVERS_DIR / b["image"])
            except Exception:
                img = None
        if img is None:
            img = cover_tile(128)
        return ctk.CTkImage(modinfo.round_corners(modinfo.square(img, 128)), size=(64, 64))

    def render_builds(self):
        clear(self.blist)
        for b in self.cfg["builds"]:
            on = b["id"] == self.cfg["build"]
            row = card(self.blist, border_color=th.accent if on else th.edge)
            row.pack(fill="x", pady=5)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text="", width=0, image=self._cover(b)).grid(row=0, column=0, padx=(14, 12), pady=14)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.grid(row=0, column=1, sticky="w")
            label(info, self.build_name(b), 17, TEXT, True).pack(anchor="w")
            label(info, self.build_summary(b), 12).pack(anchor="w", pady=(2, 0))
            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.grid(row=0, column=2, padx=14)
            if on:
                button(actions, tr("Выбрана"), None, width=112, height=38, state="disabled", fg_color=th.active,
                       border_color=th.accent, text_color_disabled=th.hi).pack(side="left")
            else:
                button(actions, tr("Выбрать"), lambda i=b["id"]: self.select_build(i), primary=True,
                       width=112, height=38).pack(side="left")
            button(actions, "", lambda i=b["id"]: self.edit_build_dialog(i), width=40, height=38,
                   image=load_icon("pencil", TEXT, 18)).pack(side="left", padx=(8, 0))
        if len(self.cfg["builds"]) == 1:
            label(self.blist, tr("Нажмите «Создать сборку», чтобы собрать свой набор модов."), 13
                  ).pack(anchor="w", padx=8, pady=12)

    def select_build(self, bid):
        self.cfg["build"] = bid
        save_cfg(self.cfg)
        self.refresh_version_labels()
        self.mods_key = None
        if self.page == "builds":
            self.render_builds()
        self.say(tr("Выбрана сборка «{name}».", name=self.build_name(self.cur_build())))

    def new_build_dialog(self):
        BuildDialog(self)

    def edit_build_dialog(self, bid):
        BuildDialog(self, bid)

    def save_cover(self, b, img):
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{b['id']}.png"
        modinfo.square(img, 128).save(COVERS_DIR / name)
        b["image"] = name
        return name

    def drop_cover(self, b):
        if b.get("image"):
            (COVERS_DIR / b["image"]).unlink(missing_ok=True)

    def add_build(self, build, fabric):
        self.cfg["builds"].append(build)
        self.cfg["build"] = build["id"]
        save_cfg(self.cfg)
        bd.folder(build, GAME_DIR, INSTANCES_DIR).mkdir(parents=True, exist_ok=True)
        self.refresh_version_labels()
        self.mods_key = None
        if self.page == "builds":
            self.render_builds()
        if fabric:
            self._start_fabric(build["version"], build["id"])

    def duplicate_build(self, bid):
        src = next(b for b in self.cfg["builds"] if b["id"] == bid)
        new = bd.new_build(tr("{name} (копия)", name=self.build_name(src)), src["version"], src.get("ram"))
        self.say(tr("Копирую сборку…"))
        threading.Thread(target=self._duplicate_worker, args=(src, new), daemon=True).start()

    def _duplicate_worker(self, src, new):
        try:
            bd.copy_parts(bd.folder(src, GAME_DIR, INSTANCES_DIR), bd.folder(new, GAME_DIR, INSTANCES_DIR))
            if self._index_path(src["id"]).exists():
                shutil.copy(self._index_path(src["id"]), self._index_path(new["id"]))
            if src.get("image") and (COVERS_DIR / src["image"]).exists():
                new["image"] = f"{new['id']}.png"
                shutil.copy(COVERS_DIR / src["image"], COVERS_DIR / new["image"])
        except Exception as e:
            self.say(tr("Ошибка: {e}", e=e), True)
            return
        self.ui(self._duplicate_done, new)

    def _duplicate_done(self, new):
        self.cfg["builds"].append(new)
        save_cfg(self.cfg)
        if self.page == "builds":
            self.render_builds()
        self.say(tr("Сборка «{name}» создана.", name=new["name"]))

    def delete_build(self, bid):
        b = next((x for x in self.cfg["builds"] if x["id"] == bid), None)
        if b is None or bid == bd.MAIN:
            return False
        if not messagebox.askyesno(tr("Удалить сборку"),
                                   tr("Удалить сборку «{name}» вместе с её модами и мирами?", name=self.build_name(b)),
                                   parent=self):
            return False
        bd.remove(bd.folder(b, GAME_DIR, INSTANCES_DIR))
        self.drop_cover(b)
        self.cfg["builds"] = [x for x in self.cfg["builds"] if x["id"] != bid]
        if self.cfg["build"] == bid:
            self.cfg["build"] = bd.MAIN
        save_cfg(self.cfg)
        self.refresh_version_labels()
        self.mods_key = None
        if self.page == "builds":
            self.render_builds()
        return True

    # ── скины ──
    def _build_skins(self, page):
        self._title(page, tr("Скины"), tr("Скин виден в игре и на вашем аватаре в лаунчере."))
        box = card(page, width=290, height=480)
        box.place(x=40, y=108)
        self.skin_preview = ctk.CTkLabel(box, text="", width=0)
        self.skin_preview.place(relx=0.5, rely=0.45, anchor="center")
        self.skin_caption = label(box, "", 13, TEXT)
        self.skin_caption.place(relx=0.5, y=440, anchor="center")
        self.skin_side = ctk.CTkFrame(page, fg_color="transparent", width=380, height=480)
        self.skin_side.place(x=360, y=108)

    def render_skins(self):
        skin = self.skin_img if self.skin_img is not None else skins.placeholder_skin()
        pv = skins.preview(skin, self.skin_slim, 7)
        self._skin_pv = ctk.CTkImage(pv, size=pv.size)
        self.skin_preview.configure(image=self._skin_pv)
        acc = self.cfg["account"]
        kind = acc.get("type", "ely")
        if self.skin_img is None:
            caption = tr("Скин не выбран")
        elif kind == "ely":
            caption = tr("Скин Ely.by: {name}", name=acc["name"])
        elif kind == "microsoft":
            caption = tr("Скин Microsoft: {name}", name=acc["name"])
        else:
            caption = tr("Локальный скин")
        self.skin_caption.configure(text=caption)

        side = self.skin_side
        clear(side)
        if kind in ("ely", "microsoft"):
            site, url = ("Ely.by", "https://ely.by/") if kind == "ely" else (
                "Minecraft.net", "https://www.minecraft.net/msaprofile/mygames/editskin")
            label(side, tr("Аккаунт {site}: {name}", site=site, name=acc["name"]), 17, TEXT, True).pack(anchor="w")
            label(side, tr("Скин берётся с аккаунта и показывается в игре автоматически. Чтобы сменить его, откройте сайт."),
                  13, wraplength=360, justify="left").pack(anchor="w", pady=(6, 16))
            button(side, tr("Открыть {site}", site=site), lambda: webbrowser.open(url), primary=True,
                   width=200).pack(anchor="w")
            button(side, tr("Обновить скин"), self.refresh_skin, width=200).pack(anchor="w", pady=(10, 0))
            return
        label(side, tr("Локальные скины (бета)"), 17, TEXT, True).pack(anchor="w")
        label(side, tr("Скин виден только вам в игре. Другие игроки на серверах его не увидят."), 13,
              wraplength=360, justify="left").pack(anchor="w", pady=(6, 12))
        button(side, tr("Добавить скин из файла…"), self.add_skin, primary=True, width=250).pack(anchor="w")
        lib = scroll_list(side, 330, 200)
        lib.pack(anchor="w", pady=(12, 8))
        current = self.cfg.get("skin")
        files = sorted(SKINS_DIR.glob("*.png")) if SKINS_DIR.is_dir() else []
        for fname, title in [("", tr("Без скина"))] + [(p.name, p.stem) for p in files]:
            on = (current["file"] if current else "") == fname
            ctk.CTkButton(lib, text=title, anchor="w", height=36, corner_radius=th.rad, text_color=TEXT,
                          border_width=1, fg_color=th.active if on else th.card,
                          border_color=th.accent if on else th.edge, hover_color=th.hover,
                          command=lambda f=fname: self.choose_skin(f)).pack(fill="x", pady=2)
        if current:
            row = ctk.CTkFrame(side, fg_color="transparent")
            row.pack(anchor="w", fill="x")
            self.model_names = [tr("Обычные руки"), tr("Тонкие руки")]
            seg = segmented(row, self.model_names, self._skin_model)
            seg.set(self.model_names[1 if current.get("model") == "slim" else 0])
            seg.pack(side="left")
            button(row, tr("Удалить"), self.delete_skin, width=100, height=32).pack(side="left", padx=10)

    def refresh_skin(self):
        acc = self.cfg["account"]
        kind = acc.get("type", "ely")
        self.skin_img, self.skin_slim = None, False
        if kind == "ely":
            threading.Thread(target=self._fetch_ely_skin, args=(acc["name"],), daemon=True).start()
        elif kind == "microsoft":
            threading.Thread(target=self._fetch_ms_skin, args=(dict(acc),), daemon=True).start()
        else:
            current = self.cfg.get("skin")
            path = SKINS_DIR / current["file"] if current else None
            if path and path.exists():
                self.skin_img = Image.open(path).convert("RGBA")
                self.skin_slim = current.get("model") == "slim"
        self._set_avatar()
        if self.page == "skins":
            self.render_skins()

    def _fetch_ely_skin(self, name):
        try:
            img, slim = skins.fetch_ely_skin(name)
        except Exception:
            return
        self.ui(self._skin_loaded, name, img, slim)

    def _fetch_ms_skin(self, acc):
        url, slim = acc.get("skin_url"), bool(acc.get("slim"))
        try:
            url, slim = ms.profile_skin(acc["token"])         # свежий скин, пока токен жив
        except Exception:
            pass
        if not url:
            return
        try:
            img = skins.fetch_skin_url(url)
        except Exception:
            return
        self.ui(self._skin_loaded, acc["name"], img, slim)

    def _skin_loaded(self, name, img, slim):
        acc = self.cfg["account"]
        if acc.get("type", "ely") not in ("ely", "microsoft") or acc["name"] != name or img is None:
            return
        self.skin_img, self.skin_slim = img, slim
        self._set_avatar()
        if self.page == "skins":
            self.render_skins()

    def add_skin(self):
        path = filedialog.askopenfilename(parent=self, title=tr("Выберите PNG со скином"),
                                          filetypes=[("PNG", "*.png")])
        if not path:
            return
        try:
            img = Image.open(path).convert("RGBA")
        except Exception:
            img = None
        if img is None or not skins.valid_skin(img):
            messagebox.showerror(tr("Скин"), tr("Скин должен быть PNG размером 64×64 или 64×32."), parent=self)
            return
        name = skins.save_skin(img, SKINS_DIR, Path(path).stem)
        self.cfg["skin"] = {"file": name, "model": "slim" if skins.looks_slim(img) else "classic"}
        save_cfg(self.cfg)
        self.refresh_skin()

    def choose_skin(self, fname):
        if not fname:
            self.cfg["skin"] = None
        else:
            img = Image.open(SKINS_DIR / fname).convert("RGBA")
            self.cfg["skin"] = {"file": fname, "model": "slim" if skins.looks_slim(img) else "classic"}
        save_cfg(self.cfg)
        self.refresh_skin()

    def _skin_model(self, value):
        self.cfg["skin"]["model"] = "slim" if value == self.model_names[1] else "classic"
        save_cfg(self.cfg)
        self.refresh_skin()

    def delete_skin(self):
        current = self.cfg.get("skin")
        if current and messagebox.askyesno(tr("Скин"), tr("Удалить скин {name}?", name=current["file"]), parent=self):
            try:
                (SKINS_DIR / current["file"]).unlink()
            except OSError:
                pass
            self.cfg["skin"] = None
            save_cfg(self.cfg)
            self.refresh_skin()

    # ── настройки ──
    def _build_settings(self, page):
        self._title(page, tr("Настройки"), tr("Параметры игры и внешний вид."))
        box = scroll_list(page, 690, 500)
        box.place(x=34, y=104)

        def section(title):
            label(box, title, 18, TEXT, True).pack(anchor="w", padx=6, pady=(20, 8))

        section(tr("Игра"))
        self.ram_lbl = label(box, "", 14, TEXT)
        self.ram_lbl.pack(anchor="w", padx=6, pady=(0, 6))
        self.ram = ctk.CTkSlider(box, from_=1, to=16, number_of_steps=15, width=440, progress_color=th.accent,
                                 button_color=th.hi, button_hover_color=th.hi, fg_color=th.edge,
                                 command=self._ram_changed)
        self.ram.set(self.cfg["ram"])
        self.ram.pack(anchor="w", padx=6)
        self.ram.bind("<ButtonRelease-1>", lambda _e: save_cfg(self.cfg), add="+")
        self._ram_changed(self.cfg["ram"])
        label(box, tr("Путь к Java (необязательно)"), 14, TEXT).pack(anchor="w", padx=6, pady=(16, 6))
        self.java = entry(box, tr("Пусто — Java подберётся автоматически"), width=440)
        self.java.insert(0, self.cfg.get("java", ""))
        self.java.pack(anchor="w", padx=6)
        self.java.bind("<FocusOut>", self._save_java, add="+")
        self.java.bind("<Return>", self._save_java, add="+")
        label(box, tr("После запуска игры"), 14, TEXT).pack(anchor="w", padx=6, pady=(16, 6))
        self.after_names = [tr("Свернуть лаунчер"), tr("Оставить открытым"), tr("Закрыть лаунчер")]
        after = segmented(box, self.after_names, self._after_picked)
        after.set(self.after_names[["hide", "keep", "close"].index(self.cfg.get("after_launch", "hide"))])
        after.pack(anchor="w", padx=6)
        label(box, tr("Папка игры"), 14, TEXT).pack(anchor="w", padx=6, pady=(16, 4))
        label(box, str(GAME_DIR), 12, wraplength=640, justify="left").pack(anchor="w", padx=6, pady=(0, 8))
        button(box, tr("Открыть папку игры"), lambda: open_folder(GAME_DIR), width=190).pack(anchor="w", padx=6)

        section(tr("Внешний вид"))
        label(box, tr("Язык"), 14, TEXT).pack(anchor="w", padx=6, pady=(0, 6))
        seg = segmented(box, list(LANGS.values()), self._lang_picked)
        seg.set(LANGS[lang()])
        seg.pack(anchor="w", padx=6)
        label(box, tr("Цвет интерфейса"), 14, TEXT).pack(anchor="w", padx=6, pady=(18, 8))
        swatches = ctk.CTkFrame(box, fg_color="transparent")
        swatches.pack(anchor="w", padx=2)
        chosen = self.cfg["accent"].upper()
        for hexcode, _name in PRESETS:
            ctk.CTkButton(swatches, text="", width=36, height=36, corner_radius=18, fg_color=hexcode,
                          hover_color=hexcode, border_width=3 if chosen == hexcode else 0, border_color=TEXT,
                          command=lambda c=hexcode: self._later(self.set_accent, c)).pack(side="left", padx=5)
        button(box, tr("Свой цвет…"), lambda: self._later(self.pick_color), width=150).pack(anchor="w", padx=6, pady=(10, 0))
        self.rad_lbl = label(box, tr("Скругление углов: {r}", r=self.cfg["radius"]), 14, TEXT)
        self.rad_lbl.pack(anchor="w", padx=6, pady=(18, 6))
        self.rad_slider = ctk.CTkSlider(box, from_=0, to=28, number_of_steps=28, width=440, progress_color=th.accent,
                                        button_color=th.hi, button_hover_color=th.hi, fg_color=th.edge,
                                        command=lambda v: self.rad_lbl.configure(
                                            text=tr("Скругление углов: {r}", r=int(round(v)))))
        self.rad_slider.set(self.cfg["radius"])
        self.rad_slider.pack(anchor="w", padx=6)
        self.rad_slider.bind("<ButtonRelease-1>", lambda _e: self._later(self.set_radius), add="+")

        section(tr("Обновления"))
        label(box, f"{NAME} {VERSION}", 14, TEXT).pack(anchor="w", padx=6, pady=(0, 8))
        self.auto_switch = ctk.CTkSwitch(box, text=tr("Обновлять автоматически"), command=self._auto_update_toggled,
                                         text_color=TEXT, progress_color=th.accent, button_color=th.hi,
                                         button_hover_color=th.hi, fg_color=th.edge)
        if self.cfg.get("auto_update", True):
            self.auto_switch.select()
        self.auto_switch.pack(anchor="w", padx=6)
        label(box, tr("Источник обновлений"), 14, TEXT).pack(anchor="w", padx=6, pady=(14, 6))
        self.update_entry = entry(box, tr("владелец/репозиторий на GitHub или ссылка на update.json"), width=440)
        self.update_entry.insert(0, self.cfg.get("update_source") or "")
        self.update_entry.pack(anchor="w", padx=6)
        self.update_entry.bind("<FocusOut>", self._save_update_source, add="+")
        self.update_entry.bind("<Return>", self._save_update_source, add="+")
        if UPDATE_REPO:
            label(box, tr("По умолчанию: {repo}", repo=UPDATE_REPO), 12).pack(anchor="w", padx=6, pady=(6, 0))
        button(box, tr("Проверить обновления"), lambda: (self._save_update_source(), self.check_updates(True)),
               width=220).pack(anchor="w", padx=6, pady=(12, 0))
        self.update_lbl = label(box, "", 13, wraplength=640, justify="left")
        self.update_lbl.pack(anchor="w", padx=6, pady=(8, 0))
        self.status_labels.append(self.update_lbl)

        section(tr("Аккаунт"))
        self.set_acc = label(box, "", 14, TEXT)
        self.set_acc.pack(anchor="w", padx=6, pady=(0, 8))
        button(box, tr("Сменить аккаунт"), self.open_account, width=190).pack(anchor="w", padx=6, pady=(0, 24))

    def _ram_changed(self, value):
        self.cfg["ram"] = int(round(value))
        self.ram_lbl.configure(text=tr("Память: {n} ГБ", n=self.cfg["ram"]))

    def _save_java(self, _event=None):
        self.cfg["java"] = self.java.get().strip()
        save_cfg(self.cfg)

    def _after_picked(self, value):
        self.cfg["after_launch"] = ["hide", "keep", "close"][self.after_names.index(value)]
        save_cfg(self.cfg)

    def _lang_picked(self, value):
        self.cfg["lang"] = next(code for code, name in LANGS.items() if name == value)
        save_cfg(self.cfg)
        self._later(self.rebuild)

    def set_accent(self, color):
        self.cfg["accent"] = color.upper()
        save_cfg(self.cfg)
        self.rebuild()

    def pick_color(self):
        _rgb, code = colorchooser.askcolor(color=self.cfg["accent"], parent=self, title=tr("Цвет интерфейса"))
        if code:
            self.set_accent(code)

    def set_radius(self):
        self.cfg["radius"] = int(round(self.rad_slider.get()))
        save_cfg(self.cfg)
        self.rebuild()

    # ── аккаунт ──
    def refresh_account(self):
        acc = self.cfg["account"]
        site = {"ely": "Ely.by", "microsoft": "Microsoft"}.get(acc.get("type", "ely"))
        self.acc_name.configure(text=acc["name"])
        self.acc_state.configure(text=site or tr("Оффлайн"), text_color=GREEN if site else DIM)
        self.set_acc.configure(text=f"{acc['name']} — " + (site or tr("оффлайн")))

    def open_account(self, tab=None):
        AccountDialog(self, tab)

    def set_account(self, acc):
        self.cfg["account"] = acc
        if acc["type"] == "offline":
            self.cfg["offline_name"] = acc["name"]
        save_cfg(self.cfg)
        self.refresh_account()
        self.refresh_skin()

    def logout_ely(self):
        acc = self.cfg["account"]
        payload = {"accessToken": acc.get("token", ""), "clientToken": self.cfg["client_token"]}
        threading.Thread(target=self._invalidate, args=(payload,), daemon=True).start()
        self.set_account({"type": "offline", "name": self.cfg.get("offline_name", "GlebSter")})

    def logout_ms(self):
        self.set_account({"type": "offline", "name": self.cfg.get("offline_name", "GlebSter")})

    @staticmethod
    def _invalidate(payload):
        try:
            ely_call("invalidate", payload)
        except Exception:
            pass

    # ── обновления ──
    def update_source(self):
        return (self.cfg.get("update_source") or UPDATE_REPO or "").strip()

    def _update_tick(self):
        """Проверка при запуске и затем раз в несколько часов."""
        if self.cfg.get("auto_update", True):
            self.check_updates(False)
        self.after(6 * 3600 * 1000, self._update_tick)

    def check_updates(self, manual=False):
        source = self.update_source()
        if not source:
            if manual:
                self.say(update_message(up.UpdateError("no_source")), True)
            return
        if self.updating:
            return
        self.updating = True
        if manual:
            self.say(tr("Проверяю обновления…"))
        threading.Thread(target=self._update_worker, args=(source, manual), daemon=True).start()

    def _update_worker(self, source, manual):
        try:
            info = up.check(source, VERSION)
            if info is None:
                if manual:
                    self.say(tr("У вас последняя версия: {v}.", v=VERSION))
                self.updating = False
                return
            if not up.can_self_update():                         # запуск из исходников: только сообщаем
                self.say(tr("Доступна версия {v}. Скачайте её: {page}", v=info["version"], page=info["page"]))
                self.updating = False
                return
            self.say(tr("Доступна версия {v}. Скачиваю обновление…", v=info["version"]))
            path = up.download(info["url"], up.new_exe_path(), info.get("sha256"),
                               progress=lambda f: setattr(self, "prog", f))
        except up.UpdateError as e:
            self.updating = False
            if manual or e.code not in ("network", "no_release", "no_source", "rate"):
                self.say(update_message(e), True)
            return
        except Exception as e:
            self.updating = False
            self.say(tr("Ошибка обновления: {e}", e=e), True)
            return
        self.ui(self._update_ready, info, str(path))

    def _update_ready(self, info, path):
        if self.busy:                                            # идёт игра или установка: ставим после неё
            self.say(tr("Обновление {v} скачано. Установлю, когда закончится игра.", v=info["version"]))
            self.after(5000, lambda: self._update_ready(info, path))
            return
        try:
            exe = up.apply_update(path)
        except Exception as e:
            self.updating = False
            self.say(tr("Не удалось установить обновление: {e}", e=e), True)
            return
        self.say(tr("Обновление {v} установлено. Перезапускаюсь…", v=info["version"]))
        save_cfg(self.cfg)
        up.restart(exe)
        self.after(300, self.destroy)

    def _auto_update_toggled(self):
        self.cfg["auto_update"] = bool(self.auto_switch.get())
        save_cfg(self.cfg)

    def _save_update_source(self, _event=None):
        self.cfg["update_source"] = self.update_entry.get().strip()
        save_cfg(self.cfg)

    # ── запуск игры ──
    def do_play(self):
        if self.busy:
            return
        ver = self.build_version()
        if not ver:
            self.say(tr("Сначала выберите версию игры."), True)
            self.go("versions")
            return
        self._save_java()
        save_cfg(self.cfg)
        self.busy = True
        self.play_btn.configure(state="disabled", text="  " + tr("Подготовка…"))
        threading.Thread(target=self._play_worker, args=(ver, dict(self.cur_build())), daemon=True).start()

    def _install_callback(self):
        total = [0]

        def set_max(n):
            total[0] = n

        def set_progress(p):
            if total[0]:
                self.prog = min(p / total[0], 1.0)

        return {"setStatus": lambda s: self.say(s), "setProgress": set_progress, "setMax": set_max}

    def _start_skin_server(self, acc):
        current = self.cfg.get("skin")
        path = SKINS_DIR / current["file"] if current else None
        if not path or not path.exists():
            return None
        try:
            ensure_injector()
            return skins.SkinServer(acc["name"], acc["uuid"], path, current.get("model") == "slim")
        except Exception:
            self.say(tr("Не удалось включить локальный скин — запускаю без него."), True)
            return None

    def _ms_session(self, acc):
        """Обновляет вход Microsoft, если токен Minecraft скоро истечёт."""
        if acc.get("expires", 0) - time.time() > 300:
            return acc
        fresh = {"type": "microsoft", **ms.refresh(self.cfg.get("ms_client_id", ""), acc.get("refresh", ""))}
        self.cfg["account"] = fresh
        save_cfg(self.cfg)
        return fresh

    def _play_worker(self, ver, build):
        server = None
        try:
            gdir = bd.folder(build, GAME_DIR, INSTANCES_DIR)           # у каждой сборки своя папка игры
            gdir.mkdir(parents=True, exist_ok=True)
            acc = dict(self.cfg["account"])
            kind = acc.get("type", "ely")
            jvm = [f"-Xmx{build.get('ram') or self.cfg['ram']}G"]
            if kind == "ely":
                self.say(tr("Проверяю вход в Ely.by…"))
                acc = ely_refresh(acc, self.cfg["client_token"])
                self.cfg["account"] = {"type": "ely", **acc}
                save_cfg(self.cfg)
                ensure_injector()
                jvm.insert(0, f"-javaagent:{INJECTOR_JAR}=ely.by")
            elif kind == "microsoft":
                self.say(tr("Проверяю вход в Microsoft…"))
                acc = self._ms_session(acc)
            else:
                acc = {"name": acc["name"], "uuid": offline_uuid(acc["name"]), "token": "0"}
                server = self._start_skin_server(acc)
                if server:
                    jvm.insert(0, f"-javaagent:{INJECTOR_JAR}={server.url}")

            self.say(tr("Устанавливаю Minecraft {v}…", v=ver))
            mll.install.install_minecraft_version(ver, str(GAME_DIR), callback=self._install_callback())

            options = {"username": acc["name"], "uuid": acc["uuid"], "token": acc["token"],
                       "launcherName": "BibaLauncher", "launcherVersion": VERSION, "jvmArguments": jvm,
                       "gameDirectory": str(gdir)}
            if self.cfg.get("java"):
                options["executablePath"] = self.cfg["java"]
            cmd = mll.command.get_minecraft_command(ver, str(GAME_DIR), options)

            self.say(tr("Игра запущена. Приятной игры!"))
            self.prog = 1.0
            ROOT.mkdir(parents=True, exist_ok=True)
            with open(LOG, "wb") as log:
                try:
                    proc = subprocess.Popen(cmd, cwd=gdir, stdout=log, stderr=subprocess.STDOUT,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except FileNotFoundError:
                    raise RuntimeError(tr("Java не найдена. Укажите путь к ней в настройках.")) from None
                self.ui(self._game_started, server is not None)
                code = proc.wait()
                self.ui(self._game_stopped)
            if code == 0:
                self.say(tr("Игра закрыта."))
            else:
                self.say(tr("Игра завершилась с ошибкой (код {code}). Подробности: {log}", code=code, log=LOG), True)
        except ElyError as e:
            if e.network:
                self.say(str(e), True)
            else:
                self.ui(self._session_expired)
        except ms.MsError as e:
            if e.code == "session":
                self.ui(self._session_expired, "microsoft")
            else:
                self.say(ms_message(e), True)
        except Exception as e:
            self.say(tr("Ошибка: {e}", e=e), True)
        finally:
            if server:
                server.stop()
            self.ui(self._idle)



    def _session_expired(self, kind="ely"):
        self.say(tr("Сессия Microsoft истекла. Войдите заново.") if kind == "microsoft"
                 else tr("Сессия Ely.by истекла. Войдите заново."), True)
        self.set_account({"type": "offline", "name": self.cfg.get("offline_name", "GlebSter")})
        self.open_account(0 if kind == "microsoft" else None)

    def _game_started(self, need_server):
        mode = self.cfg.get("after_launch", "hide")
        if mode == "close" and not need_server:
            self.after(300, self.destroy)               # игра продолжит работать сама
        elif mode in ("hide", "close"):                 # при локальном скине лаунчер должен остаться (там сервер скина)
            self.iconify()

    def _game_stopped(self):
        try:
            self.deiconify()
            self.lift()
        except tk.TclError:
            pass

    def _idle(self):
        self.busy = False
        self.play_btn.configure(state="normal", text="  " + tr("Играть"))
        self.prog = 0.0


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
