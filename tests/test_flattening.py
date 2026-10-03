"""Tests for egmtrans.flattening."""

import numpy as np
import pytest
from scipy import ndimage

from egmtrans.flattening import (
    apply_levels,
    create_labeled_array_flt,
    patch_levels,
    process_patches,
    process_patches_arcpy,
)


class TestCreateLabeledArrayFlt:
    def test_ocean_detection(self):
        arr = np.array([[0.0, 0.0, 5.5], [0.0, 3.3, 3.3], [7.7, 7.7, 0.0]], dtype=np.float32)
        result = create_labeled_array_flt(arr, min_patch_size=1)
        # Ocean pixels (value ~0) should be labeled as 1
        assert result[0, 0] == 1
        assert result[0, 1] == 1
        assert result[2, 2] == 1

    def test_nan_not_labeled_as_flat_patch(self):
        arr = np.array([[np.nan, 1.0], [1.0, 1.0]], dtype=np.float32)
        result = create_labeled_array_flt(arr, min_patch_size=1)
        # A void is neither ocean (1) nor a flat patch (>1). This used to assert
        # <= 1, which passed while fastmath=True labeled every void as ocean.
        assert result[0, 0] == 0
        # The three valid 1.0 pixels should be detected as a flat region
        assert result[0, 1] > 1
        assert result[1, 0] > 1
        assert result[1, 1] > 1

    def test_voids_never_labeled_ocean(self):
        # Voids next to ocean and inside a flat patch: none may join either mask.
        arr = np.array(
            [
                [np.nan, 0.0, 0.0, 5.0],
                [0.0, np.nan, 5.0, 5.0],
                [7.0, 7.0, 7.0, np.nan],
                [7.0, np.nan, 7.0, 7.0],
            ],
            dtype=np.float32,
        )
        result = create_labeled_array_flt(arr, min_patch_size=1)
        assert np.all(result[np.isnan(arr)] == 0)
        assert result[0, 1] == 1 and result[1, 0] == 1

    def test_labels_unchanged_by_queue_rewrite(self):
        """The flood fill matches scipy's 4-connected components, patch by patch.

        The fill used to grow a Python list of (i, j) tuples per patch; it now
        uses one preallocated index queue. Same scan order, same neighbor order,
        so the partition and the label numbering (raster order of each patch's
        first post) must be identical to an independent labeling.
        """
        rng = np.random.default_rng(7)
        arr = rng.integers(0, 4, size=(40, 50)).astype(np.float64)
        arr[rng.random(arr.shape) < 0.05] = np.nan
        min_patch_size = 5

        result = create_labeled_array_flt(arr, min_patch_size)

        assert np.all(result[np.isnan(arr)] == 0)
        assert np.all(result[arr == 0] == 1)
        for value in (1.0, 2.0, 3.0):
            components, count = ndimage.label(arr == value)
            for component in range(1, count + 1):
                posts = components == component
                labels = np.unique(result[posts])
                if posts.sum() >= min_patch_size:
                    assert labels.size == 1 and labels[0] > 1, f"patch of {value} split or unlabeled"
                    assert not np.any(result[~posts] == labels[0]), "label shared with another patch"
                else:
                    assert labels.tolist() == [0], "a patch below the minimum size was labeled"
        first_posts = [np.flatnonzero(result.ravel() == lbl)[0] for lbl in range(2, result.max() + 1)]
        assert first_posts == sorted(first_posts), "labels are not numbered in raster scan order"


class TestIntegerPatches:
    """DTED reaches the labeler as float64 whole meters; every plateau is a patch."""

    def test_dted_integer_plateau_is_labeled(self):
        i, j = np.mgrid[0:12, 0:12]
        arr = (10 + 3 * i + 5 * j).astype(np.float64)  # neighbors always differ
        arr[2:6, 2:7] = 150.0                            # a 20-post lake
        arr[:, 0] = 0.0                                  # ocean along the west edge
        result = create_labeled_array_flt(arr, min_patch_size=16)
        assert np.all(result[:, 0] == 1)
        lake = np.unique(result[2:6, 2:7])
        assert lake.size == 1 and lake[0] > 1
        outside = np.ones_like(result, dtype=bool)
        outside[2:6, 2:7] = False
        outside[:, 0] = False
        assert np.all(result[outside] == 0)

    def test_adjacent_integers_are_separate_patches(self):
        arr = np.full((6, 8), 149.0)
        arr[:, 4:] = 150.0
        result = create_labeled_array_flt(arr, min_patch_size=4)
        left, right = np.unique(result[:, :4]), np.unique(result[:, 4:])
        assert left.size == 1 and right.size == 1
        assert left[0] > 1 and right[0] > 1 and left[0] != right[0]

    def test_min_patch_size_applies_to_integer_arrays(self):
        i, j = np.mgrid[0:8, 0:8]
        arr = (10 + 3 * i + 5 * j).astype(np.float64)
        arr[1:3, 1:6] = 200.0  # 10 posts
        assert np.all(create_labeled_array_flt(arr, min_patch_size=16) == 0)
        assert np.all(create_labeled_array_flt(arr, min_patch_size=10)[1:3, 1:6] > 1)


class TestBoundaryContainment:
    """A flat patch is water only when most of its boundary lies above it."""

    def _terrain(self, n=8):
        i, j = np.mgrid[0:n, 0:n]
        return (10 + 3 * i + 5 * j).astype(np.float64)  # no two neighbors alike

    def _stats(self, arr, min_patch_size=4):
        from egmtrans.flattening import create_labeled_array_flt, patch_boundary_stats

        labeled = create_labeled_array_flt(arr, min_patch_size)
        above, below = patch_boundary_stats(arr, labeled)
        return labeled, above, below

    def test_basin_is_fully_contained(self):
        from egmtrans.flattening import containment_fraction

        arr = self._terrain()
        arr[3:5, 3:5] = 1.0
        labeled, above, below = self._stats(arr)
        label = labeled[3, 3]
        assert label > 1 and above[label] == 8 and below[label] == 0
        assert containment_fraction(above, below)[label] == 1.0

    def test_hilltop_is_not_contained(self):
        from egmtrans.flattening import containment_fraction, uncontained_labels

        arr = self._terrain()
        arr[3:5, 3:5] = 500.0
        labeled, above, below = self._stats(arr)
        label = labeled[3, 3]
        assert above[label] == 0 and below[label] == 8
        assert containment_fraction(above, below)[label] == 0.0
        assert uncontained_labels(above, below, 0.8).tolist() == [label]

    def test_slope_band_is_half_contained(self):
        """Contour bands on a gentle slope: bounded above on one side, below on the other."""
        from egmtrans.flattening import containment_fraction, uncontained_labels

        i, _ = np.mgrid[0:6, 0:6]
        arr = (100 + i // 2).astype(np.float64)  # three bands, two rows each
        labeled, above, below = self._stats(arr)
        top, middle, bottom = labeled[0, 0], labeled[2, 0], labeled[4, 0]
        fraction = containment_fraction(above, below)
        assert fraction[top] == 1.0, "the lowest band is a valley floor"
        assert fraction[middle] == 0.5
        assert fraction[bottom] == 0.0, "the highest band is a hilltop"
        assert uncontained_labels(above, below, 0.8).tolist() == sorted([middle, bottom])

    def test_ocean_neighbors_are_neutral(self):
        """A lagoon is bounded by the sea on one side and land on the other: still water."""
        from egmtrans.flattening import containment_fraction

        arr = self._terrain()
        arr[:, 0] = 0.0          # the sea
        arr[2:6, 1] = 0.5        # a lagoon along it
        labeled, above, below = self._stats(arr)
        label = labeled[3, 1]
        assert labeled[3, 0] == 1
        assert containment_fraction(above, below)[label] == 1.0
        assert above[label] == 6 and below[label] == 0, "the sea was counted as a boundary"

    def test_patch_without_boundary_is_kept(self):
        from egmtrans.flattening import uncontained_labels

        arr = np.full((4, 4), np.nan)
        arr[1:3, 1:3] = 7.0
        labeled, above, below = self._stats(arr)
        assert labeled[1, 1] > 1
        assert uncontained_labels(above, below, 0.8).size == 0

    def test_drop_labels_sets_terrain(self):
        from egmtrans.flattening import drop_labels

        labeled = np.array([[1, 2, 2], [3, 3, 0], [4, 0, 2]], dtype=np.int32)
        drop_labels(labeled, np.array([2, 4], dtype=np.int32))
        assert labeled.tolist() == [[1, 0, 0], [3, 3, 0], [0, 0, 0]]


class TestWholeCentimeters:
    """Flat areas are compared as whole centimeters, never as floats.

    The float test ``abs(a - b) < 0.01`` also joined neighbors exactly 1 cm
    apart whenever the representation error fell the right way (about four in
    ten such steps on Float32), and compared differently with and without
    Numba.
    """

    def test_height_centimeters(self):
        from egmtrans.flattening import VOID_CM, height_centimeters

        heights = np.array([[0.0, 1.004, 1.006, -1.006], [np.nan, np.inf, 1e30, 8848.86]])
        for dtype in (np.float32, np.float64):
            cm = height_centimeters(heights.astype(dtype))
            assert cm.dtype == np.int32
            assert cm.tolist() == [[0, 100, 101, -101], [VOID_CM, VOID_CM, VOID_CM, 884886]]
        whole = height_centimeters(np.array([[-32767, 5], [0, 9000]], dtype=np.int16))
        assert whole.tolist() == [[-3276700, 500], [0, 900000]]

    def test_same_centimeter_whatever_the_storage(self):
        from egmtrans.flattening import height_centimeters

        cm = np.arange(-200000, 900000, 7)
        as_float32 = (cm / 100).astype(np.float32)
        assert np.array_equal(height_centimeters(as_float32), cm)
        assert np.array_equal(height_centimeters(cm / 100), cm)

    @pytest.mark.parametrize("dtype", [np.float32, np.float64])
    @pytest.mark.parametrize("low", [0.02, 3.01, 12.34, 150.0, 799.99, -4.21])
    def test_one_centimeter_step_is_another_patch(self, dtype, low):
        arr = np.full((6, 8), low, dtype=dtype)
        arr[:, 4:] = np.round(low + 0.01, 2)
        result = create_labeled_array_flt(arr, min_patch_size=4)
        left, right = np.unique(result[:, :4]), np.unique(result[:, 4:])
        assert left.size == 1 and right.size == 1
        assert left[0] > 1 and right[0] > 1 and left[0] != right[0]

    def test_float32_and_float64_copies_get_the_same_labels(self):
        rng = np.random.default_rng(11)
        cm = rng.integers(298, 304, size=(60, 70))            # centimeter steps around 3 m
        cm[rng.random(cm.shape) < 0.2] = 0                    # some ocean
        heights = cm / 100
        heights[rng.random(cm.shape) < 0.03] = np.nan
        labels64 = create_labeled_array_flt(heights, 5)
        labels32 = create_labeled_array_flt(heights.astype(np.float32), 5)
        assert np.array_equal(labels64, labels32)
        assert labels64.max() > 2

    def test_matches_an_independent_labeling_on_centimeter_steps(self):
        rng = np.random.default_rng(5)
        cm = rng.integers(1200, 1204, size=(50, 60))
        heights = (cm / 100).astype(np.float32)
        min_patch_size = 6
        result = create_labeled_array_flt(heights, min_patch_size)
        for value in range(1200, 1204):
            components, count = ndimage.label(cm == value)
            for component in range(1, count + 1):
                posts = components == component
                labels = np.unique(result[posts])
                if posts.sum() >= min_patch_size:
                    assert labels.size == 1 and labels[0] > 1, "a patch was split, or joined a neighbor 1 cm away"
                    assert not np.any(result[~posts] == labels[0])
                else:
                    assert labels.tolist() == [0]
        first_posts = [np.flatnonzero(result.ravel() == lbl)[0] for lbl in range(2, result.max() + 1)]
        assert first_posts == sorted(first_posts)

    @pytest.mark.parametrize("dtype", [np.float32, np.float64])
    def test_ocean_is_zero_and_the_centimeter_either_side(self, dtype):
        arr = np.array([[-0.02, -0.014, -0.01, 0.0, 0.004, 0.01, 0.014, 0.016, 0.02]], dtype=dtype)
        result = create_labeled_array_flt(arr, min_patch_size=1)
        assert (result[0] == 1).tolist() == [False, True, True, True, True, True, True, False, False]

    def test_single_posts_are_patches_when_the_minimum_is_one(self):
        arr = np.array([[5.0, 6.0], [7.0, np.nan]], dtype=np.float32)
        result = create_labeled_array_flt(arr, min_patch_size=1)
        assert result.tolist() == [[2, 3], [4, 0]]

    def test_boundary_counts_use_centimeters(self):
        from egmtrans.flattening import patch_boundary_stats

        arr = np.full((5, 5), 12.34, dtype=np.float32)
        arr[0, :] = 12.35      # 1 cm above: five boundary posts above
        arr[4, :] = 12.33      # 1 cm below: five below
        arr[1:4, 0] = np.nan   # voids are neutral
        labeled = np.zeros((5, 5), dtype=np.int32)
        labeled[1:4, 1:] = 2
        for dtype in (np.float32, np.float64):
            above, below = patch_boundary_stats(arr.astype(dtype), labeled)
            assert above[2] == 4 and below[2] == 4

    def test_containment_counts_use_the_body_height(self):
        from egmtrans.flattening import containment_stats

        heights = np.full((5, 5), 12.40, dtype=np.float32)
        heights[1:4, 1:4] = 12.34          # the lake
        heights[0, 2] = 12.30              # a shore post already below it
        labeled = np.zeros((5, 5), dtype=np.int32)
        labeled[1:4, 1:4] = 2
        output = heights.astype(np.float64) - 0.25
        levels = np.array([np.nan, 0.0, 12.09])
        shore, lost_step, below, already_below = containment_stats(heights, output, labeled, levels, False)
        assert (shore, lost_step, below, already_below) == (12, 0, 0, 1)


class TestEnclosedLowSpots:
    """An enclosed low spot of fewer than max_posts posts beside water is set to its level.

    Enclosed means bounded by the water and higher ground: a spot that reaches
    lower water (an outlet), the tile edge or a void is left, and so is one of
    max_posts posts or more.
    """

    @staticmethod
    def _terrain(rows=20, cols=20, base=200.0):
        i, j = np.mgrid[0:rows, 0:cols]
        return (base + i + 0.37 * j).astype(np.float32)  # no two neighbors alike

    @staticmethod
    def _low(*shape):
        """Posts between 148.0 and 148.96 m, no two neighbors alike, so they form no patch."""
        n = int(np.prod(shape))
        return (148.0 + 0.01 * ((np.arange(n) * 7) % 97)).astype(np.float32).reshape(shape)

    @staticmethod
    def _raise(arr, max_posts=16, min_patch_size=16, shift=0.3, log_from=1.0):
        from egmtrans.flattening import (
            height_centimeters,
            label_centimeters,
            patch_centimeters,
            patch_levels,
            raise_low_spots,
        )

        cm = height_centimeters(arr)
        labeled = label_centimeters(cm, min_patch_size)
        label_cm = patch_centimeters(cm, labeled)
        output = arr.astype(np.float64) - shift
        levels, _ = patch_levels(output, labeled)
        apply_levels(output, labeled, levels, parallel=False)
        spots = raise_low_spots(output, labeled, cm, label_cm, levels, max_posts, log_from=log_from)
        return output, labeled, levels, spots

    def test_spots_beside_a_lake_are_raised_to_its_level(self):
        arr = self._terrain()
        arr[5:10, 5:10] = 150.0     # the lake
        arr[10, 7] = 149.5          # one post under its south shore
        arr[3:5, 6:9] = 148.0       # six posts on its north shore, 1 cm steps away from a patch of their own
        arr[3, 6] = 148.01
        output, labeled, levels, spots = self._raise(arr)
        lake = labeled[5, 5]
        assert lake > 1 and levels[lake] == pytest.approx(149.7)
        assert output[10, 7] == levels[lake] and labeled[10, 7] == lake
        assert np.all(output[3:5, 6:9] == levels[lake]) and np.all(labeled[3:5, 6:9] == lake)
        assert (spots.regions, spots.posts) == (2, 7)
        assert spots.largest == pytest.approx(2.0)
        assert len(spots.deep) == 1
        row, col, posts, depth = spots.deep[0]
        assert 3 <= row <= 4 and 6 <= col <= 8 and posts == 6 and depth == pytest.approx(2.0)
        # Nothing else moved.
        untouched = np.ones(arr.shape, dtype=bool)
        untouched[5:10, 5:10] = False
        untouched[10, 7] = False
        untouched[3:5, 6:9] = False
        assert np.array_equal(output[untouched], arr.astype(np.float64)[untouched] - 0.3)
        assert np.all(labeled[untouched] == 0)

    def test_a_spot_of_max_posts_is_left(self):
        arr = self._terrain()
        arr[5:10, 5:10] = 150.0
        arr[10:14, 5:9] = self._low(4, 4)   # 16 posts
        output, labeled, levels, spots = self._raise(arr, max_posts=16)
        assert spots.regions == 0
        assert np.array_equal(output[10:14, 5:9], arr[10:14, 5:9].astype(np.float64) - 0.3)
        assert np.all(labeled[10:14, 5:9] == 0)
        output, labeled, levels, spots = self._raise(arr, max_posts=17)
        assert spots.regions == 1 and spots.posts == 16 and np.all(output[10:14, 5:9] == levels[labeled[5, 5]])

    def test_spots_that_reach_the_edge_a_void_or_lower_water_are_left(self):
        arr = self._terrain(30, 30)
        arr[5:10, 5:10] = 150.0
        low = np.zeros(arr.shape, dtype=bool)
        low[10:14, 5] = True                # south of the lake, reaching a void
        arr[14, 5] = np.nan
        low[5:10, 10:30] = True             # east of the lake, reaching the east edge (and capped)
        low[0:5, 7] = True                  # north of the lake, reaching the north edge
        arr[20:25, 20:25] = 120.0           # a lower lake
        low[np.arange(10, 20), np.arange(10, 20)] = True  # a diagonal channel from the upper lake's corner to it
        arr[low] = self._low(int(low.sum()))
        output, labeled, _, spots = self._raise(arr)
        assert spots.regions == 0
        assert np.array_equal(output[low], arr[low].astype(np.float64) - 0.3) and np.all(labeled[low] == 0)

    def test_a_spot_between_two_reaches_joins_the_lower_one(self):
        arr = self._terrain()
        arr[2:6, 2:10] = 166.0      # the upper reach
        arr[8:12, 2:10] = 165.5     # the lower reach
        arr[6:8, 4:7] = 165.2       # six posts between them, touching both
        output, labeled, levels, spots = self._raise(arr)
        upper, lower = labeled[2, 2], labeled[8, 2]
        assert upper > 1 and lower > 1 and upper != lower
        assert spots.regions == 1 and spots.posts == 6
        assert np.all(output[6:8, 4:7] == levels[lower]) and np.all(labeled[6:8, 4:7] == lower)

    def test_diagonal_contact_counts(self):
        arr = self._terrain()
        arr[5:10, 5:10] = 150.0
        arr[10, 10] = 149.0         # touches the lake's corner post only
        output, labeled, levels, spots = self._raise(arr)
        assert spots.regions == 1 and output[10, 10] == levels[labeled[5, 5]] and labeled[10, 10] == labeled[5, 5]

    def test_posts_below_the_ocean_are_raised_to_zero(self):
        arr = self._terrain()
        arr[0:6, :] = 0.0           # the sea
        arr[6, 3] = -0.01           # ocean itself
        arr[6, 8:11] = -0.05        # three posts just under sea level
        arr[7, 9] = -1.2
        output, labeled, _, spots = self._raise(arr)
        assert labeled[6, 3] == 1 and output[6, 3] == 0.0
        assert spots.regions == 1 and spots.posts == 4 and spots.largest == pytest.approx(1.5)
        assert np.all(output[6, 8:11] == 0.0) and output[7, 9] == 0.0
        assert np.all(labeled[6, 8:11] == 1) and labeled[7, 9] == 1

    def test_a_spot_above_a_large_body_s_level_joins_its_surface(self):
        """A large body's level is its minimum, which can lie below a spot that
        was below the body in the input; the spot takes the level all the same,
        as a post of the body does, and the shore around it stays above."""
        arr = self._terrain()
        arr[5:10, 5:10] = 150.0
        arr[10, 4] = 149.5          # on the lake's corner
        shift = 0.3 + 0.2 * np.arange(20)[np.newaxis, :]  # the correction grows eastward
        output, labeled, levels, spots = self._raise(arr, shift=shift)
        lake = labeled[5, 5]
        assert levels[lake] == pytest.approx(150.0 - 0.3 - 1.8)
        assert 149.5 - 0.3 - 0.8 > levels[lake], "the test spot does not stand above the level"
        assert output[10, 4] == levels[lake] and labeled[10, 4] == lake
        assert spots.regions == 1 and spots.largest == 0.0
        assert output[11, 3:6].min() > levels[lake] and output[10, 3] > levels[lake]

    def test_a_spot_between_two_bodies_of_one_height_takes_the_higher_level(self):
        arr = self._terrain()
        arr[2:6, 2:6] = 150.0       # two lakes of one input height ...
        arr[2:6, 10:14] = 150.0
        arr[3:5, 6:10] = 149.0      # ... and eight posts between them, touching both
        shift = 0.3 + 0.1 * np.arange(20)[np.newaxis, :]  # the east lake comes out lower
        output, labeled, levels, spots = self._raise(arr, shift=shift)
        west, east = labeled[2, 2], labeled[2, 10]
        assert west != east and levels[west] > levels[east]
        assert spots.regions == 1
        assert np.all(labeled[3:5, 6:10] == west) and np.all(output[3:5, 6:10] == levels[west])

    @pytest.mark.parametrize("transform", [np.flipud, np.fliplr, np.transpose, np.rot90])
    def test_scan_order_does_not_matter(self, transform):
        rng = np.random.default_rng(31)
        arr = (rng.integers(14900, 15100, size=(60, 60)) / 100).astype(np.float32)
        arr[10:20, 10:20] = 150.0
        arr[30:45, 25:40] = 150.0
        arr[40:50, 45:55] = 149.4
        arr[rng.random(arr.shape) < 0.02] = np.nan
        arr[0:3, :] = 0.0
        shift = 0.3 + 0.01 * np.arange(60)[np.newaxis, :]
        output, labeled, _, spots = self._raise(arr, max_posts=12, min_patch_size=8, shift=shift)
        mirrored, mirrored_labels, _, mirrored_spots = self._raise(
            transform(arr), max_posts=12, min_patch_size=8, shift=transform(np.broadcast_to(shift, arr.shape))
        )
        assert spots.regions > 3
        assert (mirrored_spots.regions, mirrored_spots.posts) == (spots.regions, spots.posts)
        assert np.array_equal(mirrored, transform(output), equal_nan=True)
        # Labels are numbered in raster order, so compare the water they mark.
        assert np.array_equal(mirrored_labels > 0, transform(labeled > 0))
        assert np.array_equal(mirrored_labels == 1, transform(labeled == 1))

    def test_capped_growth_never_leaves_a_closed_remainder(self):
        """A long low channel touching the lake at both ends: the fill from one
        end stops at max_posts and the fill from the other must not take what
        is left for a small closed spot."""
        arr = self._terrain(30, 30)
        arr[5:10, 5:10] = 150.0
        arr[10:14, 4] = 149.0       # 4 posts down the west side ...
        arr[14, 4:11] = 149.0       # ... 7 along the bottom ...
        arr[10:14, 10] = 149.0      # ... 4 up the east side: 15 posts, both ends on the lake
        output, labeled, _, spots = self._raise(arr, max_posts=12)
        assert spots.regions == 0
        assert np.all(output[arr == 149.0] == 148.7) and np.all(labeled[arr == 149.0] == 0)
        output, labeled, levels, spots = self._raise(arr, max_posts=16)
        assert spots.regions == 1 and spots.posts == 15
        assert np.all(output[arr == 149.0] == levels[labeled[5, 5]])

    def test_whole_meter_data_is_told_from_float_data(self):
        from egmtrans.flattening import VOID_CM, has_fractional_heights, height_centimeters

        whole = np.array([[150.0, 151.0], [np.nan, -32767.0]])
        assert not has_fractional_heights(height_centimeters(whole))
        assert not has_fractional_heights(height_centimeters(whole.astype(np.float32)))
        assert not has_fractional_heights(height_centimeters(np.array([[150, 0], [7, -1]], dtype=np.int16)))
        assert has_fractional_heights(height_centimeters(np.array([[150.0, 150.5]], dtype=np.float32)))
        assert not has_fractional_heights(np.full((3, 3), VOID_CM, dtype=np.int32))

    def test_containment_counts_a_raised_post_as_water(self):
        from egmtrans.flattening import (
            containment_stats_centimeters,
            height_centimeters,
            label_centimeters,
            patch_centimeters,
            patch_levels,
            raise_low_spots,
        )

        arr = self._terrain()
        arr[5:10, 5:10] = 150.0
        arr[10, 7] = 149.5
        cm = height_centimeters(arr)
        labeled = label_centimeters(cm, 16)
        label_cm = patch_centimeters(cm, labeled)
        output = arr.astype(np.float64) - 0.3
        levels, _ = patch_levels(output, labeled)
        apply_levels(output, labeled, levels, parallel=False)
        before = containment_stats_centimeters(cm, output, labeled, levels, label_cm, False)
        assert before == (20, 0, 0, 1), "the spot counts as a shore post already below the water"
        raise_low_spots(output, labeled, cm, label_cm, levels, 16)
        after = containment_stats_centimeters(cm, output, labeled, levels, label_cm, False)
        # The spot is water now, and the land post south of it joins the shore.
        assert after == (20, 0, 0, 0)


class TestKernels:
    """What the kernels are compiled with, and that it does not matter to the result."""

    def test_nan_assumptions_excluded(self):
        from egmtrans.numba_utils import FASTMATH_FLAGS

        # 'nnan'/'ninf' let LLVM delete np.isnan() tests; 'fast' implies both.
        assert not {'nnan', 'ninf', 'fast'} & set(FASTMATH_FLAGS)

    def test_only_the_spline_kernels_take_fast_math(self):
        from egmtrans import flattening, interpolation
        from egmtrans.numba_utils import FASTMATH_FLAGS, NUMBA_AVAILABLE

        if not NUMBA_AVAILABLE:
            pytest.skip("Numba is not installed; nothing is compiled.")
        strict = [
            flattening._label_patches, flattening._patch_boundary_stats, flattening._containment_stats,
            flattening._patch_levels, flattening._apply_levels_parallel, flattening._apply_levels_sequential,
            flattening._round_half_away_scalar, flattening._enclosed_low_regions,
            interpolation._bilinear_interpolate_numba,
        ]
        for kernel in strict:
            assert not kernel.targetoptions.get('fastmath'), kernel.py_func.__name__
        for kernel in (interpolation.compute_rbf_weights, interpolation.interpolate_chunk):
            assert kernel.targetoptions['fastmath'] == set(FASTMATH_FLAGS)
        # Numba's parallel mode has crashed ArcGIS Pro: only the level writer has
        # a parallel variant, and ArcGIS runs the sequential one.
        parallel = [k.py_func.__name__ for k in strict if k.targetoptions.get('parallel')]
        assert parallel == ['_apply_levels_parallel']

    def test_compiled_kernels_equal_plain_python(self):
        """A host without Numba runs the same functions uncompiled and must label alike."""
        from egmtrans import flattening
        from egmtrans.numba_utils import NUMBA_AVAILABLE

        if not NUMBA_AVAILABLE:
            pytest.skip("Numba is not installed; the kernels already run as plain Python.")
        rng = np.random.default_rng(23)
        cm = rng.integers(-3, 9, size=(40, 45)).astype(np.int32)       # 1 cm steps around sea level
        cm[rng.random(cm.shape) < 0.05] = flattening.VOID_CM
        ocean = (cm >= -1) & (cm <= 1)
        seeds = np.flatnonzero((cm != flattening.VOID_CM) & ~ocean)
        start = np.where(ocean, 1, 0).astype(np.int32)
        compiled = flattening._label_patches(cm, start.copy(), seeds, 3)
        plain = flattening._label_patches.py_func(cm, start.copy(), seeds, 3)
        assert np.array_equal(compiled, plain) and compiled.max() > 2
        assert np.array_equal(compiled, flattening.label_centimeters(cm, 3))

        for a, b in zip(
            flattening._patch_boundary_stats(cm, compiled, flattening.VOID_CM),
            flattening._patch_boundary_stats.py_func(cm, compiled, flattening.VOID_CM),
        ):
            assert np.array_equal(a, b)

        output = (cm / 100 - 0.3).astype(np.float32)
        output[cm == flattening.VOID_CM] = np.nan
        levels, _ = flattening.patch_levels(output, compiled)
        label_cm = flattening.patch_centimeters(cm, compiled)
        for rounded in (False, True):
            args = (cm, output * (100.0 if rounded else 1.0), compiled, levels, label_cm, rounded)
            assert flattening._containment_stats(*args) == flattening._containment_stats.py_func(*args)

        seeds, seed_levels = flattening._low_seeds(cm, compiled, label_cm)
        args = (cm, compiled, label_cm, levels, seeds, seed_levels, 6, flattening.VOID_CM)
        regions = flattening._enclosed_low_regions(*args)
        assert regions[1].size > 0
        for a, b in zip(regions, flattening._enclosed_low_regions.py_func(*args)):
            assert np.array_equal(a, b)


@pytest.fixture(params=[process_patches, process_patches_arcpy], ids=["parallel", "arcpy"])
def process(request):
    return request.param


class TestProcessPatches:
    def test_ocean_set_to_zero(self, process):
        data = np.array([[10.0, 20.0], [30.0, 40.0]], dtype=np.float32)
        labels = np.array([[1, 0], [0, 0]], dtype=np.int32)
        result, levels = process(data.copy(), labels)
        assert result[0, 0] == 0.0  # ocean -> 0
        assert levels[1] == 0.0

    def test_flat_patch_set_to_minimum(self, process):
        """A patch takes its lowest transformed value, not the mean.

        The mean left the shore at a large lake's high-correction end below the
        water; the minimum never rises above any post of the patch.
        """
        data = np.array([[10.0, 12.0], [11.0, 13.0]], dtype=np.float32)
        labels = np.array([[2, 2], [2, 0]], dtype=np.int32)
        result, levels = process(data.copy(), labels)
        assert result[0, 0] == 10.0 and result[0, 1] == 10.0 and result[1, 0] == 10.0
        assert levels[2] == 10.0
        # Unlabeled pixel should be unchanged
        assert result[1, 1] == 13.0

    def test_level_override_lowers_a_patch(self, process):
        data = np.array([[10.0, 12.0], [11.0, 13.0]], dtype=np.float64)
        labels = np.array([[2, 2], [2, 0]], dtype=np.int32)
        override = np.array([np.nan, np.nan, 9.5])
        result, levels = process(data.copy(), labels, override)
        assert levels[2] == 9.5
        assert np.all(result[labels == 2] == 9.5)

    def test_override_never_raises_a_patch(self, process):
        data = np.array([[10.0, 12.0], [11.0, 13.0]], dtype=np.float64)
        labels = np.array([[2, 2], [2, 0]], dtype=np.int32)
        override = np.array([np.nan, np.nan, 11.0])
        result, levels = process(data.copy(), labels, override)
        assert levels[2] == 10.0
        assert np.all(result[labels == 2] == 10.0)

    def test_returns_levels_by_label(self):
        data = np.array([[5.0, 7.0, 9.0], [1.0, 8.0, 4.0]], dtype=np.float64)
        labels = np.array([[1, 2, 2], [1, 0, 4]], dtype=np.int32)
        levels, counts = patch_levels(data, labels)
        assert levels.shape == (5,) and counts.shape == (5,)
        assert levels[1] == 0.0 and counts[1] == 2
        assert levels[2] == 7.0 and counts[2] == 2
        assert np.isnan(levels[3]) and counts[3] == 0
        assert levels[4] == 4.0 and counts[4] == 1
        assert np.isnan(levels[0])

    def test_nan_inside_patch_is_skipped(self):
        data = np.array([[10.0, np.nan], [11.0, 13.0]], dtype=np.float64)
        labels = np.array([[2, 2], [2, 0]], dtype=np.int32)
        levels, counts = patch_levels(data, labels)
        assert levels[2] == 10.0 and counts[2] == 2
        result = apply_levels(data.copy(), labels, levels)
        assert np.isnan(result[0, 1]), "a void inside a patch was filled"
        assert result[0, 0] == 10.0 and result[1, 0] == 10.0

    def test_apply_levels_sequential_matches_parallel(self):
        rng = np.random.default_rng(3)
        data = rng.uniform(-50, 50, (30, 30))
        labels = rng.integers(0, 5, (30, 30)).astype(np.int32)
        levels, _ = patch_levels(data, labels)
        parallel = apply_levels(data.copy(), labels, levels, parallel=True)
        sequential = apply_levels(data.copy(), labels, levels, parallel=False)
        np.testing.assert_array_equal(parallel, sequential)
