"""Gestion de versiones Vanilla y ejecucion de Minecraft."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Thread
from typing import Any

from minecraft_launcher_lib import command, install, utils


ProgressCallback = Callable[[str, float], None]
ProcessCallback = Callable[[subprocess.Popen[str]], None]


class MinecraftManager:
    """Coordina el manifiesto, la instalacion y la ejecucion de versiones Vanilla."""

    def __init__(self, game_directory: Path, logger: logging.Logger) -> None:
        self.game_directory = game_directory
        self.logger = logger
        self.game_directory.mkdir(parents=True, exist_ok=True)

    def list_releases(self) -> list[dict[str, Any]]:
        """Obtiene releases oficiales desde Minecraft 1.0, excluyendo snapshots."""
        versions = utils.get_version_list()
        releases = [
            version
            for version in versions
            if version.get("type") == "release"
            and self._version_at_least_1_0(str(version.get("id", "")))
        ]
        self.logger.info("Manifiesto cargado: %d releases disponibles", len(releases))
        return releases

    def install_version(
        self,
        version_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """Descarga una version Vanilla y todos sus recursos."""
        def install_in_background() -> None:
            callback = self._build_install_callback(progress_callback)
            self.logger.info("Instalando Minecraft %s en %s", version_id, self.game_directory)
            install.install_minecraft_version(
                version_id,
                str(self.game_directory),
                callback=callback,
            )
            self.logger.info("Minecraft %s instalado", version_id)

        worker = Thread(target=install_in_background, daemon=True)
        worker.start()
        worker.join()

    def launch_version(
        self,
        version_id: str,
        username: str,
        memory_mb: int,
        process_callback: ProcessCallback | None = None,
        java_path: Path | None = None,
        uuid: str = "00000000-0000-0000-0000-000000000000",
        token: str = "0",
        extra_jvm_args: list[str] | None = None,
    ) -> subprocess.Popen[str]:
        """Lanza Minecraft sin bloquear el hilo que llama al metodo."""
        if not username.strip():
            raise ValueError("El nombre de usuario no puede estar vacio")
        if memory_mb < 512:
            raise ValueError("La memoria debe ser de al menos 512 MB")
        version_directory = self.game_directory / "versions" / version_id
        if not version_directory.exists():
            raise FileNotFoundError(
                f"Minecraft {version_id} no esta instalado. Pulsa 'Instalar version' primero."
            )
        self._prepare_inherited_version(version_id)

        jvm_arguments = [f"-Xmx{memory_mb}M"]
        if extra_jvm_args:
            jvm_arguments.extend(extra_jvm_args)

        options: dict[str, Any] = {
            "username": username.strip(),
            "uuid": uuid,
            "token": token,
            "gameDirectory": str(self.game_directory),
            "jvmArguments": jvm_arguments,
        }
        if java_path:
            options["executablePath"] = str(java_path)
        process_holder: list[subprocess.Popen[str]] = []

        def launch_in_background() -> None:
            launch_command = command.get_minecraft_command(
                version_id,
                str(self.game_directory),
                options,
            )
            process_holder.append(
                subprocess.Popen(
                    launch_command,
                    cwd=self.game_directory,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            )

        worker = Thread(target=launch_in_background, daemon=True)
        worker.start()
        worker.join()
        process = process_holder[0]
        self.logger.info("Minecraft %s iniciado con PID %s", version_id, process.pid)
        if process_callback:
            process_callback(process)
        Thread(target=self._log_process_output, args=(process,), daemon=True).start()
        return process

    def _prepare_inherited_version(self, version_id: str) -> None:
        """Sincroniza el .jar base, natives y librerias extra cuando se lanza un perfil heredado (ej. OptiFine)."""
        version_dir = self.game_directory / "versions" / version_id
        json_path = version_dir / f"{version_id}.json"
        if not json_path.exists():
            return
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        base_id = str(data.get("inheritsFrom", "")).strip()
        if not base_id and "-OptiFine" in version_id:
            base_id = version_id.split("-OptiFine", 1)[0]
            data["inheritsFrom"] = base_id
            json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if not base_id:
            return
        base_dir = self.game_directory / "versions" / base_id
        base_jar = base_dir / f"{base_id}.jar"
        target_jar = version_dir / f"{version_id}.jar"
        if base_jar.exists() and not target_jar.exists():
            shutil.copy2(base_jar, target_jar)
        base_natives = base_dir / "natives"
        target_natives = version_dir / "natives"
        if base_natives.exists() and not target_natives.exists():
            shutil.copytree(base_natives, target_natives, dirs_exist_ok=True)
        self._ensure_maven_libraries(data.get("libraries", []))

    def _ensure_maven_libraries(self, libraries: list[dict[str, Any]]) -> None:
        """Descarga librerias Maven adicionales (como net.minecraft:launchwrapper) si faltan en disco."""
        for lib in libraries:
            if not isinstance(lib, dict):
                continue
            name = str(lib.get("name", "")).strip()
            parts = name.split(":")
            if len(parts) != 3 or parts[0] == "optifine":
                continue
            group, artifact, version = parts
            rel_path = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.jar"
            target_file = self.game_directory / "libraries" / rel_path
            if target_file.exists():
                continue
            target_file.parent.mkdir(parents=True, exist_ok=True)
            base_url = str(lib.get("url", "https://libraries.minecraft.net/")).rstrip("/")
            url = f"{base_url}/{rel_path}"
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    target_file.write_bytes(resp.read())
                self.logger.info("Libreria adicional descargada: %s", name)
            except Exception as exc:
                existing = list((self.game_directory / "libraries" / group.replace(".", "/") / artifact).rglob(f"{artifact}-*.jar"))
                if existing:
                    shutil.copy2(existing[0], target_file)
                    self.logger.info("Libreria copiada desde cache local para %s", name)
                else:
                    self.logger.warning("No se pudo descargar la libreria %s: %s", name, exc)

    @staticmethod
    def _version_at_least_1_0(version_id: str) -> bool:
        try:
            major, minor = (int(part) for part in version_id.split(".")[:2])
        except (ValueError, StopIteration):
            return False
        return (major, minor) >= (1, 0)

    def _build_install_callback(
        self,
        progress_callback: ProgressCallback | None,
    ) -> Mapping[str, Callable[..., None]]:
        def set_status(status: str) -> None:
            self.logger.info("Descarga: %s", status)
            if progress_callback:
                progress_callback(status, 0.0)

        def set_progress(progress: float) -> None:
            if progress_callback:
                progress_callback("Descargando", float(progress))

        def set_maximum(maximum: int) -> None:
            if progress_callback:
                progress_callback("Preparando descarga", float(maximum))

        return {
            "setStatus": set_status,
            "setProgress": set_progress,
            "setMax": set_maximum,
        }

    def _log_process_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout:
            for line in process.stdout:
                self.logger.info("Minecraft: %s", line.rstrip())
        exit_code = process.wait()
        self.logger.info("Minecraft finalizo con codigo %s", exit_code)
