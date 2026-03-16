"""Logging configuration — ArcpyLogHandler, setup_logger, end_logger."""

from __future__ import annotations

import logging
import os
import sys

from egmtrans import _state


class ArcpyLogHandler(logging.Handler):
    """A custom logging handler that redirects log messages to arcpy."""

    def emit(self, record):
        arcpy = _state.get_arcpy()
        if arcpy:
            try:
                msg = self.format(record)
                if record.levelno >= logging.ERROR:
                    arcpy.AddError(f'ERROR: {msg}')
                elif record.levelno >= logging.WARNING:
                    arcpy.AddWarning(f'WARNING: {msg}')
                else:
                    arcpy.AddMessage(msg)
            except Exception:
                sys.stderr.write(f"ArcpyLogHandler Error: {self.format(record)}\n")


def setup_logger(
    log_file: str | None = None,
    save_log: bool = True,
    is_arc_mode: bool = False,
) -> logging.Logger:
    """Configure and return the ``egmtrans`` logger.

    Args:
        log_file: Full path to the log file.
        save_log: If True, a log file will be created.
        is_arc_mode: If True, configures logging for the ArcGIS environment.
    """
    logger = _state.get_logger()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter('%(message)s')

    if is_arc_mode:
        handler = ArcpyLogHandler()
        handler.setFormatter(formatter)
        handler.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    if save_log and log_file:
        _state.set_log_file_path(log_file)
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode='w')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logging.getLogger('numba').setLevel(logging.WARNING)
    return logger


def end_logger(log_file: str | None = None, save_log: bool = False) -> None:
    """Shutdown the logger and optionally delete the log file.

    Args:
        log_file: Path to the log file to delete. Falls back to the stored path.
        save_log: If True, the log file will not be deleted in arc mode.
    """
    logger = _state.get_logger()
    logging.shutdown()

    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())

    log_file = log_file or _state.get_log_file_path()

    if _state.get_arc_mode() and not save_log and log_file and os.path.exists(log_file):
        try:
            os.remove(log_file)
        except OSError as e:
            print(f"Could not remove log file: {e}")
