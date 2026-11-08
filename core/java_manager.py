"""Seleccion y descarga de runtimes Java de Mojang."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from minecraft_launcher_lib import runtime


class JavaManager:
    """Resuelve el runtime compatible con cada generacion de Minecraft."""

    def __init__(self, game_directory: Path, logger: logging.Logger) -> None:
        self.game_directory = game_directory
        self.logger = logger
        self.game_directory.mkdir(parents=True, exist_ok=True)

    def runtime_name_for(self, version_id: str) -> str:
        """Devuelve el identificador de runtime requerido por una version."""
        manifest_component = self._component_from_version_json(version_id)
        if manifest_component:
            return manifest_component
        major, minor, patch = self._parse_version(version_id)
        if (major, minor, patch) <= (1, 16, 5):
            return "jre-legacy"
        if (major, minor, patch) <= (1, 17, 1):
            return "java-runtime-alpha"
        if (major, minor, patch) <= (1, 20, 4):
            return "java-runtime-gamma"
        if (major, minor, patch) < (26, 0, 0):
            return "java-runtime-delta"
        return "java-runtime-epsilon"

    def _component_from_version_json(self, version_id: str) -> str | None:
        """Obtiene javaVersion.component directamente del JSON oficial de la version si existe."""
        current_id = version_id
        for _ in range(3):
            json_path = self.game_directory / "versions" / current_id / f"{current_id}.json"
            if not json_path.exists():
                return None
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            java_ver = data.get("javaVersion")
            if isinstance(java_ver, dict):
                component = str(java_ver.get("component", "")).strip()
                if component:
                    return component
            parent_id = str(data.get("inheritsFrom", "")).strip()
            if not parent_id or parent_id == current_id:
                break
            current_id = parent_id
        return None

    def ensure_runtime(self, version_id: str) -> Path:
        """Descarga el runtime requerido si no esta presente y devuelve java.exe."""
        runtime_name = self.runtime_name_for(version_id)
        executable = runtime.get_executable_path(runtime_name, self.game_directory)
        if executable is None:
            self.logger.info("Instalando runtime Java %s", runtime_name)
            runtime.install_jvm_runtime(runtime_name, self.game_directory)
            executable = runtime.get_executable_path(runtime_name, self.game_directory)
        if executable is None:
            raise RuntimeError(f"No se encontro el ejecutable del runtime {runtime_name}")
        executable_path = Path(executable)
        if executable_path.name.casefold() == "java.exe":
            javaw_path = executable_path.with_name("javaw.exe")
            if javaw_path.exists():
                executable_path = javaw_path
        self.logger.info("Runtime Java seleccionado: %s", executable_path)
        return executable_path

    @staticmethod
    def _parse_version(version_id: str) -> tuple[int, int, int]:
        parts = version_id.split(".")
        try:
            values = [int(part) for part in parts[:3]]
        except ValueError as error:
            raise ValueError(f"Version de Minecraft invalida: {version_id}") from error
        return tuple((values + [0, 0, 0])[:3])  # type: ignore[return-value]
