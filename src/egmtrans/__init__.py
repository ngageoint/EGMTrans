"""EGMTrans — Vertical datum transformation tool for DEMs."""

from egmtrans._version import __version__
from egmtrans.cli import process_file, str2bool
from egmtrans.config import (
    DATUM_MAPPING,
    DTED_EXTENSIONS,
    DTED_NODATA,
    SUPPORTED_EXTENSIONS,
    configure_gdal,
)
from egmtrans.file_utils import (
    IOPaths,
    copy_as_writable,
    copy_folder_structure,
    derive_log_path,
    ensure_writable,
    is_valid_dem,
    is_valid_filename,
    prepare_output_target,
    resolve_io_paths,
)
from egmtrans.logging_setup import end_logger, setup_logger
from egmtrans.transform import transform_vertical_datum

__all__ = [
    "__version__",
    "SUPPORTED_EXTENSIONS",
    "DTED_EXTENSIONS",
    "DTED_NODATA",
    "DATUM_MAPPING",
    "configure_gdal",
    "setup_logger",
    "end_logger",
    "process_file",
    "str2bool",
    "IOPaths",
    "resolve_io_paths",
    "derive_log_path",
    "prepare_output_target",
    "ensure_writable",
    "copy_as_writable",
    "copy_folder_structure",
    "is_valid_filename",
    "is_valid_dem",
    "transform_vertical_datum",
]

configure_gdal()
