"""EGMTrans: vertical datum transformation tool for DEMs."""

from egmtrans._version import __version__
from egmtrans.batch import BatchResult, run_batch
from egmtrans.cli import process_file, str2bool, versions_line
from egmtrans.config import (
    DATUM_MAPPING,
    DTED_EXTENSIONS,
    DTED_NODATA,
    DTED_ROOT,
    SUPPORTED_EXTENSIONS,
    configure_gdal,
    dted_target_problem,
)
from egmtrans.dted.writer import HEADER_FIELD_NAMES, DtedMetadataSource, parse_overrides
from egmtrans.file_utils import (
    DEFAULT_DTED_NAMING,
    DTED_NAMING_PRESETS,
    IOPaths,
    copy_as_writable,
    copy_folder_structure,
    dem_problem,
    derive_log_path,
    dted_naming_template,
    dted_output_name,
    ensure_writable,
    find_dems,
    folder_within,
    is_valid_dem,
    is_valid_filename,
    mask_output_name,
    prepare_output_target,
    resolve_io_paths,
)
from egmtrans.logging_setup import end_logger, setup_logger
from egmtrans.tiling import TileLevels
from egmtrans.transform import transform_vertical_datum

__all__ = [
    "__version__",
    "SUPPORTED_EXTENSIONS",
    "DTED_EXTENSIONS",
    "DTED_NODATA",
    "DTED_ROOT",
    "DATUM_MAPPING",
    "DEFAULT_DTED_NAMING",
    "DTED_NAMING_PRESETS",
    "configure_gdal",
    "dted_target_problem",
    "dted_output_name",
    "mask_output_name",
    "dem_problem",
    "folder_within",
    "setup_logger",
    "end_logger",
    "process_file",
    "run_batch",
    "BatchResult",
    "DtedMetadataSource",
    "HEADER_FIELD_NAMES",
    "parse_overrides",
    "TileLevels",
    "str2bool",
    "IOPaths",
    "resolve_io_paths",
    "dted_naming_template",
    "versions_line",
    "derive_log_path",
    "prepare_output_target",
    "ensure_writable",
    "copy_as_writable",
    "copy_folder_structure",
    "find_dems",
    "is_valid_filename",
    "is_valid_dem",
    "transform_vertical_datum",
]

configure_gdal()
