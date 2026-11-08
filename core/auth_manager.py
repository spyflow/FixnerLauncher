"""Perfiles offline locales y autenticacion Microsoft OAuth2 directa."""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable, TypeVar

import keyring

from config.settings import (
    MICROSOFT_CLIENT_ID,
    MICROSOFT_REDIRECT_URI,
    PREMIUM_PROFILE_FILE,
    PROFILES_FILE,
    load_json,
    save_json,
)


KEYRING_SERVICE = "FixnerLauncher"
KEYRING_ACCOUNT = "microsoft-refresh-token"
KEYRING_ACCESS_ACCOUNT = "microsoft-access-token"
OFFLINE_NAMESPACE = uuid.UUID("b7b7f1a4-6f5f-4e51-9a9d-4c3a9d35b4f7")

HTTP_TIMEOUT = 12.0
OPERATION_TIMEOUT = 15.0

LIVE_AUTHORIZE_URL = "https://login.live.com/oauth20_authorize.srf"
LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
LIVE_OAUTH_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

# Script JS para inyectar en QWebEngineView y evitar el bloqueo por huella / llave USB (WebAuthn / Windows Hello)
DISABLE_WEBAUTHN_JS = """
(() => {
    const blockWebAuthn = () => Promise.reject(
        new DOMException('Passkeys deshabilitadas por FixnerLauncher', 'NotSupportedError')
    );

    try {
        Object.defineProperty(window, 'PublicKeyCredential', {
            get: () => undefined,
            set: () => {},
            configurable: false
        });
    } catch (e) {
        window.PublicKeyCredential = undefined;
    }

    if (window.CredentialsContainer && window.CredentialsContainer.prototype) {
        window.CredentialsContainer.prototype.get = blockWebAuthn;
        window.CredentialsContainer.prototype.create = blockWebAuthn;
    }

    try {
        Object.defineProperty(Navigator.prototype, 'credentials', {
            get: () => ({
                get: blockWebAuthn,
                create: blockWebAuthn,
                preventSilentAccess: () => Promise.resolve(),
                store: blockWebAuthn
            }),
            configurable: false
        });
    } catch (e) {}
})();
"""

T = TypeVar("T")


def _run_daemon(func: Callable[..., T], timeout: float, *args: object, **kwargs: object) -> T:
    """Ejecuta operaciones bloqueantes en un hilo daemon para que mueran al cerrar la ventana."""
    result: list[T] = []
    error: list[BaseException] = []

    def target() -> None:
        try:
            result.append(func(*args, **kwargs))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        raise TimeoutError(f"Operacion cancelada tras {timeout}s de espera")
    if error:
        raise error[0]
    return result[0]


def _http_post_form(url: str, form_data: dict[str, str], timeout: float = HTTP_TIMEOUT) -> dict[str, Any]:
    """Ejecuta un POST application/x-www-form-urlencoded y devuelve el JSON."""
    encoded = urllib.parse.urlencode(form_data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    def _do_request() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                err_data = json.loads(body)
                msg = err_data.get("error_description") or err_data.get("error") or body
            except json.JSONDecodeError:
                msg = body or str(exc)
            raise RuntimeError(f"Error OAuth Microsoft ({exc.code}): {msg}") from exc

    return _run_daemon(_do_request, timeout + 3.0)


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = HTTP_TIMEOUT) -> dict[str, Any]:
    """Ejecuta un POST application/json y devuelve el JSON."""
    encoded = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    def _do_request() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                err_data = json.loads(body)
                msg = err_data.get("errorMessage") or err_data.get("Message") or err_data.get("XErr") or body
            except json.JSONDecodeError:
                msg = body or str(exc)
            raise RuntimeError(f"Error de autenticacion ({exc.code}) en {url}: {msg}") from exc

    return _run_daemon(_do_request, timeout + 3.0)


def _http_get_json(url: str, headers: dict[str, str], timeout: float = HTTP_TIMEOUT) -> dict[str, Any]:
    """Ejecuta un GET y devuelve el JSON."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", **headers},
        method="GET",
    )

    def _do_request() -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and url == MC_PROFILE_URL:
                raise RuntimeError("Esta cuenta de Microsoft no posee Minecraft Java Edition.") from exc
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Error HTTP {exc.code} al consultar {url}: {body or exc.reason}") from exc

    return _run_daemon(_do_request, timeout + 3.0)


def _is_jwt_token_valid(token: str, min_remaining_seconds: int = 300) -> bool:
    """Verifica si un JWT (access_token de Minecraft) aun no ha expirado."""
    if not token or token == "0":
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8"))
        exp = float(payload.get("exp", 0))
        return exp > (time.time() + min_remaining_seconds)
    except Exception:
        return False


@dataclass
class OfflineProfile:
    """Perfil local compatible con el formato esperado por el launcher."""

    name: str
    username: str
    uuid: str = "00000000-0000-0000-0000-000000000000"
    token: str = "0"


@dataclass
class PremiumProfile:
    """Perfil Xbox/Minecraft obtenido desde Microsoft Services."""

    name: str
    username: str
    uuid: str
    token: str
    refresh_token: str = ""


class AuthManager:
    """Gestiona perfiles offline y sesiones Microsoft OAuth2."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def list_profiles(self) -> list[OfflineProfile]:
        profiles = load_json(PROFILES_FILE, [])
        return [OfflineProfile(**profile) for profile in profiles if isinstance(profile, dict)]

    def get_or_create(self, username: str) -> OfflineProfile:
        clean_name = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", clean_name):
            raise ValueError("El usuario offline debe tener 3-16 caracteres alfanumericos o guion bajo")
        offline_uuid = str(uuid.uuid3(OFFLINE_NAMESPACE, clean_name.casefold()))
        profiles = self.list_profiles()
        for profile in profiles:
            if profile.username.casefold() == clean_name.casefold():
                if profile.uuid != offline_uuid:
                    profile.uuid = offline_uuid
                    save_json(PROFILES_FILE, [asdict(item) for item in profiles])
                    self.logger.info("Perfil offline actualizado: %s", clean_name)
                return profile
        profile = OfflineProfile(name=clean_name, username=clean_name, uuid=offline_uuid)
        profiles.append(profile)
        save_json(PROFILES_FILE, [asdict(item) for item in profiles])
        self.logger.info("Perfil offline guardado: %s", clean_name)
        return profile

    def prepare_microsoft_login(
        self,
        client_id: str = MICROSOFT_CLIENT_ID,
        redirect_uri: str = MICROSOFT_REDIRECT_URI,
    ) -> tuple[str, str, str]:
        """Genera la URL de autorizacion de Microsoft Live para el cliente oficial de escritorio."""
        effective_client_id = client_id or MICROSOFT_CLIENT_ID
        effective_redirect_uri = redirect_uri or MICROSOFT_REDIRECT_URI
        query = urllib.parse.urlencode(
            {
                "client_id": effective_client_id,
                "response_type": "code",
                "redirect_uri": effective_redirect_uri,
                "scope": LIVE_OAUTH_SCOPE,
                "prompt": "select_account",
            }
        )
        return f"{LIVE_AUTHORIZE_URL}?{query}", "", ""

    def _authenticate_xbox_and_minecraft(
        self,
        ms_access_token: str,
        fetch_profile: bool = True,
    ) -> dict[str, str]:
        """Intercambia el access_token de Live por XBL -> XSTS -> Minecraft access_token (+ perfil)."""
        # 1. Autenticacion Xbox Live (XBL) - sin prefijo d= para scope MBI_SSL
        xbl_response = _http_post_json(
            XBL_AUTH_URL,
            {
                "Properties": {
                    "AuthMethod": "RPS",
                    "SiteName": "user.auth.xboxlive.com",
                    "RpsTicket": ms_access_token,
                },
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT",
            },
            timeout=HTTP_TIMEOUT,
        )
        xbl_token = str(xbl_response.get("Token", ""))
        xui_list = xbl_response.get("DisplayClaims", {}).get("xui", [])
        uhs = str(xui_list[0].get("uhs", "")) if xui_list and isinstance(xui_list[0], dict) else ""
        if not xbl_token or not uhs:
            raise RuntimeError("Xbox Live no devolvio un token o UHS valido.")

        # 2. Autenticacion XSTS
        xsts_response = _http_post_json(
            XSTS_AUTH_URL,
            {
                "Properties": {
                    "SandboxId": "RETAIL",
                    "UserTokens": [xbl_token],
                },
                "RelyingParty": "rp://api.minecraftservices.com/",
                "TokenType": "JWT",
            },
            timeout=HTTP_TIMEOUT,
        )
        xsts_token = str(xsts_response.get("Token", ""))
        if not xsts_token:
            raise RuntimeError("XSTS no devolvio un token valido para Minecraft Services.")

        # 3. Login en Minecraft Services
        mc_login_response = _http_post_json(
            MC_LOGIN_URL,
            {
                "identityToken": f"XBL3.0 x={uhs};{xsts_token}",
            },
            timeout=HTTP_TIMEOUT,
        )
        mc_access_token = str(mc_login_response.get("access_token", ""))
        if not mc_access_token:
            raise RuntimeError("Minecraft Services no devolvio un access_token valido.")

        result = {"access_token": mc_access_token}

        # 4. Obtencion del perfil de Minecraft Java Edition
        if fetch_profile:
            profile_response = _http_get_json(
                MC_PROFILE_URL,
                headers={"Authorization": f"Bearer {mc_access_token}"},
                timeout=HTTP_TIMEOUT,
            )
            profile_id = str(profile_response.get("id", ""))
            profile_name = str(profile_response.get("name", ""))
            if not profile_id or not profile_name:
                raise RuntimeError("Esta cuenta no posee un perfil valido de Minecraft Java Edition.")
            result["id"] = profile_id
            result["name"] = profile_name

        return result

    def save_microsoft_login(self, login: object) -> PremiumProfile:
        """Guarda el perfil publico en disco y ambos tokens cifrados en Windows Credential Vault."""
        def value(name: str) -> str:
            if isinstance(login, dict):
                return str(login.get(name, ""))
            return str(getattr(login, name, ""))

        access_token = value("access_token")
        refresh_token = value("refresh_token")
        username = value("name")
        profile_uuid = value("id")
        if not access_token or not refresh_token or not username or not profile_uuid:
            raise RuntimeError("Microsoft no devolvio un perfil de Minecraft valido")
        _run_daemon(keyring.set_password, OPERATION_TIMEOUT, KEYRING_SERVICE, KEYRING_ACCOUNT, refresh_token)
        _run_daemon(keyring.set_password, OPERATION_TIMEOUT, KEYRING_SERVICE, KEYRING_ACCESS_ACCOUNT, access_token)
        profile = PremiumProfile(username, username, profile_uuid, access_token)
        save_json(
            PREMIUM_PROFILE_FILE,
            {
                "name": username,
                "username": username,
                "uuid": profile_uuid,
                "token": "",
                "refresh_token": "",
            },
        )
        self.logger.info("Sesion Microsoft guardada en baul de credenciales para %s", username)
        return profile

    def get_saved_premium_profile(self) -> PremiumProfile | None:
        """Devuelve los metadatos publicos del perfil premium almacenado en disco sin llamadas de red."""
        data = load_json(PREMIUM_PROFILE_FILE, None)
        if not isinstance(data, dict):
            return None
        username = str(data.get("username", "")).strip()
        profile_uuid = str(data.get("uuid", "")).strip()
        if not username or not profile_uuid:
            return None
        # Migracion automatica si existia un token antiguo en texto plano
        legacy_plain_token = str(data.get("token", "")).strip()
        if legacy_plain_token:
            try:
                _run_daemon(
                    keyring.set_password,
                    OPERATION_TIMEOUT,
                    KEYRING_SERVICE,
                    KEYRING_ACCESS_ACCOUNT,
                    legacy_plain_token,
                )
            except Exception:
                pass
            save_json(
                PREMIUM_PROFILE_FILE,
                {
                    "name": str(data.get("name") or username),
                    "username": username,
                    "uuid": profile_uuid,
                    "token": "",
                    "refresh_token": "",
                },
            )
        return PremiumProfile(
            name=str(data.get("name") or username),
            username=username,
            uuid=profile_uuid,
            token="",
        )

    def get_premium_profile(self) -> PremiumProfile | None:
        saved_meta = self.get_saved_premium_profile()
        if not saved_meta:
            return None

        cached_token = ""
        try:
            vault_access = _run_daemon(
                keyring.get_password,
                OPERATION_TIMEOUT,
                KEYRING_SERVICE,
                KEYRING_ACCESS_ACCOUNT,
            )
            if vault_access:
                cached_token = str(vault_access).strip()
        except Exception as error:
            self.logger.warning("No se pudo leer el access_token del keyring: %s", error)

        cached_profile = PremiumProfile(
            name=saved_meta.name,
            username=saved_meta.username,
            uuid=saved_meta.uuid,
            token=cached_token,
        )

        # Si el access_token cifrado en el baul del sistema aun es valido, usarlo al instante
        if _is_jwt_token_valid(cached_token):
            self.logger.info("Usando sesion Microsoft activa (token vigente en keyring) para %s", cached_profile.username)
            return cached_profile

        try:
            refresh_token = _run_daemon(keyring.get_password, OPERATION_TIMEOUT, KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception as error:
            self.logger.warning("No se pudo acceder al keyring del sistema: %s", error)
            return cached_profile if cached_token else None
        if not refresh_token:
            return cached_profile if cached_token else None
        try:
            token_data = _http_post_form(
                LIVE_TOKEN_URL,
                {
                    "client_id": MICROSOFT_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": LIVE_OAUTH_SCOPE,
                },
                timeout=HTTP_TIMEOUT,
            )
            ms_access_token = str(token_data.get("access_token", ""))
            refreshed_token = str(token_data.get("refresh_token", refresh_token))
            if not ms_access_token:
                return cached_profile if cached_token else None
            mc_data = self._authenticate_xbox_and_minecraft(ms_access_token, fetch_profile=False)
            refreshed_access_token = mc_data["access_token"]
        except Exception as error:
            self.logger.warning("No se pudo refrescar la sesion Microsoft: %s", error)
            return cached_profile if cached_token else None

        profile = PremiumProfile(
            name=saved_meta.name,
            username=saved_meta.username,
            uuid=saved_meta.uuid,
            token=str(refreshed_access_token),
        )
        if not profile.token:
            return cached_profile if cached_token else None
        try:
            _run_daemon(keyring.set_password, OPERATION_TIMEOUT, KEYRING_SERVICE, KEYRING_ACCOUNT, str(refreshed_token))
            _run_daemon(
                keyring.set_password,
                OPERATION_TIMEOUT,
                KEYRING_SERVICE,
                KEYRING_ACCESS_ACCOUNT,
                str(refreshed_access_token),
            )
        except Exception as error:
            self.logger.warning("No se pudo actualizar los tokens en keyring: %s", error)
        self.logger.info("Sesion Microsoft refrescada exitosamente para %s", profile.username)
        return profile

    def complete_microsoft_login(
        self,
        client_id: str,
        redirect_uri: str,
        auth_code: str,
        code_verifier: str = "",
    ) -> PremiumProfile:
        del code_verifier
        effective_client_id = MICROSOFT_CLIENT_ID
        effective_redirect_uri = redirect_uri or MICROSOFT_REDIRECT_URI

        token_data = _http_post_form(
            LIVE_TOKEN_URL,
            {
                "client_id": effective_client_id,
                "code": auth_code,
                "grant_type": "authorization_code",
                "redirect_uri": effective_redirect_uri,
                "scope": LIVE_OAUTH_SCOPE,
            },
            timeout=HTTP_TIMEOUT,
        )
        ms_access_token = str(token_data.get("access_token", ""))
        ms_refresh_token = str(token_data.get("refresh_token", ""))
        if not ms_access_token or not ms_refresh_token:
            raise RuntimeError("Microsoft Live no devolvio tokens de acceso validos.")

        mc_login = self._authenticate_xbox_and_minecraft(ms_access_token, fetch_profile=True)
        mc_login["refresh_token"] = ms_refresh_token
        return self.save_microsoft_login(mc_login)

    def logout_microsoft(self) -> None:
        """Cierra la sesion Microsoft eliminando el perfil local y los tokens del baul del sistema."""
        try:
            if PREMIUM_PROFILE_FILE.exists():
                PREMIUM_PROFILE_FILE.unlink()
        except OSError as error:
            self.logger.warning("No se pudo eliminar el archivo de sesion Microsoft: %s", error)
        for account_key in (KEYRING_ACCOUNT, KEYRING_ACCESS_ACCOUNT):
            try:
                _run_daemon(keyring.delete_password, OPERATION_TIMEOUT, KEYRING_SERVICE, account_key)
            except Exception:
                pass
        self.logger.info("Sesion Microsoft cerrada")

    def get_active_profile(self, username: str) -> OfflineProfile | PremiumProfile:
        clean_user = username.strip().casefold()
        data = load_json(PREMIUM_PROFILE_FILE, None)
        if isinstance(data, dict) and str(data.get("username", "")).strip().casefold() == clean_user:
            premium = self.get_premium_profile()
            if premium:
                return premium
        return self.get_or_create(username)
