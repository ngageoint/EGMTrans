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
import shutil
import stat
from dataclasses import dataclass

from osgeo import gdal

from egmtrans import _state
from egmtrans.config import INVALID_CHARACTERS, INVALID_FILENAMES, SUPPORTED_EXTENSIONS


@dataclass(frozen=True)
class IOPaths:
    """Resolved input/output locations for one EGMTrans run.

    Attributes:
        input_path: The normalised input file or folder.
        output_path: When *mode* is ``'file'`` this is the full path of the file
            to write (already joined with the input's basename if the caller
            supplied a folder).  When *mode* is ``'folder'`` it is the folder.
        mode: ``'file'`` for a single transform, ``'folder'`` for a batch run.
        log_path: Where the transform log belongs.  Derived, never created —
            the caller decides whether logging is enabled.
    """

    input_path: str
    output_path: str
    mode: str
    log_path: str


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
    return os.path.join(folder, f'{os.path.basename(folder)}_transform.log')


def resolve_io_paths(input_path: str, output_path: str) -> IOPaths:
    """Decide whether *output_path* names a file or a folder, and resolve both.

    Classification, in order:

    1. An existing directory is always a folder (so folder names containing
       dots keep working).
    2. Otherwise a supported raster extension means a file.
    3. Otherwise a folder — with a warning if the name carries some other
       extension, since that is more likely a typo than an intent.

    A file input written to a folder output is resolved to
    ``<folder>/<input basename>``, so callers always receive a concrete file
    path in :attr:`IOPaths.output_path`.

    Raises:
        ValueError: If either path is empty, the input does not exist, the
            output basename is not a legal filename, or a folder of DEMs was
            aimed at a single output file.
    """
    logger = _state.get_logger()

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
    if not is_valid_filename(output_name):
        raise ValueError(f'Invalid output name: {output_name!r}')

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
    elif input_is_file:
        mode = 'file'
        resolved_output = os.path.join(output_path, os.path.basename(input_path))
    else:
        mode, resolved_output = 'folder', output_path

    return IOPaths(
        input_path=input_path,
        output_path=resolved_output,
        mode=mode,
        log_path=derive_log_path(resolved_output, mode),
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
    if mode & stat.S_IWUSR:  # S_IWUSR == S_IWRITE (0o200); the only bit Windows honours
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


def copy_folder_structure(input_folder: str, output_folder: str) -> None:
    """Recursively copy the folder structure and all files from *input_folder*.

    Non-DEM auxiliary files (metadata, overviews, etc.) are copied alongside
    the DEMs so that the output directory mirrors the input layout.  The DEMs
    themselves are later overwritten by the transformed versions, so every copy
    is made writable regardless of the source's permissions.
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    for root, dirs, files in os.walk(input_folder):
        for d in dirs:
            os.makedirs(
                os.path.join(output_folder, os.path.relpath(os.path.join(root, d), input_folder)),
                exist_ok=True,
            )
        for f in files:
            destination = os.path.join(
                output_folder, os.path.relpath(os.path.join(root, f), input_folder)
            )
            shutil.copy2(os.path.join(root, f), destination)
            ensure_writable(destination)


def is_valid_filename(filename: str) -> bool:
    """Validate a filename against system and application constraints."""
    return (
        bool(filename)
        and not filename.isspace()
        and not any(char in filename for char in INVALID_CHARACTERS)
        and len(filename) <= 255
    )


def is_valid_dem(input_file: str) -> bool:
    """Validate if a file is a valid single-band Digital Elevation Model.

    Rejects files whose names contain reserved keywords (``INVALID_FILENAMES``)
    such as TanDEM-X auxiliary products (AMP, EDM, HEM, etc.), orthophotos,
    and mask files.  For GeoTIFF files, also verifies the file can be opened
    by GDAL and has exactly one band (multi-band TIFFs are skipped).
    """
    lower_filename = os.path.basename(input_file).lower()

    if any(keyword in lower_filename for keyword in INVALID_FILENAMES):
        return False
    if input_file.lower().endswith(('.tif', '.tiff')):
        try:
            with gdal.Open(input_file, gdal.GA_ReadOnly) as ds:
                if ds.RasterCount > 1:
                    return False
        except Exception:
            return False

    return True
