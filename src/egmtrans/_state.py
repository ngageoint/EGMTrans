"""Mutable runtime state: replaces module-level globals from the monolithic script.

The original EGMTrans.py relied on ``global`` variables for the logger, arcpy
module, arc-mode flag, and log-file path.  This module provides getter/setter
access to those values so that every other module can read or modify shared
state without circular imports or ``global`` declarations.
"""

from __future__ import annotations

import logging

_arc_mode: bool = False
_arcpy = None
_log_file_path: str | None = None


def get_arc_mode() -> bool:
    """Return True when running inside ArcGIS Pro."""
    return _arc_mode


def set_arc_mode(value: bool) -> None:
    """Enable or disable ArcGIS Pro mode."""
    global _arc_mode
    _arc_mode = value


def get_arcpy():
    """Return the ``arcpy`` module, or *None* if it has not been initialized."""
    return _arcpy


def set_arcpy(module) -> None:
    """Store the ``arcpy`` module reference after a successful import."""
    global _arcpy
    _arcpy = module


def get_log_file_path() -> str | None:
    """Return the current log-file path, or *None* if unset."""
    return _log_file_path


def set_log_file_path(path: str | None) -> None:
    """Set the path used by :func:`end_logger` to clean up the log file."""
    global _log_file_path
    _log_file_path = path


def get_logger() -> logging.Logger:
    """Return the shared ``egmtrans`` logger instance."""
    return logging.getLogger("egmtrans")


_quiet: bool = False


def get_quiet() -> bool:
    """True while a large batch keeps the ArcGIS Pro messages pane to progress lines and warnings."""
    return _quiet


def set_quiet(value: bool) -> None:
    """Set or clear the quiet mode of the ArcGIS Pro log handler."""
    global _quiet
    _quiet = value


_header_warnings: dict[str, list[str]] | None = None


def begin_header_warnings() -> None:
    """Start collecting the per-cell DTED header warnings of a batch for one summary at its end."""
    global _header_warnings
    _header_warnings = {}


def tally_header_warning(message: str, cell: str) -> bool:
    """Record a header warning of *cell* when a tally is active; True when it was recorded."""
    if _header_warnings is None:
        return False
    cells = _header_warnings.setdefault(message, [])
    if cell not in cells:
        cells.append(cell)
    return True


def end_header_warnings() -> dict[str, list[str]]:
    """Stop collecting and return the tally, message to cells in order of first appearance."""
    global _header_warnings
    tally, _header_warnings = _header_warnings or {}, None
    return tally
