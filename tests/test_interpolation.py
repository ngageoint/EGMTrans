"""Tests for egmtrans.interpolation — using small known arrays."""

import numpy as np
import pytest

from egmtrans.interpolation import bilinear_interpolation, delaunay_triangulation, spline_interpolation


@pytest.fixture
def regular_grid_points():
    """Create a simple 3x3 regular grid with known z-values."""
    x = np.array([0.0, 1.0, 2.0, 0.0, 1.0, 2.0, 0.0, 1.0, 2.0], dtype=np.float64)
    y = np.array([0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0], dtype=np.float64)
    z = np.array([0.0, 1.0, 2.0, 1.0, 2.0, 3.0, 2.0, 3.0, 4.0], dtype=np.float64)
    return {'x': x, 'y': y, 'z': z}


@pytest.fixture
def query_grid():
    """Create a query grid at half-pixel offsets."""
    x = np.array([[0.5, 1.5]])
    y = np.array([[0.5, 0.5]])
    return x, y


class TestBilinearInterpolation:
    def test_known_values(self, regular_grid_points, query_grid):
        xx, yy = query_grid
        result = bilinear_interpolation(regular_grid_points, xx, yy)
        assert result.shape == xx.shape
        # At (0.5, 0.5): bilinear of (0,1,1,2) -> 1.0
        np.testing.assert_allclose(result[0, 0], 1.0, atol=0.1)
        # At (1.5, 0.5): bilinear of (1,2,2,3) -> 2.0
        np.testing.assert_allclose(result[0, 1], 2.0, atol=0.1)

    def test_output_shape_matches_query(self, regular_grid_points):
        xx = np.zeros((5, 5), dtype=np.float64)
        yy = np.zeros((5, 5), dtype=np.float64)
        for i in range(5):
            for j in range(5):
                xx[i, j] = j * 0.5
                yy[i, j] = i * 0.5
        result = bilinear_interpolation(regular_grid_points, xx, yy)
        assert result.shape == (5, 5)


class TestDelaunayTriangulation:
    def test_known_values(self, regular_grid_points, query_grid):
        xx, yy = query_grid
        result = delaunay_triangulation(regular_grid_points, xx, yy)
        assert result.shape == xx.shape
        # Delaunay on a regular grid should give similar results to bilinear
        np.testing.assert_allclose(result[0, 0], 1.0, atol=0.2)
        np.testing.assert_allclose(result[0, 1], 2.0, atol=0.2)


class TestSplineInterpolation:
    def test_output_shape(self, regular_grid_points, query_grid):
        xx, yy = query_grid
        result = spline_interpolation(regular_grid_points, xx, yy)
        assert result.shape == xx.shape

    def test_values_in_range(self, regular_grid_points, query_grid):
        xx, yy = query_grid
        result = spline_interpolation(regular_grid_points, xx, yy)
        # Spline interpolation values should be in a reasonable range
        assert np.all(result > -5.0)
        assert np.all(result < 10.0)
