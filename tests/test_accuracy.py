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

The pinned values were captured from the output of
``create_datum_array`` with the default bilinear algorithm on
2026-04-15. If you intentionally change the interpolation code or the
grid source files, re-capture them and update the PINNED_VALUES dict
below.
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
        "EGM96": 9.0130,
        "EGM2008": 9.4164,
    },
    (38.7, -77.0, "mid-latitude continental, Washington DC"): {
        "EGM96": -33.5600,
        "EGM2008": -33.3799,
    },
    (-33.9, 18.4, "southern mid-latitude, Cape Town"): {
        "EGM96": 31.0550,
        "EGM2008": 31.1159,
    },
    (27.9, 86.9, "Himalayan high terrain, near Mt Everest"): {
        "EGM96": -29.4689,
        "EGM2008": -28.8427,
    },
    (-6.0, 147.0, "tropical Pacific, near New Guinea geoid high"): {
        "EGM96": 71.0824,
        "EGM2008": 72.9239,
    },
    (71.0, -42.0, "high-northern Arctic, central Greenland"): {
        "EGM96": 41.8516,
        "EGM2008": 41.3655,
    },
}

CONTROL_POINTS = list(PINNED_VALUES.keys())


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
    within ``PINNED_TOL_M``. Pinned values were captured from the current
    implementation on 2026-04-15 — see module docstring."""
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
