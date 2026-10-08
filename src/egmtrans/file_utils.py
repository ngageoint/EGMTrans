"""File validation, output-path resolution, and folder-copy utilities.

Provides the single source of truth for deciding whether an ``--output``
argument names a file or a folder (:func:`resolve_io_paths`), used by both the
CLI and the ArcGIS Pro toolbox so the two cannot drift apart.  Also provides
checks for valid filenames (system constraints and reserved terms like
TanDEM-X auxiliary products) and validation that a file is a usable
single-band DEM rather than an ortho, mask, or multi-band image.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import string
from collections.abc import Iterable
from dataclasses import dataclass

from osgeo import gdal

from egmtrans import _state
from egmtrans.config import (
    AUXILIARY_LAYER_CODES,
    DTED_EXTENSIONS,
    DTED_ROOT,
    INVALID_CHARACTERS,
    INVALID_FILENAME_SUBSTRINGS,
    SUPPORTED_EXTENSIONS,
)

# Band types that can hold heights. A Byte or UInt16 band is a mask or amplitude layer.
ELEVATION_DATA_TYPES = (gdal.GDT_Int16, gdal.GDT_Int32, gdal.GDT_Float32, gdal.GDT_Float64)

# An auxiliary-layer code standing alone between separators or at either end of the name.
_AUXILIARY_LAYER_TOKEN = re.compile(
    r'(?:^|[_\-. ])(?:' + '|'.join(AUXILIARY_LAYER_CODES) + r')(?=$|[_\-. ])',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IOPaths:
    """Resolved input/output locations for one EGMTrans run.

    Attributes:
        input_path: The normalized input file or folder.
        output_path: When *mode* is ``'file'`` this is the full path of the file
            to write (already joined with the input's basename if the caller
            supplied a folder).  When *mode* is ``'folder'`` it is the folder.
        mode: ``'file'`` for a single transform, ``'folder'`` for a batch run.
        log_path: Where the transform log belongs.  Derived, never created --
            the caller decides whether logging is enabled.
        output_folder: The folder the outputs go to: the output folder of a
            batch run, or the folder of the single output file.
        dted_level: The DTED level GeoTIFF inputs are converted to, if any.
        output_derived: Whether the single output file's name was derived
            from the input's (the caller gave a folder), so a naming preset
            may still rename a converted cell.
    """

    input_path: str
    output_path: str
    mode: str
    log_path: str
    output_folder: str = ''
    dted_level: int | None = None
    output_derived: bool = False


# Output names of converted cells: presets by name, or a template with the
# placeholders {stem} (the input's name without extension), {dir} (the
# input's folder relative to the input folder), {cell} (N49E006), {lat}
# (N49), {lon} (E006) and {level}. The extension is appended. The default,
# 'dted', is the layout MIL-PRF-89020B 3.10.7.2 prescribes for a delivery:
# a DTED root, one folder per longitude (E006) and the cell named for its
# southwest latitude (N49.dt2), so \DTED\E006\N49.dt2.
DTED_NAMING_PRESETS = {'stem': '{dir}/{stem}', 'cell': '{cell}', 'dted': 'DTED/{lon}/{lat}'}
DTED_NAMING_PLACEHOLDERS = ('stem', 'dir', 'cell', 'lat', 'lon', 'level')
DEFAULT_DTED_NAMING = 'dted'
# Sidecars of a DEM that describe its pyramids or statistics: wrong beside a
# transformed output, so a folder run does not carry them over.
DEM_SIDECAR_SUFFIXES = ('.aux.xml', '.ovr', '.rrd', '.aux')
# The DTED0 companion files (MIL-PRF-89020B 3.9.3): statistics in the source
# datum, so a datum transform cannot carry them over either.
DTED0_COMPANION_SUFFIXES = ('.avg', '.min', '.max')


def dted_naming_template(naming: str) -> str:
    """The template behind a ``--dted-naming`` value: a preset's, or the value itself.

    Raises:
        ValueError: If the template uses an unknown placeholder or is malformed.
    """
    template = DTED_NAMING_PRESETS.get(naming, naming)
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]
    except ValueError as e:
        raise ValueError(f'--dted-naming {naming!r} is not a valid template: {e}') from e
    unknown = sorted(set(fields) - set(DTED_NAMING_PLACEHOLDERS))
    if unknown:
        raise ValueError(
            f'--dted-naming {naming!r} uses {", ".join("{" + u + "}" for u in unknown)}; the placeholders are '
            + ', '.join('{' + p + '}' for p in DTED_NAMING_PLACEHOLDERS)
            + f' and the presets {", ".join(DTED_NAMING_PRESETS)}'
        )
    if not fields and not template.strip():
        raise ValueError('--dted-naming needs a name')
    return template


def dted_output_name(
    naming: str, input_file: str, input_root: str, cell_id: str, level: int, *, output_root: str | None = None
) -> str:
    """The output path, relative to the output folder, of the DTED cell
    *cell_id* made from *input_file* under the ``--dted-naming`` value.

    When the name starts with the ``DTED`` root and *output_root* is itself
    a folder named ``DTED``, that first segment is dropped: the delivery
    gets one root, never ``DTED/DTED``.

    Raises:
        ValueError: If the template is invalid or the name leaves the output folder.
    """
    template = dted_naming_template(naming)
    stem = os.path.splitext(os.path.basename(input_file))[0]
    folder = os.path.relpath(os.path.dirname(os.path.abspath(input_file)), os.path.abspath(input_root))
    folder = '' if folder == os.curdir else folder.replace(os.sep, '/')
    lat, lon = cell_id[:3], cell_id[3:]
    name = template.format(stem=stem, dir=folder, cell=cell_id, lat=lat, lon=lon, level=level)
    parts = [part for part in name.split('/') if part]  # a blank {dir} leaves no empty folder
    if (
        len(parts) > 1 and parts[0].upper() == DTED_ROOT and output_root
        and os.path.basename(os.path.normpath(output_root)).upper() == DTED_ROOT
    ):
        parts = parts[1:]
    name = '/'.join(parts)
    if not name:
        raise ValueError(f'--dted-naming {naming!r} gives an empty name for {cell_id}')
    name = f'{name}.dt{level}'
    normalized = os.path.normpath(name)
    if normalized.startswith(os.pardir) or os.path.isabs(normalized) or normalized.startswith(('/', '\\')):
        raise ValueError(f'--dted-naming {naming!r} names {name!r}, which leaves the output folder')
    return normalized.replace(os.sep, '/')


def mask_output_name(
    output_file: str, source_file: str | None = None, cell_id: str | None = None, *, several_cells: bool = False
) -> str:
    """The path of the flat mask written beside *output_file*.

    A DTED cell made from a GeoTIFF (*source_file* and *cell_id* given) gets
    ``<source stem>_mask.tif``, or ``<source stem>_<cell>_mask.tif`` when the
    source covers *several_cells*: a name that stays unique when the masks
    are taken out of the DTED tree, where every longitude folder holds an
    ``N49.dt2``. Every other output gets ``<output stem>_mask.tif``.
    """
    folder = os.path.dirname(output_file)
    if source_file is not None and cell_id is not None:
        stem = os.path.splitext(os.path.basename(source_file))[0]
        if several_cells:
            stem = f'{stem}_{cell_id}'
    else:
        stem = os.path.splitext(os.path.basename(output_file))[0]
    return os.path.join(folder, f'{stem}_mask.tif')


def folder_within(inner: str, outer: str) -> bool:
    """True when *inner* is *outer* or lies anywhere under it."""
    inner = os.path.normcase(os.path.abspath(inner))
    outer = os.path.normcase(os.path.abspath(outer))
    try:
        return os.path.commonpath([inner, outer]) == outer
    except ValueError:  # different drives
        return False


def derive_log_path(output_path: str, mode: str) -> str:
    """Return the log-file path for an output location.

    File outputs get ``<output basename>_transform.log`` beside them; folder
    outputs get ``<folder>/<folder name>_transform.log`` inside them.  This is a
    pure function — it never creates directories.  That matters: deriving the
    log path used to be entangled with the file/folder decision, and a wrong
    answer here created a *directory* at the output file's path.
    """
    if mode == 'file':
        base, _ = os.path.splitext(output_path)
        return f'{base}_transform.log'

    folder = os.path.normpath(output_path)
    # A drive or share root has no name of its own.
    name = os.path.basename(folder) or 'egmtrans'
    return os.path.join(folder, f'{name}_transform.log')


def resolve_io_paths(input_path: str, output_path: str, *, dted_level: int | None = None) -> IOPaths:
    """Decide whether *output_path* names a file or a folder, and resolve both.

    Classification, in order:

    1. An existing directory is always a folder (so folder names containing
       dots keep working).
    2. Otherwise a supported raster extension means a file.
    3. Otherwise a folder -- with a warning if the name carries some other
       extension, since that is more likely a typo than an intent.

    A file input written to a folder output is resolved to
    ``<folder>/<input basename>``, so callers always receive a concrete file
    path in :attr:`IOPaths.output_path`; with *dted_level*, a GeoTIFF file
    input becomes ``<folder>/<input stem>.dtN``.

    Raises:
        ValueError: If either path is empty, the input does not exist, the
            output basename is not a legal filename, a folder of DEMs was
            aimed at a single output file, or *dted_level* contradicts the
            output file's extension.
    """
    logger = _state.get_logger()
    if dted_level is not None and dted_level not in (0, 1, 2):
        raise ValueError(f'The DTED level must be 0, 1 or 2, not {dted_level}')

    if not input_path or not str(input_path).strip():
        raise ValueError('An input path is required.')
    if not output_path or not str(output_path).strip():
        raise ValueError('An output path is required.')

    input_path = os.path.normpath(input_path)
    input_is_file = os.path.isfile(input_path)
    input_is_folder = os.path.isdir(input_path)
    if not input_is_file and not input_is_folder:
        raise ValueError(f'Input path does not exist: {input_path}')

    output_path = os.path.normpath(output_path)
    output_name = os.path.basename(output_path)
    if not os.path.isdir(output_path) and not is_valid_filename(output_name):
        # An existing folder needs no name check: a drive or share root
        # (E:\, \\nas\dted) has an empty basename and is a fine output folder.
        raise ValueError(f'Invalid output name: {output_name!r}')

    if input_is_folder and folder_within(output_path, input_path):
        raise ValueError(
            f'The output folder {output_path} lies inside the input folder {input_path}, so the run would read '
            f'its own outputs. Choose an output folder outside the input folder.'
        )

    if os.path.isdir(output_path):
        output_is_file = False
        if output_path.lower().endswith(SUPPORTED_EXTENSIONS):
            logger.warning(
                f'{output_path} is an existing directory but is named like a raster file, '
                f'so the output will be written inside it. A directory with a raster '
                f'extension is usually left over from a failed run — delete it if so.'
            )
    elif output_path.lower().endswith(SUPPORTED_EXTENSIONS):
        output_is_file = True
    else:
        output_is_file = False
        extension = os.path.splitext(output_name)[1]
        if extension:
            logger.warning(
                f"Treating {output_path} as a folder. For a single output file, use one of: "
                f"{', '.join(SUPPORTED_EXTENSIONS)}."
            )

    if input_is_folder and output_is_file:
        raise ValueError(
            f'Cannot write the folder {input_path} to the single file {output_path}.\n'
            f'Give a folder as the output when the input is a folder.'
        )

    if output_is_file:
        mode, resolved_output = 'file', output_path
        if dted_level is not None:
            extension = os.path.splitext(output_name)[1].lower()
            if extension not in DTED_EXTENSIONS:
                raise ValueError(f'A DTED level was given, but the output {output_name} is not a DTED file')
            if extension != f'.dt{dted_level}':
                raise ValueError(f'The output {output_name} is not DTED level {dted_level}')
    elif input_is_file:
        mode = 'file'
        name = os.path.basename(input_path)
        if dted_level is not None and not name.lower().endswith(DTED_EXTENSIONS):
            name = f'{os.path.splitext(name)[0]}.dt{dted_level}'
        resolved_output = os.path.join(output_path, name)
    else:
        mode, resolved_output = 'folder', output_path

    return IOPaths(
        input_path=input_path,
        output_path=resolved_output,
        mode=mode,
        log_path=derive_log_path(resolved_output, mode),
        output_folder=resolved_output if mode == 'folder' else os.path.dirname(resolved_output) or os.curdir,
        dted_level=dted_level,
        output_derived=mode == 'file' and not output_is_file,
    )


def prepare_output_target(paths: IOPaths) -> None:
    """Create the output directory and prove it is writable, before any GDAL work.

    Catching an unwritable destination here turns what would otherwise surface
    deep inside GDAL as a bare ``<path>: Permission denied`` into a message that
    says what to do about it.

    Raises:
        NotADirectoryError: If the containing directory is an existing file.
        IsADirectoryError: If a file output already exists as a directory.
        PermissionError: If an existing output file is not writable.
    """
    if paths.mode == 'folder':
        target_dir = paths.output_path
    else:
        target_dir = os.path.dirname(paths.output_path) or os.curdir

    if os.path.isfile(target_dir):
        raise NotADirectoryError(f'Output folder path is an existing file: {target_dir}')
    os.makedirs(target_dir, exist_ok=True)

    if paths.mode != 'file':
        return

    if os.path.isdir(paths.output_path):
        raise IsADirectoryError(
            f'Output path is an existing directory: {paths.output_path}\n'
            f'Give a file name ending in {", ".join(SUPPORTED_EXTENSIONS)}, '
            f'or pass the folder and let EGMTrans name the file.'
        )
    if os.path.exists(paths.output_path) and not os.access(paths.output_path, os.W_OK):
        raise PermissionError(
            f'Output file exists and is read-only: {paths.output_path}\n'
            f'Clear the read-only attribute or choose a different output path.'
        )


def ensure_writable(path: str) -> None:
    """Clear the read-only bit on *path* so GDAL can reopen it with ``GA_Update``.

    DTED is routinely delivered on read-only media, and both :func:`shutil.copy`
    and :func:`shutil.copy2` carry the source's mode bits onto the copy.  On
    Windows that sets the read-only attribute, and the next update-mode open
    fails with "Permission denied".

    Raises:
        PermissionError: If the read-only bit cannot be cleared.
    """
    if not os.path.exists(path):
        return

    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & stat.S_IWUSR:  # S_IWUSR == S_IWRITE (0o200); the only bit Windows honors
        return
    try:
        os.chmod(path, mode | stat.S_IWUSR)
    except OSError as e:
        raise PermissionError(f'Could not make {path} writable: {e}') from e


def copy_as_writable(src: str, dst: str) -> str:
    """Copy *src* to *dst* without inheriting the source's permission bits.

    Uses :func:`shutil.copyfile` rather than :func:`shutil.copy` so no
    ``copymode`` runs, and refuses a directory destination instead of silently
    redirecting into it the way :func:`shutil.copy` does.

    Raises:
        IsADirectoryError: If *dst* is an existing directory.
    """
    if os.path.isdir(dst):
        raise IsADirectoryError(f'Destination is a directory, not a file: {dst}')

    ensure_writable(dst)  # so copyfile can truncate a read-only existing target
    shutil.copyfile(src, dst)
    ensure_writable(dst)
    return dst


def _sidecar_of(name: str, dems: set[str], suffixes: tuple[str, ...]) -> bool:
    """True when *name* carries one of *suffixes* on the name or stem of one of *dems* (lower-cased names)."""
    lower = name.lower()
    for suffix in suffixes:
        if not lower.endswith(suffix):
            continue
        base = lower[: -len(suffix)]
        if base in dems or any(os.path.splitext(dem)[0] == base for dem in dems):
            return True
    return False


def copy_folder_structure(input_folder: str, output_folder: str, *, skip: Iterable[str] = ()) -> list[str]:
    """Recursively copy the folder structure and the auxiliary files from *input_folder*.

    The DEMs named in *skip* (the files the run transforms) are not copied,
    and neither are their pyramid and statistics sidecars (``.ovr``,
    ``.rrd``, ``.aux``, ``.aux.xml``), which would describe the source
    beside a transformed output, nor the DTED0 companion files (``.avg``,
    ``.min``, ``.max``) beside a ``.dt0`` input, whose statistics are in the
    source datum; those are returned so the run can say so. Everything else
    (metadata, licenses, other files) is copied so that the output mirrors
    the input layout, and every copy is made writable regardless of the
    source's permissions.
    """
    skipped = {os.path.normcase(os.path.abspath(path)) for path in skip}
    companions: list[str] = []
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    for root, dirs, files in os.walk(input_folder):
        for d in dirs:
            os.makedirs(
                os.path.join(output_folder, os.path.relpath(os.path.join(root, d), input_folder)),
                exist_ok=True,
            )
        dems_here = {
            f.lower() for f in files if os.path.normcase(os.path.abspath(os.path.join(root, f))) in skipped
        }
        dted0_here = {dem for dem in dems_here if dem.endswith('.dt0')}
        for f in files:
            source = os.path.join(root, f)
            if os.path.normcase(os.path.abspath(source)) in skipped or _sidecar_of(f, dems_here, DEM_SIDECAR_SUFFIXES):
                continue
            if _sidecar_of(f, dted0_here, DTED0_COMPANION_SUFFIXES):
                companions.append(source)
                continue
            destination = os.path.join(output_folder, os.path.relpath(source, input_folder))
            shutil.copy2(source, destination)
            ensure_writable(destination)
    return companions


def find_dems(folder: str, *, skipped: list[tuple[str, str]] | None = None) -> list[str]:
    """Every DEM under *folder*, in a sorted walk so a run is the same on every OS.

    Files with a supported extension that are not DEMs (masks, orthos,
    TanDEM-X auxiliary layers) are logged and left out. A file that could not
    be opened, or a folder that could not be read, is logged as a warning
    and recorded in *skipped* as ``(path, reason)`` when a list is given,
    so a run can say how many files it never saw.
    """
    logger = _state.get_logger()
    found = []

    def unreadable(error: OSError) -> None:
        reason = f'could not be read: {error.strerror or error}'
        logger.warning(f'Folder {error.filename} {reason}; the DEMs under it are not in this run.')
        if skipped is not None:
            skipped.append((error.filename, reason))

    for root, dirs, files in os.walk(folder, onerror=unreadable):
        dirs.sort()
        for name in sorted(files):
            if not name.lower().endswith(SUPPORTED_EXTENSIONS):
                continue
            path = os.path.join(root, name)
            problem = dem_problem(path)
            if problem is None:
                found.append(path)
            elif problem.startswith('could not be opened'):
                logger.warning(f'Skipping {name}: it {problem}')
                if skipped is not None:
                    skipped.append((path, problem))
            else:
                # Not a DEM (mask, ortho, TanDEM-X auxiliary). Skip it rather
                # than aborting the batch; the plain copy stays in the output.
                logger.info(f"Skipping {name} as it's not a DEM.")
    return found


def is_valid_filename(filename: str) -> bool:
    """Validate a filename against system and application constraints."""
    return (
        bool(filename)
        and not filename.isspace()
        and not any(char in filename for char in INVALID_CHARACTERS)
        and len(filename) <= 255
    )


def dem_problem(input_file: str) -> str | None:
    """Why *input_file* is not a usable single-band DEM, or None when it is.

    Rejects orthophotos and mask files by name (``INVALID_FILENAME_SUBSTRINGS``)
    and TanDEM-X/DGED auxiliary layers (AMP, EDM, HEM, WBM, ...) whose code
    appears as a whole token in the name; the name is the only thing that
    tells a Height Error Map from the DEM beside it, since both are single
    Float32 bands with the same georeferencing.  Then opens the file with GDAL
    (DTED included) and requires exactly one band that stores heights (a Byte
    or UInt16 band is a mask or amplitude layer whatever it is called) and a
    geotransform, so the tile can be placed against its neighbors. A file
    that cannot be opened (truncated, unreadable, a path too long) gives a
    reason starting with "could not be opened", so callers can tell it from
    a file that is not a DEM.
    """
    filename = os.path.basename(input_file)
    lower_filename = filename.lower()

    if any(keyword in lower_filename for keyword in INVALID_FILENAME_SUBSTRINGS):
        return 'is named like a mask or an image'
    if _AUXILIARY_LAYER_TOKEN.search(os.path.splitext(filename)[0]):
        return 'is named like an auxiliary layer'
    try:
        with gdal.Open(input_file, gdal.GA_ReadOnly) as ds:
            if ds.RasterCount != 1:
                return f'has {ds.RasterCount} bands, not one'
            if ds.GetRasterBand(1).DataType not in ELEVATION_DATA_TYPES:
                return 'has a band type that does not hold heights'
            if ds.GetGeoTransform(can_return_null=True) is None:
                return 'has no georeferencing'
    except Exception as e:
        return f'could not be opened: {str(e).strip() or type(e).__name__}'
    return None


def is_valid_dem(input_file: str) -> bool:
    """True when *input_file* is a usable single-band DEM (see :func:`dem_problem`)."""
    return dem_problem(input_file) is None
