"""Sistema de auto-actualizacion de FixnerLauncher via GitHub Releases y SemVer 2.0.0."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path
from typing import Any

import requests

from config.settings import APP_NAME, APP_VERSION

ProgressCallback = Callable[[str, float], None]

GITHUB_REPO = "spyflow/FixnerLauncher"
GITHUB_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"

# Expresion regular oficial de Semantic Versioning 2.0.0 (sstol.key)
SEMVER_REGEX = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True)
class SemVer:
    """Representacion y comparacion estricta de versiones segun SemVer 2.0.0."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    buildmetadata: str = ""

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(self.prerelease)
        if self.buildmetadata:
            base += f"+{self.buildmetadata}"
        return base

    @classmethod
    def parse(cls, raw: str) -> SemVer | None:
        cleaned = raw.strip()
        if cleaned.startswith(("v", "V")):
            cleaned = cleaned[1:].strip()
        match = SEMVER_REGEX.match(cleaned)
        if not match:
            # Soporte de respaldo para tags cortos tipo "0.4" -> "0.4.0"
            short_match = re.match(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)$", cleaned)
            if short_match:
                return cls(int(short_match.group(1)), int(short_match.group(2)), 0)
            return None
        pre_raw = match.group("prerelease")
        pre_tuple = tuple(pre_raw.split(".")) if pre_raw else ()
        build_raw = match.group("buildmetadata") or ""
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=pre_tuple,
            buildmetadata=build_raw,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        # Segun SemVer 2.0.0 regla 10, buildmetadata se ignora al comparar precedencia
        return (
            self.major == other.major
            and self.minor == other.minor
            and self.patch == other.patch
            and self.prerelease == other.prerelease
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        core_self = (self.major, self.minor, self.patch)
        core_other = (other.major, other.minor, other.patch)
        if core_self != core_other:
            return core_self < core_other

        # Regla 11.3: Cuando major, minor y patch son iguales, una pre-release tiene menor precedencia que una version normal
        if not self.prerelease and not other.prerelease:
            return False
        if self.prerelease and not other.prerelease:
            return True
        if not self.prerelease and other.prerelease:
            return False

        # Regla 11.4: Comparar cada identificador separado por punto de izquierda a derecha
        for id_a, id_b in zip(self.prerelease, other.prerelease):
            if id_a == id_b:
                continue
            a_num = id_a.isdigit()
            b_num = id_b.isdigit()
            if a_num and b_num:
                return int(id_a) < int(id_b)
            if a_num != b_num:
                # Identificadores numericos siempre tienen menor precedencia que los no numericos
                return a_num
            return id_a < id_b

        return len(self.prerelease) < len(other.prerelease)


@dataclass(frozen=True)
class ReleaseInfo:
    """Metadatos del release remoto obtenido desde GitHub Releases."""

    version: str
    semver: SemVer
    tag_name: str
    name: str
    body: str
    html_url: str
    is_prerelease: bool
    asset_name: str
    download_url: str
    asset_size: int


class UpdateManager:
    """Consulta GitHub Releases (canal Estable y Beta) y gestiona la descarga e instalacion."""

    def __init__(self, data_directory: Path, logger: logging.Logger) -> None:
        self.data_directory = data_directory
        self.updates_dir = data_directory / "updates"
        self.logger = logger

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "User-Agent": f"{APP_NAME}/{APP_VERSION} (Spyflow)",
            "Accept": "application/vnd.github+json",
        }

    @classmethod
    def _parse_release(cls, payload: dict[str, Any]) -> ReleaseInfo | None:
        if not isinstance(payload, dict) or payload.get("draft"):
            return None
        tag_name = str(payload.get("tag_name") or "").strip()
        semver = SemVer.parse(tag_name)
        if semver is None:
            semver = SemVer.parse(str(payload.get("name") or ""))
        if semver is None:
            return None

        is_pre = bool(payload.get("prerelease")) or semver.is_prerelease
        assets = payload.get("assets")
        asset_name = ""
        download_url = ""
        asset_size = 0
        if isinstance(assets, list):
            # Priorizar FixnerLauncher_Setup.exe o cualquier instalador .exe
            sorted_assets = sorted(
                (a for a in assets if isinstance(a, dict)),
                key=lambda a: (
                    0
                    if "setup" in str(a.get("name", "")).casefold()
                    and str(a.get("name", "")).casefold().endswith(".exe")
                    else (1 if str(a.get("name", "")).casefold().endswith(".exe") else 2)
                ),
            )
            for asset in sorted_assets:
                name_candidate = str(asset.get("name") or "")
                url_candidate = str(asset.get("browser_download_url") or "")
                if name_candidate.casefold().endswith(".exe") and url_candidate:
                    asset_name = name_candidate
                    download_url = url_candidate
                    asset_size = int(asset.get("size") or 0)
                    break

        return ReleaseInfo(
            version=str(semver),
            semver=semver,
            tag_name=tag_name or f"v{semver}",
            name=str(payload.get("name") or tag_name or f"v{semver}"),
            body=str(payload.get("body") or "Sin notas de version adicionales.").strip(),
            html_url=str(payload.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"),
            is_prerelease=is_pre,
            asset_name=asset_name,
            download_url=download_url,
            asset_size=asset_size,
        )

    def check_for_updates(
        self,
        current_version: str = APP_VERSION,
        include_prereleases: bool = False,
    ) -> ReleaseInfo | None:
        """Devuelve ReleaseInfo si existe una version superior en GitHub, o None si ya esta al dia."""
        local_semver = SemVer.parse(current_version)
        if local_semver is None:
            local_semver = SemVer(0, 0, 0)

        try:
            if not include_prereleases:
                response = requests.get(
                    GITHUB_LATEST_RELEASE_URL,
                    headers=self._headers(),
                    timeout=10,
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                candidate = self._parse_release(response.json())
                if candidate and not candidate.is_prerelease and candidate.semver > local_semver:
                    return candidate
                return None

            response = requests.get(
                GITHUB_RELEASES_URL,
                params={"per_page": 20},
                headers=self._headers(),
                timeout=10,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            items = response.json()
            if not isinstance(items, list):
                return None

            parsed_releases = [
                rel for item in items if (rel := self._parse_release(item)) is not None
            ]
            if not parsed_releases:
                return None

            best_release = max(parsed_releases, key=lambda r: r.semver)
            if best_release.semver > local_semver:
                return best_release
            return None
        except Exception as exc:
            self.logger.debug("No se pudo comprobar actualizaciones: %s", exc)
            return None

    def download_update(
        self,
        release: ReleaseInfo,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        """Descarga el instalador del release en .fixnerlauncher/updates/ verificando su tamano."""
        if not release.download_url:
            raise RuntimeError(
                "El release no incluye el instalador ejecutable FixnerLauncher_Setup.exe."
            )

        self.updates_dir.mkdir(parents=True, exist_ok=True)
        safe_ver = re.sub(r"[^0-9A-Za-z._-]", "_", release.version)
        target_path = self.updates_dir / f"FixnerLauncher_Setup_{safe_ver}.exe"
        part_path = self.updates_dir / f"FixnerLauncher_Setup_{safe_ver}.exe.part"

        if target_path.exists() and release.asset_size > 0:
            if target_path.stat().st_size == release.asset_size:
                if progress_callback:
                    progress_callback(f"Instalador v{release.version} listo.", 100.0)
                return target_path

        if progress_callback:
            progress_callback(f"Descargando FixnerLauncher v{release.version}...", 2.0)

        with requests.get(
            release.download_url,
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION} (Spyflow)"},
            stream=True,
            timeout=60,
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or release.asset_size or 0)
            downloaded = 0
            with part_path.open("wb") as file_handle:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    file_handle.write(chunk)
                    downloaded += len(chunk)
                    if total > 0 and progress_callback:
                        pct = min(99.0, (downloaded / total) * 100.0)
                        progress_callback(
                            f"Descargando actualizacion v{release.version}... ({pct:.0f}%)",
                            pct,
                        )

        if release.asset_size > 0 and part_path.stat().st_size != release.asset_size:
            part_path.unlink(missing_ok=True)
            raise RuntimeError(
                "La descarga del instalador esta incompleta o corrupta. Intenta de nuevo."
            )

        part_path.replace(target_path)
        if progress_callback:
            progress_callback(f"Instalador v{release.version} descargado.", 100.0)
        return target_path

    @staticmethod
    def launch_installer(installer_path: Path) -> None:
        """Lanza el instalador de Inno Setup en modo silencioso y desacoplado."""
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_BREAKAWAY_FROM_JOB
            )
            try:
                subprocess.Popen(
                    [
                        str(installer_path),
                        "/SILENT",
                        "/CLOSEAPPLICATIONS",
                        "/FORCECLOSEAPPLICATIONS",
                        "/NORESTART",
                    ],
                    creationflags=creationflags,
                    close_fds=True,
                )
                return
            except OSError:
                creationflags = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                )

        subprocess.Popen(
            [
                str(installer_path),
                "/SILENT",
                "/CLOSEAPPLICATIONS",
                "/FORCECLOSEAPPLICATIONS",
                "/NORESTART",
            ],
            creationflags=creationflags,
            close_fds=True,
        )
