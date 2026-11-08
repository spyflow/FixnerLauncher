"""Workers Qt para no bloquear el hilo de la interfaz."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal

_ACTIVE_THREADS: set[QThread] = set()


class TaskThread(QThread):
    """Ejecuta una tarea Python en un QThread y emite su resultado."""

    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str, float)

    def __init__(self, task: Callable[..., Any], *args: Any) -> None:
        super().__init__()
        self.task = task
        self.args = args
        self.finished.connect(self._cleanup_ref)

    def start(self, priority: QThread.Priority = QThread.Priority.InheritPriority) -> None:
        _ACTIVE_THREADS.add(self)
        super().start(priority)

    def _cleanup_ref(self) -> None:
        _ACTIVE_THREADS.discard(self)

    def run(self) -> None:
        try:
            result = self.task(*self.args)
            self.succeeded.emit(result)
        except Exception as error:
            self.failed.emit(str(error))


class GameCrashMonitorWorker(QThread):
    """Supervisa el proceso de Minecraft y detecta cierres anormales."""

    finished_clean = Signal()
    crashed = Signal(int, str)

    def __init__(self, process: Any, game_directory: Any) -> None:
        super().__init__()
        self.process = process
        self.game_directory = game_directory
        self.finished.connect(self._cleanup_ref)

    def start(self, priority: QThread.Priority = QThread.Priority.InheritPriority) -> None:
        _ACTIVE_THREADS.add(self)
        super().start(priority)

    def _cleanup_ref(self) -> None:
        _ACTIVE_THREADS.discard(self)

    def run(self) -> None:
        try:
            import psutil
            while not self.isInterruptionRequested():
                try:
                    exit_code = self.process.wait(timeout=1.5)
                    break
                except psutil.TimeoutExpired:
                    continue
            else:
                return
        except psutil.NoSuchProcess:
            exit_code = -1
        if exit_code == 0:
            self.finished_clean.emit()
            return
        report = self._latest_report()
        self.crashed.emit(exit_code, report)

    def _latest_report(self) -> str:
        candidates = list((self.game_directory / "crash-reports").glob("*.txt"))
        candidates.extend((self.game_directory / "logs").glob("latest.log"))
        if not candidates:
            return "No se encontro un reporte de crash."
        latest = max(candidates, key=lambda path: path.stat().st_mtime)
        try:
            return latest.read_text(encoding="utf-8", errors="replace")[-6000:]
        except OSError as error:
            return f"No se pudo leer el reporte: {error}"
