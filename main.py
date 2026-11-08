"""Punto de entrada CLI de FixnerLauncher."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from config.settings import (
    APP_VERSION,
    DEFAULT_MEMORY_MB,
    DEFAULT_USERNAME,
    GAME_DIRECTORY,
    LOG_FILE,
    ensure_data_directory,
    get_resource_path,
)
from core.minecraft_manager import MinecraftManager
from core.java_manager import JavaManager
from core.optifine_manager import OptiFineManager
from core.sodium_manager import SodiumManager
from core.runner import MinecraftRunner
from utils.logger import configure_logging

_CHROMIUM_DISABLE_WEBAUTHN_FLAGS = (
    "--disable-features=WebAuthentication,WebAuthenticationConditionalUI,WebAuth,WebUsb"
)


def _configure_qtwebengine_flags() -> None:
    """Deshabilita a nivel Chromium cualquier puente hacia FIDO2/Windows Hello/WebUSB."""
    existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    if _CHROMIUM_DISABLE_WEBAUTHN_FLAGS not in existing_flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            f"{existing_flags} {_CHROMIUM_DISABLE_WEBAUTHN_FLAGS}".strip()
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"FixnerLauncher v{APP_VERSION} - launcher de Minecraft")
    parser.add_argument("-v", "--version", action="version", version=f"FixnerLauncher {APP_VERSION}")
    parser.set_defaults(action="gui")
    subparsers = parser.add_subparsers(dest="action", required=False)

    subparsers.add_parser("list", help="Lista las versiones release disponibles")
    subparsers.add_parser("gui", help="Abre la interfaz grafica")

    install_parser = subparsers.add_parser("install", help="Instala una version Vanilla")
    install_parser.add_argument("version", help="Identificador, por ejemplo 1.20.4")

    optifine_list_parser = subparsers.add_parser(
        "optifine-list",
        help="Lista versiones OptiFine para una version Vanilla",
    )
    optifine_list_parser.add_argument("version", help="Version Vanilla base")

    optifine_install_parser = subparsers.add_parser(
        "optifine-install",
        help="Instala una version OptiFine",
    )
    optifine_install_parser.add_argument("version", help="Version Vanilla base")
    optifine_install_parser.add_argument("--index", type=int, default=0, help="Indice del catalogo")

    sodium_list_parser = subparsers.add_parser(
        "sodium-list",
        help="Lista versiones Sodium (Fabric) para una version Vanilla",
    )
    sodium_list_parser.add_argument("version", help="Version Vanilla base")

    sodium_install_parser = subparsers.add_parser(
        "sodium-install",
        help="Instala Fabric + Sodium en mods/<version>/",
    )
    sodium_install_parser.add_argument("version", help="Version Vanilla base")
    sodium_install_parser.add_argument("--index", type=int, default=0, help="Indice del catalogo")

    launch_parser = subparsers.add_parser("launch", help="Lanza una version instalada")
    launch_parser.add_argument("version", help="Identificador de la version instalada")
    launch_parser.add_argument("--username", default=DEFAULT_USERNAME)
    launch_parser.add_argument("--memory", type=int, default=DEFAULT_MEMORY_MB, metavar="MB")
    launch_parser.add_argument(
        "--variant",
        choices=["Vanilla", "OptiFine", "Sodium"],
        default="Vanilla",
        help="Variante a lanzar (Vanilla, OptiFine, Sodium)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    ensure_data_directory()
    logger = configure_logging(LOG_FILE)
    manager = MinecraftManager(GAME_DIRECTORY, logger)
    java_manager = JavaManager(GAME_DIRECTORY, logger)
    runner = MinecraftRunner(GAME_DIRECTORY, logger)
    optifine = OptiFineManager(
        GAME_DIRECTORY,
        manager,
        java_manager,
        logger,
    )
    sodium = SodiumManager(
        GAME_DIRECTORY,
        manager,
        java_manager,
        logger,
    )

    try:
        action = args.action or "gui"
        if action == "gui":
            _configure_qtwebengine_flags()
            from PySide6.QtCore import QCoreApplication, Qt
            from PySide6.QtGui import QFont, QFontDatabase
            from PySide6.QtWidgets import QApplication
            from src.controller import AppController
            QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
            app = QApplication(sys.argv)
            app.setApplicationVersion(APP_VERSION)
            app.setWindowIcon(AppController._build_tray_icon())
            app.setQuitOnLastWindowClosed(False)
            font_path = get_resource_path("assets/Minecraft.ttf")
            if font_path.exists():
                QFontDatabase.addApplicationFont(str(font_path))
            app.setFont(QFont("Segoe UI", 10))
            styles_path = get_resource_path("src/ui/styles.qss")
            if styles_path.exists():
                app.setStyleSheet(styles_path.read_text(encoding="utf-8"))
            controller = AppController(app, GAME_DIRECTORY, logger)
            app.controller = controller
            return app.exec()
        elif action == "list":
            for version in manager.list_releases():
                print(version["id"])
        elif action == "install":
            manager.install_version(args.version)
        elif action == "optifine-list":
            for index, version in enumerate(optifine.list_versions(args.version)):
                print(f"{index}: {version.get('filename', version.get('patch', 'unknown'))}")
        elif action == "optifine-install":
            versions = optifine.list_versions(args.version)
            if not 0 <= args.index < len(versions):
                raise ValueError(f"Indice fuera de rango; hay {len(versions)} versiones disponibles")
            optifine.install_version(args.version, versions[args.index])
        elif action == "sodium-list":
            for index, version in enumerate(sodium.list_versions(args.version)):
                print(f"{index}: {version.get('filename', version.get('version_number', 'unknown'))}")
        elif action == "sodium-install":
            versions = sodium.list_versions(args.version)
            if not 0 <= args.index < len(versions):
                raise ValueError(f"Indice fuera de rango; hay {len(versions)} versiones disponibles")
            sodium.install_version(args.version, versions[args.index])
        elif action == "launch":
            runner.launch(args.version, args.username, args.memory, variant=args.variant)
    except Exception as error:
        logger.exception("La operacion fallo: %s", error)
        if sys.stderr is not None:
            print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
