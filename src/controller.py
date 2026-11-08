"""Controlador Qt, bandeja del sistema y monitor de Minecraft."""

from __future__ import annotations

import gc
import logging
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap, QPixmapCache
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from config.settings import APP_VERSION, LAUNCHER_SETTINGS_FILE, load_json, save_json
from src.ui.main_window import MainWindow
from src.workers.tasks import GameCrashMonitorWorker


class AppController:
    """Mantiene la UI y el monitor de juego desacoplados."""

    def __init__(self, app: QApplication, game_directory: Path, logger: logging.Logger) -> None:
        self.app = app
        self.game_directory = game_directory
        self.logger = logger
        self.window: MainWindow | None = None
        self.monitor: GameCrashMonitorWorker | None = None
        self.game_pid: int | None = None
        self.ui_handed_off_to_tray = False

        self.trim_timer = QTimer(app)
        self.trim_timer.setInterval(1200)
        self.trim_timer.timeout.connect(self._periodic_tray_trim)

        settings_data = load_json(LAUNCHER_SETTINGS_FILE, {})
        self.reopen_on_exit = (
            bool(settings_data.get("reopen_on_exit", True))
            if isinstance(settings_data, dict)
            else True
        )

        self.tray = QSystemTrayIcon(self._build_tray_icon(), app)
        self.tray.activated.connect(self._tray_activated)
        mc_font = QFont("Minecraft", 10)
        menu = QMenu()
        menu.setFont(mc_font)
        open_action = QAction("Abrir FixnerLauncher", menu)
        open_action.setFont(mc_font)
        open_action.triggered.connect(self.open_ui)
        menu.addAction(open_action)
        self.reopen_action = QAction("Reabrir launcher al cerrar el juego", menu)
        self.reopen_action.setFont(mc_font)
        self.reopen_action.setCheckable(True)
        self.reopen_action.setChecked(self.reopen_on_exit)
        self.reopen_action.toggled.connect(self._set_reopen_on_exit)
        menu.addAction(self.reopen_action)
        self.status_action = QAction("Estado: listo", menu)
        self.status_action.setFont(mc_font)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()
        quit_action = QAction("Salir", menu)
        quit_action.setFont(mc_font)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip(f"FixnerLauncher v{APP_VERSION}")
        self.tray.show()
        self.open_ui()

    @staticmethod
    def _build_tray_icon() -> QIcon:
        """Genera un icono ligero sin depender de un asset externo."""
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor("#11191A"))
        painter = QPainter(pixmap)
        painter.fillRect(4, 4, 24, 24, QColor("#3C8527"))
        painter.fillRect(8, 8, 16, 5, QColor("#FFFFFF"))
        painter.fillRect(8, 13, 5, 11, QColor("#FFFFFF"))
        painter.fillRect(13, 18, 8, 5, QColor("#FFFFFF"))
        painter.end()
        return QIcon(pixmap)

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_ui()

    def _set_reopen_on_exit(self, checked: bool) -> None:
        self.reopen_on_exit = bool(checked)
        settings_data = load_json(LAUNCHER_SETTINGS_FILE, {})
        if not isinstance(settings_data, dict):
            settings_data = {}
        settings_data["reopen_on_exit"] = self.reopen_on_exit
        save_json(LAUNCHER_SETTINGS_FILE, settings_data)
        self.reopen_action.blockSignals(True)
        self.reopen_action.setChecked(self.reopen_on_exit)
        self.reopen_action.blockSignals(False)
        if self.window:
            self.window.set_reopen_checkbox(self.reopen_on_exit)

    def _on_window_minimized_to_tray(self) -> None:
        self.tray.showMessage(
            "FixnerLauncher",
            "El launcher sigue abierto en la bandeja del sistema.",
            QSystemTrayIcon.MessageIcon.Information,
            2500,
        )
        QTimer.singleShot(50, self._release_window_to_tray)

    def _release_window_to_tray(self) -> None:
        """Destruye los widgets de la ventana cuando pasa a bandeja y compacta la RAM a ~2 MB."""
        if self.window:
            if self.window.worker and self.window.worker.isRunning():
                self._trim_os_working_set()
                return
            window = self.window
            self.window = None
            window.destroy_for_handoff()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QPixmapCache.clear()
        gc.collect()
        self._trim_os_working_set()
        QTimer.singleShot(350, self._trim_os_working_set)
        if not self.trim_timer.isActive():
            self.trim_timer.start()

    def _periodic_tray_trim(self) -> None:
        self._trim_os_working_set()

    def _quit(self) -> None:
        self.trim_timer.stop()
        if self.monitor and self.monitor.isRunning():
            self.monitor.requestInterruption()
        self.tray.hide()
        self.app.quit()

    def open_ui(self) -> None:
        if self.window is None:
            self.window = MainWindow(self.game_directory, self.logger)
            self.window.launch_requested.connect(self.on_game_started)
            self.window.minimized_to_tray.connect(self._on_window_minimized_to_tray)
            self.window.reopen_on_exit_changed.connect(self._set_reopen_on_exit)
            self.window.set_reopen_checkbox(self.reopen_on_exit)
        self.window.showNormal()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        QTimer.singleShot(600, self._trim_os_working_set)
        if not self.trim_timer.isActive():
            self.trim_timer.start()

    def on_game_started(self, _version: str, _username: str, pid: int) -> None:
        self.logger.info("Monitor iniciado para Minecraft PID %s", pid)
        self.game_pid = pid
        self.ui_handed_off_to_tray = False
        self.status_action.setText("Estado: Minecraft iniciando")
        self._attach_monitor(pid)
        QTimer.singleShot(5000, lambda: self._confirm_game_started(pid))

    def _confirm_game_started(self, pid: int) -> None:
        import psutil

        if self.game_pid != pid or not psutil.pid_exists(pid):
            return
        self.ui_handed_off_to_tray = True
        self._release_window_to_tray()
        self._detach_windows_console()
        self.status_action.setText("Estado: Minecraft ejecutandose")

    def _attach_monitor(self, pid: int) -> None:
        import psutil
        if self.monitor and self.monitor.isRunning():
            return
        process = psutil.Process(pid)
        self.monitor = GameCrashMonitorWorker(process, self.game_directory)
        self.monitor.finished_clean.connect(self._game_closed)
        self.monitor.crashed.connect(self._game_crashed)
        self.monitor.start()

    def _game_closed(self) -> None:
        self.game_pid = None
        self.status_action.setText("Estado: listo")
        if self.ui_handed_off_to_tray:
            if self.reopen_on_exit:
                self.open_ui()
        else:
            self.tray.showMessage(
                "Minecraft no inicio",
                "El proceso se cerro antes de completar el arranque.",
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )
            self.open_ui()

    def _game_crashed(self, code: int, report: str) -> None:
        self.game_pid = None
        self.status_action.setText("Estado: crash detectado")
        self.tray.showMessage(
            "Minecraft se cerro inesperadamente",
            f"Codigo de salida: {code}",
            QSystemTrayIcon.MessageIcon.Critical,
            5000,
        )
        self.open_ui()
        QMessageBox.critical(self.window, "Reporte de crash", f"Codigo de salida: {code}\n\n{report}")

    @staticmethod
    def _detach_windows_console() -> None:
        """Libera la consola solo cuando la UI ya fue entregada al tray."""
        import sys

        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.FreeConsole()

    @staticmethod
    def _trim_os_working_set() -> None:
        """Compacta el heap C y recorta el Working Set en Windows (~1.5 - 2 MB en el Administrador de tareas)."""
        import sys

        if sys.platform != "win32":
            return
        try:
            import ctypes

            try:
                ctypes.cdll.msvcrt._heapmin()
            except Exception:
                pass
            handle = ctypes.c_void_p(-1)
            ctypes.windll.psapi.EmptyWorkingSet(handle)
            ctypes.windll.kernel32.SetProcessWorkingSetSizeEx(
                handle,
                ctypes.c_size_t(-1),
                ctypes.c_size_t(-1),
                0,
            )
        except Exception:
            pass
