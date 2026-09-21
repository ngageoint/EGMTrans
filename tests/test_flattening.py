"""Tests for egmtrans.flattening."""

import numpy as np

from egmtrans.flattening import (
    create_labeled_array_flt,
    create_labeled_array_int,
    process_patches,
)


class TestCreateLabeledArrayInt:
    def test_ocean_labeled(self):
        arr = np.array([[0, 0, 5], [0, 3, 3], [7, 7, 0]], dtype=np.int32)
        result = create_labeled_array_int(arr)
        assert result[0, 0] == 1  # ocean
        assert result[0, 1] == 1
        assert result[1, 1] == 0  # non-ocean
        assert result[2, 2] == 1

    def test_no_ocean(self):
        arr = np.array([[1, 2], [3, 4]], dtype=np.int32)
        result = create_labeled_array_int(arr)
        assert np.all(result == 0)


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


class TestFastmathFlags:
    def test_nan_assumptions_excluded(self):
        from egmtrans.numba_utils import FASTMATH_FLAGS

        # 'nnan'/'ninf' let LLVM delete np.isnan() tests; 'fast' implies both.
        assert not {'nnan', 'ninf', 'fast'} & set(FASTMATH_FLAGS)


class TestProcessPatches:
    def test_ocean_set_to_zero(self):
        data = np.array([[10.0, 20.0], [30.0, 40.0]], dtype=np.float32)
        labels = np.array([[1, 0], [0, 0]], dtype=np.int32)
        result = process_patches(data.copy(), labels)
        assert result[0, 0] == 0.0  # ocean -> 0

    def test_flat_patch_averaged(self):
        data = np.array([[10.0, 12.0], [11.0, 13.0]], dtype=np.float32)
        labels = np.array([[2, 2], [2, 0]], dtype=np.int32)
        result = process_patches(data.copy(), labels)
        # Average of 10, 12, 11 = 11.0
        expected_avg = (10.0 + 12.0 + 11.0) / 3.0
        np.testing.assert_allclose(result[0, 0], expected_avg, atol=0.1)
        np.testing.assert_allclose(result[0, 1], expected_avg, atol=0.1)
        np.testing.assert_allclose(result[1, 0], expected_avg, atol=0.1)
        # Unlabeled pixel should be unchanged
        assert result[1, 1] == 13.0
