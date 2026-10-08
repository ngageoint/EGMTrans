"""Logging configuration: ArcpyLogHandler, setup_logger, end_logger, the
PROGRESS level, and the lines that go to the log file only."""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import traceback

from egmtrans import _state
from egmtrans._version import __version__

# Between INFO and WARNING: the one line per cell and the pass summaries that
# a large batch still shows in the ArcGIS Pro messages pane when the INFO
# detail is kept to the log file (see _state.set_quiet).
PROGRESS = 25
logging.addLevelName(PROGRESS, 'PROGRESS')


def progress(message: str) -> None:
    """Log *message* at the PROGRESS level."""
    _state.get_logger().log(PROGRESS, message)


class ArcpyLogHandler(logging.Handler):
    """A custom logging handler that redirects log messages to arcpy.

    In quiet mode (a batch of many cells) INFO lines are dropped here and kept
    in the log file; PROGRESS lines, warnings and errors still reach the pane.
    """

    def emit(self, record):
        arcpy = _state.get_arcpy()
        if arcpy:
            if _state.get_quiet() and record.levelno < PROGRESS:
                return
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


def _file_handlers(logger: logging.Logger) -> list[logging.FileHandler]:
    return [handler for handler in logger.handlers if isinstance(handler, logging.FileHandler)]


def log_to_file_only(message: str, level: int = logging.INFO) -> None:
    """Write *message* to the log file(s) only, not to the terminal or the messages pane."""
    logger = _state.get_logger()
    record = logger.makeRecord(logger.name, level, __file__, 0, message, (), None)
    for handler in _file_handlers(logger):
        handler.handle(record)


def log_traceback() -> None:
    """Write the current exception's traceback to the log file(s) only, so the
    terminal and the ArcGIS Pro messages keep their one-line error while the
    log holds what a developer needs."""
    text = traceback.format_exc()
    if text and text.strip() != 'NoneType: None':
        log_to_file_only('Traceback (for the record):\n' + text, logging.ERROR)


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
    # The path is process state: a run that opens no log file must not leave
    # the previous run's path behind for end_logger to delete.
    _state.set_log_file_path(None)
    _state.set_quiet(False)

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
        # Logging must never be able to damage the run. This block used to
        # makedirs() unconditionally, which created a directory at the output
        # file's path whenever the caller derived the log path incorrectly.
        try:
            log_dir = os.path.dirname(log_file)
            if log_dir:
                if os.path.isfile(log_dir):
                    raise NotADirectoryError(f'{log_dir} is a file, not a directory')
                os.makedirs(log_dir, exist_ok=True)
            # Append, UTF-8: a rerun's log follows the first run's SHA-256
            # lines instead of erasing them, and a path character outside the
            # system code page no longer drops its line on Windows.
            existed = os.path.isfile(log_file) and os.path.getsize(log_file) > 0
            file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        except OSError as e:
            logger.warning(f'Could not open log file {log_file}: {e}. Continuing without one.')
        else:
            _state.set_log_file_path(log_file)
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            stamp = dt.datetime.now().astimezone().isoformat(timespec='seconds')
            banner = f'{"=" * 80}\nEGMTrans {__version__} run started {stamp}\n{"=" * 80}'
            log_to_file_only(('\n' if existed else '') + banner)

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
