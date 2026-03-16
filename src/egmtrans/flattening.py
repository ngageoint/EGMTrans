"""Flat area detection and processing.

Ocean and flat inland areas (water bodies, anthropogenic surfaces) must be
preserved during vertical datum transformation so that ocean stays at 0 and
flat patches keep a uniform elevation.  This module identifies those regions
by connected-component labelling and then averages transformed values within
each patch to remove interpolation noise.
"""

from __future__ import annotations

import numpy as np
from osgeo import gdal

from egmtrans.numba_utils import get_numba_decorator, prange


@get_numba_decorator()
def label(array: np.ndarray) -> tuple[np.ndarray, int]:
    """Two-pass connected-component labelling (4-connectivity) on a binary array.

    Args:
        array: 2-D binary array where True values are the objects to label.

    Returns:
        ``(labeled, label_count)`` — *labeled* has the same shape as the input
        with each connected component assigned a unique integer, and
        *label_count* is the number of components found.
    """
    labeled = np.zeros_like(array, dtype=np.int32)
    label_count = 0
    equivalences = {}

    for i in range(array.shape[0]):
        for j in range(array.shape[1]):
            if array[i, j]:
                neighbors = []
                if i > 0 and labeled[i - 1, j] > 0:
                    neighbors.append(labeled[i - 1, j])
                if j > 0 and labeled[i, j - 1] > 0:
                    neighbors.append(labeled[i, j - 1])

                if not neighbors:
                    label_count += 1
                    labeled[i, j] = label_count
                else:
                    min_label = min(neighbors)
                    labeled[i, j] = min_label
                    for n in neighbors:
                        if n != min_label:
                            equivalences[n] = min_label

    for i in range(array.shape[0]):
        for j in range(array.shape[1]):
            if labeled[i, j] > 0:
                current_label = labeled[i, j]
                while current_label in equivalences:
                    current_label = equivalences[current_label]
                labeled[i, j] = current_label

    return labeled, label_count


@get_numba_decorator(parallel=True)
def create_labeled_array_flt(input_array: np.ndarray, min_patch_size: int = 16) -> np.ndarray:
    """Create a labeled array from a floating-point elevation array.

    Rounds elevations to the nearest centimetre to reduce noise, then
    identifies flat regions by flood-filling 4-connected neighbours with
    matching rounded values.  Small patches below *min_patch_size* are
    discarded.

    Label semantics:
    - ``0`` — uneven terrain (not masked)
    - ``1`` — ocean (elevation ≈ 0)
    - ``>1`` — distinct flat-area patches

    Args:
        input_array: 2-D float elevation array (NaN = nodata).
        min_patch_size: Minimum pixel count for a flat area to be retained.

    Returns:
        Integer label array with the same shape as the input.
    """
    valid_mask = ~np.isnan(input_array)
    result = np.zeros_like(input_array, dtype=np.int32)
    rounded = np.zeros_like(input_array)

    for i in prange(input_array.shape[0]):
        for j in range(input_array.shape[1]):
            if valid_mask[i, j]:
                rounded[i, j] = np.round(input_array[i, j], 2)

    for i in prange(input_array.shape[0]):
        for j in range(input_array.shape[1]):
            if valid_mask[i, j] and abs(rounded[i, j]) < 0.01:
                result[i, j] = 1

    current_label = 2
    processed = result > 0

    for i in range(input_array.shape[0]):
        for j in range(input_array.shape[1]):
            if not valid_mask[i, j] or processed[i, j]:
                continue

            value = rounded[i, j]
            region = [(i, j)]
            processed[i, j] = True

            k = 0
            while k < len(region):
                ci, cj = region[k]
                k += 1
                for ni, nj in [(ci - 1, cj), (ci + 1, cj), (ci, cj - 1), (ci, cj + 1)]:
                    if (
                        0 <= ni < input_array.shape[0]
                        and 0 <= nj < input_array.shape[1]
                        and valid_mask[ni, nj]
                        and not processed[ni, nj]
                        and abs(rounded[ni, nj] - value) < 0.01
                    ):
                        region.append((ni, nj))
                        processed[ni, nj] = True

            if len(region) >= min_patch_size:
                for ri, rj in region:
                    result[ri, rj] = current_label
                current_label += 1

    return result


def create_labeled_array_int(input_array: np.ndarray) -> np.ndarray:
    """Create a labeled array for integer elevation data (DTED).

    Only labels ocean (elevation == 0) as label 1.  Flat-patch detection is
    skipped because DTED values are in whole metres — a single file may have
    thousands of contiguous integer-valued regions, and the EGM96-to-EGM2008
    delta varies by much less than 1 m over moderately sized areas, so inland
    flat areas are effectively preserved without explicit labelling.

    Args:
        input_array: 2-D integer elevation array.

    Returns:
        Label array where ocean pixels are 1 and everything else is 0.
    """
    ocean_mask = input_array == 0
    labeled_array = np.zeros_like(input_array, dtype=np.int32)
    labeled_array[ocean_mask] = 1
    return labeled_array


@get_numba_decorator(parallel=True)
def process_patches(output_data: np.ndarray, labeled_array: np.ndarray) -> np.ndarray:
    """Process labeled patches: set ocean to 0, average flat areas.

    For each patch with label > 1, replaces every pixel's value with the
    patch mean.  Ocean pixels (label == 1) are set to 0.

    Uses Numba parallel execution — **do not use inside ArcGIS Pro**, which
    may become unstable.  See :func:`process_patches_arcpy` for a safe variant.

    Args:
        output_data: Transformed elevation data (modified in place).
        labeled_array: Label array from :func:`create_labeled_array_flt` or
            :func:`create_labeled_array_int`.

    Returns:
        The modified *output_data* array.
    """
    unique_labels = np.unique(labeled_array)
    max_label = np.max(unique_labels)

    sums = np.zeros(max_label + 1, dtype=np.float64)
    counts = np.zeros(max_label + 1, dtype=np.int32)

    for i in range(output_data.shape[0]):
        for j in range(output_data.shape[1]):
            lbl = labeled_array[i, j]
            if lbl > 1:
                sums[lbl] += output_data[i, j]
                counts[lbl] += 1
            elif lbl == 1:
                output_data[i, j] = 0

    averages = np.zeros(max_label + 1, dtype=np.float32)
    for lbl in range(2, max_label + 1):
        if counts[lbl] > 0:
            averages[lbl] = sums[lbl] / counts[lbl]

    for i in prange(output_data.shape[0]):
        for j in range(output_data.shape[1]):
            lbl = labeled_array[i, j]
            if lbl > 1:
                output_data[i, j] = averages[lbl]

    return output_data


@get_numba_decorator(arc_safe=True)
def process_patches_arcpy(output_data: np.ndarray, labeled_array: np.ndarray) -> np.ndarray:
    """Process patches without parallel execution (safe for ArcGIS Pro).

    Identical logic to :func:`process_patches` but uses sequential loops
    because Numba's parallel execution causes ArcGIS Pro to become unstable
    and crash.

    Args:
        output_data: Transformed elevation data (modified in place).
        labeled_array: Label array from the labelling functions.

    Returns:
        The modified *output_data* array.
    """
    unique_labels = np.unique(labeled_array)
    max_label = np.max(unique_labels)

    sums = np.zeros(max_label + 1, dtype=np.float64)
    counts = np.zeros(max_label + 1, dtype=np.int32)

    for i in range(output_data.shape[0]):
        for j in range(output_data.shape[1]):
            lbl = labeled_array[i, j]
            if lbl > 1:
                sums[lbl] += output_data[i, j]
                counts[lbl] += 1
            elif lbl == 1:
                output_data[i, j] = 0

    averages = np.zeros(max_label + 1, dtype=np.float32)
    for lbl in range(2, max_label + 1):
        if counts[lbl] > 0:
            averages[lbl] = sums[lbl] / counts[lbl]

    for i in range(output_data.shape[0]):
        for j in range(output_data.shape[1]):
            lbl = labeled_array[i, j]
            if lbl > 1:
                output_data[i, j] = averages[lbl]

    return output_data


def create_flat_mask(labeled_array: np.ndarray, mask_file: str, template_ds: gdal.Dataset) -> None:
    """Write the labeled array as a UInt32 GeoTIFF mask.

    Creates a DEFLATE-compressed GeoTIFF with the same geotransform and
    projection as *template_ds*.  Useful for QC inspection of the ocean and
    flat-area regions that were detected.

    Args:
        labeled_array: Integer label array from the labelling functions.
        mask_file: Output path for the mask GeoTIFF.
        template_ds: GDAL dataset to copy geotransform and projection from.

    Raises:
        RuntimeError: If the output file cannot be created.
    """
    driver = gdal.GetDriverByName('GTiff')
    creation_options = ['COMPRESS=DEFLATE', 'PREDICTOR=2']
    mask_ds = driver.Create(
        mask_file,
        template_ds.RasterXSize, template_ds.RasterYSize,
        1, gdal.GDT_UInt32,
        options=creation_options,
    )
    if mask_ds is None:
        raise RuntimeError(f'Failed to create output file: {mask_file}')

    mask_ds.SetGeoTransform(template_ds.GetGeoTransform())
    mask_ds.SetProjection(template_ds.GetProjection())

    mask_band = mask_ds.GetRasterBand(1)
    mask_band.WriteArray(labeled_array)
    mask_band.SetNoDataValue(0)
    mask_band.FlushCache()
    mask_ds = None
