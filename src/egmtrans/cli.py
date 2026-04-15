"""Command-line interface — process_file, main, and helpers.

``process_file`` is the primary public entry point used by both the CLI and
the ArcGIS Pro toolbox.  It validates inputs, checks for datum mismatches
(with interactive prompts in CLI mode), and delegates to
:func:`~egmtrans.transform.transform_vertical_datum`.

``main`` provides the argparse-based CLI and handles single-file or
batch-directory processing.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time

from osgeo import gdal

from egmtrans import _state
from egmtrans.arcpy_compat import init_arcpy
from egmtrans.config import (
    DATUM_MAPPING,
    DTED_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    verify_grids,
)
from egmtrans.crs import standardize_srs
from egmtrans.download import ensure_grids
from egmtrans.file_utils import copy_folder_structure, is_valid_dem, is_valid_filename
from egmtrans.logging_setup import end_logger, setup_logger
from egmtrans.numba_utils import NUMBA_AVAILABLE
from egmtrans.transform import transform_vertical_datum


def log_numba_availability() -> None:
    """Log whether Numba is available for JIT compilation."""
    logger = _state.get_logger()
    if NUMBA_AVAILABLE:
        msg = (
            "Python's Numba library is available. "
            "Flat and ocean patches will be processed in parallel for maximum speed."
        )
    else:
        msg = (
            "Python's Numba library is not available. "
            "Flattening and interpolation will be MUCH slower (20-50 times!).\n"
            "NGA recommends using a custom ArcGIS Pro environment with Numba installed for faster execution."
        )
    logger.info(msg)


def str2bool(v: str | bool | None) -> bool:
    """Convert various string representations of boolean values to actual booleans.

    Accepts ``'yes'``/``'no'``, ``'true'``/``'false'``, ``'t'``/``'f'``,
    ``'y'``/``'n'``, ``'1'``/``'0'``, ``True``/``False``, and ``None``
    (returns False).  Used by argparse and interactive prompts.

    Raises:
        argparse.ArgumentTypeError: If the input cannot be interpreted.
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def delete_output_directory(output_dir: str, max_retries: int = 3, retry_delay: float = 1.0) -> bool:
    """Safely delete an output directory with retry mechanism.

    Shuts down the logger first so file handles are released, then retries
    deletion up to *max_retries* times with a delay between attempts (GDAL
    and ArcPy may not release handles immediately).

    Returns:
        True if deletion succeeded, False if all attempts failed.
    """
    logger = _state.get_logger()
    end_logger()

    for attempt in range(max_retries):
        try:
            time.sleep(retry_delay)
            shutil.rmtree(output_dir)
            print("Output directory was deleted.")
            return True
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} of {max_retries} failed: Could not delete output directory: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)

    logger.error(f"All {max_retries} attempts to delete the output directory failed.")
    return False


def process_file(
    input_file: str,
    output_file: str,
    source_datum: str,
    target_datum: str,
    flatten: bool,
    create_mask: bool,
    min_patch_size: int,
    algorithm: str,
    abs_horiz_accuracy: int | None = None,
    save_log: bool = True,
    check_for_wrong_datum: bool = True,
    arc_mode: bool = False,
) -> bool | None:
    """Process a single file for vertical datum transformation.

    Performs comprehensive validation before calling
    :func:`~egmtrans.transform.transform_vertical_datum`:

    1. Validates file format and accessibility.
    2. Checks datum compatibility (DTED cannot target WGS84, etc.).
    3. Verifies the file's CRS/header matches the stated source datum;
       prompts the user (CLI) or logs a warning (ArcGIS) on mismatch.
    4. Disables flattening for WGS84 transforms (orthometric-only operation).
    5. Handles same-datum copies (optimised GeoTIFF with compound CRS).

    Args:
        input_file: Path to the input DEM.
        output_file: Path for the transformed output.
        source_datum: Source vertical datum.
        target_datum: Target vertical datum.
        flatten: Whether to retain flat areas.
        create_mask: Whether to create a flat-area mask.
        min_patch_size: Minimum flat-area size in pixels.
        algorithm: Interpolation algorithm name.
        abs_horiz_accuracy: Fallback horizontal accuracy for DTED output.
        save_log: Whether to save the log file.
        check_for_wrong_datum: Whether to verify datum consistency with file header.
        arc_mode: Whether to use ArcPy processing path.

    Returns:
        ``True`` if a datum mismatch was acknowledged and processing continued,
        ``False`` if the transformation was aborted or failed, or ``None`` if
        processing completed normally.
    """
    logger = _state.get_logger()

    if len(logger.handlers) == 1 and isinstance(logger.handlers[0], logging.NullHandler):
        setup_logger(is_arc_mode=arc_mode)

    _state.set_arc_mode(arc_mode)
    if arc_mode:
        init_arcpy()
    else:
        if not NUMBA_AVAILABLE:
            logger.warning("Numba is not available. Processing will be slower.")

    if arc_mode:
        log_numba_availability()

    verify_grids(source_datum, target_datum)

    if not is_valid_dem(input_file):
        logger.error(f"Skipping {os.path.basename(input_file)} as it's not a DEM.\n")
        return False

    input_is_dted = input_file.lower().endswith(DTED_EXTENSIONS)
    output_is_dted = output_file.lower().endswith(DTED_EXTENSIONS)
    ignore_wrong_datum = False

    if not any(input_file.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
        logger.error(
            f"Unsupported input file format. Supported formats are: "
            f"{', '.join(SUPPORTED_EXTENSIONS)}\nAborting transformation."
        )
        return False

    if output_is_dted and not input_is_dted:
        logger.error('DTED files can only be created from other DTED files.\nAborting transformation.')
        return False

    if output_is_dted and target_datum == 'WGS84':
        logger.error('DTED data can only be in EGM2008 or EGM96, not WGS84.\nAborting transformation.')
        return False

    if create_mask and not flatten:
        logger.error(
            f"To create a mask of flat areas, you must also set "
            f"{'Retain Flat Areas' if arc_mode else '--flatten'} to True.\nAborting transformation."
        )
        return False

    input_ds = gdal.Open(input_file, gdal.GA_ReadOnly)
    if input_ds is None:
        logger.error(f'Failed to open input file: {input_file}')
        return False

    metadata = input_ds.GetMetadata()
    projection = input_ds.GetProjection()
    logger.debug(f"Input file's projection: {projection}")
    src_srs = standardize_srs(projection)
    logger.debug(f"Input file's SRS: {src_srs}")

    file_datum = None
    if input_is_dted:
        file_datum = metadata.get('DTED_VerticalDatum')
        file_datum = 'EGM96' if file_datum in ('E96', 'MSL') else 'EGM2008' if file_datum == 'E08' else None
    else:
        file_datum = src_srs.GetAttrValue('VERT_CS')
    logger.info(f"Input file header's vertical datum: {file_datum}")

    if file_datum and source_datum not in file_datum and check_for_wrong_datum:
        logger.info(
            f"The input file's vertical datum ({file_datum}) does not match "
            f"the specified source datum ({source_datum})."
        )
        if arc_mode:
            logger.warning(f"Ignoring the input file's vertical datum, using {source_datum} instead.")
            logger.warning(
                f"If {source_datum} is incorrect, delete {output_file} and "
                f"try again with the correct datum: {file_datum}."
            )
        else:
            user_input = input("Do you wish to proceed and ignore the input file's vertical datum? (yes/no): ").strip()
            if not str2bool(user_input):
                logger.error("Aborting transformation.")
                return False
            logger.info(f"Ignoring the input file's vertical datum, using {source_datum} instead.")

        ignore_wrong_datum = True
    else:
        ignore_wrong_datum = True

    if (source_datum == 'WGS84' or target_datum == 'WGS84') and flatten:
        logger.info("Flattening is not supported for WGS84 ellipsoid height transforms. Proceeding without flattening.")
        flatten = False

    if source_datum == target_datum:
        if not input_is_dted:
            logger.warning(
                "Source and target vertical datums are the same. "
                "No vertical transformation or flattening will occur."
            )
            logger.warning(
                "This operation will create an optimized GeoTIFF copy rounded to 1 cm, "
                "with Compound CRS and DEFLATE compression."
            )
            if arc_mode:
                logger.info("Proceeding to create GeoTIFF copy with Compound CRS metadata and optimized compression.")
            else:
                user_input = input("Do you wish to proceed? (yes/no): ").strip()
                if not str2bool(user_input):
                    logger.error("Aborting transformation.")
                    return False
                logger.info("Proceeding to create GeoTIFF copy with Compound CRS metadata and optimized compression.")
        else:
            logger.error("Source and target vertical datums are the same.\nAborting transformation.")
            return False

    try:
        transform_vertical_datum(
            input_file, output_file, source_datum, target_datum,
            flatten, create_mask, min_patch_size, algorithm,
            abs_horiz_accuracy, save_log,
        )
    except Exception as e:
        logger.error(f"Transformation failed: {e}.")
        return False

    return ignore_wrong_datum


def main() -> None:
    """CLI entry point for EGMTrans.

    Parses command-line arguments, sets up logging, and processes either a
    single file or an entire directory tree.  For batch processing, copies
    the input folder structure first, then transforms each supported DEM in
    place.  Datum-mismatch confirmation is requested once and applied to all
    subsequent files.
    """
    parser = argparse.ArgumentParser(
        description="Transform vertical datum between WGS 84 ellipsoid, EGM96, and EGM2008 for DTED and GeoTIFF files."
    )
    parser.add_argument("-i", "--input", required=True, help="Input file or folder containing DTED files")
    parser.add_argument("-o", "--output", required=True, help="Output file or folder for transformed DTED files")
    parser.add_argument("-s", "--source_datum", required=True, help="Source vertical datum")
    parser.add_argument("-t", "--target_datum", required=True, help="Target vertical datum")
    parser.add_argument(
        "-f", "--flatten", required=False, type=str2bool, nargs='?', const=True, default=True,
        help="Retain flat areas (default: True)",
    )
    parser.add_argument(
        "-m", "--create_mask", required=False, type=str2bool, nargs='?', const=True, default=False,
        help="Create a mask of ocean and, if GeoTIFF, other flat areas (default: False)",
    )
    parser.add_argument(
        "-p", "--min_patch_size", required=False, type=int, nargs='?', const=True, default=16,
        help="Minimum patch size in pixels for flat areas (default: 16)",
    )
    parser.add_argument(
        "-a", "--algorithm", required=False, choices=['bilinear', 'delaunay', 'spline', 'proj'],
        nargs='?', const=True, default='bilinear',
        help="Interpolation algorithm (default: bilinear)",
    )
    parser.add_argument(
        "--abs_horiz_accuracy", required=False, type=int,
        help="Absolute horizontal accuracy in meters (for DTED output only)",
    )
    parser.add_argument(
        "-l", "--log_file", required=False, type=str2bool, nargs='?', const=True, default=True,
        help="Save a log file (default: True)",
    )

    args = parser.parse_args()

    for item in list(DATUM_MAPPING.keys()):
        if args.source_datum.upper() in item:
            args.source_datum = item
        if args.target_datum.upper() in item:
            args.target_datum = item

    input_is_file = os.path.isfile(args.input)
    output_is_file = (
        any(args.output.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS)
        and is_valid_filename(os.path.basename(args.output))
    )
    input_is_folder = os.path.isdir(args.input)
    output_is_folder = os.path.isdir(args.output) or is_valid_filename(os.path.basename(args.output))

    log_path = None
    if args.log_file:
        if output_is_folder:
            log_name = f"{os.path.basename(os.path.normpath(args.output))}_transform.log"
            log_path = os.path.join(args.output, log_name)
        elif output_is_file:
            base, _ = os.path.splitext(args.output)
            log_path = f"{base}_transform.log"

    logger = setup_logger(log_path, args.log_file, False)

    args_list = list(vars(args).items())
    for i, (arg, value) in enumerate(args_list):
        if isinstance(value, str):
            value = value.replace('\\\\', '\\')
        if i == len(args_list) - 1:
            logger.info(f"Argument - {arg}: {value}\n\n")
        else:
            logger.info(f"Argument - {arg}: {value}")

    try:
        downloaded = ensure_grids(message_func=logger.info)
        if downloaded:
            logger.info(f"Downloaded {len(downloaded)} geoid grid file(s).\n")
    except Exception as e:
        logger.error(
            f"Failed to download geoid grid files: {e}\n"
            f"Download manually from: "
            f"https://github.com/ngageoint/EGMTrans/releases/tag/datum-grids-v1\n"
            f"Place the .tif files in the datums/ folder."
        )
        end_logger()
        sys.exit(1)

    if input_is_file and output_is_file:
        logger.info(f"Processing file: {args.output}")
        process_file(
            args.input, args.output, args.source_datum, args.target_datum,
            args.flatten, args.create_mask, args.min_patch_size, args.algorithm,
            args.abs_horiz_accuracy, args.log_file,
        )
    elif input_is_file and output_is_folder:
        output_file = os.path.join(args.output, os.path.basename(args.input))
        logger.info(f"Processing file: {output_file}")
        process_file(
            args.input, output_file, args.source_datum, args.target_datum,
            args.flatten, args.create_mask, args.min_patch_size, args.algorithm,
            args.abs_horiz_accuracy, args.log_file,
        )
    elif input_is_folder and output_is_folder:
        copy_folder_structure(args.input, args.output)
        ignore_wrong_datum = False
        files_processed = False
        process_complete = True

        for root, _, files in os.walk(args.input):
            for file in files:
                if file.lower().endswith(SUPPORTED_EXTENSIONS):
                    input_file = os.path.join(root, file)
                    relative_path = os.path.relpath(input_file, args.input)
                    output_file = os.path.join(args.output, relative_path)
                    logger.info(f"Processing file: {output_file}")
                    try:
                        if not ignore_wrong_datum:
                            result = process_file(
                                input_file, output_file, args.source_datum, args.target_datum,
                                args.flatten, args.create_mask, args.min_patch_size, args.algorithm,
                                args.abs_horiz_accuracy, args.log_file, not ignore_wrong_datum,
                            )
                            if result is True:
                                ignore_wrong_datum = True
                                files_processed = True
                            elif result is False:
                                files_processed = False
                                process_complete = False
                                break
                        else:
                            process_file(
                                input_file, output_file, args.source_datum, args.target_datum,
                                args.flatten, args.create_mask, args.min_patch_size, args.algorithm,
                                args.abs_horiz_accuracy, args.log_file,
                                check_for_wrong_datum=False,
                            )
                            files_processed = True
                    except Exception as e:
                        logger.info(f"Error processing {input_file}: {str(e)}")
            if not process_complete:
                break

        if not files_processed:
            logger.info(
                f"NOTE: The files in {args.input} were copied to output directory "
                f"{args.output} but not transformed."
            )
            if _state.get_arc_mode():
                success = delete_output_directory(args.output, 3, 1.0)
                if success:
                    logger.info("Output directory deleted successfully.")
                else:
                    logger.error("Failed to delete output directory.")
            else:
                user_input = input("Do you wish to delete the output directory? (yes/no): ").strip()
                if str2bool(user_input):
                    success = delete_output_directory(args.output, 3, 1.0)
                    if success:
                        print("Output directory deleted successfully.")
                        logger.info("Output directory deleted successfully.")
                    else:
                        print("Failed to delete output directory.")
                        logger.info("Failed to delete output directory.")
                else:
                    print("Output directory with copied files was retained, but files were not transformed.")
                    logger.info(
                        "Output directory with copied files was retained, but files were not transformed."
                    )
    else:
        logger.error("Input and output must both be files or both be folders.")
        end_logger()
        sys.exit(1)

    logger.info("Processing completed.")
    end_logger(save_log=args.log_file)
