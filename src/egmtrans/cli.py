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
from egmtrans.batch import run_batch
from egmtrans.config import (
    DTED_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    get_datums_dir,
    normalize_datum,
    required_grids,
    verify_grids,
)
from egmtrans.crs import standardize_srs
from egmtrans.download import ensure_grids
from egmtrans.file_utils import (
    IOPaths,
    is_valid_dem,
    prepare_output_target,
    resolve_io_paths,
)
from egmtrans.flattening import DEFAULT_CONTAINMENT
from egmtrans.logging_setup import end_logger, setup_logger
from egmtrans.numba_utils import NUMBA_AVAILABLE
from egmtrans.tiling import TileLevels
from egmtrans.transform import transform_vertical_datum

# DTED tiles are edge-matched products. Only the bilinear resampling gives the
# same correction at a shared post whatever the tile extent (the spline solve
# depends on the clipped grid, Delaunay on the triangulation), and even a
# millimeter of difference moves posts by 1 m after rounding to whole meters:
# measured 0.067% of posts for spline and 0.032% for Delaunay against bilinear
# on a DTED2 tile. Set to False to warn instead of refusing.
DTED_REQUIRES_BILINEAR = True


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


def datum_arg(value: str) -> str:
    """argparse ``type`` for the ``-s`` / ``-t`` datum options."""
    try:
        return normalize_datum(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


class NonInteractiveError(RuntimeError):
    """A confirmation prompt was needed but there is no one to answer it."""


def confirm(question: str, assume_yes: bool = False) -> bool:
    """Ask a yes/no question on the terminal, or answer it without asking.

    Returns True at once when *assume_yes* is set.  A closed stdin (a container,
    a scheduler, ``pythonw``) raises :class:`NonInteractiveError` instead of
    crashing in ``input()``; piped answers such as ``echo yes |`` still work.
    """
    if assume_yes:
        return True
    if sys.stdin is None:
        raise NonInteractiveError(question)
    try:
        answer = input(f"{question} (yes/no): ").strip()
    except EOFError as e:
        raise NonInteractiveError(question) from e
    return str2bool(answer)


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


def _configure_runtime(arc_mode: bool) -> None:
    """Set up logging, the ArcGIS mode flag and ArcPy once per run."""
    logger = _state.get_logger()

    if len(logger.handlers) == 1 and isinstance(logger.handlers[0], logging.NullHandler):
        setup_logger(is_arc_mode=arc_mode)

    _state.set_arc_mode(arc_mode)
    if arc_mode:
        init_arcpy()
        log_numba_availability()
    elif not NUMBA_AVAILABLE:
        logger.warning("Numba is not available. Processing will be slower.")


def check_file_datum(
    input_file: str,
    output_file: str,
    source_datum: str,
    arc_mode: bool,
    assume_yes: bool,
    prompt: bool = True,
) -> bool:
    """Compare the file's own vertical datum with *source_datum*.

    Logs the datum the header or CRS carries.  When it disagrees with
    *source_datum* and *prompt* is set, asks whether to go on (CLI) or warns
    and goes on (ArcGIS Pro).

    Returns:
        False if the file could not be opened or the user declined; True otherwise.

    Raises:
        NonInteractiveError: If a prompt is needed, stdin is closed, and
            *assume_yes* is False.
    """
    logger = _state.get_logger()

    # gdal.UseExceptions() is on, so a failed open raises rather than returning None.
    try:
        with gdal.Open(input_file, gdal.GA_ReadOnly) as input_ds:
            metadata = input_ds.GetMetadata()
            projection = input_ds.GetProjection()
    except RuntimeError as e:
        logger.error(f'Failed to open input file {input_file}: {e}')
        return False
    logger.debug(f"Input file's projection: {projection}")
    src_srs = standardize_srs(projection)
    logger.debug(f"Input file's SRS: {src_srs}")

    if input_file.lower().endswith(DTED_EXTENSIONS):
        file_datum = metadata.get('DTED_VerticalDatum')
        file_datum = 'EGM96' if file_datum in ('E96', 'MSL') else 'EGM2008' if file_datum == 'E08' else None
    else:
        file_datum = src_srs.GetAttrValue('VERT_CS')
    logger.info(f"Input file header's vertical datum: {file_datum}")

    if file_datum and source_datum not in file_datum and prompt:
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
            if not confirm("Do you wish to proceed and ignore the input file's vertical datum?", assume_yes):
                logger.error("Aborting transformation.")
                return False
            logger.info(f"Ignoring the input file's vertical datum, using {source_datum} instead.")
    return True


def check_same_datum(input_file: str, source_datum: str, target_datum: str, arc_mode: bool, assume_yes: bool) -> bool:
    """Handle a run whose source and target datums are the same.

    A GeoTIFF is then rewritten as an optimized copy with a compound CRS after
    a confirmation (CLI); a DTED file is refused.

    Returns:
        False if the run should not go on.

    Raises:
        NonInteractiveError: If a prompt is needed, stdin is closed, and
            *assume_yes* is False.
    """
    logger = _state.get_logger()
    if source_datum != target_datum:
        return True
    if input_file.lower().endswith(DTED_EXTENSIONS):
        logger.error("Source and target vertical datums are the same.\nAborting transformation.")
        return False

    logger.warning(
        "Source and target vertical datums are the same. "
        "No vertical transformation or flattening will occur."
    )
    logger.warning(
        "This operation will create an optimized GeoTIFF copy rounded to 1 cm, "
        "with Compound CRS and DEFLATE compression."
    )
    if not arc_mode and not confirm("Do you wish to proceed?", assume_yes):
        logger.error("Aborting transformation.")
        return False
    logger.info("Proceeding to create GeoTIFF copy with Compound CRS metadata and optimized compression.")
    return True


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
    assume_yes: bool = False,
    tile_levels: TileLevels | None = None,
    min_containment: float = DEFAULT_CONTAINMENT,
) -> bool:
    """Process a single file for vertical datum transformation.

    Performs comprehensive validation before calling
    :func:`~egmtrans.transform.transform_vertical_datum`:

    1. Validates file format and accessibility.
    2. Checks datum compatibility (DTED cannot target WGS84, etc.) and that a
       DTED output uses the bilinear algorithm (see :data:`DTED_REQUIRES_BILINEAR`).
    3. Verifies the file's CRS/header matches the stated source datum;
       prompts the user (CLI) or logs a warning (ArcGIS) on mismatch.
    4. Disables flattening for WGS84 transforms (orthometric-only operation).
    5. Handles same-datum copies (optimized GeoTIFF with compound CRS).

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
        assume_yes: Answer yes to the confirmation prompts instead of asking
            (CLI mode only), for unattended runs.
        tile_levels: Water-body levels merged across the tiles of a batch run.
        min_containment: Share of a flat area's boundary that must lie above it
            for the area to count as a water body.

    Returns:
        ``True`` if the file was transformed, ``False`` if the transformation
        was aborted or failed.

    Raises:
        NonInteractiveError: If a prompt is needed, stdin is closed, and
            *assume_yes* is False.
    """
    logger = _state.get_logger()
    _configure_runtime(arc_mode)
    verify_grids(source_datum, target_datum)

    if not is_valid_dem(input_file):
        logger.error(f"Skipping {os.path.basename(input_file)} as it's not a DEM.\n")
        return False

    input_is_dted = input_file.lower().endswith(DTED_EXTENSIONS)
    output_is_dted = output_file.lower().endswith(DTED_EXTENSIONS)

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

    if output_is_dted and algorithm != 'bilinear':
        message = (
            f"DTED output requires the bilinear algorithm. '{algorithm}' gives a correction that depends on "
            f"the tile extent or differs from bilinear by millimeters, and either moves posts by 1 m after "
            f"rounding to whole meters, so tiles that should edge-match would not."
        )
        if DTED_REQUIRES_BILINEAR:
            logger.error(f"{message}\nAborting transformation.")
            return False
        logger.warning(message)

    if create_mask and not flatten:
        logger.error(
            f"To create a mask of flat areas, you must also set "
            f"{'Retain Flat Areas' if arc_mode else '--flatten'} to True.\nAborting transformation."
        )
        return False

    if os.path.isdir(output_file):
        logger.error(
            f'Output path is a directory, not a file: {output_file}\nAborting transformation.'
        )
        return False

    if not check_file_datum(input_file, output_file, source_datum, arc_mode, assume_yes, prompt=check_for_wrong_datum):
        return False

    if (source_datum == 'WGS84' or target_datum == 'WGS84') and flatten:
        logger.info("Flattening is not supported for WGS84 ellipsoid height transforms. Proceeding without flattening.")
        flatten = False

    if not check_same_datum(input_file, source_datum, target_datum, arc_mode, assume_yes):
        return False

    try:
        transform_vertical_datum(
            input_file, output_file, source_datum, target_datum,
            flatten, create_mask, min_patch_size, algorithm,
            abs_horiz_accuracy, save_log, tile_levels=tile_levels, min_containment=min_containment,
        )
    except Exception as e:
        logger.error(f"Transformation failed: {e}.")
        return False

    return True


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
    parser.add_argument("-i", "--input", required=True, help="Input DEM file, or folder of DEMs (DTED or GeoTIFF)")
    parser.add_argument("-o", "--output", required=True, help="Output file, or folder for the transformed DEMs")
    parser.add_argument(
        "-s", "--source_datum", required=True, type=datum_arg,
        help="Source vertical datum (WGS84, EGM96, or EGM2008)",
    )
    parser.add_argument(
        "-t", "--target_datum", required=True, type=datum_arg,
        help="Target vertical datum (WGS84, EGM96, or EGM2008)",
    )
    parser.add_argument(
        "-f", "--flatten", required=False, type=str2bool, nargs='?', const=True, default=True,
        help="Retain flat areas (default: True)",
    )
    parser.add_argument(
        "-m", "--create_mask", required=False, type=str2bool, nargs='?', const=True, default=False,
        help="Create a mask of the ocean and the water bodies, DTED included (default: False)",
    )
    # No nargs='?' here: a bare -p used to become True (one post) and a bare -a
    # became True, which passed the choices check and ran the spline branch.
    parser.add_argument(
        "-p", "--min_patch_size", required=False, type=int, default=16,
        help="Minimum patch size in pixels for flat areas, DTED included (default: 16)",
    )
    parser.add_argument(
        "-c", "--containment", required=False, type=float, default=DEFAULT_CONTAINMENT,
        help="Share of a flat area's boundary that must lie above it for the area to count as a water "
             "body and be flattened, 0 to 1 (default: 0.8; 0 keeps every flat area)",
    )
    parser.add_argument(
        "-a", "--algorithm", required=False, choices=['bilinear', 'delaunay', 'spline', 'proj'],
        default='bilinear',
        help="Interpolation algorithm (default: bilinear; DTED output accepts only bilinear)",
    )
    parser.add_argument(
        "--abs_horiz_accuracy", required=False, type=int,
        help="Absolute horizontal accuracy in meters (for DTED output only)",
    )
    parser.add_argument(
        "-l", "--log_file", required=False, type=str2bool, nargs='?', const=True, default=True,
        help="Save a log file (default: True)",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Proceed without asking when the file's vertical datum disagrees with -s, or when "
             "-s equals -t for a GeoTIFF. Needed for unattended runs such as a container.",
    )
    parser.add_argument(
        "--context", action="append", default=[], metavar="FOLDER",
        help="Folder of neighboring tiles to analyze but not transform, so that a water body which "
             "continues into them gets the level a run including them would give it. May be repeated.",
    )
    parser.add_argument(
        "--water-levels", metavar="FILE",
        help="Water-level table written by an earlier run's --export-water-levels. A water body found "
             "in it takes the table's level when that is lower than the level found in this run.",
    )
    parser.add_argument(
        "--export-water-levels", metavar="FILE",
        help="Write the level of every water body that touches a tile edge, keyed by the edge crossing, "
             "for later runs over neighboring tiles.",
    )

    args = parser.parse_args()

    try:
        paths = resolve_io_paths(args.input, args.output)
    except ValueError as e:
        parser.error(str(e))
    if not 0.0 <= args.containment <= 1.0:
        parser.error(f"--containment must be between 0 and 1, not {args.containment}")
    for folder in args.context:
        if not os.path.isdir(folder):
            parser.error(f"--context folder does not exist: {folder}")
    if args.water_levels and not os.path.isfile(args.water_levels):
        parser.error(f"--water-levels file does not exist: {args.water_levels}")

    try:
        prepare_output_target(paths)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    logger = setup_logger(paths.log_path if args.log_file else None, args.log_file, False)

    args_list = list(vars(args).items())
    for i, (arg, value) in enumerate(args_list):
        if isinstance(value, str):
            value = value.replace('\\\\', '\\')
        if i == len(args_list) - 1:
            logger.info(f"Argument - {arg}: {value}\n\n")
        else:
            logger.info(f"Argument - {arg}: {value}")

    try:
        # Only the grids this transform reads. The Explorer grids come from
        # download_grids.py, so a container holding two grids stays offline.
        downloaded = ensure_grids(
            datums_dir=get_datums_dir(),
            filenames=required_grids(args.source_datum, args.target_datum),
            message_func=logger.info,
        )
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

    try:
        exit_code = _dispatch(args, paths, logger)
    except NonInteractiveError as e:
        logger.error(
            f"Cannot ask \"{e}\" because there is no terminal to answer it. "
            f"Re-run with --yes to proceed without asking."
        )
        end_logger(save_log=args.log_file)
        sys.exit(2)

    if exit_code:
        logger.error("Processing completed with errors.")
    else:
        logger.info("Processing completed.")
    end_logger(save_log=args.log_file)
    sys.exit(exit_code)


def _dispatch(args: argparse.Namespace, paths: IOPaths, logger: logging.Logger) -> int:
    """Transform one file or every DEM under a folder; return the exit code."""
    exit_code = 0
    batch_options = args.context or args.water_levels or args.export_water_levels

    if paths.mode == 'file' and not batch_options:
        logger.info(f"Processing file: {paths.output_path}")
        if process_file(
            paths.input_path, paths.output_path, args.source_datum, args.target_datum,
            args.flatten, args.create_mask, args.min_patch_size, args.algorithm,
            args.abs_horiz_accuracy, args.log_file, assume_yes=args.yes,
            min_containment=args.containment,
        ) is False:
            exit_code = 1
    else:
        result = run_batch(
            paths, args.source_datum, args.target_datum,
            args.flatten, args.create_mask, args.min_patch_size, args.algorithm,
            args.abs_horiz_accuracy, args.log_file, assume_yes=args.yes,
            context_folders=args.context, water_levels=args.water_levels,
            export_water_levels=args.export_water_levels, min_containment=args.containment,
        )
        exit_code = result.exit_code

        if paths.mode == 'folder' and result.files_processed == 0:
            logger.info(
                f"NOTE: No DEM under {args.input} was transformed. The output directory "
                f"{args.output} holds at most copies of the other files."
            )
            if _state.get_arc_mode():
                success = delete_output_directory(args.output, 3, 1.0)
                if success:
                    logger.info("Output directory deleted successfully.")
                else:
                    logger.error("Failed to delete output directory.")
            else:
                # --yes covers the datum prompts only: deleting a folder needs a human answer.
                try:
                    delete = confirm("Do you wish to delete the output directory?")
                except NonInteractiveError:
                    delete = False
                if delete:
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

    return exit_code
