"""Tests for egmtrans.crs."""

import pytest
from osgeo import osr

from egmtrans.crs import (
    create_compound_srs,
    get_horizontal_epsg,
    get_horizontal_name,
    get_horizontal_srs,
    get_proj4,
    standardize_srs,
)


@pytest.fixture
def wgs84_srs():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    return srs


@pytest.fixture
def utm_srs():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32617)  # UTM zone 17N
    return srs


def test_get_horizontal_srs_geographic(wgs84_srs):
    horiz = get_horizontal_srs(wgs84_srs)
    assert horiz.IsGeographic()


def test_get_horizontal_srs_projected(utm_srs):
    horiz = get_horizontal_srs(utm_srs)
    assert horiz.IsProjected()


def test_get_horizontal_epsg_geographic(wgs84_srs):
    epsg = get_horizontal_epsg(wgs84_srs)
    assert epsg == 4326


def test_get_horizontal_epsg_projected(utm_srs):
    epsg = get_horizontal_epsg(utm_srs)
    assert epsg == 32617


def test_get_horizontal_name_geographic(wgs84_srs):
    name = get_horizontal_name(wgs84_srs)
    assert "WGS" in name or "wgs" in name.lower()


def test_get_horizontal_name_projected(utm_srs):
    name = get_horizontal_name(utm_srs)
    assert name is not None


def test_get_proj4(wgs84_srs):
    proj4 = get_proj4(wgs84_srs)
    assert isinstance(proj4, str)
    assert len(proj4) > 0


def test_standardize_srs(wgs84_srs):
    wkt = wgs84_srs.ExportToWkt()
    result = standardize_srs(wkt)
    assert result.IsGeographic()


def test_create_compound_srs_wgs84(wgs84_srs):
    result = create_compound_srs(wgs84_srs, 'WGS84')
    # WGS84 + geographic -> EPSG:4979
    code = result.GetAuthorityCode(None)
    assert code == '4979'


def test_create_compound_srs_egm96(wgs84_srs):
    result = create_compound_srs(wgs84_srs, 'EGM96')
    assert result.IsCompound()


def test_create_compound_srs_egm2008(wgs84_srs):
    result = create_compound_srs(wgs84_srs, 'EGM2008')
    assert result.IsCompound()
