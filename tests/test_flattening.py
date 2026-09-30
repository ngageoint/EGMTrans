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

    def test_dted_integer_plateau_is_labelled(self):
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


class TestFastmathFlags:
    def test_nan_assumptions_excluded(self):
        from egmtrans.numba_utils import FASTMATH_FLAGS

        # 'nnan'/'ninf' let LLVM delete np.isnan() tests; 'fast' implies both.
        assert not {'nnan', 'ninf', 'fast'} & set(FASTMATH_FLAGS)


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
