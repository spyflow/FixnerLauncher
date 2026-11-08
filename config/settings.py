"""Configuracion central de FixnerLauncher."""

from __future__ import annotations

import os
import sys
import json
import shutil
from pathlib import Path
from typing import Any


def get_resource_path(relative_path: str) -> Path:
    """Resuelve rutas de assets tanto en desarrollo como en el ejecutable compilado (PyInstaller)."""
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base_path / relative_path


def get_app_version() -> str:
    """Lee la version unica desde el archivo VERSION en la raiz del proyecto."""
    version_file = get_resource_path("VERSION")
    try:
        if version_file.exists():
            version = version_file.read_text(encoding="utf-8").strip()
            if version:
                return version
    except OSError:
        pass
    return "0.0.0"


APP_NAME = "FixnerLauncher"
APP_VERSION = get_app_version()
DEFAULT_USERNAME = "Player"
DEFAULT_MEMORY_MB = 4096
MICROSOFT_CLIENT_ID = "00000000402b5328"
MICROSOFT_REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"


def get_data_directory() -> Path:
    """Devuelve el directorio aislado donde se guardan datos del launcher."""
    configured_directory = os.environ.get("FIXNER_HOME")
    if configured_directory:
        return Path(configured_directory).expanduser()

    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        if app_data:
            return Path(app_data) / ".fixnerlauncher"

    return Path.home() / ".fixnerlauncher"


DATA_DIRECTORY = get_data_directory()
FIXNER_DIR = DATA_DIRECTORY
GAME_DIRECTORY = FIXNER_DIR
LOG_DIRECTORY = DATA_DIRECTORY / "logs"
LOG_FILE = LOG_DIRECTORY / "fixner_launcher.log"
PROFILES_FILE = DATA_DIRECTORY / "profiles.json"
PREMIUM_PROFILE_FILE = DATA_DIRECTORY / "premium_profile.json"
LAUNCHER_PROFILES_FILE = DATA_DIRECTORY / "launcher_profiles.json"
LAUNCHER_SETTINGS_FILE = DATA_DIRECTORY / "settings.json"


def ensure_data_directory() -> None:
    """Crea el directorio aislado y el perfil minimo esperado por Minecraft."""
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    migrate_legacy_game_directory()
    if not LAUNCHER_PROFILES_FILE.exists():
        save_json(LAUNCHER_PROFILES_FILE, {"profiles": {}, "settings": {}, "version": 3})


def migrate_legacy_game_directory() -> None:
    """Mueve datos de la ruta MVP anterior al FIXNER_DIR obligatorio."""
    legacy_directory = DATA_DIRECTORY / "minecraft"
    if not legacy_directory.exists() or legacy_directory == GAME_DIRECTORY:
        return
    for source in legacy_directory.iterdir():
        destination = DATA_DIRECTORY / source.name
        if destination.exists():
            continue
        try:
            shutil.move(str(source), str(destination))
        except OSError:
            continue
    try:
        legacy_directory.rmdir()
    except OSError:
        pass


def load_json(path: Path, default: Any) -> Any:
    """Carga JSON y devuelve un valor predeterminado si aun no existe."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, value: Any) -> None:
    """Guarda JSON creando antes el directorio padre."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True), encoding="utf-8")
