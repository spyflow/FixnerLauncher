"""Consulta, descarga e instalacion desatendida de OptiFine."""

from __future__ import annotations

import logging
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import Any

import requests

from core.java_manager import JavaManager
from core.minecraft_manager import MinecraftManager
from config.settings import LAUNCHER_PROFILES_FILE, ensure_data_directory


ProgressCallback = Callable[[str, float], None]
OPTiFINE_ENDPOINT = "https://bmclapi2.bangbang93.com/optifine/{version}"


class OptiFineManager:
    """Mantiene OptiFine como una capa sobre una instalacion Vanilla existente."""

    BRIDGE_SOURCE = """import java.io.File;
import java.lang.reflect.Method;

public class OptiFineBridge {
    public static void main(String[] args) throws Exception {
        Class<?> installer = Class.forName("optifine.Installer");
        try {
            File mcDir = new File(args[0]);
            Method doInstall = installer.getMethod("doInstall", File.class);
            doInstall.invoke(null, mcDir);
        } catch (NoSuchMethodException legacyInstaller) {
            Method doInstall = installer.getMethod("doInstall");
            doInstall.invoke(null);
        }
    }
}
"""

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

    def list_versions(self, minecraft_version: str) -> list[dict[str, Any]]:
        response = requests.get(
            OPTiFINE_ENDPOINT.format(version=minecraft_version),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("La respuesta de OptiFine no tiene el formato esperado")
        return [item for item in payload if isinstance(item, dict)]

    def install_version(
        self,
        minecraft_version: str,
        optifine_version: dict[str, Any],
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        """Instala el perfil OptiFine usando el instalador en modo desatendido."""
        ensure_data_directory()
        result_holder: list[Path] = []
        error_holder: list[Exception] = []

        def install_in_background() -> None:
            try:
                result_holder.append(
                    self._install_version(
                        minecraft_version,
                        optifine_version,
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
        optifine_version: dict[str, Any],
        progress_callback: ProgressCallback | None,
    ) -> Path:
        """Instala OptiFine con verificacion final; si falla revierte lo creado en esta operacion."""
        versions_dir = self.game_directory / "versions"
        vanilla_was_complete = self.minecraft_manager.is_installation_complete(minecraft_version)
        existing_profiles = (
            {path.name for path in versions_dir.glob(f"{minecraft_version}-OptiFine*")}
            if versions_dir.exists()
            else set()
        )
        try:
            result = self._install_version_steps(minecraft_version, optifine_version, progress_callback)
            new_profiles = [
                path.name
                for path in versions_dir.glob(f"{minecraft_version}-OptiFine*")
                if path.is_dir()
            ]
            if not new_profiles:
                raise RuntimeError("OptiFine no genero ningun perfil de version.")
            problems: list[str] = []
            for profile_id in new_profiles:
                profile_problems = self.minecraft_manager.verify_installation(profile_id)
                if profile_problems and profile_id not in existing_profiles:
                    problems.extend(profile_problems)
            if problems:
                raise RuntimeError("La instalacion de OptiFine quedo incompleta: " + ", ".join(problems[:5]))
            return result
        except Exception:
            self.logger.error("Instalacion de OptiFine %s fallida; revirtiendo cambios parciales", minecraft_version)
            if versions_dir.exists():
                for path in versions_dir.glob(f"{minecraft_version}-OptiFine*"):
                    if path.name not in existing_profiles:
                        self.minecraft_manager.delete_installation(path.name)
            if not vanilla_was_complete:
                self.minecraft_manager.delete_installation(minecraft_version)
            raise

    def _install_version_steps(
        self,
        minecraft_version: str,
        optifine_version: dict[str, Any],
        progress_callback: ProgressCallback | None,
    ) -> Path:
        base_start = 0.0
        if not self.minecraft_manager.is_installation_complete(minecraft_version):
            self.minecraft_manager.install_version(
                minecraft_version,
                (lambda msg, pct: progress_callback(msg, pct * 0.75)) if progress_callback else None,
            )
            base_start = 75.0

        download_url = self._download_url(minecraft_version, optifine_version)
        filename = str(optifine_version.get("filename", "OptiFine-installer.jar"))
        with tempfile.TemporaryDirectory(prefix="fixner-optifine-") as temporary_directory:
            installer = Path(temporary_directory) / filename
            self._download(
                download_url,
                installer,
                (
                    lambda msg, pct: progress_callback(
                        msg,
                        base_start + (pct / 100.0) * (90.0 - base_start),
                    )
                )
                if progress_callback
                else None,
            )
            if progress_callback:
                progress_callback("Ejecutando instalador OptiFine · 92%", 92.0)
            java_path = self.java_manager.ensure_runtime(minecraft_version)
            self._run_headless_bridge(java_path, installer, Path(temporary_directory), minecraft_version)
            self._ensure_profile_for_legacy(
                minecraft_version,
                optifine_version,
                installer,
            )
            self._sync_inherited_version_assets(minecraft_version)
            if progress_callback:
                progress_callback("OptiFine instalado correctamente · 100%", 100.0)
        self.logger.info("OptiFine instalado para Minecraft %s", minecraft_version)
        return self.game_directory / "versions"

    def _run_headless_bridge(
        self,
        java_path: Path,
        installer: Path,
        temp_directory: Path,
        minecraft_version: str,
    ) -> None:
        bridge_java = temp_directory / "OptiFineBridge.java"
        bridge_java.write_text(self.BRIDGE_SOURCE, encoding="utf-8")
        if not LAUNCHER_PROFILES_FILE.exists():
            raise FileNotFoundError(
                f"No existe el perfil aislado requerido: {LAUNCHER_PROFILES_FILE}"
            )
        javac_path = self._find_javac(java_path)
        bridge_java_path = java_path.with_name("java.exe") if java_path.name.casefold() == "javaw.exe" else java_path
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            [
                str(javac_path),
                "-source",
                "1.8",
                "-target",
                "1.8",
                "-cp",
                str(installer),
                str(bridge_java),
            ],
            check=True,
            capture_output=True,
            creationflags=creation_flags,
        )
        separator = ";" if os.name == "nt" else ":"
        classpath = f"{installer}{separator}{temp_directory}"
        sandbox_appdata = temp_directory / "appdata"
        sandbox_appdata.mkdir(parents=True, exist_ok=True)
        sandbox_minecraft = sandbox_appdata / ".minecraft"
        sandbox_minecraft.mkdir(parents=True, exist_ok=True)
        (sandbox_minecraft / "launcher_profiles.json").write_text(
            '{"profiles": {}, "settings": {}, "version": 3}',
            encoding="utf-8",
        )
        vanilla_src = self.game_directory / "versions" / minecraft_version
        if vanilla_src.exists():
            sandbox_vanilla = sandbox_minecraft / "versions" / minecraft_version
            shutil.copytree(vanilla_src, sandbox_vanilla, dirs_exist_ok=True)
        environment = os.environ.copy()
        if os.name == "nt":
            environment["APPDATA"] = str(sandbox_appdata)
        result = subprocess.run(
            [
                str(bridge_java_path),
                "-Djava.awt.headless=true",
                "-cp",
                classpath,
                "OptiFineBridge",
                str(self.game_directory),
            ],
            capture_output=True,
            text=True,
            creationflags=creation_flags,
            env=environment,
        )
        if result.returncode != 0:
            self.logger.warning(
                "OptiFineBridge finalizo con codigo %s: %s %s",
                result.returncode,
                result.stdout.strip(),
                result.stderr.strip(),
            )
        self._import_legacy_installation(sandbox_appdata)

    def _sync_inherited_version_assets(self, minecraft_version: str) -> None:
        """Asegura que el .jar base, inheritsFrom, librerias y natives esten disponibles en el perfil OptiFine."""
        versions_dir = self.game_directory / "versions"
        for optifine_dir in versions_dir.glob(f"{minecraft_version}-OptiFine*"):
            if not optifine_dir.is_dir():
                continue
            self.minecraft_manager._prepare_inherited_version(optifine_dir.name)

    @staticmethod
    def _find_javac(java_path: Path) -> Path:
        """Busca un compilador JDK sin exigir que el runtime de Minecraft lo incluya."""
        executable_name = "javac.exe" if os.name == "nt" else "javac"
        runtime_javac = java_path.with_name(executable_name)
        if runtime_javac.exists():
            return runtime_javac
        path_javac = shutil.which(executable_name)
        if path_javac:
            return Path(path_javac)
        if os.name == "nt":
            candidates = (
                Path(os.environ.get("JAVA_HOME", "")) / "bin" / executable_name,
                Path("C:/Program Files/Java/jdk-17/bin") / executable_name,
                Path("C:/Program Files/Java/bin") / executable_name,
            )
            for candidate in candidates:
                if candidate.exists():
                    return candidate
        raise RuntimeError(
            "OptiFine necesita javac para compilar el bridge. "
            "Instala un JDK o configura JAVA_HOME."
        )

    def _ensure_profile_for_legacy(
        self,
        minecraft_version: str,
        optifine_version: dict[str, Any],
        installer: Path,
    ) -> None:
        """Genera el perfil heredado si una build antigua no lo crea sola."""
        patch = str(optifine_version.get("patch", ""))
        edition = str(optifine_version.get("type", "HD_U"))
        profile_id = f"{minecraft_version}-OptiFine_{edition}_{patch}"
        profile_directory = self.game_directory / "versions" / profile_id
        profile_path = profile_directory / f"{profile_id}.json"
        if profile_path.exists():
            return
        vanilla_path = self.game_directory / "versions" / minecraft_version / f"{minecraft_version}.json"
        if not vanilla_path.exists():
            raise FileNotFoundError(f"No existe el perfil Vanilla {minecraft_version}")
        vanilla = json.loads(vanilla_path.read_text(encoding="utf-8"))
        profile = dict(vanilla)
        profile.update(
            {
                "id": profile_id,
                "inheritsFrom": minecraft_version,
                "jar": minecraft_version,
                "mainClass": "net.minecraft.launchwrapper.Launch",
                "minecraftArguments": (
                    "--username ${auth_player_name} --version ${version_name} "
                    "--gameDir ${game_directory} --assetsDir ${assets_root} "
                    "--assetIndex ${assets_index_name} --uuid ${auth_uuid} "
                    "--accessToken ${auth_access_token} --userProperties ${user_properties} "
                    "--userType ${user_type} --tweakClass optifine.OptiFineTweaker"
                ),
            }
        )
        libraries = list(profile.get("libraries", []))
        libraries.append({"name": f"optifine:OptiFine:{minecraft_version}_{edition}_{patch}"})
        if not any("launchwrapper" in str(item.get("name", "")) for item in libraries if isinstance(item, dict)):
            libraries.append({"name": "net.minecraft:launchwrapper:1.5"})
        profile["libraries"] = libraries
        profile_directory.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        library_directory = self.game_directory / "libraries" / "optifine" / "OptiFine" / f"{minecraft_version}_{edition}_{patch}"
        library_directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(installer, library_directory / f"OptiFine-{minecraft_version}_{edition}_{patch}.jar")
        self.logger.info("Perfil OptiFine legacy generado: %s", profile_id)

    def _import_legacy_installation(self, sandbox_appdata: Path) -> None:
        """Importa la salida del instalador legacy sin tocar el .minecraft real."""
        legacy_directory = sandbox_appdata / ".minecraft"
        if not legacy_directory.exists():
            return
        source_libraries = legacy_directory / "libraries"
        if source_libraries.exists():
            destination = self.game_directory / "libraries"
            shutil.copytree(source_libraries, destination, dirs_exist_ok=True)
        source_versions = legacy_directory / "versions"
        if source_versions.exists():
            destination = self.game_directory / "versions"
            destination.mkdir(parents=True, exist_ok=True)
            for version_directory in source_versions.glob("*-OptiFine*"):
                target = destination / version_directory.name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(version_directory, target)

    @staticmethod
    def _download_url(minecraft_version: str, version: dict[str, Any]) -> str:
        if version.get("url"):
            return str(version["url"])
        return (
            f"https://bmclapi2.bangbang93.com/optifine/{minecraft_version}/"
            f"{version.get('type', '')}/{version.get('patch', '')}"
        )

    def _download(
        self,
        url: str,
        destination: Path,
        progress_callback: ProgressCallback | None,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with requests.get(url, stream=True, timeout=60) as response:
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
                                progress_callback("Descargando OptiFine", downloaded * 100 / total)
                if total and downloaded != total:
                    raise requests.ConnectionError(f"Descarga incompleta ({downloaded}/{total} bytes)")
                if downloaded == 0:
                    raise requests.ConnectionError("Descarga vacia")
                return
            except requests.RequestException as error:
                last_error = error
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
                self.logger.warning("Descarga OptiFine fallida (intento %d/3): %s", attempt + 1, error)
        raise RuntimeError(f"No se pudo descargar OptiFine tras 3 intentos: {last_error}")
