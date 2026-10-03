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

Heights are compared as whole centimeters (:func:`height_centimeters`), never
as floats.  The kernels below only compare integers and copy values, so the
compiled code and the plain-Python fallback cannot disagree, whatever the data
type of the heights.  Until 1.8.0 "the same height to 1 cm" was the float test
``abs(a - b) < 0.01``, which also joined neighbors exactly 1 cm apart (about
four in ten such steps on Float32) and gave different patches with and without
Numba.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from osgeo import gdal

from egmtrans.numba_utils import get_numba_decorator, prange

# What height_centimeters() holds where there is no height: a void, or a value
# no height can have. It equals no height, so a void never joins a patch.
VOID_CM = np.iinfo(np.int32).min
# The ocean is 0 and the two centimeters either side of it.
OCEAN_CM = 1


def height_centimeters(array: np.ndarray) -> np.ndarray:
    """Heights as whole centimeters (int32), :data:`VOID_CM` where there is none.

    ``rint(h * 100)`` in double precision, whatever the type of *array*: the
    product of a Float32 height and 100 is exact there, so the centimeter of a
    height does not depend on how it is stored.
    """
    array = np.atleast_1d(np.asarray(array))
    out = np.empty(array.shape, dtype=np.int32)
    limit = float(np.iinfo(np.int32).max)
    # Row blocks: a whole 9001 x 9001 tile in double precision is 650 MB.
    step = max(1, 4_000_000 // max(array[0].size if array.shape[0] else 1, 1))
    for start in range(0, array.shape[0], step):
        cm = np.rint(array[start:start + step].astype(np.float64) * 100.0)
        with np.errstate(invalid='ignore'):
            no_height = ~(np.abs(cm) < limit)  # NaN, infinity, or beyond any height
        cm[no_height] = 0.0
        block = cm.astype(np.int32)
        block[no_height] = VOID_CM
        out[start:start + step] = block
    return out


@get_numba_decorator()
def _label_patches(cm: np.ndarray, result: np.ndarray, seeds: np.ndarray, min_patch_size: int) -> np.ndarray:
    """Flood-fill the 4-connected patches of one centimeter value, from *seeds* in raster order.

    *result* comes in with the ocean set to 1; every patch of at least
    *min_patch_size* posts gets the next label from 2.
    """
    rows, cols = cm.shape
    visited = result > 0
    # One flat-index queue for every fill, as long as the posts that can be in
    # a patch at all. A queue of rows * cols was 650 MB for a 0.4" tile.
    queue = np.empty(max(seeds.size, 1), dtype=np.int64)
    current_label = 2

    for s in range(seeds.size):
        start = seeds[s]
        i = start // cols
        j = start % cols
        if visited[i, j]:
            continue

        value = cm[i, j]
        queue[0] = start
        head = 0
        tail = 1
        visited[i, j] = True

        while head < tail:
            ci = queue[head] // cols
            cj = queue[head] % cols
            head += 1
            for ni, nj in ((ci - 1, cj), (ci + 1, cj), (ci, cj - 1), (ci, cj + 1)):
                if 0 <= ni < rows and 0 <= nj < cols and not visited[ni, nj] and cm[ni, nj] == value:
                    queue[tail] = ni * cols + nj
                    tail += 1
                    visited[ni, nj] = True

        if tail >= min_patch_size:
            for k in range(tail):
                result[queue[k] // cols, queue[k] % cols] = current_label
            current_label += 1

    return result


def label_centimeters(cm: np.ndarray, min_patch_size: int = 16) -> np.ndarray:
    """Label the ocean and the flat patches of a :func:`height_centimeters` array.

    See :func:`create_labeled_array_flt` for the labels.
    """
    valid = cm != VOID_CM
    ocean = (cm >= -OCEAN_CM) & (cm <= OCEAN_CM)
    result = np.zeros(cm.shape, dtype=np.int32)
    result[ocean] = 1

    candidate = valid & ~ocean
    if min_patch_size > 1:
        # A patch of two or more posts can only start at a post with an equal
        # neighbor, about 2% of the posts of a tile outside its water. Starting
        # the fills there, in the same raster order, gives the same patches and
        # the same numbering, and keeps the plain-Python fallback usable.
        equal = np.zeros(cm.shape, dtype=bool)
        along = cm[:, 1:] == cm[:, :-1]
        equal[:, 1:] |= along
        equal[:, :-1] |= along
        across = cm[1:, :] == cm[:-1, :]
        equal[1:, :] |= across
        equal[:-1, :] |= across
        candidate &= equal
    seeds = np.flatnonzero(candidate)
    return _label_patches(cm, result, seeds, int(min_patch_size))


def create_labeled_array_flt(input_array: np.ndarray, min_patch_size: int = 16) -> np.ndarray:
    """Create a labeled array from an elevation array.

    Takes every height as whole centimeters, then identifies flat regions by
    flood-filling 4-connected neighbors of one centimeter value.  Small patches
    below *min_patch_size* are discarded.  On whole-meter data (DTED) every
    plateau of one value is a patch.

    Label semantics:
    - ``0`` -- uneven terrain, or a void (NaN), which is never masked
    - ``1`` -- ocean (0 cm and the centimeter either side of it)
    - ``>1`` -- distinct flat-area patches, numbered in raster scan order of
      their first post, so the labels of a given array are reproducible

    Args:
        input_array: 2-D elevation array (NaN = nodata).
        min_patch_size: Minimum pixel count for a flat area to be retained.

    Returns:
        Integer label array with the same shape as the input.
    """
    return label_centimeters(height_centimeters(input_array), min_patch_size)


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
def _patch_boundary_stats(cm: np.ndarray, labeled_array: np.ndarray, void_cm: int) -> tuple[np.ndarray, np.ndarray]:
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
            height = cm[i, j]
            for ni, nj in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                if 0 <= ni < rows and 0 <= nj < cols:
                    other = labeled_array[ni, nj]
                    if other == lbl or other == 1:
                        continue
                    neighbor = cm[ni, nj]
                    if neighbor == void_cm:
                        continue
                    if neighbor > height:
                        above[lbl] += 1
                    elif neighbor < height:
                        below[lbl] += 1
    return above, below


def boundary_stats_centimeters(cm: np.ndarray, labeled_array: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """:func:`patch_boundary_stats` on a :func:`height_centimeters` array."""
    return _patch_boundary_stats(cm, labeled_array, VOID_CM)


def patch_boundary_stats(input_data: np.ndarray, labeled_array: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Count, per patch, the boundary neighbors above and below it in the input.

    Every 4-neighbor of a patch post that carries a different label is a
    boundary adjacency: *above* when its input height is higher than the
    post's, *below* when lower, both in whole centimeters.  Ocean neighbors
    (label 1) are neutral, since a lagoon or a river mouth is bounded by the
    sea on one side and is water all the same; voids and neighbors of the same
    centimeter are neutral too.

    Returns:
        ``(above, below)``: int64 arrays indexed by label.
    """
    return boundary_stats_centimeters(height_centimeters(input_data), labeled_array)


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


def has_fractional_heights(cm: np.ndarray) -> bool:
    """Whether any height of a :func:`height_centimeters` array is not a whole meter.

    Tells float elevation data from whole-meter data however it is stored:
    DTED, an integer GeoTIFF and a Float32 file of whole meters all say no.
    """
    step = max(1, 4_000_000 // max(cm.shape[-1], 1))
    for start in range(0, cm.shape[0], step):
        block = cm[start:start + step]
        if np.any((block % 100 != 0) & (block != VOID_CM)):
            return True
    return False


# ---------------------------------------------------------------------------
# Enclosed low spots beside water
# ---------------------------------------------------------------------------
#
# Float elevation data can hold posts below the water body beside them, and
# resampling blends a low spot that a one-post dam kept from the water into a
# post that touches it. A low spot that is enclosed, bounded by the water and
# by higher ground, can only be a defect, so it is raised to the water level.
# A low spot that reaches lower water is an outlet and is left alone, as is
# one that reaches the tile edge or a void, where its extent is unknown, and
# one of max_posts posts or more, which is terrain the data means.

# The 8 neighbors of a post, as row and column offsets.
_NEIGHBOR_ROWS = np.array([-1, -1, -1, 0, 0, 1, 1, 1], dtype=np.int64)
_NEIGHBOR_COLS = np.array([-1, 0, 1, -1, 1, -1, 0, 1], dtype=np.int64)

# What a region grown by _enclosed_low_regions turned out to be.
_CLOSED = 0
_OPEN = 1
_CAPPED = 2

# A "water level" no water has: what a post touches when it touches no water.
_NO_WATER = np.iinfo(np.int32).max


def _low_seeds(cm: np.ndarray, labeled: np.ndarray, label_cm: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Unlabeled valid posts that touch water and lie below the lowest water level they touch.

    Returns their flat indexes and those levels, ordered by level and then by
    position, which is the order :func:`_enclosed_low_regions` relies on.
    """
    rows, cols = cm.shape
    seeds = []
    seed_levels = []
    step = max(1, 4_000_000 // max(cols, 1))
    for start in range(0, rows, step):
        stop = min(start + step, rows)
        # The block with a one-post halo, padded so the tile edge touches no water.
        lo, hi = max(start - 1, 0), min(stop + 1, rows)
        block = labeled[lo:hi]
        padded = np.full((hi - lo + 2, cols + 2), _NO_WATER, dtype=np.int32)
        padded[1:-1, 1:-1] = np.where(block > 0, label_cm[block], _NO_WATER)
        touch = np.full((stop - start, cols), _NO_WATER, dtype=np.int32)
        top = start - lo + 1
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if di or dj:
                    np.minimum(touch, padded[top + di:top + di + stop - start, 1 + dj:1 + dj + cols], out=touch)
        block_cm = cm[start:stop]
        seed = (labeled[start:stop] == 0) & (block_cm != VOID_CM) & (touch != _NO_WATER) & (block_cm < touch)
        found = np.flatnonzero(seed)
        seeds.append(found + start * cols)
        seed_levels.append(touch.ravel()[found])
    seeds = np.concatenate(seeds) if seeds else np.empty(0, dtype=np.int64)
    seed_levels = np.concatenate(seed_levels) if seed_levels else np.empty(0, dtype=np.int32)
    order = np.lexsort((seeds, seed_levels))
    return seeds[order], seed_levels[order]


@get_numba_decorator()
def _enclosed_low_regions(
    cm: np.ndarray,
    labeled: np.ndarray,
    label_cm: np.ndarray,
    levels: np.ndarray,
    seeds: np.ndarray,
    seed_levels: np.ndarray,
    max_posts: int,
    void_cm: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Grow, from every seed in turn, the 8-connected region of unlabeled valid
    posts below the seed's water level, and keep the closed ones.

    A region is open when it reaches the tile edge, a void, water at a lower
    level, or a post of a region settled before it, and capped when it would
    pass *max_posts* posts.  The seeds come ordered by level: a settled post
    then always belongs to a region that is open at the current level too
    (an earlier region at this level or a lower one was open, or it was closed
    and so is lower water now), which is what makes the outcome a property of
    the posts and not of the order they are visited in.

    Returns the posts of the closed regions (flat indexes), where each region
    starts in that array, and the label each is raised to: the touching water
    body at the region's level with the highest output level, the lowest label
    on a tie.
    """
    rows, cols = cm.shape
    # 0: not seen; 1: in the region being grown; 2: settled by an earlier region.
    state = np.zeros(rows * cols, dtype=np.uint8)
    queue = np.empty(max(max_posts, 1), dtype=np.int64)
    posts = np.empty(1024, dtype=np.int64)
    starts = np.empty(64, dtype=np.int64)
    labels = np.empty(64, dtype=np.int32)
    n_posts = 0
    n_regions = 0

    for s in range(seeds.size):
        seed = seeds[s]
        if state[seed] != 0:
            continue
        level = seed_levels[s]
        queue[0] = seed
        state[seed] = 1
        head = 0
        tail = 1
        status = _CLOSED
        best_label = 0
        best_level = 0.0
        while head < tail and status == _CLOSED:
            current = queue[head]
            head += 1
            i = current // cols
            j = current % cols
            for k in range(8):
                ni = i + _NEIGHBOR_ROWS[k]
                nj = j + _NEIGHBOR_COLS[k]
                if ni < 0 or ni >= rows or nj < 0 or nj >= cols:
                    status = _OPEN  # the tile edge: the region may go on next door
                    break
                lbl = labeled[ni, nj]
                if lbl > 0:
                    water = label_cm[lbl]
                    if water < level:
                        status = _OPEN  # lower water: an outlet
                        break
                    if water == level:
                        out_level = levels[lbl]
                        if not np.isnan(out_level) and (
                            best_label == 0 or out_level > best_level or (out_level == best_level and lbl < best_label)
                        ):
                            best_label = lbl
                            best_level = out_level
                    continue
                height = cm[ni, nj]
                if height == void_cm:
                    status = _OPEN  # unknown ground
                    break
                if height >= level:
                    continue  # a wall
                flat = ni * cols + nj
                if state[flat] == 1:
                    continue
                if state[flat] == 2:
                    status = _OPEN  # joins a region settled before this one
                    break
                if tail >= max_posts:
                    status = _CAPPED
                    break
                queue[tail] = flat
                tail += 1
                state[flat] = 1
        for k in range(tail):
            state[queue[k]] = 2
        if status != _CLOSED or tail >= max_posts or best_label == 0:
            continue

        if n_posts + tail > posts.size:
            bigger = np.empty(max(2 * posts.size, n_posts + tail), dtype=np.int64)
            bigger[:n_posts] = posts[:n_posts]
            posts = bigger
        if n_regions >= starts.size:
            bigger_starts = np.empty(2 * starts.size, dtype=np.int64)
            bigger_starts[:n_regions] = starts[:n_regions]
            starts = bigger_starts
            bigger_labels = np.empty(2 * labels.size, dtype=np.int32)
            bigger_labels[:n_regions] = labels[:n_regions]
            labels = bigger_labels
        starts[n_regions] = n_posts
        labels[n_regions] = best_label
        n_regions += 1
        for k in range(tail):
            posts[n_posts] = queue[k]
            n_posts += 1

    return posts[:n_posts], starts[:n_regions], labels[:n_regions]


@get_numba_decorator()
def _region_status(
    cm: np.ndarray,
    labeled: np.ndarray,
    label_cm: np.ndarray,
    void_cm: int,
    level: int,
    seeds: np.ndarray,
    cap: int,
    state: np.ndarray,
) -> int:
    """Grow, from all of *seeds* at once, the 8-connected region of unlabeled
    valid posts below *level*, and say whether it is closed, open or capped.

    *state* is a scratch array of zeros, one byte per post of *cm*; it is
    left as zeros.
    """
    rows, cols = cm.shape
    queue = np.empty(max(cap, 1), dtype=np.int64)
    tail = 0
    status = _CLOSED
    for s in range(seeds.size):
        seed = seeds[s]
        if state[seed] == 0:
            if tail >= cap:
                status = _CAPPED
                break
            queue[tail] = seed
            tail += 1
            state[seed] = 1
    head = 0
    while head < tail and status == _CLOSED:
        current = queue[head]
        head += 1
        i = current // cols
        j = current % cols
        for k in range(8):
            ni = i + _NEIGHBOR_ROWS[k]
            nj = j + _NEIGHBOR_COLS[k]
            if ni < 0 or ni >= rows or nj < 0 or nj >= cols:
                status = _OPEN
                break
            lbl = labeled[ni, nj]
            if lbl > 0:
                if label_cm[lbl] < level:
                    status = _OPEN
                    break
                continue
            height = cm[ni, nj]
            if height == void_cm:
                status = _OPEN
                break
            if height >= level:
                continue
            flat = ni * cols + nj
            if state[flat] == 1:
                continue
            if tail >= cap:
                status = _CAPPED
                break
            queue[tail] = flat
            tail += 1
            state[flat] = 1
    for k in range(tail):
        state[queue[k]] = 0
    return status


def region_open_on_grid(
    cm: np.ndarray,
    labeled: np.ndarray,
    label_cm: np.ndarray,
    seeds: np.ndarray,
    level: int,
    cap: int,
    state: np.ndarray,
) -> bool:
    """Whether the low region grown from *seeds* on this grid is open.

    The region is the 8-connected unlabeled valid posts below *level* (whole
    centimeters) reached from the seeds (flat indexes); it is open when it
    reaches the grid edge, a void or water at a lower level, or grows past
    *cap* posts. *state* is a zeroed uint8 scratch array of ``cm.size``
    entries, reused across calls.
    """
    status = _region_status(
        cm, labeled, label_cm, VOID_CM, int(level), np.ascontiguousarray(seeds, dtype=np.int64), int(cap), state
    )
    return status != _CLOSED


def enclosed_low_regions(
    cm: np.ndarray, labeled: np.ndarray, label_cm: np.ndarray, levels: np.ndarray, max_posts: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Find the enclosed low spots of fewer than *max_posts* posts beside water.

    A low spot is an 8-connected region of unlabeled valid posts that touches
    a water body (the ocean, or a patch left in *labeled* after the water
    verdicts) and lies below its input level, in whole centimeters.  It is
    enclosed when it reaches no tile edge, no void and no water at a lower
    level; a region that does reach lower water is an outlet.  A post that
    touches water at several levels is judged against the lowest, and no post
    is ever in two spots.

    Args:
        cm: The input heights from :func:`height_centimeters`.
        labeled: The labels, with every patch that is not water set to 0.
        label_cm: Every patch's input centimeters (:func:`patch_centimeters`).
        levels: Every patch's output level (:func:`patch_levels`).
        max_posts: A spot of this many posts or more is terrain, and left.

    Returns:
        ``(posts, starts, labels)``: the flat indexes of the posts of every
        spot, the index in *posts* where each spot starts, and the label of
        the body each spot belongs to: of the bodies at its level that it
        touches, the one with the highest output level.
    """
    if max_posts < 1:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int32)
    seeds, seed_levels = _low_seeds(cm, labeled, label_cm)
    return _enclosed_low_regions(
        cm, labeled, label_cm, np.ascontiguousarray(levels, dtype=np.float64),
        seeds, seed_levels, int(max_posts), VOID_CM,
    )


@dataclass
class RaisedSpots:
    """What :func:`raise_low_spots` did: the number of spots and posts, the
    largest raise in meters, for every spot raised by a meter or more its
    deepest post as ``(row, column, posts, raise)``, and the number of spots
    the caller's check left alone."""

    regions: int = 0
    posts: int = 0
    largest: float = 0.0
    deep: list[tuple[int, int, int, float]] = field(default_factory=list)
    skipped: int = 0


def raise_low_spots(
    output_data: np.ndarray,
    labeled: np.ndarray,
    cm: np.ndarray,
    label_cm: np.ndarray,
    levels: np.ndarray,
    max_posts: int,
    log_from: float = 1.0,
    accept=None,
) -> RaisedSpots:
    """Set every enclosed low spot beside water to the water's output level, in place.

    The spots are those of :func:`enclosed_low_regions`.  Every post of a
    spot takes the body's level, as a post of the body does, and the body's
    label in *labeled*, so the mask shows it as water and the containment
    count judges its land neighbors against the body, not against it.  The
    ground around a spot is at or above the body in the input, so it stays at
    or above the level, as a shore does.  A spot is raised in all but one
    case: a large body's level is its minimum, which can lie below a spot's
    own transformed value when the correction varies across the body by more
    than the spot's depth; the spot then joins the surface like any post of
    the body.  Call it after :func:`patch_levels`, so a spot never enters a
    body's minimum.

    Args:
        output_data: The transformed heights, set in place.
        labeled: The labels, with every patch that is not water set to 0;
            the posts of every spot are labeled in place.
        cm, label_cm, levels, max_posts: As for :func:`enclosed_low_regions`.
        log_from: Spots raised by this many meters or more are reported with
            their deepest post.
        accept: An optional ``accept(posts, label) -> bool`` called for every
            spot (its flat indexes and the body's label); a spot it refuses is
            left as it is and counted in :attr:`RaisedSpots.skipped`.
    """
    posts, starts, labels = enclosed_low_regions(cm, labeled, label_cm, levels, max_posts)
    spots = RaisedSpots()
    if posts.size == 0:
        return spots

    if accept is not None:
        ends = np.append(starts[1:], posts.size)
        keep = np.array(
            [accept(posts[start:end], int(label)) for start, end, label in zip(starts, ends, labels)], dtype=bool
        )
        spots.skipped = int((~keep).sum())
        if not keep.all():
            pieces = [posts[start:end] for start, end, ok in zip(starts, ends, keep) if ok]
            labels = labels[keep]
            starts = np.cumsum([0] + [piece.size for piece in pieces[:-1]]).astype(np.int64)
            posts = np.concatenate(pieces) if pieces else np.empty(0, dtype=np.int64)
            if posts.size == 0:
                return spots

    sizes = np.diff(np.append(starts, posts.size))
    post_labels = np.repeat(labels, sizes)
    target = np.asarray(levels, dtype=np.float64)[post_labels]
    rows, cols = np.unravel_index(posts, output_data.shape)
    own = output_data[rows, cols].astype(np.float64)
    raised_by = np.where(np.isfinite(own), target - own, 0.0)
    output_data[rows, cols] = target
    labeled[rows, cols] = post_labels

    deepest = np.maximum.reduceat(raised_by, starts)
    spots.regions = int(starts.size)
    spots.posts = int(posts.size)
    spots.largest = max(float(deepest.max()), 0.0)
    for region in np.flatnonzero(deepest >= log_from):
        start = int(starts[region])
        at = start + int(np.argmax(raised_by[start:start + int(sizes[region])]))
        spots.deep.append((int(rows[at]), int(cols[at]), int(sizes[region]), float(deepest[region])))
    return spots


@get_numba_decorator()
def _round_half_away_scalar(value: float) -> float:
    # In double precision whatever the array type, so that the compiled kernel
    # and the plain-Python fallback add the half in the same arithmetic.
    v = np.float64(value)
    return np.floor(v + 0.5) if v >= 0 else np.ceil(v - 0.5)


@get_numba_decorator()
def _containment_stats(
    cm: np.ndarray,
    output_data: np.ndarray,
    labeled_array: np.ndarray,
    levels: np.ndarray,
    label_cm: np.ndarray,
    rounded: bool,
) -> tuple[int, int, int, int]:
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
            for ni, nj in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                if 0 <= ni < rows and 0 <= nj < cols:
                    lbl = labeled_array[ni, nj]
                    if lbl > 1:
                        level = levels[lbl]
                        if np.isnan(level):
                            continue
                        is_shore = True
                        # Against the body's own input height, not the
                        # neighbor's: a neighbor raised to the water level
                        # carries the label but not the height.
                        step_in = cm[i, j] - label_cm[lbl]
                        post = value
                        if rounded:
                            post = _round_half_away_scalar(value)
                            level = _round_half_away_scalar(level)
                        if step_in < 0:
                            was_below = True
                        elif post < level:
                            is_below = True
                        elif post == level and step_in > 0:
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


def patch_centimeters(cm: np.ndarray, labeled_array: np.ndarray) -> np.ndarray:
    """The input height of every patch in whole centimeters, indexed by label.

    Every post of a patch has the same centimeter, so any one of them gives
    it; the ocean (label 1) is 0 and unused labels are :data:`VOID_CM`.  Call
    it on the labels as the labeler made them, before any post is added to a
    patch that does not share its height.
    """
    flat = labeled_array.ravel()
    label_cm = np.full(int(flat.max(initial=0)) + 1, VOID_CM, dtype=np.int32)
    members = np.flatnonzero(flat > 1)
    label_cm[flat[members]] = cm.ravel()[members]
    if label_cm.size > 1:
        label_cm[1] = 0
    return label_cm


def containment_stats_centimeters(
    cm: np.ndarray,
    output_data: np.ndarray,
    labeled_array: np.ndarray,
    levels: np.ndarray,
    label_cm: np.ndarray,
    rounded: bool,
) -> tuple[int, int, int, int]:
    """:func:`containment_stats` on a :func:`height_centimeters` array and the
    patches' input centimeters (:func:`patch_centimeters`)."""
    return _containment_stats(
        cm, output_data, labeled_array, np.ascontiguousarray(levels, dtype=np.float64), label_cm, rounded
    )


def containment_stats(
    input_data: np.ndarray,
    output_data: np.ndarray,
    labeled_array: np.ndarray,
    levels: np.ndarray,
    rounded: bool,
) -> tuple[int, int, int, int]:
    """Count shore posts and what the transform did to their step above the water.

    A shore post is an unlabeled, finite post with a 4-neighbor in a patch
    (label > 1; the ocean is left out, since land at or below 0 m beside the
    sea is a property of the input).  Only changes the transform introduced
    are counted: a post that was already below the flat area in the input (a
    plateau above its surroundings) is neither a lost step nor a defect.
    Input heights are compared in whole centimeters.  With *rounded*, the post
    and the level are rounded to whole meters first, as a DTED write does.

    Returns:
        ``(shore, lost_step, below, already_below)``: shore posts; those that
        were above the neighboring patch in the input and are now exactly at
        its level; those that were at or above it and are now below it (a
        nonzero count deserves a look: the minimum rule keeps a shore post at
        least 1 cm above its water from falling below it, but a post blended
        from water and land can sit a few millimeters above the water, less
        than the correction changes between two posts in the steepest places);
        and those that were already below it in the input (outlets, dam faces,
        dipping shores the source did not raise), which are left as they are.
    """
    cm = height_centimeters(input_data)
    return containment_stats_centimeters(
        cm, output_data, labeled_array, levels, patch_centimeters(cm, labeled_array), rounded
    )


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
