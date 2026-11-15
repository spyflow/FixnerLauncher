"""Consulta, descarga e instalacion de Fabric Loader + Dosium aislado por version."""

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
DOSIUM_RELEASES_LATEST_URL = "https://api.github.com/repos/spyflow/Dosium/releases/latest"
MODRINTH_FABRIC_API_URL = "https://api.modrinth.com/v2/project/fabric-api/version"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (Spyflow)"
SUPPORTED_DOSIUM_MC_VERSIONS = ["1.20.4"]


class DosiumManager:
    """Gestiona perfiles Fabric + mod Dosium en carpetas aisladas por version (mods/<version>/)."""

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
        """Devuelve la carpeta de mods exclusiva para Dosium (FIXNER_DIR/mods/<version>.dosium)."""
        mods_dir = self.game_directory / "mods" / f"{minecraft_version}.dosium"
        mods_dir.mkdir(parents=True, exist_ok=True)
        return mods_dir

    def list_versions(self, minecraft_version: str) -> list[dict[str, Any]]:
        """Consulta la release oficial de Dosium en GitHub si la version de Minecraft es soportada."""
        if minecraft_version not in SUPPORTED_DOSIUM_MC_VERSIONS:
            return []

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

        # 2. Consultar assets del release latest en GitHub
        response = requests.get(
            DOSIUM_RELEASES_LATEST_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("La respuesta de GitHub para Dosium no tiene el formato esperado")

        assets = payload.get("assets", [])
        tag_name = str(payload.get("tag_name", "v1.0.0"))
        results: list[dict[str, Any]] = []

        for asset in assets:
            if not isinstance(asset, dict):
                continue
            filename = str(asset.get("name", ""))
            # Buscamos el jar principal del mod (ej. dosium-fabric-mc1.20.4-1.0.0+build.3.jar), evitando api o dev
            if not filename.endswith(".jar"):
                continue
            if "-api" in filename or "-dev" in filename or "-sources" in filename:
                continue

            download_url = str(asset.get("browser_download_url", ""))
            if not download_url:
                continue

            results.append(
                {
                    "id": str(asset.get("id", "")),
                    "name": f"Dosium {tag_name} ({filename})",
                    "version_number": tag_name,
                    "filename": filename,
                    "url": download_url,
                }
            )

        # Si por alguna razon todos tenian sufijos o ninguno coincidio, fallback al primer jar que no sea api
        if not results:
            for asset in assets:
                filename = str(asset.get("name", ""))
                if filename.endswith(".jar") and "-api" not in filename:
                    results.append(
                        {
                            "id": str(asset.get("id", "")),
                            "name": f"Dosium {tag_name} ({filename})",
                            "version_number": tag_name,
                            "filename": filename,
                            "url": str(asset.get("browser_download_url", "")),
                        }
                    )
                    break

        return results

    def install_version(
        self,
        minecraft_version: str,
        dosium_version: dict[str, Any],
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        """Instala Vanilla base, Fabric Loader y el mod Dosium en FIXNER_DIR/mods/<version>/."""
        ensure_data_directory()
        result_holder: list[Path] = []
        error_holder: list[Exception] = []

        def install_in_background() -> None:
            try:
                result_holder.append(
                    self._install_version(
                        minecraft_version,
                        dosium_version,
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
        dosium_version: dict[str, Any],
        progress_callback: ProgressCallback | None,
    ) -> Path:
        """Instala con verificacion final; si algo falla revierte lo creado en esta operacion."""
        versions_dir = self.game_directory / "versions"
        vanilla_was_complete = self.minecraft_manager.is_installation_complete(minecraft_version)
        existing_profiles = {path.name for path in versions_dir.glob(f"fabric-loader-*-{minecraft_version}")} if versions_dir.exists() else set()
        mods_dir = self.get_version_mods_directory(minecraft_version)
        existing_mods = {path.name for path in mods_dir.glob("dosium*.jar")}
        try:
            profile_path = self._install_version_steps(minecraft_version, dosium_version, progress_callback)
            profile_id = profile_path.name
            problems = self.minecraft_manager.verify_installation(profile_id)
            dosium_jars = [path for path in mods_dir.glob("dosium*.jar") if path.is_file() and path.stat().st_size > 0]
            if not dosium_jars:
                problems.append("falta el mod de Dosium")
            if problems:
                raise RuntimeError(
                    "La instalacion de Dosium quedo incompleta: " + ", ".join(problems[:5])
                )
            return profile_path
        except Exception:
            self.logger.error("Instalacion de Dosium %s fallida; revirtiendo cambios parciales", minecraft_version)
            if versions_dir.exists():
                for path in versions_dir.glob(f"fabric-loader-*-{minecraft_version}"):
                    if path.name not in existing_profiles:
                        self.minecraft_manager.delete_installation(path.name)
            for path in mods_dir.glob("dosium*.jar"):
                if path.name not in existing_mods or path.stat().st_size == 0:
                    try:
                        path.unlink()
                    except OSError:
                        pass
            try:
                if not any(mods_dir.iterdir()):
                    mods_dir.rmdir()
            except OSError:
                pass
            if not vanilla_was_complete:
                self.minecraft_manager.delete_installation(minecraft_version)
            raise

    def _install_version_steps(
        self,
        minecraft_version: str,
        dosium_version: dict[str, Any],
        progress_callback: ProgressCallback | None,
    ) -> Path:
        # 1. Asegurar que la version Vanilla base este completamente instalada y verificada
        base_start = 0.0
        if not self.minecraft_manager.is_installation_complete(minecraft_version):
            self.minecraft_manager.install_version(
                minecraft_version,
                (lambda msg, pct: progress_callback(msg, pct * 0.72)) if progress_callback else None,
            )
            base_start = 72.0

        # 2. Asegurar el runtime de Java para esta version
        java_pct = max(base_start + 3.0, 15.0)
        if progress_callback:
            progress_callback(f"Verificando runtime de Java · {int(java_pct)}%", java_pct)
        self.java_manager.ensure_runtime(minecraft_version)

        # 3. Instalar el perfil de Fabric Loader y sus librerias
        fabric_start = max(base_start + 6.0, 30.0)
        fabric_end = 90.0
        if progress_callback:
            progress_callback(f"Instalando Fabric Loader · {int(fabric_start)}%", fabric_start)
        profile_id = self._install_fabric_loader(
            minecraft_version,
            (
                lambda msg, pct: progress_callback(
                    msg,
                    fabric_start + (pct / 100.0) * (fabric_end - fabric_start),
                )
            )
            if progress_callback
            else None,
        )

        # 4. Descargar el mod Dosium en la carpeta aislada FIXNER_DIR/mods/<minecraft_version>.dosium/
        mods_dir = self.get_version_mods_directory(minecraft_version)
        # Limpiar cualquier jar previo de dosium o sodium en esa carpeta para evitar conflictos
        for existing in list(mods_dir.glob("dosium*.jar")) + list(mods_dir.glob("sodium*.jar")):
            try:
                existing.unlink()
            except OSError:
                pass

        filename = str(dosium_version.get("filename", f"dosium-fabric-{minecraft_version}.jar"))
        download_url = str(dosium_version.get("url", ""))
        if not download_url:
            versions = self.list_versions(minecraft_version)
            if not versions:
                raise RuntimeError(f"No hay versiones de Dosium disponibles para Minecraft {minecraft_version}")
            filename = versions[0]["filename"]
            download_url = versions[0]["url"]

        destination = mods_dir / filename
        self._download(
            download_url,
            destination,
            (lambda msg, pct: progress_callback(msg, 90.0 + (pct / 100.0) * 5.0))
            if progress_callback
            else None,
        )

        # 5. Asegurar Fabric API en la carpeta de mods (requerido por Dosium)
        self._ensure_fabric_api(
            minecraft_version,
            mods_dir,
            (lambda msg, pct: progress_callback(msg, 95.0 + (pct / 100.0) * 5.0))
            if progress_callback
            else None,
        )

        if progress_callback:
            progress_callback("Dosium instalado correctamente · 100%", 100.0)
        self.logger.info(
            "Dosium (%s) instalado con perfil %s en %s",
            filename,
            profile_id,
            mods_dir,
        )
        return self.game_directory / "versions" / profile_id

    def _ensure_fabric_api(
        self,
        minecraft_version: str,
        mods_dir: Path,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """Descarga e instala la version compatible de Fabric API si no esta presente en la carpeta de mods."""
        if any(mods_dir.glob("fabric-api*.jar")):
            return
        if progress_callback:
            progress_callback("Consultando Fabric API para Dosium...", 10.0)
        try:
            resp = requests.get(
                MODRINTH_FABRIC_API_URL,
                params={
                    "loaders": json.dumps(["fabric"]),
                    "game_versions": json.dumps([minecraft_version]),
                },
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=25,
            )
            resp.raise_for_status()
            versions = resp.json()
            if not isinstance(versions, list) or not versions:
                self.logger.warning("No se encontraron versiones de Fabric API para %s en Modrinth", minecraft_version)
                return
            files = versions[0].get("files", [])
            primary_file = next((f for f in files if f.get("primary")), files[0] if files else None)
            if not primary_file:
                return
            api_filename = str(primary_file.get("filename", f"fabric-api-{minecraft_version}.jar"))
            api_url = str(primary_file.get("url", ""))
            if not api_url:
                return
            if progress_callback:
                progress_callback(f"Descargando {api_filename}...", 50.0)
            self._download(api_url, mods_dir / api_filename, progress_callback)
            self.logger.info("Fabric API instalado en %s: %s", mods_dir, api_filename)
        except Exception as exc:
            self.logger.warning("No se pudo descargar automaticamente Fabric API: %s", exc)

    def _install_fabric_loader(
        self,
        minecraft_version: str,
        progress_callback: ProgressCallback | None = None,
    ) -> str:
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
        missing = self.minecraft_manager._prepare_inherited_version(profile_id, progress_callback)
        if missing:
            raise RuntimeError(
                "No se pudieron descargar librerias de Fabric: " + ", ".join(missing[:5])
            )
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
                                pct = (downloaded * 100.0) / total
                                progress_callback(f"Descargando Dosium ({destination.name})...", pct)
                if total and downloaded != total:
                    raise requests.ConnectionError(
                        f"Descarga incompleta ({downloaded}/{total} bytes)"
                    )
                if downloaded == 0:
                    raise requests.ConnectionError("Descarga vacia")
                return
            except requests.RequestException as error:
                last_error = error
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
                self.logger.warning("Descarga de Dosium fallida (intento %d/3): %s", attempt + 1, error)
        raise RuntimeError(f"No se pudo descargar Dosium tras 3 intentos: {last_error}")
