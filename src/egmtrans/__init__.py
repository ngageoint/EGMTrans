"""EGMTrans — Vertical datum transformation tool for DEMs."""

from egmtrans._version import __version__
from egmtrans.cli import process_file, str2bool
from egmtrans.config import DATUM_MAPPING, DTED_EXTENSIONS, SUPPORTED_EXTENSIONS, configure_gdal
from egmtrans.file_utils import copy_folder_structure, is_valid_dem, is_valid_filename
from egmtrans.logging_setup import end_logger, setup_logger
from egmtrans.transform import transform_vertical_datum

__all__ = [
    "__version__",
    "SUPPORTED_EXTENSIONS",
    "DTED_EXTENSIONS",
    "DATUM_MAPPING",
    "configure_gdal",
    "setup_logger",
    "end_logger",
    "process_file",
    "str2bool",
    "copy_folder_structure",
    "is_valid_filename",
    "is_valid_dem",
    "transform_vertical_datum",
]

configure_gdal()
