"""File validation and folder-copy utilities.

Provides checks for valid filenames (system constraints and reserved terms
like TanDEM-X auxiliary products) and validation that a file is a usable
single-band DEM rather than an ortho, mask, or multi-band image.
"""

import os
import shutil

from osgeo import gdal

from egmtrans.config import INVALID_CHARACTERS, INVALID_FILENAMES


def copy_folder_structure(input_folder: str, output_folder: str) -> None:
    """Recursively copy the folder structure and all files from *input_folder*.

    Non-DEM auxiliary files (metadata, overviews, etc.) are copied alongside
    the DEMs so that the output directory mirrors the input layout.  The DEMs
    themselves are later overwritten by the transformed versions.
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
            shutil.copy2(
                os.path.join(root, f),
                os.path.join(output_folder, os.path.relpath(os.path.join(root, f), input_folder)),
            )


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
                if ds is None:
                    return False
                if ds.RasterCount > 1:
                    return False
        except Exception:
            return False

    return True
