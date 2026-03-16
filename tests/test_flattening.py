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
        # NaN pixels should never be labeled as a flat land patch (label > 1)
        assert result[0, 0] <= 1
        # The three valid 1.0 pixels should be detected as a flat region
        assert result[0, 1] > 1
        assert result[1, 0] > 1
        assert result[1, 1] > 1


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
