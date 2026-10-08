"""Command-line interface: process_file, main, and helpers.

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
import sys

from osgeo import gdal

from egmtrans import _state
from egmtrans._version import __version__
from egmtrans.arcpy_compat import init_arcpy
from egmtrans.batch import plan_units, run_batch, writable_file_problem
from egmtrans.cli_dted import SUBCOMMANDS
from egmtrans.config import (
    DTED_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    dted_target_problem,
    get_datums_dir,
    normalize_datum,
    required_grids,
    verify_grids,
)
from egmtrans.crs import standardize_srs
from egmtrans.download import ensure_grids
from egmtrans.dted.header import CellGeometry, read_header
from egmtrans.dted.validate import count as count_issues
from egmtrans.dted.writer import DtedMetadataSource, header_plan_lines, parse_overrides
from egmtrans.file_utils import (
    DEFAULT_DTED_NAMING,
    DTED_NAMING_PRESETS,
    IOPaths,
    dted_naming_template,
    is_valid_dem,
    prepare_output_target,
    resolve_io_paths,
)
from egmtrans.flattening import DEFAULT_CONTAINMENT
from egmtrans.logging_setup import end_logger, log_traceback, setup_logger
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
    """Log whether Numba is available, and which Numba (ArcGIS mode)."""
    logger = _state.get_logger()
    if NUMBA_AVAILABLE:
        import numba

        logger.info(f'Numba {numba.__version__} is available: compiled kernels are in use.')
    else:
        logger.warning(
            'Numba is not available: the kernels run as plain Python, which is much slower on large tiles. '
            'A custom ArcGIS Pro environment with Numba installed is faster; the results are the same.'
        )


def versions_line() -> str:
    """What two producers compare first: the versions behind a run."""
    import numpy
    from osgeo import gdal as _gdal

    numba_version = 'absent'
    if NUMBA_AVAILABLE:
        import numba

        numba_version = numba.__version__
    python = '.'.join(str(part) for part in sys.version_info[:3])
    return (
        f'EGMTrans {__version__}, Python {python}, GDAL {_gdal.__version__}, numpy {numpy.__version__}, '
        f'numba {numba_version}'
    )


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
    except (EOFError, OSError) as e:
        # EOFError: stdin at end. OSError: a captured or invalid stdin.
        raise NonInteractiveError(question) from e
    return str2bool(answer)


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


def file_datum_of(input_file: str) -> str | None:
    """The vertical datum *input_file* declares: EGM96 or EGM2008 from the DSI
    code of a DTED header (MSL counts as EGM96; read from the raw bytes, since
    GDAL would answer from a stale ``.aux.xml`` sidecar), or the name of a
    GeoTIFF's vertical CRS; None when it declares none.

    Raises:
        OSError, ValueError, RuntimeError: If the file cannot be opened or read.
    """
    if input_file.lower().endswith(DTED_EXTENSIONS):
        code = read_header(input_file).stripped('dsi.vertical_datum')
        return 'EGM96' if code in ('E96', 'MSL') else 'EGM2008' if code == 'E08' else None
    with gdal.Open(input_file, gdal.GA_ReadOnly) as input_ds:
        projection = input_ds.GetProjection()
    return standardize_srs(projection).GetAttrValue('VERT_CS')


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
        file_datum = file_datum_of(input_file)
    except (OSError, ValueError, RuntimeError) as e:
        logger.error(f'Failed to read the vertical datum of {input_file}: {e}')
        return False
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


def check_same_datum(
    input_file: str, source_datum: str, target_datum: str, arc_mode: bool, assume_yes: bool, converting: bool = False
) -> bool:
    """Handle a run whose source and target datums are the same.

    A GeoTIFF is then rewritten as an optimized copy with a compound CRS after
    a confirmation (CLI); a DTED file is refused. A conversion to DTED
    (*converting*) goes on without asking: the cell is resampled and its
    water flattened whatever the datum.

    Returns:
        False if the run should not go on.

    Raises:
        NonInteractiveError: If a prompt is needed, stdin is closed, and
            *assume_yes* is False.
    """
    logger = _state.get_logger()
    if source_datum != target_datum:
        return True
    if converting:
        logger.info('Source and target vertical datums are the same: the cell is resampled, not shifted.')
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


def conversion_cell(input_file: str, output_file: str, dted_cell: CellGeometry | None) -> CellGeometry | None:
    """The DTED cell a GeoTIFF input is converted to, or None for a transform.

    A ``.dtN`` output with a GeoTIFF input is a conversion: the cell is
    *dted_cell* when the caller names it (a batch run does), else the one
    whole cell the raster covers.

    Raises:
        ValueError: If the raster cannot become DTED, covers no whole cell or
            several, or *dted_cell* is not at the output's level.
    """
    output_is_dted = output_file.lower().endswith(DTED_EXTENSIONS)
    input_is_dted = input_file.lower().endswith(DTED_EXTENSIONS)
    if not output_is_dted or input_is_dted:
        if dted_cell is not None:
            raise ValueError('A cell can only be named for a GeoTIFF written as DTED')
        return None
    level = int(output_file[-1])
    if dted_cell is not None:
        if dted_cell.level != level:
            raise ValueError(f'Cell {dted_cell.cell_id} is level {dted_cell.level}, but the output is level {level}')
        return dted_cell
    units = plan_units([(input_file, None)], level, 'cell', os.path.dirname(input_file), os.path.dirname(output_file))
    if len(units) != 1:
        raise ValueError(
            f'{os.path.basename(input_file)} covers {len(units)} whole cells; write it to a folder with '
            f'--dted-level and --dted-naming to get one DTED file per cell'
        )
    return units[0].cell


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
    dted_metadata: DtedMetadataSource | None = None,
    *,
    dted_cell: CellGeometry | None = None,
    dted_plan: bool = True,
    mask_file: str | None = None,
) -> bool:
    """Process a single file for vertical datum transformation, or convert a
    GeoTIFF cell to DTED.

    Performs comprehensive validation before calling
    :func:`~egmtrans.transform.transform_vertical_datum`:

    1. Validates file format and accessibility.
    2. Checks datum compatibility (DTED is written in EGM96 only), that a
       DTED output uses the bilinear algorithm (see :data:`DTED_REQUIRES_BILINEAR`),
       and that a DTED input is not written at another level.
    3. For a GeoTIFF written as DTED, finds the cell to convert
       (:func:`conversion_cell`) and checks that its header can be completed.
    4. Verifies the file's CRS/header matches the stated source datum;
       prompts the user (CLI) or logs a warning (ArcGIS) on mismatch.
    5. Disables flattening for WGS84 transforms (orthometric-only operation).
    6. Handles same-datum copies (optimized GeoTIFF with compound CRS); a
       conversion goes on without asking.

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
        dted_metadata: The DTED metadata index and product profile for the
            header of a DTED output (``--dted-index``, ``--dted-profile``).
        dted_cell: The cell of a GeoTIFF written as DTED, when a batch run
            names it; found from the raster otherwise.
        dted_plan: Log the DTED header plan (the example header and the
            source of every supplied field) and, in CLI mode, ask before
            going on. A batch run shows the plan once itself and passes False.
        mask_file: Where the flat mask goes; the default name otherwise
            (see :func:`~egmtrans.file_utils.mask_output_name`).

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
    if min_patch_size is None or int(min_patch_size) < 1:
        logger.error(
            f'The minimum patch size must be at least 1 post, not {min_patch_size!r}.\nAborting transformation.'
        )
        return False

    input_is_dted = input_file.lower().endswith(DTED_EXTENSIONS)
    output_is_dted = output_file.lower().endswith(DTED_EXTENSIONS)

    if not any(input_file.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
        logger.error(
            f"Unsupported input file format. Supported formats are: "
            f"{', '.join(SUPPORTED_EXTENSIONS)}\nAborting transformation."
        )
        return False

    if input_is_dted and output_is_dted and input_file[-1] != output_file[-1]:
        logger.error(
            f'A DTED file keeps its level: {os.path.basename(input_file)} cannot be written as '
            f'{os.path.basename(output_file)}.\nAborting transformation.'
        )
        return False

    try:
        cell = conversion_cell(input_file, output_file, dted_cell)
    except ValueError as e:
        logger.error(f'{e}\nAborting transformation.')
        return False

    if output_is_dted:
        problem = dted_target_problem(target_datum)
        if problem:
            logger.error(f'{problem}\nAborting transformation.')
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

    source = dted_metadata if dted_metadata is not None and not dted_metadata.empty else None
    plan = None
    if cell is not None and tile_levels is None:
        # The batch run checked its cells before writing anything; a single
        # conversion checks its own header now, before the datum prompts.
        from egmtrans.io import new_dted_header

        try:
            metadata = source.for_cell(cell.cell_id) if source is not None else None
            header, sources = new_dted_header(cell, target_datum, abs_horiz_accuracy, metadata=metadata)
        except (ValueError, LookupError) as e:
            logger.error(
                f'The DTED header of cell {cell.cell_id} cannot be completed: {e}\n'
                f'A DTED cell made from GeoTIFF needs --dted-profile and/or --dted-index.\nAborting transformation.'
            )
            return False
        plan = (cell.cell_id, None, header, sources)
    elif input_is_dted and output_is_dted and source is not None and tile_levels is None and dted_plan:
        # A DTED-to-DTED rewrite with an index, a profile or overrides: show
        # what they change before the file is touched.
        from egmtrans.io import preview_dted_header

        try:
            base, cell_geometry, header, sources = preview_dted_header(
                input_file, target_datum, abs_horiz_accuracy, source=source,
            )
        except (OSError, ValueError, LookupError) as e:
            logger.error(f'The DTED header of {os.path.basename(input_file)} cannot be completed: {e}\n'
                         f'Aborting transformation.')
            return False
        plan = (cell_geometry.cell_id, base, header, sources)
    if plan is not None and dted_plan:
        cell_id, base, header, sources = plan
        describe = source.describe() if source is not None else 'no index or profile'
        for line in header_plan_lines(describe, cell_id, input_file, output_file, header, sources, base=base):
            logger.info(line)
        if not arc_mode and not confirm('Write the DTED header as planned?', assume_yes):
            logger.error('Aborting transformation.')
            return False

    if not check_file_datum(input_file, output_file, source_datum, arc_mode, assume_yes, prompt=check_for_wrong_datum):
        return False

    if (source_datum == 'WGS84' or target_datum == 'WGS84') and flatten:
        logger.info("Flattening is not supported for WGS84 ellipsoid height transforms. Proceeding without flattening.")
        if create_mask:
            logger.warning(
                'No mask is written for a WGS84 transform: nothing is flattened, so there is nothing to mask.'
            )
        flatten = False

    if not check_same_datum(input_file, source_datum, target_datum, arc_mode, assume_yes, converting=cell is not None):
        return False

    try:
        transform_vertical_datum(
            input_file, output_file, source_datum, target_datum,
            flatten, create_mask, min_patch_size, algorithm,
            abs_horiz_accuracy, save_log, tile_levels=tile_levels, min_containment=min_containment,
            dted_metadata=dted_metadata, cell=cell, mask_file=mask_file,
        )
    except Exception as e:
        logger.error(f"Transformation failed: {e}.")
        log_traceback()
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
    # The DTED tools are subcommands; the transform keeps its flag-only form.
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        from egmtrans.cli_dted import main as dted_main

        sys.exit(dted_main(sys.argv[1:]))

    parser = argparse.ArgumentParser(
        description="Transform vertical datum between WGS 84 ellipsoid, EGM96, and EGM2008 for DTED and GeoTIFF files.",
        epilog="DTED header tools: 'egmtrans dted-header FILE...' reports and validates headers; "
               "'egmtrans dted-index build|validate' builds and checks a metadata index. "
               "Run either with --help for its options.",
    )
    parser.add_argument("-i", "--input", required=True, help="Input DEM file, or folder of DEMs (DTED or GeoTIFF)")
    parser.add_argument("-o", "--output", required=True, help="Output file, or folder for the transformed DEMs")
    parser.add_argument(
        "-s", "--source_datum", required=True, type=datum_arg,
        help="Source vertical datum (WGS84, EGM96, or EGM2008)",
    )
    parser.add_argument(
        "-t", "--target_datum", required=True, type=datum_arg,
        help="Target vertical datum (WGS84, EGM96, or EGM2008; DTED output is EGM96 only)",
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
        help="Proceed without asking when the file's vertical datum disagrees with -s, when "
             "-s equals -t for a GeoTIFF, or after the DTED header plan. Needed for unattended runs "
             "such as a container.",
    )
    parser.add_argument(
        "--context", action="append", default=[], metavar="FOLDER",
        help="Additional tiles that constrain the input: read and analyzed with it, never processed or written, "
             "so that a water body which continues into them gets the level a run including them would give "
             "it. Use it for neighbors produced in another run; the input's own tiles are always analyzed "
             "together, tiles that are also in the input are skipped, and the tiles must be in the source "
             "datum. May be repeated.",
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
    parser.add_argument(
        "--dted-index", metavar="FILE",
        help="DTED metadata index (.gpkg or .parquet) whose row for the output cell fills the DTED header; "
             "a cell the index does not hold fails. See 'egmtrans dted-index --help'.",
    )
    parser.add_argument(
        "--dted-profile", metavar="FILE",
        help="DTED product profile (TOML) of header constants; the index row overrides it field by field.",
    )
    parser.add_argument(
        "--dted-level", type=int, choices=[0, 1, 2], default=None,
        help="Write every GeoTIFF input as DTED of this level, one file per whole one-degree cell it covers; "
             "a DTED input must be at this level. The GeoTIFF must lie on the whole-degree lattice, as "
             "TanDEM-X tiles do. A .dt0/.dt1/.dt2 output name for a single GeoTIFF needs no level.",
    )
    parser.add_argument(
        "--dted-naming", default=DEFAULT_DTED_NAMING, metavar="NAME|TEMPLATE",
        help="How DTED cells made from GeoTIFF are laid out under the output folder: "
             + ", ".join(f"'{k}' ({v})" for k, v in DTED_NAMING_PRESETS.items())
             + ", or a template with {stem}, {dir}, {cell}, {lat}, {lon} and {level}; the extension is added "
             f"(default: {DEFAULT_DTED_NAMING}, the standard DTED/E006/N49.dt2 tree of MIL-PRF-89020B 3.10.7.2).",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="Leave a planned output that already exists and verifies as finished alone, so a cancelled or "
             "failed run can be rerun without redoing the cells it wrote.",
    )
    parser.add_argument(
        "--dted-set", action="append", default=[], metavar="FIELD=VALUE",
        help="Write this value in every DTED header, over the index row and the profile (may be repeated). "
             "FIELD is a header column of the index (producer_code, compilation_date, abs_horiz_acc, ...); "
             "an accuracy takes NA, a date takes YYYY-MM, YYYY-MM-DD or today.",
    )

    args = parser.parse_args()

    try:
        paths = resolve_io_paths(args.input, args.output, dted_level=args.dted_level)
        dted_naming_template(args.dted_naming)
    except ValueError as e:
        parser.error(str(e))
    if not 0.0 <= args.containment <= 1.0:
        parser.error(f"--containment must be between 0 and 1, not {args.containment}")
    for folder in args.context:
        if not os.path.isdir(folder):
            parser.error(f"--context folder does not exist: {folder}")
    if args.water_levels and not os.path.isfile(args.water_levels):
        parser.error(f"--water-levels file does not exist: {args.water_levels}")
    if args.export_water_levels:
        problem = writable_file_problem(args.export_water_levels, "--export-water-levels")
        if problem:
            parser.error(problem)
    for option, path in (("--dted-index", args.dted_index), ("--dted-profile", args.dted_profile)):
        if path and not os.path.isfile(path):
            parser.error(f"{option} file does not exist: {path}")
    try:
        overrides = parse_overrides(args.dted_set)
    except ValueError as e:
        parser.error(f"--dted-set: {e}")
    try:
        dted_metadata = DtedMetadataSource.load(args.dted_index, args.dted_profile, overrides)
    except (OSError, ValueError, RuntimeError) as e:
        parser.error(str(e))
    # DTED is written in EGM96 only (MIL-PRF-89020B 3.2.2): said before anything
    # runs when the arguments already show that DTED will be written. A folder
    # of DTED inputs is caught by the batch planner.
    writes_dted = (
        paths.dted_level is not None
        or paths.input_path.lower().endswith(DTED_EXTENSIONS)
        or paths.output_path.lower().endswith(DTED_EXTENSIONS)
    )
    if writes_dted:
        problem = dted_target_problem(args.target_datum)
        if problem:
            parser.error(problem)

    try:
        prepare_output_target(paths)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    logger = setup_logger(paths.log_path if args.log_file else None, args.log_file, False)
    logger.info(versions_line())

    if not dted_metadata.empty:
        issues = dted_metadata.validate(args.dted_level)
        for issue in issues:
            if issue.severity == 'error':
                logger.error(str(issue))
        if count_issues(issues, 'error'):
            logger.error("The DTED metadata index is not valid; see 'egmtrans dted-index validate'.")
            end_logger(save_log=args.log_file)
            sys.exit(2)
        logger.info(f"DTED header metadata: {dted_metadata.describe()}")
        if dted_metadata.index is not None or dted_metadata.profile is not None:
            coverage = dted_metadata.coverage(args.dted_level, args.abs_horiz_accuracy)
            logger.info(f"DTED header fields: {coverage.summary()}")

    args_list = list(vars(args).items())
    for i, (arg, value) in enumerate(args_list):
        if isinstance(value, str):
            value = value.replace('\\\\', '\\')
        if i == len(args_list) - 1:
            logger.info(f"Argument - {arg}: {value}\n\n")
        else:
            logger.info(f"Argument - {arg}: {value}")
    args.dted_metadata = dted_metadata

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
    except KeyboardInterrupt:
        logger.error("Stopped by the user.")
        end_logger(save_log=args.log_file)
        sys.exit(1)
    except Exception as e:
        logger.error(f"EGMTrans stopped: {e}")
        log_traceback()
        end_logger(save_log=args.log_file)
        sys.exit(1)

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

    if paths.mode == 'file' and not batch_options and paths.dted_level is None:
        logger.info(f"Processing file: {paths.output_path}")
        if process_file(
            paths.input_path, paths.output_path, args.source_datum, args.target_datum,
            args.flatten, args.create_mask, args.min_patch_size, args.algorithm,
            args.abs_horiz_accuracy, args.log_file, assume_yes=args.yes,
            min_containment=args.containment, dted_metadata=getattr(args, 'dted_metadata', None),
        ) is False:
            exit_code = 1
    else:
        result = run_batch(
            paths, args.source_datum, args.target_datum,
            args.flatten, args.create_mask, args.min_patch_size, args.algorithm,
            args.abs_horiz_accuracy, args.log_file, assume_yes=args.yes,
            context_folders=args.context, water_levels=args.water_levels,
            export_water_levels=args.export_water_levels, min_containment=args.containment,
            dted_metadata=getattr(args, 'dted_metadata', None),
            dted_level=paths.dted_level, dted_naming=args.dted_naming, skip_existing=args.skip_existing,
        )
        exit_code = result.exit_code

        if paths.mode == 'folder' and result.files_processed == 0 and not result.skipped_existing:
            logger.info(
                f"No DEM under {args.input} was transformed. The output folder {args.output} keeps the log of "
                f"this run and whatever it held before."
            )

    return exit_code
