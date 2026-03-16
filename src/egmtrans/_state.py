"""Mutable runtime state — replaces module-level globals from the monolithic script.

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
    """Return the ``arcpy`` module, or *None* if it has not been initialised."""
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
