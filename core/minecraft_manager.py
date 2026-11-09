"""Gestion de versiones Vanilla y ejecucion de Minecraft."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Thread
from typing import Any

from minecraft_launcher_lib import _helper, command, install, runtime, utils


ProgressCallback = Callable[[str, float], None]
ProcessCallback = Callable[[subprocess.Popen[str]], None]

_ORIGINAL_DOWNLOAD_FILE = _helper.download_file


def _resilient_download_file(url: str, path: str, *args: Any, **kwargs: Any) -> bool:
    """Envuelve download_file de minecraft_launcher_lib con reintentos ante IncompleteRead/cortes de red."""
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            return _ORIGINAL_DOWNLOAD_FILE(url, path, *args, **kwargs)
        except Exception as exc:
            last_exc = exc
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            if attempt < 3:
                time.sleep(0.8 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    return False


_helper.download_file = _resilient_download_file
install.download_file = _resilient_download_file
runtime.download_file = _resilient_download_file


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
        """Descarga una version Vanilla y la verifica; si queda incompleta lanza error y limpia lo parcial."""
        error_holder: list[Exception] = []
        version_dir = self.game_directory / "versions" / version_id
        existed_before = self.is_installation_complete(version_id)

        def install_in_background() -> None:
            callback = self._build_install_callback(progress_callback)
            self.logger.info("Instalando Minecraft %s en %s", version_id, self.game_directory)
            last_error: Exception | None = None
            problems: list[str] = []
            for attempt in range(3):
                try:
                    install.install_minecraft_version(
                        version_id,
                        str(self.game_directory),
                        callback=callback,
                    )
                    last_error = None
                except Exception as exc:
                    last_error = exc
                    self.logger.warning(
                        "Intento %d/3 de instalacion de Minecraft %s interrumpido (%s). Reanudando...",
                        attempt + 1,
                        version_id,
                        exc,
                    )
                if last_error is None:
                    self._ensure_client_jar(version_id)
                    problems = self.verify_installation(version_id)
                    if not problems:
                        break
                    self.logger.warning(
                        "Intento %d/3: instalacion de %s incompleta (%s). Reintentando...",
                        attempt + 1,
                        version_id,
                        ", ".join(problems[:4]),
                    )
                if attempt < 2:
                    time.sleep(1.0)
            if last_error is not None or problems:
                if not existed_before:
                    shutil.rmtree(version_dir, ignore_errors=True)
                detail = str(last_error) if last_error is not None else ", ".join(problems[:5])
                error_holder.append(
                    RuntimeError(
                        f"No se pudo completar la descarga de Minecraft {version_id}. "
                        f"Revisa tu conexion e intentalo de nuevo. ({detail})"
                    )
                )
                return
            self.logger.info("Minecraft %s instalado", version_id)

        worker = Thread(target=install_in_background, daemon=True)
        worker.start()
        worker.join()
        if error_holder:
            raise error_holder[0]

    def _ensure_client_jar(self, version_id: str) -> None:
        """Garantiza que versions/<id>/<id>.jar exista descargandolo desde el JSON de la version si falta."""
        version_dir = self.game_directory / "versions" / version_id
        jar_path = version_dir / f"{version_id}.jar"
        if jar_path.exists() and jar_path.stat().st_size > 0:
            return
        json_path = version_dir / f"{version_id}.json"
        if not json_path.exists():
            return
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        client_info = data.get("downloads", {}).get("client", {})
        if not isinstance(client_info, dict) or not client_info.get("url"):
            return
        url = str(client_info["url"])
        sha1 = str(client_info["sha1"]) if client_info.get("sha1") else None
        self.logger.info("Recuperando client.jar faltante para Minecraft %s...", version_id)
        _resilient_download_file(
            url,
            str(jar_path),
            sha1=sha1,
            minecraft_directory=str(self.game_directory),
            overwrite=True,
        )

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

    def _prepare_inherited_version(
        self,
        version_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> list[str]:
        """Sincroniza el .jar base, natives y librerias extra de un perfil heredado. Devuelve librerias faltantes."""
        version_dir = self.game_directory / "versions" / version_id
        json_path = version_dir / f"{version_id}.json"
        if not json_path.exists():
            return []
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        base_id = str(data.get("inheritsFrom", "")).strip()
        if not base_id and "-OptiFine" in version_id:
            base_id = version_id.split("-OptiFine", 1)[0]
            data["inheritsFrom"] = base_id
            json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if not base_id:
            self._ensure_client_jar(version_id)
            return []
        base_dir = self.game_directory / "versions" / base_id
        base_jar = base_dir / f"{base_id}.jar"
        if not base_jar.exists() or base_jar.stat().st_size == 0:
            self.logger.warning(
                "Archivo base %s no encontrado para %s; completando instalacion base...",
                base_jar.name,
                version_id,
            )
            self._ensure_client_jar(base_id)
            if not base_jar.exists():
                self.install_version(base_id, progress_callback)
        target_jar = version_dir / f"{version_id}.jar"
        if base_jar.exists() and (not target_jar.exists() or target_jar.stat().st_size == 0):
            shutil.copy2(base_jar, target_jar)
        base_natives = base_dir / "natives"
        target_natives = version_dir / "natives"
        if base_natives.exists() and not target_natives.exists():
            shutil.copytree(base_natives, target_natives, dirs_exist_ok=True)
        return self._ensure_maven_libraries(data.get("libraries", []), progress_callback)

    def _ensure_maven_libraries(
        self,
        libraries: list[dict[str, Any]],
        progress_callback: ProgressCallback | None = None,
    ) -> list[str]:
        """Descarga librerias Maven adicionales si faltan. Devuelve los nombres que no se pudieron obtener."""
        missing: list[str] = []
        valid_libs = [
            lib
            for lib in libraries
            if isinstance(lib, dict)
            and len(str(lib.get("name", "")).strip().split(":")) == 3
            and str(lib.get("name", "")).strip().split(":")[0] != "optifine"
        ]
        total_libs = max(1, len(valid_libs))
        for idx, lib in enumerate(valid_libs, start=1):
            name = str(lib.get("name", "")).strip()
            group, artifact, version = name.split(":")
            rel_path = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.jar"
            target_file = self.game_directory / "libraries" / rel_path
            pct = (idx / float(total_libs)) * 100.0
            if target_file.exists() and target_file.stat().st_size > 0:
                if progress_callback:
                    progress_callback(
                        f"Verificando librerias ({idx}/{total_libs}) · {int(pct)}%",
                        pct,
                    )
                continue
            if progress_callback:
                progress_callback(
                    f"Descargando librerias ({idx}/{total_libs}) · {int(pct)}% — {artifact}-{version}.jar",
                    pct,
                )
            target_file.parent.mkdir(parents=True, exist_ok=True)
            base_url = str(lib.get("url", "https://libraries.minecraft.net/")).rstrip("/")
            url = f"{base_url}/{rel_path}"
            downloaded_ok = False
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(url, timeout=20) as resp:
                        payload = resp.read()
                    if not payload:
                        raise OSError("respuesta vacia")
                    target_file.write_bytes(payload)
                    downloaded_ok = True
                    self.logger.info("Libreria adicional descargada: %s", name)
                    break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.6 * (attempt + 1))
            if downloaded_ok:
                continue
            existing = [
                path
                for path in (self.game_directory / "libraries" / group.replace(".", "/") / artifact).rglob(f"{artifact}-*.jar")
                if path.is_file() and path.stat().st_size > 0
            ]
            if existing:
                shutil.copy2(existing[0], target_file)
                self.logger.info("Libreria copiada desde cache local para %s", name)
            else:
                self.logger.warning("No se pudo descargar la libreria %s: %s", name, last_exc)
                missing.append(f"{artifact}-{version}.jar")
        return missing

    def verify_installation(self, version_id: str) -> list[str]:
        """Devuelve la lista de problemas de una instalacion; vacia si esta completa y utilizable."""
        problems: list[str] = []
        json_path = self.game_directory / "versions" / version_id / f"{version_id}.json"
        if not json_path.is_file() or json_path.stat().st_size == 0:
            return [f"falta {version_id}.json"]
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return [f"{version_id}.json corrupto"]
        base_id = str(data.get("inheritsFrom", "")).strip()
        jar_owner = base_id or version_id
        jar_path = self.game_directory / "versions" / jar_owner / f"{jar_owner}.jar"
        if not jar_path.is_file() or jar_path.stat().st_size == 0:
            problems.append(f"falta {jar_owner}.jar")
        else:
            expected = data.get("downloads", {}).get("client", {}).get("size") if isinstance(data.get("downloads"), dict) else None
            if isinstance(expected, int) and expected > 0 and jar_path.stat().st_size != expected:
                problems.append(f"{jar_owner}.jar incompleto")
        sources = [data]
        if base_id:
            base_json = self.game_directory / "versions" / base_id / f"{base_id}.json"
            try:
                sources.append(json.loads(base_json.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                problems.append(f"falta {base_id}.json")
        for source in sources:
            for lib in source.get("libraries", []):
                if not isinstance(lib, dict):
                    continue
                if "rules" in lib and not _helper.parse_rule_list(lib["rules"], {}):
                    continue
                artifact = lib.get("downloads", {}).get("artifact") if isinstance(lib.get("downloads"), dict) else None
                if isinstance(artifact, dict) and artifact.get("path") and artifact.get("url"):
                    lib_file = self.game_directory / "libraries" / str(artifact["path"])
                    if not lib_file.is_file() or lib_file.stat().st_size == 0:
                        problems.append(f"falta libreria {lib_file.name}")
        return problems

    def is_installation_complete(self, version_id: str) -> bool:
        return not self.verify_installation(version_id)

    def delete_installation(self, version_id: str) -> bool:
        """Elimina versions/<id> con reintentos (Windows puede bloquear archivos). Devuelve True si desaparecio."""
        target = self.game_directory / "versions" / version_id
        for attempt in range(4):
            shutil.rmtree(target, ignore_errors=True)
            if not target.exists():
                return True
            time.sleep(0.4 * (attempt + 1))
        return not target.exists()

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
        state: dict[str, Any] = {
            "phase": "libraries",
            "max": 1,
            "current": 0,
            "last_pct": 0.0,
            "last_file": "",
        }
        phase_labels = {
            "libraries": "Descargando librerias",
            "assets": "Descargando recursos",
            "runtime": "Descargando Java Runtime",
        }
        phase_ranges = {
            "libraries": (0.0, 28.0),
            "assets": (28.0, 82.0),
            "runtime": (82.0, 99.0),
        }

        def _emit() -> None:
            if not progress_callback:
                return
            phase = str(state["phase"])
            total = max(1, int(state["max"]))
            current = min(total, max(0, int(state["current"])))
            start_pct, end_pct = phase_ranges.get(phase, (0.0, 99.0))
            # Cada archivo de la tanda tiene el mismo peso (1 / total)
            ratio = current / float(total)
            computed = start_pct + ratio * (end_pct - start_pct)
            pct = max(float(state["last_pct"]), min(99.5, computed))
            state["last_pct"] = pct
            label = phase_labels.get(phase, "Descargando archivos")
            suffix = f" — {state['last_file']}" if state["last_file"] else ""
            progress_callback(f"{label} ({current}/{total}) · {int(pct)}%{suffix}", pct)

        def set_status(status: str) -> None:
            self.logger.info("Descarga: %s", status)
            if status == "Download Libraries":
                state["phase"] = "libraries"
                state["current"] = 0
                state["last_file"] = ""
            elif status == "Download Assets":
                state["phase"] = "assets"
                state["current"] = 0
                state["last_file"] = ""
                state["last_pct"] = max(float(state["last_pct"]), 28.0)
            elif status == "Install java runtime":
                state["phase"] = "runtime"
                state["current"] = 0
                state["last_file"] = ""
                state["last_pct"] = max(float(state["last_pct"]), 82.0)
            elif status == "Installation complete":
                state["last_pct"] = 100.0
                if progress_callback:
                    progress_callback("Instalacion completada · 100%", 100.0)
                return
            elif status.startswith("Download "):
                state["last_file"] = status[len("Download "):].strip()
            _emit()

        def set_progress(progress: float) -> None:
            state["current"] = int(progress)
            _emit()

        def set_maximum(maximum: int) -> None:
            state["max"] = max(1, int(maximum) + 1)
            state["current"] = 0
            _emit()

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
