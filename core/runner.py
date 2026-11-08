"""Fachada de ejecucion para Vanilla y perfiles derivados (OptiFine / Sodium)."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from core.auth_manager import AuthManager
from core.java_manager import JavaManager
from core.minecraft_manager import MinecraftManager


class MinecraftRunner:
    """Construye lanzamientos usando perfiles y runtimes gestionados."""

    def __init__(self, game_directory: Path, logger: logging.Logger) -> None:
        self.auth_manager = AuthManager(logger)
        self.java_manager = JavaManager(game_directory, logger)
        self.minecraft_manager = MinecraftManager(game_directory, logger)

    def launch(
        self,
        version_id: str,
        username: str,
        memory_mb: int,
        variant: str = "Vanilla",
    ) -> subprocess.Popen[str]:
        profile = self.auth_manager.get_active_profile(username)
        java_path = self.java_manager.ensure_runtime(version_id)
        extra_jvm_args: list[str] = []

        if variant == "Vanilla":
            launch_id = version_id
        elif variant == "OptiFine":
            launch_id = self._resolve_optifine_id(version_id)
        elif variant == "Sodium":
            launch_id = self._resolve_sodium_id(version_id)
            mods_dir = (self.minecraft_manager.game_directory / "mods" / version_id).resolve()
            mods_dir.mkdir(parents=True, exist_ok=True)
            extra_jvm_args.append(f"-Dfabric.addMods={mods_dir}")
        else:
            launch_id = version_id

        return self.minecraft_manager.launch_version(
            launch_id,
            profile.username,
            memory_mb,
            java_path=java_path,
            uuid=profile.uuid,
            token=profile.token,
            extra_jvm_args=extra_jvm_args,
        )

    def _resolve_optifine_id(self, minecraft_version: str) -> str:
        versions_directory = self.minecraft_manager.game_directory / "versions"
        for candidate in versions_directory.glob(f"{minecraft_version}-OptiFine*"):
            profile_path = candidate / f"{candidate.name}.json"
            if not profile_path.exists():
                continue
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if profile.get("inheritsFrom") == minecraft_version:
                return candidate.name
        raise FileNotFoundError(
            f"No hay una instalacion OptiFine para Minecraft {minecraft_version}"
        )

    def _resolve_sodium_id(self, minecraft_version: str) -> str:
        versions_directory = self.minecraft_manager.game_directory / "versions"
        for candidate in sorted(versions_directory.glob(f"fabric-loader-*-{minecraft_version}"), reverse=True):
            profile_path = candidate / f"{candidate.name}.json"
            if not profile_path.exists():
                continue
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if profile.get("inheritsFrom") == minecraft_version:
                return candidate.name
        raise FileNotFoundError(
            f"No hay una instalacion Sodium (Fabric) para Minecraft {minecraft_version}"
        )
