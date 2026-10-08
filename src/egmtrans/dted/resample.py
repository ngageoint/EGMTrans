"""Resample a whole-degree lattice onto the post grid of a DTED cell.

A source raster whose posts lie on a whole-degree lattice (an integer number
of posts per degree on each axis, a post on every whole degree) and a DTED
cell share their edges, so every DTED post sits at a rational position
``k * N / M`` between source posts, with N and M whole numbers. Bilinear
interpolation at such a position is a weighted mean with whole-number
weights, which is what this module computes: ``sum(w * v) / sum(w)`` in
double precision, with every product and sum exact for Float32 data, so the
result is the correctly rounded value of the exact rational and the same on
every host. The same function takes a geoid grid up to the DTED posts.

Nothing here uses a float reduction, Numba or GDAL: elementwise numpy only.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from egmtrans.dted.header import CellGeometry
from egmtrans.dted.schema import LEVELS

# How far a post may sit off the whole-degree lattice, as a share of the
# post spacing, before the raster is refused.
LATTICE_TOLERANCE = 1e-3

# On a TanDEM-X longitude-spacing boundary (the latitude of the cell's
# pole-ward edge), the coarser tile's posts per degree as a share of the
# finer tile's: (numerator, denominator).
BAND_EDGE_STEP: dict[int, tuple[int, int]] = {50: (2, 3), 60: (3, 4), 70: (2, 3), 80: (3, 5), 85: (1, 2)}

# Rows and columns of a level-2 grid kept for the lower levels.
THIN_STEP = {2: 1, 1: 3, 0: 30}


class ResampleError(ValueError):
    """The raster cannot be resampled onto DTED cells."""


def _posts_per_degree(spacing: float, axis: str) -> int:
    if not spacing > 0:
        raise ResampleError(f'the {axis} spacing is {spacing}, not positive')
    per_degree = 1.0 / spacing
    n = int(round(per_degree))
    # The far edge of a one-degree cell is off by |per_degree - n| posts.
    if n < 1 or abs(per_degree - n) > LATTICE_TOLERANCE:
        raise ResampleError(f'the {axis} spacing of {spacing:.12g} degrees is not a whole number of posts per degree')
    return n


def _lattice_index(position: float, n: int, axis: str) -> int:
    index = position * n
    nearest = int(round(index))
    if abs(index - nearest) > LATTICE_TOLERANCE:
        raise ResampleError(
            f'the first post lies at {axis} {position:.12g}, {abs(index - nearest):.4f} of a post off the lattice '
            f'of {n} posts per degree'
        )
    return nearest


@dataclass(frozen=True)
class SourceGrid:
    """A raster's posts as a whole-degree lattice.

    Post (row, col) lies at longitude ``(west + col) / per_degree_x`` and
    latitude ``(north - row) / per_degree_y``: every position is a whole
    number of lattice steps.
    """

    per_degree_x: int
    per_degree_y: int
    west: int   # lattice index of the first column
    north: int  # lattice index of the first row
    cols: int
    rows: int

    @classmethod
    def from_geotransform(
        cls, geotransform: tuple[float, float, float, float, float, float], cols: int, rows: int
    ) -> SourceGrid:
        """The lattice of a raster, from its GDAL geotransform and size.

        Posts are the pixel centers, as GDAL places them for a pixel-is-point
        raster.

        Raises:
            ResampleError: If the raster is rotated, its spacing is not a
                whole number of posts per degree, or its posts are off the
                lattice by more than :data:`LATTICE_TOLERANCE` of a post.
        """
        if geotransform[2] != 0 or geotransform[4] != 0:
            raise ResampleError('the raster is rotated')
        if cols < 2 or rows < 2:
            raise ResampleError(f'the raster is {cols} x {rows} posts; at least 2 x 2 are needed')
        nx = _posts_per_degree(geotransform[1], 'longitude')
        ny = _posts_per_degree(-geotransform[5], 'latitude')
        west = _lattice_index(geotransform[0] + 0.5 * geotransform[1], nx, 'longitude')
        north = _lattice_index(geotransform[3] + 0.5 * geotransform[5], ny, 'latitude')
        return cls(nx, ny, west, north, cols, rows)

    @property
    def east(self) -> int:
        """Lattice index of the last column."""
        return self.west + self.cols - 1

    @property
    def south(self) -> int:
        """Lattice index of the last row."""
        return self.north - self.rows + 1

    def cells(self) -> list[tuple[int, int]]:
        """Every whole-degree cell ``(lon0, lat0)`` with posts on all four of
        its edges, north to south and west to east."""
        lon_first = -(-self.west // self.per_degree_x)  # ceil
        lon_last = self.east // self.per_degree_x - 1
        lat_first = -(-self.south // self.per_degree_y)
        lat_last = self.north // self.per_degree_y - 1
        return [
            (lon0, lat0)
            for lat0 in range(lat_last, lat_first - 1, -1)
            for lon0 in range(lon_first, lon_last + 1)
        ]

    def partial_cells(self) -> list[tuple[int, int]]:
        """Every whole-degree cell ``(lon0, lat0)`` the raster touches without
        posts on all four of its edges, north to south and west to east: the
        cells :meth:`cells` leaves out, so a run can say so."""
        whole = set(self.cells())
        lon_first = self.west // self.per_degree_x
        lon_last = -(-self.east // self.per_degree_x) - 1
        lat_first = self.south // self.per_degree_y
        lat_last = -(-self.north // self.per_degree_y) - 1
        return [
            (lon0, lat0)
            for lat0 in range(lat_last, lat_first - 1, -1)
            for lon0 in range(lon_first, lon_last + 1)
            if (lon0, lat0) not in whole
        ]

    def window(self, lon0: int, lat0: int) -> tuple[slice, slice]:
        """The row and column slices of the cell's posts, edges included.

        Raises:
            ResampleError: If the raster does not hold the whole cell.
        """
        if not (
            self.west <= lon0 * self.per_degree_x and (lon0 + 1) * self.per_degree_x <= self.east
            and self.south <= lat0 * self.per_degree_y and (lat0 + 1) * self.per_degree_y <= self.north
        ):
            raise ResampleError(f'the raster does not hold the whole cell at longitude {lon0}, latitude {lat0}')
        col0 = lon0 * self.per_degree_x - self.west
        row0 = self.north - (lat0 + 1) * self.per_degree_y
        return slice(row0, row0 + self.per_degree_y + 1), slice(col0, col0 + self.per_degree_x + 1)


@dataclass(frozen=True)
class AxisMap:
    """Where each of *target* evenly spaced posts falls among *source* evenly
    spaced posts over the same span: the lower source index, and the share of
    the upper neighbor as ``numerator / denominator``."""

    lower: np.ndarray
    numerator: np.ndarray
    denominator: int

    @classmethod
    def between(cls, source: int, target: int) -> AxisMap:
        """The map from *source* posts to *target* posts spanning the same extent."""
        if source < 1 or target < 1:
            raise ResampleError('a grid needs at least one post on each axis')
        if target == 1 or source == 1:
            return cls(np.zeros(target, dtype=np.int64), np.zeros(target, dtype=np.int64), 1)
        ratio = Fraction(source - 1, target - 1)
        k = np.arange(target, dtype=np.int64)
        position = k * ratio.numerator  # in units of 1 / ratio.denominator
        lower = position // ratio.denominator
        numerator = position - lower * ratio.denominator
        # The last post sits exactly on the last source post.
        lower[-1] = source - 1
        numerator[-1] = 0
        return cls(lower, numerator, ratio.denominator)

    @property
    def upper(self) -> np.ndarray:
        """The upper source index: the lower one where the post sits on a node."""
        return np.where(self.numerator > 0, self.lower + 1, self.lower)

    @property
    def weights(self) -> tuple[np.ndarray, np.ndarray]:
        """Whole-number weights of the lower and the upper neighbor; they sum to the denominator."""
        return self.denominator - self.numerator, self.numerator.copy()


def _blocks(rows: int, cols: int, block_posts: int = 4_000_000):
    step = max(1, block_posts // max(cols, 1))
    for start in range(0, rows, step):
        yield start, min(start + step, rows)


def regrid_bilinear(window: np.ndarray, rows: int, cols: int, *, block_posts: int = 4_000_000) -> np.ndarray:
    """Bilinear values of *window* at *rows* x *cols* posts spanning the same extent.

    The posts of *window* and of the result share the four corners. Each
    result post is the weighted mean of its two to four source neighbors
    with whole-number weights, void (NaN) neighbors dropped; it is NaN when no
    valid neighbor carries weight. Float64 out. *block_posts* sets the size
    of the row blocks the work is done in; it cannot change the result.
    """
    source = np.asarray(window)
    if source.ndim != 2:
        raise ResampleError('the source window must be 2-D')
    source = source.astype(np.float64, copy=False)
    x = AxisMap.between(source.shape[1], cols)
    y = AxisMap.between(source.shape[0], rows)
    wx_lo, wx_hi = x.weights
    x_lo, x_hi = x.lower, x.upper
    out = np.empty((rows, cols), dtype=np.float64)
    for start, stop in _blocks(rows, cols, block_posts):
        y_lo = y.lower[start:stop, np.newaxis]
        y_hi = y.upper[start:stop, np.newaxis]
        wy_lo = (y.denominator - y.numerator[start:stop])[:, np.newaxis]
        wy_hi = y.numerator[start:stop][:, np.newaxis]
        numerator = np.zeros((stop - start, cols), dtype=np.float64)
        denominator = np.zeros((stop - start, cols), dtype=np.int64)
        for r, c, w in (
            (y_lo, x_lo, wy_lo * wx_lo), (y_lo, x_hi, wy_lo * wx_hi),
            (y_hi, x_lo, wy_hi * wx_lo), (y_hi, x_hi, wy_hi * wx_hi),
        ):
            values = source[r, c]
            valid = ~np.isnan(values) & (w > 0)
            numerator += w * np.where(valid, values, 0.0)
            denominator += np.where(valid, w, 0)
        with np.errstate(invalid='ignore', divide='ignore'):
            out[start:stop] = np.where(denominator > 0, numerator / denominator, np.nan)
    return out


def carry_labels(labels: np.ndarray, rows: int, cols: int, valid: np.ndarray | None = None) -> np.ndarray:
    """Labels of the *rows* x *cols* posts that *labels* (a source window) resamples to.

    A post takes label L when every valid source post that contributes to it
    with nonzero weight has label L; otherwise 0. *valid* marks the source
    posts that hold a height (all of them when None).
    """
    source = np.asarray(labels)
    if valid is None:
        valid = np.ones(source.shape, dtype=bool)
    x = AxisMap.between(source.shape[1], cols)
    y = AxisMap.between(source.shape[0], rows)
    x_lo, x_hi = x.lower, x.upper
    x_has_hi = x.numerator > 0
    out = np.zeros((rows, cols), dtype=np.int32)
    for start, stop in _blocks(rows, cols):
        y_lo = y.lower[start:stop, np.newaxis]
        y_hi = y.upper[start:stop, np.newaxis]
        y_has_hi = (y.numerator[start:stop] > 0)[:, np.newaxis]
        chosen = np.full((stop - start, cols), -1, dtype=np.int64)
        agree = np.ones((stop - start, cols), dtype=bool)
        for r, c, counts in (
            (y_lo, x_lo, np.ones((stop - start, cols), dtype=bool)),
            (y_lo, x_hi, np.broadcast_to(x_has_hi, (stop - start, cols))),
            (y_hi, x_lo, np.broadcast_to(y_has_hi, (stop - start, cols))),
            (y_hi, x_hi, y_has_hi & x_has_hi),
        ):
            value = source[r, c]
            consider = counts & valid[r, c]
            chosen = np.where(consider & (chosen < 0), value, chosen)
            agree &= ~consider | (value == chosen)
        out[start:stop] = np.where(agree & (chosen > 0), chosen, 0)
    return out


def thin(array: np.ndarray, level: int) -> np.ndarray:
    """A level-2 cell grid thinned to *level*: every third post for level 1,
    every thirtieth for level 0 (a copy, so the caller may drop the grid)."""
    if level not in LEVELS:
        raise ValueError(f'DTED level must be 0, 1 or 2, not {level}')
    step = THIN_STEP[level]
    return np.ascontiguousarray(array[::step, ::step])


def level2_shape(cell: CellGeometry) -> tuple[int, int]:
    """Rows and columns of the level-2 grid of *cell*'s one-degree square."""
    work = CellGeometry(2, cell.lon0, cell.lat0)
    return work.lat_points, work.lon_lines


def master_row(
    edge_row: np.ndarray, posts_per_degree: int, step: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray] | None:
    """Recover the coarser tile's row from a finer tile's edge row that copies it.

    On a longitude-spacing boundary the finer tile's edge row holds, at every
    post, the value of the nearest post of the coarser tile's row (either on a
    tie). *step* is the coarser spacing's share of the finer one (numerator,
    denominator). Returns the coarser row and the index in *edge_row* each of
    its posts was taken from, or None when *edge_row* is not such a copy, so
    a row of real data is never replaced.
    """
    row = np.asarray(edge_row)
    numerator, denominator = step
    if row.ndim != 1 or row.size != posts_per_degree + 1:
        raise ResampleError(f'an edge row of {posts_per_degree} posts per degree has {posts_per_degree + 1} posts')
    if (posts_per_degree * numerator) % denominator:
        return None
    coarse_per_degree = posts_per_degree * numerator // denominator
    # Coarse post j lies at fine position j * denominator / numerator; its
    # nearest fine post, rounding up on a tie.
    j = np.arange(coarse_per_degree + 1, dtype=np.int64)
    fine_of_coarse = (2 * j * denominator + numerator) // (2 * numerator)
    coarse = row[fine_of_coarse]

    # Fine post i lies at coarse position q / d; it must equal the coarse post
    # below or above, whichever is nearer, either one on a tie.
    q = np.arange(posts_per_degree + 1, dtype=np.int64) * numerator
    d = denominator
    below = q // d
    above = -(-q // d)
    to_below = q - below * d
    to_above = above * d - q
    nearer = np.where(to_below <= to_above, below, above)
    other = np.where(to_below == to_above, above, nearer)
    same = _same_value(row, coarse[nearer]) | _same_value(row, coarse[other])
    if not same.all():
        return None
    return coarse, fine_of_coarse


def _same_value(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if np.issubdtype(a.dtype, np.floating):
        return (a == b) | (np.isnan(a) & np.isnan(b))
    return a == b


def band_edge_step(cell_lat0: int, posts_per_degree_x: int) -> tuple[int, int] | None:
    """The :data:`BAND_EDGE_STEP` of the cell whose southern edge is at
    *cell_lat0*, by the latitude of its pole-ward edge; None off the table."""
    edge = cell_lat0 + 1 if cell_lat0 >= 0 else -cell_lat0
    step = BAND_EDGE_STEP.get(edge)
    if step is None or (posts_per_degree_x * step[0]) % step[1]:
        return None
    return step
