"""Interpolation algorithms — bilinear, Delaunay triangulation, and thin-plate spline (RBF).

Each public function accepts a dictionary of source points (``x``, ``y``, ``z``
arrays from the clipped datum grid) and 2-D meshgrid arrays (``xx``, ``yy``)
representing the output raster pixel centres.  Data is processed in chunks for
memory efficiency and, where possible, accelerated by Numba JIT compilation.
"""

from __future__ import annotations

import numpy as np
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
from scipy.spatial import Delaunay

from egmtrans import _state
from egmtrans.numba_utils import get_numba_decorator, prange, tqdm

# ---------------------------------------------------------------------------
# RBF helpers (numba-accelerated)
# ---------------------------------------------------------------------------

@get_numba_decorator()
def compute_rbf_weights(coords: np.ndarray, values: np.ndarray, epsilon: float = 0.1) -> np.ndarray:
    """Compute weights for RBF interpolation using the thin-plate spline kernel.

    Constructs and solves the system ``A @ w = z`` where each element of the
    interpolation matrix is ``A[i,j] = r² ln(r + ε)`` (with *r* being the
    Euclidean distance between points *i* and *j*).

    Args:
        coords: Array of shape ``(n_points, 2)`` with x, y coordinates.
        values: Array of shape ``(n_points,)`` with z-values at those points.
        epsilon: Small constant to prevent singularity at ``r = 0``.

    Returns:
        Array of shape ``(n_points,)`` containing the computed RBF weights.
    """
    n_points = coords.shape[0]
    A = np.empty((n_points, n_points), dtype=np.float32)

    for i in prange(n_points):
        for j in range(n_points):
            dx = coords[i, 0] - coords[j, 0]
            dy = coords[i, 1] - coords[j, 1]
            r = np.sqrt(dx * dx + dy * dy)
            A[i, j] = r * r * np.log(r + epsilon) if r > 0 else 0

    weights = np.linalg.solve(A, values)
    return weights


@get_numba_decorator()
def interpolate_chunk(
    source_coords: np.ndarray, weights: np.ndarray, chunk_points: np.ndarray, epsilon: float = 0.1
) -> np.ndarray:
    """Interpolate values for a chunk of target points using pre-computed RBF weights.

    Uses the same thin-plate spline kernel as :func:`compute_rbf_weights`.
    Processing is chunked by the caller to limit peak memory usage.

    Args:
        source_coords: Array ``(n_points, 2)`` of original x, y coordinates.
        weights: Pre-computed RBF weights from :func:`compute_rbf_weights`.
        chunk_points: Array ``(n_chunk, 2)`` of target points to interpolate.
        epsilon: Singularity-prevention constant (must match weight computation).

    Returns:
        Array ``(n_chunk,)`` of interpolated values.
    """
    n_points = source_coords.shape[0]
    n_chunk = chunk_points.shape[0]
    result = np.zeros(n_chunk, dtype=np.float32)

    for i in prange(n_chunk):
        for j in range(n_points):
            dx = chunk_points[i, 0] - source_coords[j, 0]
            dy = chunk_points[i, 1] - source_coords[j, 1]
            r = np.sqrt(dx * dx + dy * dy)
            result[i] += weights[j] * (r * r * np.log(r + epsilon) if r > 0 else 0)

    return result


# ---------------------------------------------------------------------------
# Bilinear interpolation
# ---------------------------------------------------------------------------

@get_numba_decorator()
def _bilinear_interpolate_numba(x_vals, y_vals, grid, x_min, y_min, x_step, y_step, nx, ny):
    """Low-level numba-accelerated bilinear interpolation on a regular grid."""
    n_points = len(x_vals)
    result = np.empty(n_points, dtype=np.float32)

    for i in prange(n_points):
        x = x_vals[i]
        y = y_vals[i]

        x_idx = (x - x_min) / x_step
        y_idx = (y - y_min) / y_step

        x0 = int(np.floor(x_idx))
        y0 = int(np.floor(y_idx))
        x1 = x0 + 1
        y1 = y0 + 1

        if x0 < 0 or x1 >= nx or y0 < 0 or y1 >= ny:
            x0 = max(0, min(nx - 1, x0))
            x1 = max(0, min(nx - 1, x1))
            y0 = max(0, min(ny - 1, y0))
            y1 = max(0, min(ny - 1, y1))
            if x0 == x1 or y0 == y1:
                result[i] = grid[y0, x0]
                continue

        wx = x_idx - x0
        wy = y_idx - y0

        v00 = grid[y0, x0]
        v01 = grid[y1, x0]
        v10 = grid[y0, x1]
        v11 = grid[y1, x1]

        v0 = v00 * (1 - wx) + v10 * wx
        v1 = v01 * (1 - wx) + v11 * wx
        result[i] = v0 * (1 - wy) + v1 * wy

    return result


def bilinear_interpolation(points: dict[str, np.ndarray], xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
    """Perform bilinear interpolation on a regular grid, with Delaunay fallback.

    Workflow:
    1. Detects whether the input points form a regular grid.
    2. If regular, builds a lookup grid and uses Numba-accelerated bilinear
       interpolation (:func:`_bilinear_interpolate_numba`).
    3. If irregular, falls back to SciPy Delaunay triangulation with
       nearest-neighbour fill for NaN gaps.

    Data is processed in row-chunks whose size is adapted to the estimated
    memory footprint (smaller chunks for datasets > 1 GB).

    Args:
        points: Dict with ``'x'``, ``'y'``, ``'z'`` arrays from the datum grid.
        xx: 2-D meshgrid of x-coordinates for the output raster.
        yy: 2-D meshgrid of y-coordinates for the output raster.

    Returns:
        2-D array of interpolated values matching the shape of *xx* / *yy*.
    """
    logger = _state.get_logger()

    try:
        unique_x = np.unique(points['x'])
        unique_y = np.unique(points['y'])

        expected_points = len(unique_x) * len(unique_y)
        actual_points = len(points['x'])
        is_regular_grid = expected_points == actual_points

        if not is_regular_grid:
            logger.warning("Input points do not form a regular grid. Falling back to Delaunay triangulation.")

            n_points = len(points['x'])
            source_points = np.empty((n_points, 2), dtype=np.float32)
            source_points[:, 0] = points['x']
            source_points[:, 1] = points['y']
            values = points['z'].astype(np.float32)

            tri = Delaunay(source_points)
            lin_interp = LinearNDInterpolator(tri, values)
            nearest_interp = NearestNDInterpolator(tri, values)

            n_rows, n_cols = xx.shape
            result = np.empty(xx.shape, dtype=np.float32)
            chunk_size = max(50, min(200, n_rows // 10))

            for i in range(0, n_rows, chunk_size):
                end_idx = min(i + chunk_size, n_rows)
                xx_chunk = xx[i:end_idx, :]
                yy_chunk = yy[i:end_idx, :]
                n_chunk_rows = end_idx - i
                points_to_interp = np.column_stack((xx_chunk.ravel(), yy_chunk.ravel()))
                chunk_result = lin_interp(points_to_interp)
                nan_mask = np.isnan(chunk_result)
                if np.any(nan_mask):
                    chunk_result[nan_mask] = nearest_interp(points_to_interp[nan_mask])
                result[i:end_idx, :] = chunk_result.reshape((n_chunk_rows, xx.shape[1]))

            return result

        unique_x.sort()
        unique_y.sort()

        grid_z = np.zeros((len(unique_y), len(unique_x)), dtype=np.float32)

        x_indices = {}
        for i, x in enumerate(unique_x):
            x_indices[x] = i
        y_indices = {}
        for i, y in enumerate(unique_y):
            y_indices[y] = i

        for i in range(len(points['x'])):
            ix = x_indices[points['x'][i]]
            iy = y_indices[points['y'][i]]
            grid_z[iy, ix] = points['z'][i]

        x_min, x_max = unique_x[0], unique_x[-1]
        y_min, y_max = unique_y[0], unique_y[-1]
        x_step = (x_max - x_min) / (len(unique_x) - 1) if len(unique_x) > 1 else 1.0
        y_step = (y_max - y_min) / (len(unique_y) - 1) if len(unique_y) > 1 else 1.0

        n_rows, n_cols = xx.shape
        result = np.empty(xx.shape, dtype=np.float32)

        estimated_memory_mb = (len(unique_x) * len(unique_y) * 4) / (1024 * 1024)
        estimated_output_mb = n_rows * n_cols * 4 / (1024 * 1024)
        total_estimated_mb = estimated_memory_mb + estimated_output_mb

        if total_estimated_mb > 1000:
            logger.info(f"Large dataset detected (est. {total_estimated_mb:.1f}MB). Using memory-efficient settings.")
            chunk_size = max(20, min(50, n_rows // 20))
        else:
            chunk_size = max(50, min(200, n_rows // 10))

        with tqdm(total=n_rows, desc="Bilinear Interpolation", unit="rows") as pbar:
            for i in range(0, n_rows, chunk_size):
                end_idx = min(i + chunk_size, n_rows)
                xx_chunk = xx[i:end_idx, :].astype(np.float32)
                yy_chunk = yy[i:end_idx, :].astype(np.float32)

                chunk_result = _bilinear_interpolate_numba(
                    xx_chunk.ravel(), yy_chunk.ravel(),
                    grid_z, x_min, y_min, x_step, y_step,
                    len(unique_x), len(unique_y),
                )
                result[i:end_idx, :] = chunk_result.reshape((end_idx - i, xx.shape[1]))
                pbar.update(end_idx - i)

            return result

    except Exception as e:
        logger.error(f"Bilinear interpolation failed: {str(e)}")
        raise


# ---------------------------------------------------------------------------
# Delaunay triangulation
# ---------------------------------------------------------------------------

def delaunay_triangulation(points: dict[str, np.ndarray], xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
    """Perform interpolation using SciPy Delaunay triangulation.

    More accurate than bilinear for irregular point distributions, but slower
    and tends to produce more visible mesh edges for regular grids.  Uses
    ``LinearNDInterpolator`` with ``NearestNDInterpolator`` fallback for NaN
    regions at the convex-hull boundary.  Data is chunked for memory efficiency.

    Args:
        points: Dict with ``'x'``, ``'y'``, ``'z'`` arrays from the datum grid.
        xx: 2-D meshgrid of x-coordinates for the output raster.
        yy: 2-D meshgrid of y-coordinates for the output raster.

    Returns:
        2-D array of interpolated values matching the shape of *xx* / *yy*.
    """
    logger = _state.get_logger()

    try:
        n_points = len(points['x'])
        source_points = np.empty((n_points, 2), dtype=np.float32)
        source_points[:, 0] = points['x']
        source_points[:, 1] = points['y']
        values = points['z'].astype(np.float32)

        n_rows, n_cols = xx.shape
        result = np.empty(xx.shape, dtype=np.float32)

        estimated_memory_mb = n_points * 2 * 4 / (1024 * 1024)
        estimated_output_mb = n_rows * n_cols * 4 / (1024 * 1024)
        total_estimated_mb = estimated_memory_mb + estimated_output_mb

        if total_estimated_mb > 1000:
            chunk_size = max(20, min(50, n_rows // 20))
        else:
            chunk_size = max(50, min(200, n_rows // 10))
        n_chunks = int(np.ceil(n_rows / chunk_size))

        tri = Delaunay(source_points)
        lin_interp = LinearNDInterpolator(tri, values)
        nearest_interp = NearestNDInterpolator(tri, values)

        with tqdm(total=n_rows, desc="Delaunay Triangulation", unit="rows") as pbar:
            for i in range(0, n_rows, chunk_size):
                end_idx = min(i + chunk_size, n_rows)
                n_chunk_rows = end_idx - i

                try:
                    xx_chunk = xx[i:end_idx, :]
                    yy_chunk = yy[i:end_idx, :]
                    points_to_interp = np.column_stack((xx_chunk.ravel(), yy_chunk.ravel()))
                    chunk_result = lin_interp(points_to_interp)
                    nan_mask = np.isnan(chunk_result)
                    if np.any(nan_mask):
                        chunk_result[nan_mask] = nearest_interp(points_to_interp[nan_mask])
                    result[i:end_idx, :] = chunk_result.reshape((n_chunk_rows, xx.shape[1]))
                except Exception as e:
                    logger.warning(f"Optimized processing failed for chunk {i // chunk_size + 1}/{n_chunks}: {str(e)}")
                    logger.warning("Retrying with pure scipy implementation")
                    xx_chunk = xx[i:end_idx, :]
                    yy_chunk = yy[i:end_idx, :]
                    points_to_interp = np.column_stack((xx_chunk.ravel(), yy_chunk.ravel()))
                    chunk_result = lin_interp(points_to_interp)
                    nan_mask = np.isnan(chunk_result)
                    if np.any(nan_mask):
                        chunk_result[nan_mask] = nearest_interp(points_to_interp[nan_mask])
                    result[i:end_idx, :] = chunk_result.reshape((n_chunk_rows, xx.shape[1]))

                pbar.update(n_chunk_rows)

        return result

    except Exception as e:
        logger.error(f'Error in linear_interpolation: {str(e)}')
        raise


# ---------------------------------------------------------------------------
# Spline (RBF) interpolation
# ---------------------------------------------------------------------------

def spline_interpolation(points: dict[str, np.ndarray], xx: np.ndarray, yy: np.ndarray) -> np.ndarray:
    """Perform thin-plate spline interpolation using Numba-accelerated RBF.

    Orchestrates the RBF workflow:
    1. Converts input points to float32 coordinate/value arrays.
    2. Computes RBF weights via :func:`compute_rbf_weights`.
    3. Interpolates the output grid in row-chunks via :func:`interpolate_chunk`.

    Highest accuracy of the three algorithms, but significantly slower —
    best suited for sparse input points and moderate output grids.

    Args:
        points: Dict with ``'x'``, ``'y'``, ``'z'`` arrays from the datum grid.
        xx: 2-D meshgrid of x-coordinates for the output raster.
        yy: 2-D meshgrid of y-coordinates for the output raster.

    Returns:
        2-D array of interpolated values matching the shape of *xx* / *yy*.
    """
    logger = _state.get_logger()

    try:
        n_points = len(points['x'])
        coords = np.empty((n_points, 2), dtype=np.float32)
        coords[:, 0] = points['x']
        coords[:, 1] = points['y']
        values = points['z'].astype(np.float32)

        weights = compute_rbf_weights(coords, values)

        chunk_size = 100
        n_rows = xx.shape[0]
        result = np.empty(xx.shape, dtype=np.float32)
        chunk_points = np.empty((chunk_size * xx.shape[1], 2), dtype=np.float32)

        with tqdm(total=n_rows, desc="Spline Interpolation ", unit="rows") as pbar:
            for start_idx in range(0, n_rows, chunk_size):
                end_idx = min(start_idx + chunk_size, n_rows)
                n_chunk_rows = end_idx - start_idx

                chunk_size_actual = n_chunk_rows * xx.shape[1]
                chunk_points_view = chunk_points[:chunk_size_actual]
                chunk_points_view[:, 0] = xx[start_idx:end_idx, :].ravel()
                chunk_points_view[:, 1] = yy[start_idx:end_idx, :].ravel()

                chunk_result = interpolate_chunk(coords, weights, chunk_points_view)
                result[start_idx:end_idx, :] = chunk_result.reshape((n_chunk_rows, xx.shape[1]))
                pbar.update(n_chunk_rows)

        return result

    except Exception as e:
        logger.error(f'Error in spline_interpolation: {str(e)}')
        raise
