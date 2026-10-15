"""Tests for egmtrans.transform."""

import os

import numpy as np
import pytest

from egmtrans.config import BASE_PATH, DATUM_MAPPING
from egmtrans.transform import (
    create_datum_array,
    create_gdal_warp_array,
    create_interp_array,
    transform_vertical_datum,
)
from tests.conftest import point_geotransform, read_band, write_geotiff


def _grids_available() -> bool:
    return all(
        os.path.isfile(os.path.join(BASE_PATH, "datums", DATUM_MAPPING[datum]["grid"]))
        for datum in ("EGM96", "EGM2008")
    )


requires_grids = pytest.mark.skipif(
    not _grids_available(),
    reason="Geoid grid files not present; run 'python download_grids.py' to fetch.",
)


def test_transform_functions_exist():
    """Verify all expected transform functions are importable."""
    assert callable(create_gdal_warp_array)
    assert callable(create_interp_array)
    assert callable(create_datum_array)
    assert callable(transform_vertical_datum)


@requires_grids
def test_flatten_keeps_voids_ocean_and_flat_patches(tmp_dir):
    """Voids stay voids, ocean stays 0 m, and a lake stays one flat surface.

    Placed off Mindanao, where EGM2008 - EGM96 reaches several meters, so a
    lake that was not flattened as one patch could not pass by accident.
    Before the fastmath fix every void came out at 0 m.
    """
    from osgeo import gdal, osr

    rows = cols = 40
    nodata = -9999.0
    i, j = np.mgrid[0:rows, 0:cols]
    dem = (200.0 + i + 0.37 * j).astype(np.float32)  # no two terrain pixels share a value
    dem[:, :10] = 0.0                                  # ocean
    dem[20:30, 20:30] = 150.0                          # lake, in a basin below the terrain
    voids = [(0, 0), (5, 3), (25, 25), (12, 33)]      # in the ocean, the lake, and on land
    for r, c in voids:
        dem[r, c] = nodata

    src = os.path.join(tmp_dir, "voids_dem.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((126.30, 0.01, 0.0, 7.90, 0.0, -0.01))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(dem)
    ds.GetRasterBand(1).SetNoDataValue(nodata)
    ds = None

    out = os.path.join(tmp_dir, "voids_dem_egm96.tif")
    transform_vertical_datum(
        input_file=src,
        output_file=out,
        src_datum="EGM2008",
        tgt_datum="EGM96",
        flatten=True,
        create_mask=False,
        min_patch_size=16,
        algorithm="bilinear",
        save_log=False,
    )

    out_ds = gdal.Open(out)
    result = out_ds.GetRasterBand(1).ReadAsArray()
    out_ds = None

    void = dem == nodata
    assert np.all(np.isnan(result[void])), "a void was written as a height"
    assert np.all(np.isfinite(result[~void]))

    ocean = (dem == 0.0) & ~void
    assert np.all(result[ocean] == 0.0)

    lake = np.zeros_like(void)
    lake[20:30, 20:30] = True
    lake &= ~void
    assert np.unique(result[lake]).size == 1, "the lake surface is no longer flat"
    assert abs(float(result[lake][0]) - 150.0) > 0.5, "the lake was not shifted with the datum"


@requires_grids
def test_dted_flat_patch_is_one_level(tmp_dir):
    """A DTED lake whose corrected height straddles a rounding boundary is one level.

    DTED used to have only its ocean flattened, so the whole-meter rounding split
    such a lake into two levels along the boundary. The tile sits off Mindanao,
    where the correction changes by meters across a degree, and the lake covers
    half the tile, so the split is certain; the test checks that precondition.
    """
    from egmtrans.io import round_half_away
    from tests.conftest import read_band, write_dted

    posts = 121
    i, j = np.mgrid[0:posts, 0:posts]
    dem = (10 + 3 * i + 5 * j).astype(np.int16)  # neighbors always differ
    dem[30:90, 20:100] = 150
    src = write_dted(os.path.join(tmp_dir, "n06e126.dt0"), dem, 126, 6)
    out = os.path.join(tmp_dir, "n06e126_egm96.dt0")

    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False)

    result = read_band(out)
    lake = result[30:90, 20:100]
    assert np.unique(lake).size == 1, f"the lake came out at {np.unique(lake).tolist()}"

    egm96 = create_datum_array(src, "EGM96", "bilinear", tmp_dir, tmp_dir)
    egm2008 = create_datum_array(src, "EGM2008", "bilinear", tmp_dir, tmp_dir)
    raw = 150 - (egm96 - egm2008)[30:90, 20:100].astype(np.float64)
    assert np.unique(round_half_away(raw)).size >= 2, "the test lake does not straddle a rounding boundary"
    assert lake[0, 0] == round_half_away(raw.min())
    # Land posts (no two neighbors alike, so no patches) are rounded one by one.
    land = round_half_away(dem[:30, :].astype(np.float64) - (egm96 - egm2008)[:30, :])
    assert np.array_equal(result[:30, :], land)


@requires_grids
def test_single_file_reports_edge_touching_water_bodies(tmp_dir, log_lines):
    """A lake on the tile edge is listed, because its level may differ next door."""
    from osgeo import gdal, osr

    rows = cols = 40
    i, j = np.mgrid[0:rows, 0:cols]
    dem = (200.0 + i + 0.37 * j).astype(np.float32)
    dem[20:30, 30:40] = 150.0   # reaches the east edge
    dem[5:10, 5:10] = 80.0      # interior: not listed

    src = os.path.join(tmp_dir, "edge_lake.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((126.30, 0.01, 0.0, 7.90, 0.0, -0.01))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(dem)
    ds = None

    out = os.path.join(tmp_dir, "edge_lake_egm96.tif")
    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False)

    text = "\n".join(log_lines)
    assert "touching an edge of this run" in text
    assert "150.00 m" in text and "edge_lake.tif:E" in text
    assert "80.00 m" not in text
    assert "1 touch the run boundary" in text


@requires_grids
def test_containment_is_counted_and_never_broken(tmp_dir, log_lines):
    """Shore posts that were above a lake in the input never end below it."""
    from egmtrans.flattening import containment_stats, create_labeled_array_flt, patch_levels
    from tests.conftest import read_band, write_dted

    posts = 121
    i, j = np.mgrid[0:posts, 0:posts]
    dem = (500 + 3 * i + 5 * j).astype(np.int16)
    dem[30:90, 20:100] = 150  # a lake in a basin: every shore post is above it
    src = write_dted(os.path.join(tmp_dir, "n06e126.dt0"), dem, 126, 6)
    out = os.path.join(tmp_dir, "n06e126_egm96.dt0")

    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False)

    line = next(line for line in log_lines if line.startswith("Containment:"))
    assert " 0 that were at or above it are now below it" in line
    assert "shore posts border a water body" in line and "0 were already below it" in line

    result = read_band(out).astype(np.float64)
    labeled = create_labeled_array_flt(dem.astype(np.float64), 16)
    levels, _ = patch_levels(result, labeled)
    shore, lost_step, below, already_below = containment_stats(
        dem.astype(np.float64), result, labeled, levels, True
    )
    assert shore == 2 * 60 + 2 * 80 and below == 0 and already_below == 0
    assert np.all(result[29, 20:100] > result[30, 20:100])
    assert np.all(result[90, 20:100] > result[30, 20:100])


@requires_grids
def test_uncontained_flat_area_is_left_as_terrain(tmp_dir, log_lines):
    """A flat hilltop is not a water body: its posts are transformed one by one
    and it stays out of the mask, while the basin lake beside it is flattened."""
    from egmtrans.io import round_half_away
    from tests.conftest import read_band, write_dted

    posts = 121
    i, j = np.mgrid[0:posts, 0:posts]
    dem = (500 + 3 * i + 5 * j).astype(np.int16)
    dem[30:60, 20:60] = 150     # a lake in a basin: every boundary post is above it
    dem[70:100, 20:60] = 2000   # a mesa: every boundary post is below it
    src = write_dted(os.path.join(tmp_dir, "n06e126.dt0"), dem, 126, 6)
    out = os.path.join(tmp_dir, "n06e126_egm96.dt0")

    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, True, 16, "bilinear", save_log=False)

    result = read_band(out)
    assert np.unique(result[30:60, 20:60]).size == 1, "the lake is not one level"

    egm96 = create_datum_array(src, "EGM96", "bilinear", tmp_dir, tmp_dir)
    egm2008 = create_datum_array(src, "EGM2008", "bilinear", tmp_dir, tmp_dir)
    expected_mesa = round_half_away(2000 - (egm96 - egm2008)[70:100, 20:60].astype(np.float64))
    assert np.array_equal(result[70:100, 20:60], expected_mesa), "the mesa was flattened"

    mask = read_band(os.path.join(tmp_dir, "n06e126_egm96_mask.tif"))
    assert np.all(mask[30:60, 20:60] > 1) and np.all(mask[70:100, 20:60] == 0)
    assert any(line.startswith("Left 1 flat area(s) of 1,200 posts as terrain") for line in log_lines)

    # With the filter off, the mesa is a flat area like any other.
    out_all = os.path.join(tmp_dir, "n06e126_all.dt0")
    transform_vertical_datum(
        src, out_all, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False, min_containment=0.0
    )
    assert np.unique(read_band(out_all)[70:100, 20:60]).size == 1


def _write_float_tile(path, dem, nodata=None, x0=126.30):
    from osgeo import gdal, osr

    rows, cols = dem.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, 0.01, 0.0, 7.90, 0.0, -0.01))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(dem)
    if nodata is not None:
        ds.GetRasterBand(1).SetNoDataValue(nodata)
    ds = None
    return path


def test_cog_is_written_from_memory_with_nodata_and_point_registration(tmp_dir):
    """The COG is translated from an in-memory dataset: no uncompressed copy under
    the output folder, NoData NaN, pixel-is-point, the values to the centimeter."""
    from osgeo import gdal

    from egmtrans.transform import load_input, write_output

    rng = np.random.default_rng(11)
    dem = (rng.integers(-500, 300000, size=(30, 40)) / 100).astype(np.float32)
    dem[3:6, 7:12] = -9999.0
    src = _write_float_tile(os.path.join(tmp_dir, "in.tif"), dem, nodata=-9999.0)
    temp_dir = os.path.join(tmp_dir, "temp")
    os.makedirs(temp_dir)
    loaded = load_input(src, temp_dir)
    out = os.path.join(tmp_dir, "out.tif")
    write_output(loaded, loaded.array.astype(np.float64), out, "EGM96", None, temp_dir)
    assert not any(name.endswith("_final_temp.tif") for name in os.listdir(temp_dir))
    ds = gdal.Open(out)
    band = ds.GetRasterBand(1)
    assert ds.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") == "COG"
    assert ds.GetMetadataItem("COMPRESSION", "IMAGE_STRUCTURE") == "DEFLATE"
    assert ds.GetMetadataItem("AREA_OR_POINT") == "Point"
    assert np.isnan(band.GetNoDataValue())
    values = band.ReadAsArray()
    band = None
    ds = None
    void = dem == -9999.0
    assert np.all(np.isnan(values[void]))
    assert np.array_equal(values[~void], np.round(dem[~void].astype(np.float64), 2).astype(np.float32))


@requires_grids
def test_enclosed_low_spot_beside_a_lake_is_raised_and_reported(tmp_dir, log_lines):
    """A few posts below the lake beside them, enclosed by higher ground, come
    out at the lake's level and in the mask; an outlet to lower water does not."""
    from osgeo import gdal

    rows = cols = 40
    i, j = np.mgrid[0:rows, 0:cols]
    dem = (200.0 + i + 0.37 * j).astype(np.float32)
    dem[20:30, 20:30] = 150.0                 # the lake
    dem[30, 25] = 149.5                       # one post under its south shore
    dem[18:20, 22:25] = 147.0 + 0.01 * np.arange(6).reshape(2, 3)   # six posts on its north shore
    dem[5:10, 5:10] = 120.0                   # a lower lake ...
    dem[np.arange(10, 20), np.arange(10, 20)] = 149.0 - 0.01 * np.arange(10)  # ... with a channel to it
    src = _write_float_tile(os.path.join(tmp_dir, "spots.tif"), dem)
    out = os.path.join(tmp_dir, "spots_egm96.tif")

    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, True, 16, "bilinear", save_log=False)

    with gdal.Open(out) as ds:
        result = ds.GetRasterBand(1).ReadAsArray()
    level = result[20, 20]
    assert np.all(result[20:30, 20:30] == level)
    assert result[30, 25] == level and np.all(result[18:20, 22:25] == level)
    # The channel keeps its own transformed values.
    egm96 = create_datum_array(src, "EGM96", "bilinear", tmp_dir, tmp_dir)
    egm2008 = create_datum_array(src, "EGM2008", "bilinear", tmp_dir, tmp_dir)
    channel = (np.arange(10, 20), np.arange(10, 20))
    expected = np.round(dem[channel].astype(np.float64) - (egm96 - egm2008)[channel], 2)
    assert np.allclose(result[channel], expected, atol=0.006), "the outlet channel was raised"
    with gdal.Open(os.path.join(tmp_dir, "spots_egm96_mask.tif")) as ds:
        mask = ds.GetRasterBand(1).ReadAsArray()
    assert mask[30, 25] == mask[20, 20] and np.all(mask[18:20, 22:25] == mask[20, 20])
    assert np.all(mask[np.arange(10, 20), np.arange(10, 20)] == 0)
    text = "\n".join(log_lines)
    assert "Raised 2 enclosed low spot(s) of 7 posts beside water bodies" in text
    # Up to 3 m less what the correction varies across the lake, at the post's center.
    assert "Raised 6 post(s) by up to 2." in text and "(longitude 126.545000, latitude 7.715000)" in text


@requires_grids
def test_whole_meter_data_raises_no_low_spot(tmp_dir, log_lines):
    """On DTED, and on a float tile of whole meters, a post below a plateau is terrain."""
    from egmtrans.io import round_half_away
    from tests.conftest import read_band, write_dted

    posts = 121
    i, j = np.mgrid[0:posts, 0:posts]
    dem = (500 + 3 * i + 5 * j).astype(np.int16)
    dem[30:90, 20:100] = 150
    dem[90, 50] = 149
    src = write_dted(os.path.join(tmp_dir, "n06e126.dt0"), dem, 126, 6)
    out = os.path.join(tmp_dir, "n06e126_egm96.dt0")
    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False)
    result = read_band(out)
    # The post below the lake is rounded on its own, like any land post.
    egm96 = create_datum_array(src, "EGM96", "bilinear", tmp_dir, tmp_dir)
    egm2008 = create_datum_array(src, "EGM2008", "bilinear", tmp_dir, tmp_dir)
    assert result[90, 50] == round_half_away(149 - (egm96 - egm2008)[90, 50])

    tile = _write_float_tile(os.path.join(tmp_dir, "whole.tif"), dem[:40, :40].astype(np.float32))
    out_tile = os.path.join(tmp_dir, "whole_egm96.tif")
    transform_vertical_datum(tile, out_tile, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False)
    assert not any("low spot" in line for line in log_lines)


@requires_grids
def test_all_ocean_rows_stay_at_zero(tmp_dir):
    """Whole rows of ocean must come out at 0 m, not as voids.

    The output was written before its NoData value (NaN) was set, so GDAL skipped
    each all-zero row as an empty block and filled it with NaN on close. A tile
    whose southern rows are open sea lost them all. 4000 columns make every row
    its own strip, as in a real 1-arc-second tile.
    """
    from osgeo import gdal, osr

    rows, cols = 48, 4000
    dem = np.tile(np.linspace(5.0, 400.0, cols, dtype=np.float32), (rows, 1))
    dem[:, :50] = 0.0     # a strip of ocean along the west edge
    dem[30:, :] = 0.0     # and open sea across the southern rows

    src = os.path.join(tmp_dir, "coastal.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((126.30, 0.0001, 0.0, 7.90, 0.0, -0.0001))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(dem)
    ds = None

    out = os.path.join(tmp_dir, "coastal_egm96.tif")
    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False)

    out_ds = gdal.Open(out)
    result = out_ds.GetRasterBand(1).ReadAsArray()
    out_ds = None

    assert not np.isnan(result).any(), f"{int(np.isnan(result).sum())} pixels came out as voids"
    assert np.all(result[dem == 0.0] == 0.0)


class TestCoast:
    """The coast keeps its height above the sea in both directions; the geoid is faked, so the correction is exact."""

    ROWS, COLS = 6, 40

    def _tile(self, tmp_dir, name="coast.tif"):
        land = np.tile(np.linspace(0.2, 20.0, self.COLS - 5, dtype=np.float32), (self.ROWS, 1))
        dem = np.concatenate([np.zeros((self.ROWS, 5), np.float32), land], axis=1)   # the sea in the five west columns
        dem[1:5, 7:11] = 0.5                                                         # a lagoon of 16 posts
        return _write_float_tile(os.path.join(tmp_dir, name), dem), dem

    @staticmethod
    def _fake_geoid(monkeypatch, egm96, egm2008=0.0):
        from osgeo import gdal

        from egmtrans import transform

        values = {"EGM96": egm96, "EGM2008": egm2008}

        def fake(input_file, datum, algorithm, temp_dir, output_dir):
            ds = gdal.Open(input_file)
            shape = (ds.RasterYSize, ds.RasterXSize)
            ds = None
            return np.full(shape, values[datum], dtype=np.float32)

        monkeypatch.setattr(transform, "create_datum_array", fake)

    @staticmethod
    def _convert(src, out, src_datum="EGM2008", tgt_datum="EGM96", **kwargs):
        transform_vertical_datum(
            src, out, src_datum, tgt_datum, True, False, 16, "bilinear", save_log=False, **kwargs
        )
        return read_band(out)

    def test_a_void_bridge_through_a_high_tile_joins_the_low_ground_to_the_sea(self, tmp_dir, monkeypatch):
        """Pass 1 records the low ground of a tile with voids even when every post
        of it stands above the band top, because a void passes low ground through;
        an all-void tile holds one region that joins both of its seams."""
        from egmtrans.tiling import find_seams, low_ground
        from egmtrans.transform import analyze_tile

        self._fake_geoid(monkeypatch, egm96=2.5)
        rows, cols = self.ROWS, self.COLS
        sea = np.full((rows, cols), 5.0, np.float32)
        sea[:, :5] = 0.0
        high = np.full((rows, cols), 100.0, np.float32)
        high[2, :] = -9999.0                               # a void row from the west edge to the east edge
        inland = np.full((rows, cols), 5.0, np.float32)    # no sea
        all_void = np.full((rows, cols), -9999.0, np.float32)
        for middle, name in ((high, "void_row"), (all_void, "all_void")):
            tiles = []
            for i, (dem, x0) in enumerate(((sea, 126.30), (middle, 126.70), (inland, 127.10))):
                path = _write_float_tile(os.path.join(tmp_dir, f"{name}_{i}.tif"), dem, nodata=-9999.0, x0=x0)
                temp_dir = os.path.join(tmp_dir, f"{name}_{i}_temp")
                os.makedirs(temp_dir)
                tiles.append(analyze_tile(path, "EGM2008", "EGM96", "bilinear", 16, temp_dir, i, path + ".out"))
            low = low_ground(tiles, find_seams(tiles))
            assert [group.tile_ids for group in low.groups] == [[0, 1, 2]] and low.groups[0].coastal, name
            assert low.is_coastal(2, 1), f"the inland tile's region is coastal ({name})"

    def test_a_lowered_coast_keeps_its_height_above_the_sea(self, tmp_dir, monkeypatch, log_lines):
        src, dem = self._tile(tmp_dir)
        self._fake_geoid(monkeypatch, egm96=2.5)    # N96 - N08 = 2.5: EGM96 lowers the land by 2.5 m
        out = self._convert(src, os.path.join(tmp_dir, "out.tif"))
        land = dem > 0
        assert np.all(out[:, :5] == 0) and out[land].min() > 0
        strip = land & (dem <= 2.5)
        np.testing.assert_allclose(out[strip], dem[strip], atol=0.0051)    # kept, to the centimeter of the COG
        assert np.all(out[1:5, 7:11] == np.float32(0.5)), "the lagoon keeps its height above the sea too"
        above = dem >= 12.5
        np.testing.assert_allclose(out[above], dem[above] - 2.5, atol=0.0051)    # the plain shift, to the centimeter
        band = land & (dem > 2.5) & (dem < 12.5)
        np.testing.assert_allclose(out[band], dem[band] - 2.5 * (dem[band] - 2.5) / 10.0, atol=0.0051)
        assert np.all(np.diff(out[0, 5:]) > 0), "monotone along the ramp"
        line = next(line for line in log_lines if line.startswith("Coastal containment: "))
        assert "would have lowered the shore by up to 2.50 m" in line and "kept their height above the sea" in line
        assert any("Low ground connected to the sea: 1 region(s)" in line for line in log_lines)

    def test_a_raised_coast_stands_on_no_cliff(self, tmp_dir, monkeypatch, log_lines):
        src, dem = self._tile(tmp_dir)
        self._fake_geoid(monkeypatch, egm96=-2.5)   # EGM96 raises the land by 2.5 m
        out = self._convert(src, os.path.join(tmp_dir, "out.tif"))
        land = dem > 0
        strip = land & (dem <= 2.5)
        np.testing.assert_allclose(out[strip], dem[strip], atol=0.0051)    # kept, to the centimeter of the COG
        assert out[0, 5] == np.float32(0.2), "the first land post stays at its height above the sea"
        above = dem >= 10.0
        np.testing.assert_allclose(out[above], dem[above] + 2.5, atol=0.0051)
        band = land & (dem > 2.5) & (dem < 10.0)
        np.testing.assert_allclose(out[band], dem[band] + 2.5 * (dem[band] - 2.5) / 7.5, atol=0.0051)
        assert np.all(np.diff(out[0, 5:]) > 0)
        assert any("would have raised the shore by up to 2.50 m" in line for line in log_lines)

    def test_the_two_directions_are_inverses(self, tmp_dir, monkeypatch):
        src, dem = self._tile(tmp_dir)
        self._fake_geoid(monkeypatch, egm96=2.5)
        down = os.path.join(tmp_dir, "egm96.tif")
        self._convert(src, down)
        back = self._convert(down, os.path.join(tmp_dir, "egm2008.tif"), "EGM96", "EGM2008")
        land = (dem > 0) & (dem != np.float32(0.5))   # the lagoon is leveled, one way
        np.testing.assert_allclose(back[land], dem[land], atol=0.011)     # two roundings to the centimeter

    def test_off_the_plain_shift_sinks_the_coast(self, tmp_dir, monkeypatch, log_lines):
        src, dem = self._tile(tmp_dir)
        self._fake_geoid(monkeypatch, egm96=2.5)
        out = self._convert(src, os.path.join(tmp_dir, "out.tif"), coast_band=0)
        assert out[0, 5] == pytest.approx(0.2 - 2.5, abs=0.0051)
        assert not any("Coastal containment" in line for line in log_lines)

    def test_dted_writes_one_meter_at_coastal_land_that_rounds_to_zero(self, tmp_dir, monkeypatch, log_lines):
        from egmtrans import transform
        from egmtrans.dted.header import CellGeometry
        from tests.test_determinism import metadata

        posts = 301
        i, j = np.mgrid[0:posts, 0:posts]
        # Shallow land from 0.2 m, rising a millimeter per post with no two neighbors alike.
        dem = (0.2 + 0.001 * j + 0.01 * ((7 * i + 13 * j) % 23)).astype(np.float32)
        dem[:, :60] = 0.0
        src = write_geotiff(os.path.join(tmp_dir, "cell.tif"), dem, point_geotransform(126, 6, posts))
        geoid = {"EGM96": 2.5, "EGM2008": 0.0}
        monkeypatch.setattr(
            transform, "geoid_on_cell", lambda datum, cell: np.full((cell.lat_points, cell.lon_lines), geoid[datum])
        )
        for band, expected in ((10.0, 1), (0.0, -2)):
            log_lines.clear()
            out = os.path.join(tmp_dir, f"N06E126_{band:g}.dt0")
            transform_vertical_datum(
                src, out, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False,
                dted_metadata=metadata(0), cell=CellGeometry(0, 126, 6), coast_band=band,
            )
            values = read_band(out)
            assert np.all(values[:, :20] == 0) and np.all(values[:, 30:] == expected), band
            assert any(line.startswith("Wrote 1 m at ") for line in log_lines) == (band > 0)
