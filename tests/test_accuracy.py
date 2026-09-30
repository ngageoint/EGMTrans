"""Numerical regression tests for the vertical datum transform.

These tests exercise the real geoid grids in ``datums/`` and pin the tool's
output at a set of global control points. If the grid files are not present
locally the tests skip cleanly so that the rest of the suite still runs.

What this suite verifies for external reviewers:

1. The interpolation pipeline in ``create_datum_array`` returns values
   that are finite and within a plausible global bound for EGM96 and
   EGM2008 geoid undulations.
2. The tool's output at six global control points matches values
   captured from the reference implementation (see PINNED_VALUES
   below). Any change to the interpolation or grid-handling code that
   shifts a value by more than ``PINNED_TOL_M`` will fail the test.
3. At high-gradient locations (New Guinea geoid high, Greenland, the
   Himalayas) the EGM2008 - EGM96 delta has the expected magnitude and
   sign, confirming that both grids are loaded and used consistently.
4. A full end-to-end ``transform_vertical_datum`` round-trip
   (EGM96 -> EGM2008 -> EGM96) preserves elevation values to within
   ``ROUND_TRIP_TOL_M``.

The pinned values are the output of ``create_datum_array`` with the
default bilinear algorithm. Every control point sits exactly on a
1-arc-minute geoid grid node, so each pinned value is also the raw grid
value at that coordinate — ``test_exact_node_matches_grid`` asserts that
independently, which is what makes these pins verifiable rather than
merely historical. Re-captured 2026-08-31 after fixing a half-pixel
registration offset (query points were built at cell corners while the
geoid source points were at cell centers); the previous pins were off by
0.3 cm to 7.0 cm, largest at high-gradient locations.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from egmtrans.config import BASE_PATH, DATUM_MAPPING
from egmtrans.transform import create_datum_array, transform_vertical_datum

# --- Declared accuracy envelope ---------------------------------------------

# Tolerance for pinned-value regression checks. 1 mm is ~1e-5 relative to
# the grid's 1-arc-minute post spacing — this is a strict regression check
# intended to catch unintentional changes in the interpolation pipeline.
PINNED_TOL_M = 0.001

# Round-trip EGM96 -> EGM2008 -> EGM96 tolerance. The tool rounds outputs
# to 1 cm (see README "Notes"), and interpolation noise accumulates over
# two passes, so 2 cm is the appropriate envelope.
ROUND_TRIP_TOL_M = 0.02

# Plausible global bound on any single EGM2008 or EGM96 geoid undulation.
# Real extrema are roughly -107 m (Indian Ocean Geoid Low) and +85 m
# (New Guinea). We widen this slightly for safety.
PLAUSIBLE_UNDULATION_BOUND_M = 120.0

# --- Pinned values from the reference implementation -----------------------

# (lat, lon, label) -> {'EGM96': undulation_m, 'EGM2008': undulation_m}
#
# Captured with: create_datum_array(tiny_tif, <datum>, 'bilinear', ...)
# where tiny_tif is a 3x3 GeoTIFF at 0.01 deg pixel size centered on
# (lat, lon). See _make_tiny_geotiff below.
PINNED_VALUES: dict[tuple[float, float, str], dict[str, float]] = {
    (0.0, 10.0, "equatorial Atlantic, near Gulf of Guinea"): {
        "EGM96": 9.0000,
        "EGM2008": 9.4000,
    },
    (38.7, -77.0, "mid-latitude continental, Washington DC"): {
        "EGM96": -33.5780,
        "EGM2008": -33.3990,
    },
    (-33.9, 18.4, "southern mid-latitude, Cape Town"): {
        "EGM96": 31.0520,
        "EGM2008": 31.1180,
    },
    (27.9, 86.9, "Himalayan high terrain, near Mt Everest"): {
        "EGM96": -29.5390,
        "EGM2008": -28.8660,
    },
    (-6.0, 147.0, "tropical Pacific, near New Guinea geoid high"): {
        "EGM96": 71.1210,
        "EGM2008": 73.0220,
    },
    (71.0, -42.0, "high-northern Arctic, central Greenland"): {
        "EGM96": 41.8630,
        "EGM2008": 41.3730,
    },
}

CONTROL_POINTS = list(PINNED_VALUES.keys())

# The level of a 150 m lake off Mindanao (7.6-7.7 N, 126.5-126.6 E) transformed
# from EGM2008 to EGM96 with flattening: 150 m minus the largest EGM96 - EGM2008
# difference over the lake, since a flat patch takes the lowest of its
# transformed values (the mean would give 155.035 m, half the 0.95 m range of
# the correction higher). Captured 2026-09-30 with the bilinear algorithm.
PINNED_LAKE_LEVEL_M = 154.5454


# --- Helpers ---------------------------------------------------------------


def _grid_path(datum: str) -> str:
    return os.path.join(BASE_PATH, "datums", DATUM_MAPPING[datum]["grid"])


def _grids_available() -> bool:
    return all(
        os.path.isfile(_grid_path(datum)) for datum in ("EGM96", "EGM2008")
    )


requires_grids = pytest.mark.skipif(
    not _grids_available(),
    reason="Geoid grid files not present; run 'python download_grids.py' to fetch.",
)


def _make_tiny_geotiff(tmp_dir: str, lat: float, lon: float) -> str:
    """Create a 3x3 synthetic GeoTIFF centered on (lat, lon) at 0.01 deg spacing."""
    from osgeo import gdal, osr

    path = os.path.join(tmp_dir, f"pt_{lat:+06.2f}_{lon:+07.2f}.tif")
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(path, 3, 3, 1, gdal.GDT_Float32)

    pixel = 0.01
    origin_lon = lon - 1.5 * pixel
    origin_lat = lat + 1.5 * pixel
    ds.SetGeoTransform((origin_lon, pixel, 0.0, origin_lat, 0.0, -pixel))

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())

    band = ds.GetRasterBand(1)
    band.WriteArray(np.full((3, 3), 100.0, dtype=np.float32))
    band.SetNoDataValue(-9999.0)
    band.FlushCache()
    ds = None
    return path


# --- Tests -----------------------------------------------------------------


@requires_grids
@pytest.mark.parametrize("lat,lon,label", CONTROL_POINTS)
@pytest.mark.parametrize("datum", ["EGM96", "EGM2008"])
def test_undulation_finite_and_bounded(tmp_dir, lat, lon, label, datum):
    """``create_datum_array`` returns a finite value within the plausible
    global bound at every control point."""
    tiny_tif = _make_tiny_geotiff(tmp_dir, lat, lon)
    undulation = create_datum_array(
        tiny_tif, datum, algorithm="bilinear", temp_dir=tmp_dir, output_dir=tmp_dir
    )
    value = float(undulation[1, 1])

    assert np.isfinite(value), f"{datum} undulation not finite at {label}"
    assert abs(value) < PLAUSIBLE_UNDULATION_BOUND_M, (
        f"{datum} undulation {value:.3f} m at {label} exceeds plausible bound"
    )


@requires_grids
@pytest.mark.parametrize("lat,lon,label", CONTROL_POINTS)
@pytest.mark.parametrize("datum", ["EGM96", "EGM2008"])
def test_pinned_values(tmp_dir, lat, lon, label, datum):
    """``create_datum_array`` output matches the pinned reference value
    within ``PINNED_TOL_M``. See the module docstring; the pins are cross-checked
    against the raw grid by ``test_exact_node_matches_grid``."""
    tiny_tif = _make_tiny_geotiff(tmp_dir, lat, lon)
    undulation = create_datum_array(
        tiny_tif, datum, algorithm="bilinear", temp_dir=tmp_dir, output_dir=tmp_dir
    )
    actual = float(undulation[1, 1])
    expected = PINNED_VALUES[(lat, lon, label)][datum]

    assert abs(actual - expected) < PINNED_TOL_M, (
        f"{datum} at {label}: expected {expected:.4f} m (pinned), "
        f"got {actual:.4f} m, drift {actual - expected:+.4f} m"
    )


@requires_grids
@pytest.mark.parametrize("lat,lon,label", CONTROL_POINTS)
@pytest.mark.parametrize("datum", ["EGM96", "EGM2008"])
def test_exact_node_matches_grid(tmp_dir, lat, lon, label, datum):
    """Interpolating at an exact grid node returns that node's value.

    Every control point lies on a whole arc-minute in both axes, so it
    coincides with a post of the 1-arc-minute geoid grid, and bilinear
    interpolation there must reproduce the stored value exactly. This pins the
    *registration* of the resampling rather than a historical output: a
    half-pixel offset between the geoid source points (cell centers) and the
    DEM query points shows up here immediately, whereas a pinned value only
    records whatever the code did on the day it was captured.
    """
    from osgeo import gdal

    with gdal.Open(_grid_path(datum)) as grid_ds:
        gt = grid_ds.GetGeoTransform()
        band = grid_ds.GetRasterBand(1)
        scale = band.GetScale() or 1
        col = (lon - gt[0]) / gt[1] - 0.5
        row = (lat - gt[3]) / gt[5] - 0.5
        assert abs(col - round(col)) < 1e-6 and abs(row - round(row)) < 1e-6, (
            f"{label} is not on a grid node; this test assumes whole-arc-minute control points"
        )
        expected = float(band.ReadAsArray(int(round(col)), int(round(row)), 1, 1)[0][0]) * scale

    tiny_tif = _make_tiny_geotiff(tmp_dir, lat, lon)
    undulation = create_datum_array(
        tiny_tif, datum, algorithm="bilinear", temp_dir=tmp_dir, output_dir=tmp_dir
    )
    actual = float(undulation[1, 1])

    assert abs(actual - expected) < PINNED_TOL_M, (
        f"{datum} at {label}: the query point coincides with a grid node holding "
        f"{expected:.4f} m, but interpolation returned {actual:.4f} m "
        f"(offset {actual - expected:+.4f} m) — the source and query grids are "
        f"misregistered."
    )


@requires_grids
def test_high_gradient_deltas_have_expected_sign(tmp_dir):
    """At three high-gradient locations the (EGM2008 - EGM96) delta is
    non-trivial and has the physically expected sign. This confirms that
    both grids are actually being used (a stale or duplicate grid would
    produce ~0 delta everywhere)."""
    samples = [
        # (lat, lon, min_delta, max_delta, label)
        (-6.0, 147.0, 1.0, 3.0, "New Guinea — strongly positive"),
        (71.0, -42.0, -1.0, -0.1, "central Greenland — negative"),
        (27.9, 86.9, 0.2, 1.5, "Mt Everest region — positive"),
    ]
    for lat, lon, lo, hi, label in samples:
        tiny_tif = _make_tiny_geotiff(tmp_dir, lat, lon)
        egm96 = create_datum_array(
            tiny_tif, "EGM96", "bilinear", tmp_dir, tmp_dir
        )
        egm08 = create_datum_array(
            tiny_tif, "EGM2008", "bilinear", tmp_dir, tmp_dir
        )
        delta = float(egm08[1, 1] - egm96[1, 1])
        assert lo < delta < hi, (
            f"{label}: delta {delta:+.3f} m outside expected [{lo}, {hi}]"
        )


@requires_grids
def test_pinned_lake_level_uses_patch_minimum(tmp_dir):
    """A flat patch comes out at the minimum of its transformed values.

    The written value (rounded to 1 cm) must agree with ``150 - max(delta)``
    over the lake computed independently from ``create_datum_array``, and that
    expectation must match the pinned constant, so a change to either the
    level rule or the interpolation shows up here.
    """
    from osgeo import gdal, osr

    rows = cols = 40
    i, j = np.mgrid[0:rows, 0:cols]
    dem = (200.0 + i + 0.37 * j).astype(np.float32)  # terrain above the lake: a basin
    dem[:, :10] = 0.0
    dem[20:30, 20:30] = 150.0

    src = os.path.join(tmp_dir, "lake.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, cols, rows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((126.30, 0.01, 0.0, 7.90, 0.0, -0.01))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(dem)
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    ds = None

    out = os.path.join(tmp_dir, "lake_egm96.tif")
    transform_vertical_datum(src, out, "EGM2008", "EGM96", True, False, 16, "bilinear", save_log=False)
    out_ds = gdal.Open(out)
    lake = out_ds.GetRasterBand(1).ReadAsArray()[20:30, 20:30].astype(np.float64)
    out_ds = None
    assert np.unique(lake).size == 1
    written = float(lake[0, 0])

    egm96 = create_datum_array(src, "EGM96", "bilinear", tmp_dir, tmp_dir)
    egm2008 = create_datum_array(src, "EGM2008", "bilinear", tmp_dir, tmp_dir)
    delta = (egm96 - egm2008)[20:30, 20:30].astype(np.float64)
    expected = 150.0 - delta.max()

    assert abs(expected - PINNED_LAKE_LEVEL_M) < PINNED_TOL_M, (
        f"lake level expectation {expected:.4f} m drifted from the pinned {PINNED_LAKE_LEVEL_M} m"
    )
    assert abs(written - expected) <= 0.005 + 1e-9, (
        f"written {written} m is not the cm-rounded minimum {expected:.4f} m"
    )
    assert written < 150.0 - delta.mean(), "the lake sits at its mean level, not its minimum"


@requires_grids
def test_round_trip_egm96_egm2008_egm96(tmp_dir):
    """Transforming EGM96 -> EGM2008 -> EGM96 returns the original elevations
    to within ``ROUND_TRIP_TOL_M``. Uses a 20x20 synthetic DEM centered near
    Washington DC where the EGM2008 - EGM96 delta is non-trivial."""
    from osgeo import gdal, osr

    input_path = os.path.join(tmp_dir, "rt_input.tif")
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(input_path, 20, 20, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((-77.1, 0.01, 0.0, 39.0, 0.0, -0.01))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    original = np.random.default_rng(seed=42).uniform(50.0, 500.0, (20, 20)).astype(
        np.float32
    )
    ds.GetRasterBand(1).WriteArray(original)
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    ds.FlushCache()
    ds = None

    mid_path = os.path.join(tmp_dir, "rt_egm2008.tif")
    transform_vertical_datum(
        input_file=input_path,
        output_file=mid_path,
        src_datum="EGM96",
        tgt_datum="EGM2008",
        flatten=False,
        create_mask=False,
        min_patch_size=16,
        algorithm="bilinear",
        save_log=False,
    )

    back_path = os.path.join(tmp_dir, "rt_egm96.tif")
    transform_vertical_datum(
        input_file=mid_path,
        output_file=back_path,
        src_datum="EGM2008",
        tgt_datum="EGM96",
        flatten=False,
        create_mask=False,
        min_patch_size=16,
        algorithm="bilinear",
        save_log=False,
    )

    # Hold the dataset reference until ReadAsArray completes; otherwise the
    # Python GC can free the Dataset mid-call and raise a SWIG TypeError.
    back_ds = gdal.Open(back_path)
    back = back_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    back_ds = None

    diff = np.abs(back - original.astype(np.float64))
    assert diff.max() < ROUND_TRIP_TOL_M, (
        f"round-trip max error {diff.max():.4f} m exceeds tolerance "
        f"{ROUND_TRIP_TOL_M} m"
    )
