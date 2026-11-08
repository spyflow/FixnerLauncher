"""Configuracion del logging de FixnerLauncher."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def configure_logging(log_file: Path) -> logging.Logger:
    """Configura logging en consola y archivo, devolviendo el logger principal."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("fixner")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if sys.stderr is not None:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    return logger
