"""Biblioteca local Qt y dialogo dedicado para agregar versiones."""

from __future__ import annotations

import logging
import os
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap, QPixmapCache
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QProgressBar, QSpinBox, QVBoxLayout, QWidget,
)

from core.java_manager import JavaManager
from core.auth_manager import AuthManager
from core.minecraft_manager import MinecraftManager
from core.optifine_manager import OptiFineManager
from core.sodium_manager import SodiumManager
from core.runner import MinecraftRunner
from config.settings import (
    APP_VERSION,
    LAUNCHER_SETTINGS_FILE,
    MICROSOFT_CLIENT_ID,
    MICROSOFT_REDIRECT_URI,
    get_resource_path,
    load_json,
    save_json,
)
from src.workers.tasks import TaskThread


class MicrosoftLoginDialog(QDialog):
    """Dialog OAuth2 que captura el redirect sin pedir credenciales al launcher."""

    login_completed = Signal(object)

    def __init__(self, auth: AuthManager, parent: QWidget | None = None) -> None:
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        # Inicializar sin widget padre directo evita que QWebEngineView fuerce a Qt
        # a destruir y recrear el HWND nativo de MainWindow (parpadeo/cierre de 1s).
        super().__init__(None)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.auth = auth
        self.client_id = MICROSOFT_CLIENT_ID
        self.worker: TaskThread | None = None
        self._cleaned_up = False

        self.setWindowTitle("Iniciar sesion con Microsoft")
        self.resize(760, 640)
        if parent is not None:
            parent_geo = parent.frameGeometry()
            self.move(
                parent_geo.center().x() - self.width() // 2,
                parent_geo.center().y() - self.height() // 2,
            )
        layout = QVBoxLayout(self)
        self.status = QLabel("Preparando inicio de sesion...", objectName="Muted")
        layout.addWidget(self.status)

        self.web_profile = QWebEngineProfile(self)
        self.web_page = QWebEnginePage(self.web_profile, self)
        self.browser = QWebEngineView(self)
        self.browser.setPage(self.web_page)

        self._disable_passkey_prompt()
        layout.addWidget(self.browser, 1)
        self.browser.urlChanged.connect(self._url_changed)

        parameters = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": MICROSOFT_REDIRECT_URI,
            "scope": "service::user.auth.xboxlive.com::MBI_SSL",
            "prompt": "select_account",
        }
        login_url = f"https://login.live.com/oauth20_authorize.srf?{urllib.parse.urlencode(parameters)}"
        self.browser.setUrl(QUrl(login_url))

    def _disable_passkey_prompt(self) -> None:
        """Evita que WebAuthn abra el popup nativo de huella/PIN/passkey."""
        from PySide6.QtWebEngineCore import QWebEngineScript

        script = QWebEngineScript()
        script.setName("disable-fixner-passkeys")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(
            """
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
        )
        self.web_profile.scripts().insert(script)

    def _url_changed(self, url: QUrl) -> None:
        if self._cleaned_up:
            return
        address = url.toString()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(address).query)
        if "error" in query:
            error_code = query.get("error", [""])[0]
            error_desc = query.get("error_description", [error_code])[0]
            self.status.setText(f"No se pudo iniciar sesion ({error_code}): {error_desc}")
            return
        if "code" not in query:
            return
        auth_code = query.get("code", [""])[0]
        if not auth_code:
            self.status.setText("Microsoft no devolvio un codigo de autorizacion.")
            return

        # Oculta el aviso anti-phishing de oauth20_desktop.srf mientras valida con Xbox/Minecraft
        self.browser.stop()
        self.browser.setHtml(
            "<body style='background:#121212;color:#aaa;font-family:sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:90vh;'>"
            "<h3>Conectando con Xbox Live y Minecraft...</h3></body>"
        )
        self.browser.setEnabled(False)
        self.status.setText("Validando cuenta con Xbox y Minecraft Services...")

        self.worker = TaskThread(
            self.auth.complete_microsoft_login,
            self.client_id,
            MICROSOFT_REDIRECT_URI,
            auth_code,
        )
        self.worker.succeeded.connect(self._completed)
        self.worker.failed.connect(lambda error: self.status.setText(f"No se pudo iniciar sesion: {error}"))
        self.worker.start()

    def _completed(self, profile: object) -> None:
        self.login_completed.emit(profile)
        self.accept()

    def _cleanup_browser(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        try:
            self.browser.urlChanged.disconnect(self._url_changed)
        except Exception:
            pass
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.quit()
        self.browser.stop()
        self.browser.setPage(None)
        self.web_page.deleteLater()
        self.web_profile.deleteLater()
        self.browser.deleteLater()

    def reject(self) -> None:
        self._cleanup_browser()
        super().reject()

    def accept(self) -> None:
        self._cleanup_browser()
        super().accept()

    def closeEvent(self, event) -> None:
        self._cleanup_browser()
        super().closeEvent(event)

class ImageHero(QFrame):
    """Hero cinematografico con la imagen de portada del launcher."""

    def __init__(self, image_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.image = QPixmap(str(image_path))
        self.setObjectName("ImageHero")

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        width, height = self.width(), self.height()
        if not self.image.isNull():
            scaled = self.image.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (scaled.width() - width) // 2
            y = (scaled.height() - height) // 2
            painter.drawPixmap(0, 0, scaled, x, y, width, height)
        else:
            painter.fillRect(self.rect(), Qt.GlobalColor.black)
        painter.end()


class AddVersionDialog(QDialog):
    """Carga el catalogo remoto solo dentro del flujo de agregar."""

    install_requested = Signal(str, str, object)

    def __init__(
        self,
        manager: MinecraftManager,
        optifine: OptiFineManager,
        sodium: SodiumManager,
        parent: QWidget,
        initial_variant: str = "Vanilla",
    ) -> None:
        super().__init__(parent)
        self.manager = manager
        self.optifine = optifine
        self.sodium = sodium
        self.worker: TaskThread | None = None
        self._query_token: int = 0
        self.optifine_versions: list[dict[str, Any]] = []
        self.sodium_versions: list[dict[str, Any]] = []
        self.setWindowTitle("Agregar una version")
        self.setMinimumSize(520, 300)
        self._build_ui(initial_variant)
        self._load_catalog()

    def _build_ui(self, initial_variant: str = "Vanilla") -> None:
        layout = QVBoxLayout(self)
        title = QLabel("Agregar a tu biblioteca")
        title.setStyleSheet("font-size: 24px; font-weight: 800;")
        layout.addWidget(title)
        layout.addWidget(QLabel("La version se instalara en el directorio aislado.", objectName="Muted"))
        self.variant = QComboBox()
        self.variant.addItems(["Vanilla", "OptiFine", "Sodium"])
        self.variant.setCurrentText(initial_variant)
        self.variant.currentTextChanged.connect(self._variant_changed)
        layout.addWidget(QLabel("Variante", objectName="Muted"))
        layout.addWidget(self.variant)
        layout.addWidget(QLabel("Version base", objectName="Muted"))
        self.version = QComboBox()
        self.version.setEnabled(False)
        self.version.currentTextChanged.connect(self._base_changed)
        layout.addWidget(self.version)
        has_edition = initial_variant in ("OptiFine", "Sodium")
        label_text = "Edicion Sodium (Fabric)" if initial_variant == "Sodium" else "Edicion OptiFine"
        self.edition_label = QLabel(label_text, objectName="Muted")
        self.edition_label.setVisible(has_edition)
        layout.addWidget(self.edition_label)
        self.edition = QComboBox()
        self.edition.setVisible(has_edition)
        layout.addWidget(self.edition)
        self.status = QLabel("Consultando catalogo oficial...", objectName="Muted")
        layout.addWidget(self.status)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.add = buttons.addButton("Agregar version", QDialogButtonBox.ButtonRole.AcceptRole)
        self.add.setObjectName("Primary")
        self.add.setEnabled(False)
        buttons.accepted.connect(self._install)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_catalog(self) -> None:
        self.worker = TaskThread(self.manager.list_releases)
        self.worker.succeeded.connect(self._catalog_loaded)
        self.worker.failed.connect(lambda error: self.status.setText(f"Error: {error}"))
        self.worker.start()

    def _catalog_loaded(self, versions: list[dict[str, Any]]) -> None:
        self.version.blockSignals(True)
        self.version.addItems([str(item["id"]) for item in versions])
        self.version.blockSignals(False)
        self.version.setEnabled(True)
        current_variant = self.variant.currentText()
        if current_variant in ("OptiFine", "Sodium"):
            self._variant_changed(current_variant)
        else:
            self.add.setEnabled(bool(versions))
            self.status.setText(f"{len(versions)} versiones disponibles")

    def _variant_changed(self, variant: str) -> None:
        self._query_token += 1
        has_edition = variant in ("OptiFine", "Sodium")
        self.edition_label.setVisible(has_edition)
        self.edition.setVisible(has_edition)
        self.optifine_versions = []
        self.sodium_versions = []
        base_version = self.version.currentText()
        if variant == "OptiFine":
            self.edition_label.setText("Edicion OptiFine")
            if base_version:
                self._load_optifine(base_version)
        elif variant == "Sodium":
            self.edition_label.setText("Edicion Sodium (Fabric)")
            if base_version:
                self._load_sodium(base_version)
        else:
            self.add.setEnabled(bool(base_version))
            self.status.setText("Selecciona una version para agregar Vanilla.")

    def _base_changed(self, version: str) -> None:
        variant = self.variant.currentText()
        if variant == "OptiFine" and version:
            self._load_optifine(version)
        elif variant == "Sodium" and version:
            self._load_sodium(version)

    def _load_optifine(self, version: str) -> None:
        self._query_token += 1
        token = self._query_token
        self.add.setEnabled(False)
        self.edition.clear()
        self.status.setText("Consultando ediciones OptiFine...")
        worker = TaskThread(lambda: self.optifine.list_versions(version))
        worker.succeeded.connect(lambda versions: self._optifine_loaded(token, versions))
        worker.failed.connect(
            lambda error: self.status.setText(f"OptiFine no disponible: {error}")
            if token == self._query_token
            else None
        )
        self.worker = worker
        worker.start()

    def _optifine_loaded(self, token: int, versions: list[dict[str, Any]]) -> None:
        if token != self._query_token:
            return
        self.optifine_versions = versions
        self.edition.clear()
        self.edition.addItems([str(item.get("filename", item.get("patch", "Edicion"))) for item in versions])
        self.add.setEnabled(bool(versions))
        self.status.setText(f"{len(versions)} ediciones disponibles")

    def _load_sodium(self, version: str) -> None:
        self._query_token += 1
        token = self._query_token
        self.add.setEnabled(False)
        self.edition.clear()
        self.status.setText("Consultando ediciones de Sodium (Modrinth + Fabric)...")
        worker = TaskThread(lambda: self.sodium.list_versions(version))
        worker.succeeded.connect(lambda versions: self._sodium_loaded(token, version, versions))
        worker.failed.connect(
            lambda error: self.status.setText(f"Sodium no disponible: {error}")
            if token == self._query_token
            else None
        )
        self.worker = worker
        worker.start()

    def _sodium_loaded(self, token: int, mc_version: str, versions: list[dict[str, Any]]) -> None:
        if token != self._query_token:
            return
        self.sodium_versions = versions
        self.edition.clear()
        self.edition.addItems([str(item.get("filename", item.get("version_number", "Sodium"))) for item in versions])
        self.add.setEnabled(bool(versions))
        if versions:
            self.status.setText(
                f"{len(versions)} version(es) de Sodium · Carpeta aislada: mods/{mc_version}/"
            )
        else:
            self.status.setText(f"No hay Sodium disponible para Minecraft {mc_version}.")

    def _install(self) -> None:
        version = self.version.currentText()
        variant = self.variant.currentText()
        if variant == "OptiFine":
            index = self.edition.currentIndex()
            if 0 <= index < len(self.optifine_versions):
                self.install_requested.emit(version, "OptiFine", self.optifine_versions[index])
        elif variant == "Sodium":
            index = self.edition.currentIndex()
            if 0 <= index < len(self.sodium_versions):
                self.install_requested.emit(version, "Sodium", self.sodium_versions[index])
        else:
            self.install_requested.emit(version, "Vanilla", {})
        self.accept()


class MainWindow(QMainWindow):
    """Launcher centrado en las instalaciones locales del usuario."""

    launch_requested = Signal(str, str, int)
    progress_changed = Signal(str, float)
    minimized_to_tray = Signal()
    reopen_on_exit_changed = Signal(bool)

    def __init__(self, game_directory: Path, logger: logging.Logger) -> None:
        super().__init__()
        self.game_directory = game_directory
        self.logger = logger
        self.manager = MinecraftManager(game_directory, logger)
        self.auth = AuthManager(logger)
        self.runner = MinecraftRunner(game_directory, logger)
        java_mgr = JavaManager(game_directory, logger)
        self.optifine = OptiFineManager(game_directory, self.manager, java_mgr, logger)
        self.sodium = SodiumManager(game_directory, self.manager, java_mgr, logger)
        self.installed: list[dict[str, str]] = []
        self.worker: TaskThread | None = None
        self._destroying_for_handoff = False
        self.setWindowTitle(f"FixnerLauncher v{APP_VERSION}")
        self.resize(1180, 760)
        self._build_ui()
        self.progress_changed.connect(self._show_progress)
        self.refresh_library()
        self._load_saved_account()

    def _build_ui(self) -> None:
        self._build_hero_ui()
        return

    def _build_hero_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(20, 18, 20, 18)
        root_layout.setSpacing(14)
        topbar = QHBoxLayout()
        topbar.addWidget(QLabel("FIXNER", objectName="Brand"))
        topbar.addWidget(QLabel(f"  /  PLAY  ·  v{APP_VERSION}", objectName="Muted"))
        topbar.addStretch()
        self.library_count = QLabel("", objectName="Muted")
        topbar.addWidget(self.library_count)
        self.add_button = QPushButton("＋  Agregar")
        self.add_button.setObjectName("Primary")
        self._add_menu()
        topbar.addWidget(self.add_button)
        self.login_button = QPushButton("◉  Iniciar sesion")
        self.login_button.clicked.connect(self._handle_microsoft_button)
        topbar.addWidget(self.login_button)
        root_layout.addLayout(topbar)

        image_path = get_resource_path("launcherb.jpg")
        hero = ImageHero(image_path)
        self.hero_frame = hero
        hero.setMinimumHeight(430)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(58, 42, 58, 42)
        hero_layout.addStretch(1)
        eyebrow = QLabel("FIXNER LAUNCHER")
        eyebrow.setObjectName("HeroEyebrow")
        hero_layout.addWidget(eyebrow, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.hero_title = QLabel("MINECRAFT")
        self.hero_title.setObjectName("HeroTitle")
        hero_layout.addWidget(self.hero_title, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.hero_status = QLabel("")
        self.hero_status.setObjectName("HeroSubtitle")
        self.hero_status.setVisible(False)
        selector_container = QWidget()
        selector_container.setFixedSize(310, 46)
        selector_row = QHBoxLayout(selector_container)
        selector_row.setContentsMargins(0, 0, 0, 0)
        selector_row.setSpacing(6)
        self.selector = QComboBox()
        self.selector.setObjectName("HeroSelector")
        self.selector.setFixedSize(310, 46)
        self.selector.currentIndexChanged.connect(self._selector_changed)
        selector_row.addWidget(self.selector)
        self.mods_folder_button = QPushButton()
        self.mods_folder_button.setObjectName("ModsFolderButton")
        self.mods_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mods_folder_button.setFixedSize(46, 46)
        self.mods_folder_button.setIcon(self._build_folder_icon())
        self.mods_folder_button.setIconSize(QSize(22, 22))
        self.mods_folder_button.setToolTip("Abrir carpeta de mods de esta versión")
        self.mods_folder_button.setVisible(False)
        self.mods_folder_button.clicked.connect(self._open_selected_mods_folder)
        selector_row.addWidget(self.mods_folder_button)
        hero_layout.addWidget(selector_container, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.play_button = QPushButton("▶   JUGAR")
        self.play_button.setObjectName("PlayHero")
        self.play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_button.setMinimumSize(250, 58)
        self.play_button.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.play_button.clicked.connect(self._launch)
        hero_layout.addWidget(self.play_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        hero_layout.addStretch(1)
        root_layout.addWidget(hero, 1)

        lower = QHBoxLayout()
        lower.setSpacing(14)
        profile = QFrame(objectName="Details")
        profile_layout = QVBoxLayout(profile)
        self.profile_mode_label = QLabel("PERFIL OFFLINE", objectName="Muted")
        profile_layout.addWidget(self.profile_mode_label)
        self.username = QLineEdit("Player")
        self.username.textChanged.connect(self._update_account_indicator)
        profile_layout.addWidget(self.username)
        lower.addWidget(profile, 1)
        memory_panel = QFrame(objectName="Details")
        memory_layout = QVBoxLayout(memory_panel)
        memory_layout.addWidget(QLabel("MEMORIA RAM", objectName="Muted"))
        self.memory = QSpinBox()
        self.memory.setRange(512, 32768)
        self.memory.setSingleStep(512)
        self.memory.setValue(4096)
        self.memory.setSuffix(" MB")
        memory_layout.addWidget(self.memory)
        lower.addWidget(memory_panel, 1)
        actions = QFrame(objectName="Details")
        actions_layout = QVBoxLayout(actions)
        self.detail_variant = QLabel("", objectName="Muted")
        self.detail_variant.setVisible(False)
        settings_data = load_json(LAUNCHER_SETTINGS_FILE, {})
        reopen_enabled = bool(settings_data.get("reopen_on_exit", True)) if isinstance(settings_data, dict) else True
        self.reopen_checkbox = QCheckBox("Reabrir launcher al cerrar el juego")
        self.reopen_checkbox.setChecked(reopen_enabled)
        self.reopen_checkbox.toggled.connect(self._on_reopen_toggled)
        actions_layout.addWidget(self.reopen_checkbox)
        self.remove_button = QPushButton("Eliminar seleccionada")
        self.remove_button.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.remove_button.clicked.connect(self._remove_selected)
        actions_layout.addWidget(self.remove_button)
        lower.addWidget(actions, 1)
        root_layout.addLayout(lower)
        self.status = QLabel("Listo", objectName="Muted")
        root_layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root_layout.addWidget(self.progress)
        self.search = QLineEdit()
        self.search.setVisible(False)
        self.filter = QComboBox()
        self.filter.addItems(["Todas", "Vanilla", "OptiFine", "Sodium"])
        self.filter.setVisible(False)
        self.version_list = QListWidget()
        self.version_list.setVisible(False)
        self.setCentralWidget(root)

    def _legacy_build_ui(self) -> None:
        """Layout anterior conservado temporalmente para compatibilidad interna."""
        content = QWidget()
        main = QVBoxLayout(content)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Mis instalaciones")
        title.setStyleSheet("font-size: 32px; font-weight: 800;")
        title_box.addWidget(title)
        self.subtitle = QLabel("Versiones listas para jugar en este equipo.", objectName="Muted")
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch()
        refresh = QPushButton("↻  Actualizar")
        refresh.clicked.connect(self.refresh_library)
        header.addWidget(refresh)
        self.add_button = QPushButton("＋  Agregar version")
        self.add_button.setObjectName("Primary")
        self._add_menu()
        header.addWidget(self.add_button)
        main.addLayout(header)
        hero = QFrame(objectName="Hero")
        hero_layout = QVBoxLayout(hero)
        self.hero_title = QLabel("Selecciona una instalacion")
        self.hero_title.setStyleSheet("font-size: 26px; font-weight: 800;")
        hero_layout.addWidget(self.hero_title)
        self.hero_status = QLabel("Tu biblioteca local esta vacia.", objectName="Muted")
        hero_layout.addWidget(self.hero_status)
        main.addWidget(hero)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar entre tus instalaciones...")
        self.search.textChanged.connect(self._render_library)
        filters.addWidget(self.search)
        self.filter = QComboBox()
        self.filter.addItems(["Todas", "Vanilla", "OptiFine"])
        self.filter.currentTextChanged.connect(self._render_library)
        filters.addWidget(self.filter)
        main.addLayout(filters)
        self.version_list = QListWidget()
        self.version_list.currentItemChanged.connect(self._version_selected)
        main.addWidget(self.version_list, 1)
        self.status = QLabel("Listo", objectName="Muted")
        main.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        main.addWidget(self.progress)
        root_layout.addWidget(content, 1)
        root_layout.addWidget(self._details())
        self.setCentralWidget(root)

    def _sidebar(self) -> QFrame:
        sidebar = QFrame(objectName="Sidebar")
        sidebar.setFixedWidth(205)
        layout = QVBoxLayout(sidebar)
        layout.addWidget(QLabel("FIXNER\nMINECRAFT LAUNCHER", objectName="Brand"))
        layout.addSpacing(28)
        navigation = (
            ("▶  Inicio", self.refresh_library),
            ("▦  Mis instalaciones", self.refresh_library),
            ("⚙  Ajustes", self._open_settings),
            ("◉  Cuenta", self._open_account),
        )
        for label, action in navigation:
            button = QPushButton(label)
            button.setToolTip(label)
            button.clicked.connect(action)
            layout.addWidget(button)
        layout.addStretch()
        layout.addWidget(QLabel("●  Directorio aislado", objectName="Muted"))
        layout.addWidget(QLabel(f"v{APP_VERSION} / Qt6", objectName="Muted"))
        return sidebar

    def _details(self) -> QFrame:
        panel = QFrame(objectName="Details")
        panel.setFixedWidth(270)
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("DETALLES", objectName="Muted"))
        self.detail_title = QLabel("Sin seleccion")
        self.detail_title.setStyleSheet("font-size: 20px; font-weight: 800;")
        layout.addWidget(self.detail_title)
        self.detail_variant = QLabel("Selecciona una instalacion para ver sus acciones.", objectName="Muted")
        self.detail_variant.setWordWrap(True)
        layout.addWidget(self.detail_variant)
        layout.addSpacing(22)
        layout.addWidget(QLabel("PERFIL OFFLINE", objectName="Muted"))
        self.username = QLineEdit("Player")
        layout.addWidget(self.username)
        layout.addWidget(QLabel("MEMORIA RAM", objectName="Muted"))
        self.memory = QSpinBox()
        self.memory.setRange(512, 32768)
        self.memory.setSingleStep(512)
        self.memory.setValue(4096)
        self.memory.setSuffix(" MB")
        layout.addWidget(self.memory)
        layout.addStretch()
        play = QPushButton("▶  JUGAR")
        play.setObjectName("Primary")
        play.clicked.connect(self._launch)
        layout.addWidget(play)
        remove = QPushButton("Eliminar instalacion")
        remove.clicked.connect(self._remove_selected)
        layout.addWidget(remove)
        return panel

    def _add_menu(self) -> None:
        menu = QMenu(self)
        vanilla = QAction("Vanilla oficial", menu)
        optifine = QAction("OptiFine", menu)
        sodium = QAction("Sodium (Fabric)", menu)
        vanilla.triggered.connect(lambda: self._open_add_dialog("Vanilla"))
        optifine.triggered.connect(lambda: self._open_add_dialog("OptiFine"))
        sodium.triggered.connect(lambda: self._open_add_dialog("Sodium"))
        menu.addAction(vanilla)
        menu.addAction(optifine)
        menu.addAction(sodium)
        self.add_button.setMenu(menu)

    def refresh_library(
        self,
        select_target: tuple[str, str, str] | None = None,
    ) -> None:
        self.installed = self._scan_installed()
        self._render_library(select_target=select_target)
        self.library_count.setText(f"{len(self.installed)} instalacion(es)")
        if not self.installed:
            self.hero_status.setText("Tu biblioteca esta vacia · usa Agregar para comenzar")

    def _scan_installed(self) -> list[dict[str, str]]:
        versions_dir = self.game_directory / "versions"
        if not versions_dir.exists():
            return []
        entries: list[dict[str, str]] = []
        for directory in sorted(versions_dir.iterdir(), reverse=True):
            json_file = directory / f"{directory.name}.json"
            if not json_file.exists():
                continue
            if "OptiFine" in directory.name:
                variant = "OptiFine"
                base = directory.name.split("-OptiFine", 1)[0]
            elif directory.name.startswith("fabric-loader-"):
                variant = "Sodium"
                profile_data = load_json(json_file, {})
                base = (
                    str(profile_data.get("inheritsFrom", ""))
                    if isinstance(profile_data, dict) and profile_data.get("inheritsFrom")
                    else directory.name.rsplit("-", 1)[-1]
                )
            else:
                variant = "Vanilla"
                base = directory.name
            entries.append({"id": directory.name, "base": base, "variant": variant})
        return entries

    def _render_library(
        self,
        select_target: tuple[str, str, str] | None = None,
    ) -> None:
        query = self.search.text().casefold()
        selected = self._selected_id()
        target_base, target_variant, target_id = select_target if select_target else ("", "", "")
        self.selector.blockSignals(True)
        self.selector.clear()
        matched_index = -1
        for entry in self.installed:
            if self.filter.currentText() != "Todas" and entry["variant"] != self.filter.currentText():
                continue
            if query and query not in entry["id"].casefold():
                continue
            self.selector.addItem(f"{entry['base']}   ·   {entry['variant']}", entry["id"])
            current_idx = self.selector.count() - 1
            if select_target:
                if (target_id and entry["id"] == target_id) or (
                    entry["base"] == target_base and entry["variant"] == target_variant
                ):
                    matched_index = current_idx
            elif entry["id"] == selected:
                matched_index = current_idx
        if matched_index >= 0:
            self.selector.setCurrentIndex(matched_index)
        self.selector.blockSignals(False)
        self._selector_changed(self.selector.currentIndex())

    def _selected_id(self) -> str:
        value = self.selector.currentData()
        return str(value) if value else ""

    @staticmethod
    def _build_folder_icon() -> QIcon:
        """Dibuja un icono de carpeta pixel-art para el boton de mods."""
        pixmap = QPixmap(22, 22)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.fillRect(2, 4, 8, 2, QColor("#F5C242"))
        painter.fillRect(1, 6, 20, 13, QColor("#8C6210"))
        painter.fillRect(2, 6, 18, 11, QColor("#E5A922"))
        painter.fillRect(2, 8, 18, 9, QColor("#F5C242"))
        painter.fillRect(2, 8, 18, 2, QColor("#FFE48F"))
        painter.end()
        return QIcon(pixmap)

    def _open_selected_mods_folder(self) -> None:
        version_id = self._selected_id()
        entry = next((value for value in self.installed if value["id"] == version_id), None)
        if not entry or entry["variant"] != "Sodium":
            return
        mods_dir = self.sodium.get_version_mods_directory(entry["base"])
        if os.name == "nt":
            os.startfile(str(mods_dir))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(mods_dir)))

    def _selector_changed(self, _index: int) -> None:
        version_id = self._selected_id()
        entry = next((value for value in self.installed if value["id"] == version_id), None)
        if not entry:
            self.hero_title.setText("MINECRAFT")
            if hasattr(self, "mods_folder_button"):
                self.mods_folder_button.setVisible(False)
                self.selector.setFixedSize(310, 46)
            return
        self.hero_title.setText(f"MINECRAFT {entry['base']}")
        if entry["variant"] == "Sodium":
            if hasattr(self, "mods_folder_button"):
                self.selector.setFixedSize(258, 46)
                self.mods_folder_button.setToolTip(
                    f"Abrir carpeta de mods de Minecraft {entry['base']} (mods/{entry['base']})"
                )
                self.mods_folder_button.setVisible(True)
        else:
            if hasattr(self, "mods_folder_button"):
                self.mods_folder_button.setVisible(False)
                self.selector.setFixedSize(310, 46)

    def _version_selected(self, item: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        version_id = str(item.data(Qt.ItemDataRole.UserRole)) if item and item.data(Qt.ItemDataRole.UserRole) else ""
        entry = next((value for value in self.installed if value["id"] == version_id), None)
        if entry:
            self.hero_title.setText(f"Minecraft {entry['base']}")
            self.hero_status.setText(f"{entry['variant']} · Instalacion local lista")
            self.detail_title.setText(entry["base"])
            self.detail_variant.setText(f"ID: {entry['id']}\nVariante: {entry['variant']}\nDirectorio aislado")

    def _open_add_dialog(self, variant: str = "Vanilla") -> None:
        dialog = AddVersionDialog(self.manager, self.optifine, self.sodium, self, initial_variant=variant)
        dialog.install_requested.connect(self._install_new_version)
        dialog.exec()

    def _open_account(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Cuenta offline")
        dialog.setMinimumWidth(420)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Perfil offline", objectName="Muted"))
        layout.addWidget(QLabel("No se solicitan credenciales ni contrasenas."))
        username = QLineEdit(self.username.text())
        username.setPlaceholderText("3-16 caracteres alfanumericos")
        layout.addWidget(username)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        save = buttons.addButton("Guardar perfil", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("Primary")
        buttons.accepted.connect(lambda: self._save_account(dialog, username.text()))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _save_account(self, dialog: QDialog, username: str) -> None:
        try:
            AuthManager(self.logger).get_or_create(username)
        except ValueError as error:
            QMessageBox.warning(dialog, "Cuenta offline", str(error))
            return
        self.username.setText(username.strip())
        dialog.accept()
        self.status.setText(f"Perfil offline activo: {username.strip()}")

    def _handle_microsoft_button(self) -> None:
        if self.auth.get_saved_premium_profile():
            self._logout_microsoft()
        else:
            self._open_microsoft_login()

    def _logout_microsoft(self) -> None:
        self.auth.logout_microsoft()
        self.username.setReadOnly(False)
        offline_profiles = self.auth.list_profiles()
        fallback_name = offline_profiles[-1].username if offline_profiles else "Player"
        self.username.setText(fallback_name)
        self._update_account_indicator(fallback_name)
        self.status.setText(f"Sesion Microsoft cerrada · Perfil offline activo: {fallback_name}")

    def _open_microsoft_login(self) -> None:
        dialog = MicrosoftLoginDialog(self.auth, self)
        dialog.login_completed.connect(self._microsoft_logged_in)
        result = dialog.exec()
        if result == QDialog.DialogCode.Rejected:
            self.status.setText("Inicio de sesion cancelado: no se completo la autenticacion Microsoft.")
            QMessageBox.warning(
                self,
                "Inicio de sesion cancelado",
                "La ventana de Microsoft se cerro antes de completar la autenticacion.",
            )

    def _load_saved_account(self) -> None:
        """Carga automaticamente la cuenta Microsoft guardada en disco al abrir la ventana."""
        saved_premium = self.auth.get_saved_premium_profile()
        if saved_premium and saved_premium.username:
            self.username.setText(saved_premium.username)
            self._update_account_indicator(saved_premium.username)
            self.status.setText(f"Sesion Microsoft activa (Online): {saved_premium.username}")
        else:
            self._update_account_indicator(self.username.text())

    def _update_account_indicator(self, current_text: str) -> None:
        """Actualiza la etiqueta del perfil y el boton de sesion segun el estado de la cuenta."""
        saved_premium = self.auth.get_saved_premium_profile()
        clean_current = current_text.strip().casefold()
        if saved_premium and saved_premium.username.strip().casefold() == clean_current:
            self.profile_mode_label.setText("CUENTA MICROSOFT (ONLINE)")
            self.username.setReadOnly(True)
            self.login_button.setText("◉  Cerrar sesion")
            self.login_button.setToolTip(f"Cerrar sesion de {saved_premium.username}")
        else:
            self.profile_mode_label.setText("PERFIL OFFLINE")
            self.username.setReadOnly(False)
            self.login_button.setText("◉  Iniciar sesion")
            self.login_button.setToolTip("Iniciar sesion con Microsoft")

    def _microsoft_logged_in(self, profile: object) -> None:
        username = str(getattr(profile, "username", ""))
        if username:
            self.username.setText(username)
            self._update_account_indicator(username)
            self.status.setText(f"Sesion Microsoft activa (Online): {username}")

    def _open_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Ajustes del launcher")
        dialog.setMinimumWidth(460)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Directorio de juego", objectName="Muted"))
        directory = QLineEdit(str(self.game_directory))
        directory.setReadOnly(True)
        layout.addWidget(directory)
        layout.addWidget(QLabel("Memoria RAM predeterminada", objectName="Muted"))
        memory = QSpinBox()
        memory.setRange(512, 32768)
        memory.setSingleStep(512)
        memory.setValue(self.memory.value())
        memory.setSuffix(" MB")
        layout.addWidget(memory)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(lambda: dialog.accept())
        layout.addWidget(buttons)
        dialog.exec()
        self.memory.setValue(memory.value())

    def _set_installing_state(self, installing: bool, tooltip: str = "") -> None:
        if hasattr(self, "play_button"):
            self.play_button.setEnabled(not installing)
            self.play_button.setToolTip(tooltip if installing else "")
            self.play_button.setCursor(
                Qt.CursorShape.ForbiddenCursor
                if installing
                else Qt.CursorShape.PointingHandCursor
            )
        if hasattr(self, "remove_button"):
            self.remove_button.setEnabled(not installing)
            self.remove_button.setToolTip(tooltip if installing else "")
            self.remove_button.setCursor(
                Qt.CursorShape.ForbiddenCursor
                if installing
                else Qt.CursorShape.ArrowCursor
            )

    def _install_new_version(self, version: str, variant: str, variant_version: dict[str, Any]) -> None:
        self.progress.setVisible(True)
        installing_msg = f"Instalando {variant} {version}..."
        self.status.setText(installing_msg)
        self._set_installing_state(True, installing_msg)
        progress = lambda status, value: self.progress_changed.emit(status, value)
        if variant == "OptiFine":
            task = lambda: self.optifine.install_version(version, variant_version, progress)
        elif variant == "Sodium":
            task = lambda: self.sodium.install_version(version, variant_version, progress)
        else:
            task = lambda: self.manager.install_version(version, progress)
        self._run_task(task, lambda result: self._installation_done(version, variant, result))

    def _installation_done(self, version: str, variant: str, installed_id: object = None) -> None:
        self._set_installing_state(False)
        self.progress.setVisible(False)
        if variant == "Sodium":
            self.status.setText(f"Sodium {version} instalado correctamente en mods/{version}/.")
        else:
            self.status.setText(f"{variant} {version} instalada correctamente.")
        target_id = str(installed_id) if isinstance(installed_id, str) else ""
        self.refresh_library(select_target=(version, variant, target_id))

    def _launch(self) -> None:
        version_id = self._selected_id()
        if not version_id:
            QMessageBox.information(self, "Biblioteca", "Selecciona una instalacion primero.")
            return
        entry = next(item for item in self.installed if item["id"] == version_id)
        username = self.username.text()
        memory = self.memory.value()
        self.status.setText("Iniciando Minecraft...")
        self._run_task(lambda: self.runner.launch(entry["base"], username, memory, entry["variant"]), lambda process: self.launch_requested.emit(entry["base"], username, process.pid))

    def _remove_selected(self) -> None:
        version_id = self._selected_id()
        if not version_id:
            return
        entry = next((item for item in self.installed if item["id"] == version_id), None)
        if QMessageBox.question(self, "Eliminar instalacion", f"Eliminar {version_id}?") != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(self.game_directory / "versions" / version_id, ignore_errors=True)
        if entry and entry["variant"] == "Sodium":
            mods_dir = self.game_directory / "mods" / entry["base"]
            if mods_dir.exists():
                for jar in mods_dir.glob("sodium*.jar"):
                    try:
                        jar.unlink()
                    except OSError:
                        pass
                try:
                    if not any(mods_dir.iterdir()):
                        mods_dir.rmdir()
                except OSError:
                    pass
        self.refresh_library()

    def _show_progress(self, status: str, value: float) -> None:
        self.status.setText(status)
        self.progress.setValue(int(min(100, value)))

    def _run_task(self, task: Any, callback: Any) -> None:
        if self.worker and self.worker.isRunning():
            self.status.setText("Ya hay una operacion en curso...")
            return
        self.worker = TaskThread(task)
        self.worker.succeeded.connect(callback)
        self.worker.failed.connect(self._operation_failed)
        self.worker.start()

    def _operation_failed(self, error: str) -> None:
        self._set_installing_state(False)
        self.progress.setVisible(False)
        self.status.setText("La operacion fallo.")
        QMessageBox.critical(self, "FixnerLauncher", error)

    def _on_reopen_toggled(self, checked: bool) -> None:
        settings_data = load_json(LAUNCHER_SETTINGS_FILE, {})
        if not isinstance(settings_data, dict):
            settings_data = {}
        settings_data["reopen_on_exit"] = bool(checked)
        save_json(LAUNCHER_SETTINGS_FILE, settings_data)
        self.reopen_on_exit_changed.emit(bool(checked))

    def set_reopen_checkbox(self, checked: bool) -> None:
        if hasattr(self, "reopen_checkbox"):
            self.reopen_checkbox.blockSignals(True)
            self.reopen_checkbox.setChecked(bool(checked))
            self.reopen_checkbox.blockSignals(False)

    def destroy_for_handoff(self) -> None:
        """Destruye la ventana y sus pixmaps al pasar a modo bandeja."""
        self._destroying_for_handoff = True
        if hasattr(self, "hero_frame") and self.hero_frame is not None:
            self.hero_frame.image = QPixmap()
        QPixmapCache.clear()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.close()

    def closeEvent(self, event: Any) -> None:
        """Oculta la ventana; el proceso permanece disponible desde la bandeja."""
        if self._destroying_for_handoff:
            event.accept()
            return
        self.hide()
        self.status.setText("FixnerLauncher sigue activo en la bandeja.")
        self.minimized_to_tray.emit()
        event.ignore()
