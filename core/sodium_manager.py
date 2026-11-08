"""Consulta, descarga e instalacion de Fabric Loader + Sodium aislado por version."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import Any

import requests

from config.settings import APP_NAME, APP_VERSION, ensure_data_directory
from core.java_manager import JavaManager
from core.minecraft_manager import MinecraftManager


ProgressCallback = Callable[[str, float], None]

FABRIC_META_LOADERS_URL = "https://meta.fabricmc.net/v2/versions/loader/{version}"
FABRIC_META_PROFILE_URL = "https://meta.fabricmc.net/v2/versions/loader/{version}/{loader}/profile/json"
MODRINTH_SODIUM_VERSIONS_URL = "https://api.modrinth.com/v2/project/sodium/version"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (Spyflow)"


class SodiumManager:
    """Gestiona perfiles Fabric + mod Sodium en carpetas aisladas por version (mods/<version>/)."""

    def __init__(
        self,
        game_directory: Path,
        minecraft_manager: MinecraftManager,
        java_manager: JavaManager,
        logger: logging.Logger,
    ) -> None:
        self.game_directory = game_directory
        self.minecraft_manager = minecraft_manager
        self.java_manager = java_manager
        self.logger = logger

    def get_version_mods_directory(self, minecraft_version: str) -> Path:
        """Devuelve la carpeta de mods exclusiva para una version de Minecraft (FIXNER_DIR/mods/<version>)."""
        mods_dir = self.game_directory / "mods" / minecraft_version
        mods_dir.mkdir(parents=True, exist_ok=True)
        return mods_dir

    def list_versions(self, minecraft_version: str) -> list[dict[str, Any]]:
        """Consulta las versiones compatibles de Sodium (Fabric) en Modrinth para la version de Minecraft dada."""
        # 1. Verificar que Fabric soporte esta version de Minecraft
        loader_resp = requests.get(
            FABRIC_META_LOADERS_URL.format(version=minecraft_version),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        if loader_resp.status_code == 404:
            return []
        loader_resp.raise_for_status()
        loaders = loader_resp.json()
        if not isinstance(loaders, list) or not loaders:
            return []

        # 2. Consultar versiones de Sodium para Fabric en Modrinth
        response = requests.get(
            MODRINTH_SODIUM_VERSIONS_URL,
            params={
                "loaders": json.dumps(["fabric"]),
                "game_versions": json.dumps([minecraft_version]),
            },
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("La respuesta de Modrinth para Sodium no tiene el formato esperado")

        results: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            files = [f for f in item.get("files", []) if isinstance(f, dict) and str(f.get("filename", "")).endswith(".jar")]
            if not files:
                continue
            primary_file = next((f for f in files if f.get("primary")), files[0])
            filename = str(primary_file.get("filename", ""))
            download_url = str(primary_file.get("url", ""))
            if not filename or not download_url:
                continue
            results.append(
                {
                    "id": str(item.get("id", "")),
                    "name": str(item.get("name", filename)),
                    "version_number": str(item.get("version_number", "")),
                    "version_type": str(item.get("version_type", "release")),
                    "filename": filename,
                    "url": download_url,
                }
            )
        return results

    def install_version(
        self,
        minecraft_version: str,
        sodium_version: dict[str, Any],
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        """Instala Vanilla base, Fabric Loader y el mod Sodium en FIXNER_DIR/mods/<version>/."""
        ensure_data_directory()
        result_holder: list[Path] = []
        error_holder: list[Exception] = []

        def install_in_background() -> None:
            try:
                result_holder.append(
                    self._install_version(
                        minecraft_version,
                        sodium_version,
                        progress_callback,
                    )
                )
            except Exception as error:
                error_holder.append(error)

        worker = Thread(target=install_in_background, daemon=True)
        worker.start()
        worker.join()
        if error_holder:
            raise error_holder[0]
        return result_holder[0]

    def _install_version(
        self,
        minecraft_version: str,
        sodium_version: dict[str, Any],
        progress_callback: ProgressCallback | None,
    ) -> Path:
        # 1. Asegurar que la version Vanilla base este instalada
        vanilla_path = self.game_directory / "versions" / minecraft_version
        if not vanilla_path.exists():
            self.minecraft_manager.install_version(minecraft_version, progress_callback)

        # 2. Asegurar el runtime de Java para esta version
        if progress_callback:
            progress_callback("Verificando runtime de Java...", 15.0)
        self.java_manager.ensure_runtime(minecraft_version)

        # 3. Instalar el perfil de Fabric Loader y sus librerias
        if progress_callback:
            progress_callback("Instalando Fabric Loader...", 35.0)
        profile_id = self._install_fabric_loader(minecraft_version)

        # 4. Descargar el mod Sodium en la carpeta aislada FIXNER_DIR/mods/<minecraft_version>/
        mods_dir = self.get_version_mods_directory(minecraft_version)
        for existing_sodium in mods_dir.glob("sodium*.jar"):
            try:
                existing_sodium.unlink()
            except OSError:
                pass

        filename = str(sodium_version.get("filename", f"sodium-fabric-{minecraft_version}.jar"))
        download_url = str(sodium_version.get("url", ""))
        if not download_url:
            versions = self.list_versions(minecraft_version)
            if not versions:
                raise RuntimeError(f"No hay versiones de Sodium disponibles para Minecraft {minecraft_version}")
            filename = versions[0]["filename"]
            download_url = versions[0]["url"]

        destination = mods_dir / filename
        self._download(download_url, destination, progress_callback)
        if progress_callback:
            progress_callback("Sodium instalado correctamente", 100.0)
        self.logger.info(
            "Sodium (%s) instalado con perfil %s en %s",
            filename,
            profile_id,
            mods_dir,
        )
        return self.game_directory / "versions" / profile_id

    def _install_fabric_loader(self, minecraft_version: str) -> str:
        """Obtiene e instala el perfil oficial de Fabric Loader y descarga sus librerias Maven."""
        loader_resp = requests.get(
            FABRIC_META_LOADERS_URL.format(version=minecraft_version),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        loader_resp.raise_for_status()
        loaders = loader_resp.json()
        if not isinstance(loaders, list) or not loaders:
            raise RuntimeError(f"Fabric Loader no soporta Minecraft {minecraft_version}")

        loader_version = next(
            (
                str(item["loader"]["version"])
                for item in loaders
                if isinstance(item, dict) and isinstance(item.get("loader"), dict) and item["loader"].get("stable")
            ),
            str(loaders[0]["loader"]["version"]),
        )

        profile_resp = requests.get(
            FABRIC_META_PROFILE_URL.format(version=minecraft_version, loader=loader_version),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        profile_resp.raise_for_status()
        profile_data = profile_resp.json()
        if not isinstance(profile_data, dict) or not profile_data.get("id"):
            raise RuntimeError("El perfil devuelto por Fabric Meta API es invalido")

        profile_id = str(profile_data["id"])
        profile_dir = self.game_directory / "versions" / profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_json_path = profile_dir / f"{profile_id}.json"
        profile_json_path.write_text(json.dumps(profile_data, indent=2), encoding="utf-8")

        # Sincronizar jar base, natives y librerias de Fabric
        self.minecraft_manager._prepare_inherited_version(profile_id)
        return profile_id

    def _download(
        self,
        url: str,
        destination: Path,
        progress_callback: ProgressCallback | None,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with requests.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    stream=True,
                    timeout=60,
                ) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("content-length", 0))
                    downloaded = 0
                    with destination.open("wb") as output:
                        for chunk in response.iter_content(chunk_size=1024 * 256):
                            if not chunk:
                                continue
                            output.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and total:
                                progress_callback("Descargando Sodium...", 50.0 + (downloaded * 50.0 / total))
                return
            except requests.RequestException as error:
                last_error = error
                self.logger.warning("Descarga de Sodium fallida (intento %d/3): %s", attempt + 1, error)
        raise RuntimeError(f"No se pudo descargar Sodium tras 3 intentos: {last_error}")
