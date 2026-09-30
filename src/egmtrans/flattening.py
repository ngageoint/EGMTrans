"""Flat area detection and processing.

Ocean and flat inland areas (water bodies, anthropogenic surfaces) must be
preserved during vertical datum transformation so that ocean stays at 0 and
flat patches keep a uniform elevation.  This module identifies those regions
by connected-component labeling and then sets every patch to the lowest of
its transformed values.

The minimum, rather than the mean, is what keeps a water body below its
shore: a shore post is at least as high as the water in the input, the
correction changes by millimeters between neighboring posts, and rounding is
monotonic, so ``round(h_shore - dN) >= round(h_water - dN) >= min`` at every
post without touching a single land post.  A mean would leave the shore at a
large lake's high-correction end below the water by up to half the range of
the correction across the lake (2 to 5 m across the largest lakes).
"""

from __future__ import annotations

import numpy as np
from osgeo import gdal

from egmtrans.numba_utils import get_numba_decorator, prange


@get_numba_decorator(parallel=True)
def create_labeled_array_flt(input_array: np.ndarray, min_patch_size: int = 16) -> np.ndarray:
    """Create a labeled array from an elevation array.

    Rounds elevations to the nearest centimeter to reduce noise, then
    identifies flat regions by flood-filling 4-connected neighbors with
    matching rounded values.  Small patches below *min_patch_size* are
    discarded.  On integer data (DTED read as float) the tolerance is exact
    equality, so every plateau of one whole-meter value is a patch.

    Label semantics:
    - ``0`` -- uneven terrain, or a void (NaN), which is never masked
    - ``1`` -- ocean (elevation within 1 cm of 0)
    - ``>1`` -- distinct flat-area patches, numbered in raster scan order of
      their first post, so the labels of a given array are reproducible

    The void test depends on Numba compiling without the ``nnan`` fastmath flag;
    see :data:`egmtrans.numba_utils.FASTMATH_FLAGS`.

    Args:
        input_array: 2-D float elevation array (NaN = nodata).
        min_patch_size: Minimum pixel count for a flat area to be retained.

    Returns:
        Integer label array with the same shape as the input.
    """
    rows, cols = input_array.shape
    valid_mask = ~np.isnan(input_array)
    result = np.zeros_like(input_array, dtype=np.int32)
    rounded = np.zeros_like(input_array)

    for i in prange(rows):
        for j in range(cols):
            if valid_mask[i, j]:
                rounded[i, j] = np.round(input_array[i, j], 2)

    for i in prange(rows):
        for j in range(cols):
            if valid_mask[i, j] and abs(rounded[i, j]) < 0.01:
                result[i, j] = 1

    current_label = 2
    processed = result > 0
    # One flat-index queue for every fill. A Python list of (i, j) tuples grew
    # to the size of the largest patch, which for a tile-sized lake is hundreds
    # of megabytes; the scan order and the neighbor order are unchanged, so the
    # labels are the same as before.
    queue = np.empty(rows * cols, dtype=np.int64)

    for i in range(rows):
        for j in range(cols):
            if not valid_mask[i, j] or processed[i, j]:
                continue

            value = rounded[i, j]
            queue[0] = i * cols + j
            head = 0
            tail = 1
            processed[i, j] = True

            while head < tail:
                ci = queue[head] // cols
                cj = queue[head] % cols
                head += 1
                for ni, nj in [(ci - 1, cj), (ci + 1, cj), (ci, cj - 1), (ci, cj + 1)]:
                    if (
                        0 <= ni < rows
                        and 0 <= nj < cols
                        and valid_mask[ni, nj]
                        and not processed[ni, nj]
                        and abs(rounded[ni, nj] - value) < 0.01
                    ):
                        queue[tail] = ni * cols + nj
                        tail += 1
                        processed[ni, nj] = True

            if tail >= min_patch_size:
                for k in range(tail):
                    result[queue[k] // cols, queue[k] % cols] = current_label
                current_label += 1

    return result


@get_numba_decorator()
def _patch_levels(output_data: np.ndarray, labeled_array: np.ndarray, level_override: np.ndarray):
    """Per-label minimum of *output_data*, lowered further by any finite override.

    Returns ``(levels, counts)`` indexed by label: ocean (label 1) is 0.0,
    unused labels are NaN, and *counts* holds the number of finite posts seen
    for each label so a caller can verify that a level computed elsewhere was
    computed from the same patch.
    """
    rows, cols = output_data.shape
    max_label = 0
    for i in range(rows):
        for j in range(cols):
            if labeled_array[i, j] > max_label:
                max_label = labeled_array[i, j]

    levels = np.full(max_label + 1, np.nan)
    counts = np.zeros(max_label + 1, dtype=np.int64)
    for i in range(rows):
        for j in range(cols):
            lbl = labeled_array[i, j]
            if lbl > 1:
                value = output_data[i, j]
                if not np.isnan(value):
                    counts[lbl] += 1
                    if np.isnan(levels[lbl]) or value < levels[lbl]:
                        levels[lbl] = value
            elif lbl == 1:
                levels[1] = 0.0
                counts[1] += 1

    for lbl in range(2, min(max_label + 1, level_override.shape[0])):
        override = level_override[lbl]
        if not np.isnan(override) and (np.isnan(levels[lbl]) or override < levels[lbl]):
            levels[lbl] = override

    return levels, counts


@get_numba_decorator(parallel=True)
def _apply_levels_parallel(output_data: np.ndarray, labeled_array: np.ndarray, levels: np.ndarray) -> np.ndarray:
    for i in prange(output_data.shape[0]):
        for j in range(output_data.shape[1]):
            lbl = labeled_array[i, j]
            if lbl == 1:
                output_data[i, j] = 0
            elif lbl > 1 and not np.isnan(levels[lbl]) and not np.isnan(output_data[i, j]):
                output_data[i, j] = levels[lbl]
    return output_data


@get_numba_decorator()
def _apply_levels_sequential(output_data: np.ndarray, labeled_array: np.ndarray, levels: np.ndarray) -> np.ndarray:
    for i in range(output_data.shape[0]):
        for j in range(output_data.shape[1]):
            lbl = labeled_array[i, j]
            if lbl == 1:
                output_data[i, j] = 0
            elif lbl > 1 and not np.isnan(levels[lbl]) and not np.isnan(output_data[i, j]):
                output_data[i, j] = levels[lbl]
    return output_data


# Share of a flat area's boundary that must lie above it for the area to count as
# a water body. Measured on the samples: slope contour bands and roofs fall
# between 0 and 80%, lakes, basins and coastal flats above it.
DEFAULT_CONTAINMENT = 0.8


@get_numba_decorator()
def patch_boundary_stats(input_data: np.ndarray, labeled_array: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Count, per patch, the boundary neighbors above and below it in the input.

    Every 4-neighbor of a patch post that carries a different label is a
    boundary adjacency: *above* when its input height is higher than the
    post's, *below* when lower.  Ocean neighbors (label 1) are neutral, since
    a lagoon or a river mouth is bounded by the sea on one side and is water
    all the same; voids and equal heights (within 5 mm) are neutral too.

    Returns:
        ``(above, below)``: int64 arrays indexed by label.
    """
    rows, cols = labeled_array.shape
    max_label = 0
    for i in range(rows):
        for j in range(cols):
            if labeled_array[i, j] > max_label:
                max_label = labeled_array[i, j]
    above = np.zeros(max_label + 1, dtype=np.int64)
    below = np.zeros(max_label + 1, dtype=np.int64)
    for i in range(rows):
        for j in range(cols):
            lbl = labeled_array[i, j]
            if lbl <= 1:
                continue
            height = input_data[i, j]
            for ni, nj in [(i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)]:
                if 0 <= ni < rows and 0 <= nj < cols:
                    other = labeled_array[ni, nj]
                    if other == lbl or other == 1:
                        continue
                    neighbor = input_data[ni, nj]
                    if np.isnan(neighbor):
                        continue
                    if neighbor > height + 0.005:
                        above[lbl] += 1
                    elif neighbor < height - 0.005:
                        below[lbl] += 1
    return above, below


def containment_fraction(above: np.ndarray, below: np.ndarray) -> np.ndarray:
    """``above / (above + below)`` per label; NaN where a patch has no boundary to compare."""
    total = above + below
    return np.where(total > 0, above / np.maximum(total, 1), np.nan)


def uncontained_labels(above: np.ndarray, below: np.ndarray, minimum: float) -> np.ndarray:
    """Labels above 1 whose containment is below *minimum*.

    A patch with no comparable boundary (an island of flat ground in voids, or
    a patch covering the whole tile) is kept, since nothing argues against it.
    """
    fraction = containment_fraction(above, below)
    labels = np.flatnonzero(~np.isnan(fraction) & (fraction < minimum))
    return labels[labels > 1].astype(np.int32)


def drop_labels(labeled_array: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Set the given labels to 0 (terrain) in place and return the array."""
    if labels.size:
        keep = np.ones(int(labeled_array.max()) + 1, dtype=bool)
        keep[labels] = False
        labeled_array[~keep[labeled_array]] = 0
    return labeled_array


@get_numba_decorator()
def _round_half_away_scalar(value: float) -> float:
    return np.floor(value + 0.5) if value >= 0 else np.ceil(value - 0.5)


@get_numba_decorator()
def containment_stats(
    input_data: np.ndarray,
    output_data: np.ndarray,
    labeled_array: np.ndarray,
    levels: np.ndarray,
    rounded: bool,
) -> tuple[int, int, int]:
    """Count shore posts and what the transform did to their step above the water.

    A shore post is an unlabeled, finite post with a 4-neighbor in a patch
    (label > 1; the ocean is left out, since land at or below 0 m beside the
    sea is a property of the input).  Only changes the transform introduced
    are counted: a post that was already below the flat area in the input (a
    plateau above its surroundings) is neither a lost step nor a defect.  With
    *rounded*, the post and the level are rounded to whole meters first, as a
    DTED write does.

    Returns:
        ``(shore, lost_step, below, already_below)``: shore posts; those that
        were above the neighboring patch in the input and are now exactly at
        its level; those that were at or above it and are now below it (zero by
        construction of the minimum rule; a nonzero count is a defect); and
        those that were already below it in the input (outlets, dam faces,
        dipping shores the source did not raise), which are left as they are.
    """
    rows, cols = output_data.shape
    shore = 0
    lost_step = 0
    below = 0
    already_below = 0
    for i in range(rows):
        for j in range(cols):
            if labeled_array[i, j] != 0:
                continue
            value = output_data[i, j]
            if np.isnan(value):
                continue
            is_shore = False
            is_below = False
            is_lost = False
            was_below = False
            for ni, nj in [(i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)]:
                if 0 <= ni < rows and 0 <= nj < cols:
                    lbl = labeled_array[ni, nj]
                    if lbl > 1:
                        level = levels[lbl]
                        if np.isnan(level):
                            continue
                        is_shore = True
                        # The neighbor carries the patch's input height (equal
                        # within the 1 cm labeling tolerance).
                        step_in = input_data[i, j] - input_data[ni, nj]
                        post = value
                        if rounded:
                            post = _round_half_away_scalar(value)
                            level = _round_half_away_scalar(level)
                        if step_in < -0.005:
                            was_below = True
                        elif post < level:
                            is_below = True
                        elif post == level and step_in > 0.005:
                            is_lost = True
            if is_shore:
                shore += 1
                if is_below:
                    below += 1
                elif is_lost:
                    lost_step += 1
                elif was_below:
                    already_below += 1
    return shore, lost_step, below, already_below


def _override_array(level_override: np.ndarray | None) -> np.ndarray:
    if level_override is None:
        return np.empty(0, dtype=np.float64)
    return np.ascontiguousarray(level_override, dtype=np.float64)


def patch_levels(
    output_data: np.ndarray, labeled_array: np.ndarray, level_override: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Return the level of every patch and the number of posts it was taken from.

    Args:
        output_data: Transformed elevation data.
        labeled_array: Label array from :func:`create_labeled_array_flt`.
        level_override: Optional float array indexed by label. A finite entry
            lower than the patch's own minimum replaces it (the same water body
            reaches a lower level in another tile); a higher entry is ignored,
            so a level from elsewhere can never lift water above its shore.

    Returns:
        ``(levels, counts)``: float64 and int64 arrays of length
        ``max_label + 1``. ``levels[1]`` is 0.0 (ocean), unused entries are NaN.
    """
    return _patch_levels(output_data, labeled_array, _override_array(level_override))


def apply_levels(
    output_data: np.ndarray, labeled_array: np.ndarray, levels: np.ndarray, parallel: bool = True
) -> np.ndarray:
    """Set ocean posts to 0 and every other labeled post to its patch level, in place.

    Uses Numba parallel execution unless *parallel* is False.  **Do not use
    the parallel path inside ArcGIS Pro**, which may become unstable.
    """
    levels = np.ascontiguousarray(levels, dtype=np.float64)
    if parallel:
        return _apply_levels_parallel(output_data, labeled_array, levels)
    return _apply_levels_sequential(output_data, labeled_array, levels)


def process_patches(
    output_data: np.ndarray, labeled_array: np.ndarray, level_override: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Process labeled patches: set ocean to 0, set flat areas to their minimum.

    For each patch with label > 1, replaces every pixel's value with the lowest
    transformed value in the patch (or a lower *level_override*, see
    :func:`patch_levels`).  Ocean pixels (label == 1) are set to 0.

    Uses Numba parallel execution -- **do not use inside ArcGIS Pro**, which
    may become unstable.  See :func:`process_patches_arcpy` for a safe variant.

    Args:
        output_data: Transformed elevation data (modified in place).
        labeled_array: Label array from :func:`create_labeled_array_flt`.
        level_override: Optional per-label levels from other tiles.

    Returns:
        ``(output_data, levels)``: the modified array and the level of every
        label as returned by :func:`patch_levels`.
    """
    levels, _counts = patch_levels(output_data, labeled_array, level_override)
    return apply_levels(output_data, labeled_array, levels, parallel=True), levels


def process_patches_arcpy(
    output_data: np.ndarray, labeled_array: np.ndarray, level_override: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Process patches without parallel execution (safe for ArcGIS Pro).

    Identical logic to :func:`process_patches` but uses sequential loops
    because Numba's parallel execution causes ArcGIS Pro to become unstable
    and crash.
    """
    levels, _counts = patch_levels(output_data, labeled_array, level_override)
    return apply_levels(output_data, labeled_array, levels, parallel=False), levels


def create_flat_mask(
    labeled_array: np.ndarray,
    mask_file: str,
    geotransform: tuple[float, float, float, float, float, float],
    projection: str,
) -> None:
    """Write the labeled array as a UInt32 GeoTIFF mask.

    Creates a DEFLATE-compressed GeoTIFF with the given geotransform and
    projection.  Useful for QC inspection of the ocean and flat-area regions
    that were detected.

    Args:
        labeled_array: Integer label array from :func:`create_labeled_array_flt`.
        mask_file: Output path for the mask GeoTIFF.
        geotransform: GDAL geotransform of the input DEM.
        projection: WKT projection of the input DEM.

    Raises:
        RuntimeError: If GDAL cannot create the output file.
    """
    rows, cols = labeled_array.shape
    driver = gdal.GetDriverByName('GTiff')
    creation_options = ['COMPRESS=DEFLATE', 'PREDICTOR=2']
    mask_ds = driver.Create(mask_file, cols, rows, 1, gdal.GDT_UInt32, options=creation_options)

    mask_ds.SetGeoTransform(geotransform)
    mask_ds.SetProjection(projection)

    mask_band = mask_ds.GetRasterBand(1)
    mask_band.WriteArray(labeled_array)
    mask_band.SetNoDataValue(0)
    # A live band reference would keep the dataset, and the file, from closing.
    mask_band = None
    mask_ds.Close()
