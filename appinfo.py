"""Название, версия и источник обновлений.
При сборке на GitHub файл build_info.py (создаётся автоматически) задаёт VERSION и UPDATE_REPO."""
NAME = "Biba Launcher"
EXE_NAME = "BibaLauncher.exe"
VERSION = "1.0.0"
UPDATE_REPO = ""            # «владелец/репозиторий» на GitHub: оттуда лаунчер берёт обновления

try:
    from build_info import UPDATE_REPO, VERSION      # noqa: F401
except ImportError:
    pass
