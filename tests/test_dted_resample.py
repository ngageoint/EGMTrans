"""Tests for egmtrans.dted.resample: the whole-degree lattice, the integer-weight
bilinear resampler, carried labels, thinning and the band-edge master row."""

import ast
import math
import os
from fractions import Fraction

import numpy as np
import pytest
from osgeo import gdal

from egmtrans.dted import resample
from egmtrans.dted.header import CellGeometry
from egmtrans.dted.resample import (
    BAND_EDGE_STEP,
    AxisMap,
    ResampleError,
    SourceGrid,
    band_edge_step,
    carry_labels,
    master_row,
    regrid_bilinear,
    thin,
)
from tests.conftest import lattice_geotransform, point_geotransform

# TanDEM-X posts per degree of longitude by band, with the DTED level-2
# longitude lines of the zone the band falls in.
LATITUDE_BANDS = [(9000, 3601), (6000, 1801), (4500, 1801), (3000, 1201), (3000, 901), (1800, 601), (900, 601)]


def reference_bilinear(window, rows, cols):
    """Exact rational bilinear values, rounded once to double."""
    source_rows, source_cols = window.shape
    out = np.full((rows, cols), np.nan)
    for i in range(rows):
        for j in range(cols):
            py = Fraction(i * (source_rows - 1), rows - 1) if rows > 1 else Fraction(0)
            px = Fraction(j * (source_cols - 1), cols - 1) if cols > 1 else Fraction(0)
            y0, x0 = math.floor(py), math.floor(px)
            fy, fx = py - y0, px - x0
            terms = []
            for yy, wy in ((y0, 1 - fy), (y0 + 1, fy)):
                for xx, wx in ((x0, 1 - fx), (x0 + 1, fx)):
                    w = wy * wx
                    if w == 0 or yy >= source_rows or xx >= source_cols or np.isnan(window[yy, xx]):
                        continue
                    terms.append((w, Fraction(float(window[yy, xx]))))
            if terms:
                out[i, j] = float(sum(w * v for w, v in terms) / sum(w for w, _ in terms))
    return out


class TestSourceGrid:
    @pytest.mark.parametrize('per_degree', [n for n, _ in LATITUDE_BANDS])
    def test_band_spacings_are_lattices(self, per_degree):
        grid = SourceGrid.from_geotransform(lattice_geotransform(6, 49, per_degree, 9000), per_degree + 1, 9001)
        assert (grid.per_degree_x, grid.per_degree_y) == (per_degree, 9000)
        assert (grid.west, grid.north) == (6 * per_degree, 50 * 9000)
        assert grid.cells() == [(6, 49)]
        rows, cols = grid.window(6, 49)
        assert (rows, cols) == (slice(0, 9001), slice(0, per_degree + 1))

    def test_ulp_noise_in_the_geotransform_is_tolerated(self):
        gt = list(lattice_geotransform(-1, 50, 6000, 9000))
        gt[0] = np.nextafter(np.nextafter(gt[0], 1), 1)
        gt[1] = np.nextafter(gt[1], 0)
        gt[3] = np.nextafter(gt[3], 0)
        grid = SourceGrid.from_geotransform(tuple(gt), 6001, 9001)
        assert (grid.per_degree_x, grid.per_degree_y, grid.west, grid.north) == (6000, 9000, -6000, 459000)

    def test_rejections_name_the_reason(self):
        with pytest.raises(ResampleError, match='rotated'):
            SourceGrid.from_geotransform((6.0, 0.001, 0.0001, 50.0, 0.0, -0.001), 100, 100)
        with pytest.raises(ResampleError, match='longitude spacing .* not a whole number of posts per degree'):
            SourceGrid.from_geotransform((6.0, 0.00011, 0.0, 50.0, 0.0, -0.0001), 100, 100)
        with pytest.raises(ResampleError, match='latitude spacing .* not a whole number'):
            SourceGrid.from_geotransform((6.0, 0.0001, 0.0, 50.0, 0.0, -0.00015), 100, 100)
        with pytest.raises(ResampleError, match='first post lies at longitude .* off the lattice'):
            SourceGrid.from_geotransform((6.00005 - 0.00005, 0.0001, 0.0, 50.0 + 0.00005, 0.0, -0.0001), 100, 100)
        with pytest.raises(ResampleError, match='first post lies at latitude .* off the lattice'):
            SourceGrid.from_geotransform((6.0 - 0.00005, 0.0001, 0.0, 50.0 + 0.00003, 0.0, -0.0001), 100, 100)
        with pytest.raises(ResampleError, match='not positive'):
            SourceGrid.from_geotransform((6.0, 0.0001, 0.0, 50.0, 0.0, 0.0001), 100, 100)
        with pytest.raises(ResampleError, match='at least 2 x 2'):
            SourceGrid.from_geotransform(lattice_geotransform(6, 49, 10, 10), 1, 11)

    def test_a_tile_without_its_south_row_and_east_column_holds_no_cell(self):
        # A Copernicus-style tile: 3600 x 3600 posts from the north-west corner.
        grid = SourceGrid.from_geotransform(lattice_geotransform(126, 7, 3600, 3600), 3600, 3600)
        assert grid.cells() == []
        with pytest.raises(ResampleError, match='does not hold the whole cell'):
            grid.window(126, 7)

    def test_cells_of_wide_tall_and_mosaic_rasters(self):
        wide = SourceGrid.from_geotransform(lattice_geotransform(6, 49, 60, 60, extent_x=2.0), 121, 61)
        assert wide.cells() == [(6, 49), (7, 49)]
        tall = SourceGrid.from_geotransform(lattice_geotransform(6, 48, 60, 60, extent_y=2.0), 61, 121)
        assert tall.cells() == [(6, 49), (6, 48)]
        assert tall.window(6, 48) == (slice(60, 121), slice(0, 61))
        # A mosaic that starts and ends off the whole degree holds the cells inside it.
        gt = (5.5 - 1 / 120, 1 / 60, 0.0, 51.25 + 1 / 120, 0.0, -1 / 60)
        mosaic = SourceGrid.from_geotransform(gt, 181, 151)   # 5.5 to 8.5, 51.25 down to 48.75
        assert mosaic.cells() == [(6, 50), (7, 50), (6, 49), (7, 49)]
        assert mosaic.window(7, 49) == (slice(75, 136), slice(90, 151))

    def test_point_geotransform_of_the_test_tiles(self):
        grid = SourceGrid.from_geotransform(point_geotransform(126, 6, 121), 121, 121)
        assert grid.cells() == [(126, 6)] and grid.per_degree_x == 120


class TestAxisMap:
    @pytest.mark.parametrize('per_degree,cols', LATITUDE_BANDS)
    def test_bands_reduce_to_halves_and_thirds(self, per_degree, cols):
        axis = AxisMap.between(per_degree + 1, cols)
        assert axis.denominator in (1, 2, 3)
        assert axis.lower[0] == 0 and axis.numerator[0] == 0
        assert axis.lower[-1] == per_degree and axis.numerator[-1] == 0
        # Position k * per_degree / (cols - 1), exactly.
        for k in (1, 2, 3, cols // 2, cols - 2):
            position = Fraction(k * per_degree, cols - 1)
            assert axis.lower[k] == math.floor(position)
            assert Fraction(int(axis.numerator[k]), axis.denominator) == position - math.floor(position)

    def test_geoid_grid_up_to_the_posts(self):
        axis = AxisMap.between(61, 3601)
        assert axis.denominator == 60 and axis.lower[61] == 1 and axis.numerator[61] == 1

    def test_single_posts_and_weights(self):
        one = AxisMap.between(5, 1)
        assert one.lower.tolist() == [0] and one.denominator == 1
        same = AxisMap.between(4, 4)
        assert same.lower.tolist() == [0, 1, 2, 3] and same.denominator == 1
        lo, hi = AxisMap.between(3, 5).weights
        assert lo.tolist() == [2, 1, 2, 1, 2] and hi.tolist() == [0, 1, 0, 1, 0]
        assert AxisMap.between(3, 5).upper.tolist() == [0, 1, 1, 2, 2]
        with pytest.raises(ResampleError):
            AxisMap.between(0, 3)


class TestRegridBilinear:
    def _window(self, rows, cols, seed=1, voids=0.05):
        rng = np.random.default_rng(seed)
        window = (rng.integers(-30000, 300000, size=(rows, cols)) / 100).astype(np.float32)
        if voids:
            window[rng.random(window.shape) < voids] = np.nan
        return window

    @pytest.mark.parametrize(
        'shape', [((7, 11), (10, 5)), ((11, 7), (6, 13)), ((13, 13), (25, 25)), ((4, 61), (7, 121))]
    )
    def test_equals_exact_rational_arithmetic(self, shape):
        (source_rows, source_cols), (rows, cols) = shape
        window = self._window(source_rows, source_cols)
        result = regrid_bilinear(window, rows, cols)
        assert result.dtype == np.float64
        assert np.array_equal(result, reference_bilinear(window, rows, cols), equal_nan=True)

    def test_block_size_does_not_change_the_result(self):
        window = self._window(31, 41)
        whole = regrid_bilinear(window, 46, 61)
        assert np.array_equal(whole, regrid_bilinear(window, 46, 61, block_posts=61), equal_nan=True)
        assert np.array_equal(whole, regrid_bilinear(window, 46, 61, block_posts=7), equal_nan=True)

    def test_a_constant_area_stays_exact_and_nodes_keep_their_value(self):
        window = np.full((16, 13), 799.99, dtype=np.float32)
        window[3:9, 2:7] = 123.45
        result = regrid_bilinear(window, 31, 25)
        assert np.all(result[0:4, 0:4] == np.float32(799.99))
        assert np.all(result[8:14, 6:10] == np.float32(123.45))
        # Every other post of the result is a node of the source.
        assert np.array_equal(result[::2, ::2], window.astype(np.float64))

    def test_void_rules(self):
        window = np.array([[1.0, np.nan, 5.0], [3.0, 4.0, np.nan]], dtype=np.float32)
        result = regrid_bilinear(window, 3, 5)
        assert np.isnan(result[0, 2]), 'a void node stays void'
        assert result[0, 1] == 1.0 and result[0, 3] == 5.0, 'the valid neighbor alone'
        assert result[1, 2] == 4.0, 'three voids of four: the one valid neighbor'
        assert np.isnan(result[2, 4]) and result[2, 3] == 4.0
        assert np.array_equal(result, reference_bilinear(window, 3, 5), equal_nan=True)
        assert np.isnan(regrid_bilinear(np.full((3, 3), np.nan), 5, 5)).all()

    def test_level_1_equals_level_2_thinned(self):
        window = self._window(41, 31, voids=0.02)
        level2 = regrid_bilinear(window, 121, 91)
        level1 = regrid_bilinear(window, 41, 31)
        assert np.array_equal(thin(level2, 1), level1, equal_nan=True)
        assert np.array_equal(thin(level2, 2), level2, equal_nan=True)
        assert thin(level2, 0).shape == (5, 4)
        with pytest.raises(ValueError):
            thin(level2, 3)

    def test_agrees_with_gdal_warp(self, tmp_dir):
        """gdal.Warp with XSCALE=1, YSCALE=1 is plain bilinear at the posts: an
        independent oracle, agreeing to the last places of a double."""
        from tests.conftest import write_geotiff

        rows, cols = 61, 46
        window = self._window(31, 61, voids=0)
        src = write_geotiff(os.path.join(tmp_dir, 'src.tif'), window, lattice_geotransform(6, 49, 60, 30))
        px, py = 1 / (cols - 1), 1 / (rows - 1)
        ds = gdal.Warp(
            '', src, format='MEM', resampleAlg='bilinear', width=cols, height=rows,
            outputBounds=(6 - px / 2, 49 - py / 2, 7 + px / 2, 50 + py / 2),
            warpOptions=['XSCALE=1', 'YSCALE=1'], outputType=gdal.GDT_Float64,
        )
        warped = ds.GetRasterBand(1).ReadAsArray()
        ds = None
        result = regrid_bilinear(window, rows, cols)
        assert np.abs(result - warped).max() < 1e-9

    def test_no_float_reduction_in_the_module(self):
        """The resampler adds its four terms elementwise; a reduction over an
        axis could sum in another order on another build of numpy."""
        source = open(resample.__file__).read()
        tree = ast.parse(source)
        calls = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not calls & {'sum', 'mean', 'dot', 'matmul', 'einsum', 'cumsum', 'average', 'nansum'}
        assert 'numba' not in source and 'gdal' not in source


class TestCarryLabels:
    def test_all_contributors_rule(self):
        labels = np.zeros((3, 5), dtype=np.int32)
        labels[:, :3] = 2       # a body on the west, 3 columns wide
        labels[0, 4] = 3
        carried = carry_labels(labels, 3, 3)   # columns at source 0, 2, 4
        assert carried[:, 0].tolist() == [2, 2, 2]
        assert carried[:, 1].tolist() == [2, 2, 2]
        assert carried[:, 2].tolist() == [3, 0, 0]
        between = carry_labels(labels, 3, 2)   # columns at source 0 and 4: nodes
        assert between[:, 0].tolist() == [2, 2, 2] and between[0, 1] == 3
        blend = carry_labels(labels, 3, 4)     # position 4/3: between source 1 and 2 (both 2), 8/3: 2 and 3
        assert blend[:, 1].tolist() == [2, 2, 2] and blend[:, 2].tolist() == [0, 0, 0]

    def test_a_body_with_one_post_voids_and_ocean(self):
        labels = np.zeros((5, 5), dtype=np.int32)
        labels[2, 2] = 7                     # a pond of one source post
        labels[4, :] = 1                     # ocean along the south edge
        valid = np.ones((5, 5), dtype=bool)
        valid[2, 1] = False                  # a void beside the pond is no contributor
        labels[0, 0:2] = 4
        valid[0, 1] = False
        carried = carry_labels(labels, 3, 3, valid)  # posts at source 0, 2, 4
        assert carried[1, 1] == 7 and carried[2, :].tolist() == [1, 1, 1]
        assert carried[0, 0] == 4
        halfway = carry_labels(labels, 5, 9, valid)  # columns every half post
        assert halfway[2, 4] == 7 and halfway[2, 3] == 7, 'the void neighbor is ignored'
        assert halfway[2, 5] == 0, 'a terrain neighbor breaks the carry'
        assert np.all(halfway[4, :] == 1)
        assert carried.dtype == np.int32


class TestMasterRow:
    @pytest.mark.parametrize('edge', sorted(BAND_EDGE_STEP))
    @pytest.mark.parametrize('hemisphere', ['north', 'south'])
    def test_recovers_the_coarse_row_from_a_nearest_neighbor_copy(self, edge, hemisphere):
        numerator, denominator = BAND_EDGE_STEP[edge]
        fine = 90 * denominator
        coarse = fine * numerator // denominator
        lat0 = edge - 1 if hemisphere == 'north' else -edge
        assert band_edge_step(lat0, fine) == (numerator, denominator)
        rng = np.random.default_rng(edge)
        coarse_row = (rng.integers(-500, 200000, size=coarse + 1) / 100).astype(np.float32)
        coarse_row[3] = np.nan
        # Fine post i takes the nearest coarse post, the upper one on a tie.
        nearest = np.floor(np.arange(fine + 1) * Fraction(numerator, denominator) + Fraction(1, 2)).astype(int)
        fine_row = coarse_row[np.minimum(nearest, coarse)]
        recovered = master_row(fine_row, fine, (numerator, denominator))
        assert recovered is not None
        row, taken = recovered
        assert np.array_equal(row, coarse_row, equal_nan=True)
        assert np.array_equal(fine_row[taken], coarse_row, equal_nan=True)
        # A tie resolved the other way is a copy too.
        lower = np.ceil(np.arange(fine + 1) * Fraction(numerator, denominator) - Fraction(1, 2)).astype(int)
        other = coarse_row[np.maximum(lower, 0)]
        assert master_row(other, fine, (numerator, denominator))[0].tolist() == pytest.approx(row.tolist(), nan_ok=True)

    def test_a_row_of_real_data_is_left_alone(self):
        rng = np.random.default_rng(3)
        row = (rng.integers(0, 100000, size=91) / 100).astype(np.float32)
        assert master_row(row, 90, (2, 3)) is None
        ocean = np.zeros(91, dtype=np.float32)
        assert master_row(ocean, 90, (2, 3)) is not None, 'an all-ocean row is a copy of itself'
        with pytest.raises(ResampleError):
            master_row(row, 60, (2, 3))
        assert master_row(row, 90, (2, 7)) is None, 'a step that does not divide the row'
        assert band_edge_step(48, 9000) is None and band_edge_step(-49, 9000) is None
        assert band_edge_step(49, 9001) is None, 'a spacing the step does not divide'

    def test_shared_posts_of_two_cells_agree_only_with_the_rule(self):
        """The cell below a band boundary gets its edge row from the master row,
        so both cells derive the shared row from the same data."""
        numerator, denominator = BAND_EDGE_STEP[50]
        fine, coarse = 180, 120
        rng = np.random.default_rng(50)
        coarse_row = (rng.integers(10000, 30000, size=coarse + 1) / 100).astype(np.float32)
        nearest = np.floor(np.arange(fine + 1) * Fraction(numerator, denominator) + Fraction(1, 2)).astype(int)
        fine_row = coarse_row[np.minimum(nearest, coarse)]

        north_cell = CellGeometry(2, 6, 50)   # zone II: 1801 lines
        south_cell = CellGeometry(2, 6, 49)   # zone I: 3601 lines
        north_edge = regrid_bilinear(coarse_row[np.newaxis, :], 1, north_cell.lon_lines)[0]
        naive = regrid_bilinear(fine_row[np.newaxis, :], 1, south_cell.lon_lines)[0]
        row, _ = master_row(fine_row, fine, (numerator, denominator))
        rebuilt = regrid_bilinear(row[np.newaxis, :], 1, south_cell.lon_lines)[0]
        shared = slice(0, None, 2)   # every second post of the finer cell lies on the coarser one
        assert np.array_equal(rebuilt[shared], north_edge)
        assert not np.array_equal(naive[shared], north_edge)
