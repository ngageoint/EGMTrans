"""The DTED Level 0 companion files of MIL-PRF-89020B 3.9.3: ``.avg``, ``.min`` and ``.max``.

Each file holds, for every DTED0 post, one statistic of the DTED Level 1
posts in the 30" x 30" area around the post: the average, the minimum or the
maximum of 11 x 11 posts ("Each 30" x 30" area of DTED1 (11 posts x 11
posts) are searched for the minimum and maximum value; while the average is
calculated from the 121 values"). The rows and columns of posts that divide
adjacent areas count in both, as the specification notes. The area is one
DTED0 spacing wide on each axis in every zone, since DTED1 posts are ten
times as dense as DTED0 posts on both axes; at the cell edges it is clipped
to the cell ("This file shall not cross whole degree latitude and longitude
lines"). Voids are left out, an area with no valid post is void, and the
average is rounded to the nearest meter, halves away from zero, like the
posts themselves. The files carry the cell's header verbatim (each "consists
of the User Header Label (UHL), Data Set Identification (DSI) Record,
Accuracy Description Record (ACC) and the Data records") and are written and
verified by the DTED codec exactly like the cell.
"""

from __future__ import annotations

import numpy as np

from egmtrans.dted.resample import THIN_STEP
from egmtrans.dted.schema import NULL_ELEVATION

COMPANION_EXTENSIONS = ('.avg', '.min', '.max')
# DTED1 posts on either side of a DTED0 post that fall within its 30" area.
HALF_WINDOW = 5
# DTED1 posts per DTED0 post on each axis.
STEP = THIN_STEP[0] // THIN_STEP[1]


def _round_half_away(array: np.ndarray) -> np.ndarray:
    return np.where(array >= 0, np.floor(array + 0.5), np.ceil(array - 0.5))


def dted0_companions(level1: np.ndarray) -> dict[str, np.ndarray]:
    """The ``avg``, ``min`` and ``max`` arrays of the DTED0 cell whose DTED1
    grid is *level1* (rows north to south, whole meters, voids as NaN or
    -32767), as int32 arrays with voids as -32767.

    Raises:
        ValueError: If *level1* is not a full DTED1 cell grid.
    """
    values = np.asarray(level1, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError(f'a DTED1 grid is a 2-D array of posts, not {values.ndim}-D')
    values = np.where(values == NULL_ELEVATION, np.nan, values)
    rows1, cols1 = values.shape
    if (rows1 - 1) % STEP or (cols1 - 1) % STEP:
        raise ValueError(f'a DTED1 grid of {cols1} x {rows1} posts does not thin to DTED0 by {STEP}')
    rows0, cols0 = (rows1 - 1) // STEP + 1, (cols1 - 1) // STEP + 1

    # NaN padding of five posts: a window at the cell edge then reaches only
    # the posts inside the cell.
    padded = np.full((rows1 + 2 * HALF_WINDOW, cols1 + 2 * HALF_WINDOW), np.nan)
    padded[HALF_WINDOW:-HALF_WINDOW, HALF_WINDOW:-HALF_WINDOW] = values
    offsets = np.arange(2 * HALF_WINDOW + 1)
    row_index = (np.arange(rows0) * STEP)[:, None] + offsets
    col_index = (np.arange(cols0) * STEP)[:, None] + offsets
    windows = padded[row_index[:, None, :, None], col_index[None, :, None, :]]  # (rows0, cols0, 11, 11)

    valid = ~np.isnan(windows)
    count = valid.sum(axis=(2, 3))
    any_valid = count > 0
    total = np.where(valid, windows, 0.0).sum(axis=(2, 3))
    mean = np.divide(total, count, out=np.zeros_like(total), where=any_valid)
    minimum = np.where(valid, windows, np.inf).min(axis=(2, 3))
    maximum = np.where(valid, windows, -np.inf).max(axis=(2, 3))

    def finish(array: np.ndarray) -> np.ndarray:
        rounded = _round_half_away(np.where(any_valid, array, 0.0))
        return np.where(any_valid, rounded, NULL_ELEVATION).astype(np.int32)

    return {'avg': finish(mean), 'min': finish(minimum), 'max': finish(maximum)}
