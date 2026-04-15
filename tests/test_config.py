"""Tests for egmtrans.config."""

import os

from egmtrans.config import (
    BASE_PATH,
    DATUM_MAPPING,
    DTED_EXTENSIONS,
    INVALID_CHARACTERS,
    INVALID_FILENAMES,
    SUPPORTED_EXTENSIONS,
    get_crs_dir,
    get_datums_dir,
)


def test_supported_extensions_are_lowercase():
    for ext in SUPPORTED_EXTENSIONS:
        assert ext.startswith('.'), f"{ext} should start with '.'"
        assert ext == ext.lower(), f"{ext} should be lowercase"


def test_dted_extensions_subset_of_supported():
    for ext in DTED_EXTENSIONS:
        assert ext in SUPPORTED_EXTENSIONS


def test_datum_mapping_has_required_keys():
    for _name, details in DATUM_MAPPING.items():
        assert 'epsg' in details
        assert 'grid' in details
        assert 'dted_code' in details


def test_wgs84_has_no_grid():
    assert DATUM_MAPPING['WGS84']['grid'] is None


def test_egm96_grid_filename():
    assert DATUM_MAPPING['EGM96']['grid'] == 'us_nga_egm96_1.tif'


def test_egm2008_grid_filename():
    assert DATUM_MAPPING['EGM2008']['grid'] == 'us_nga_egm08_1.tif'


def test_base_path_is_absolute():
    assert os.path.isabs(BASE_PATH)


def test_get_datums_dir():
    assert get_datums_dir().endswith('datums')


def test_get_crs_dir():
    assert get_crs_dir().endswith('crs')


def test_invalid_characters_present():
    assert len(INVALID_CHARACTERS) > 0


def test_invalid_filenames_is_tuple():
    assert isinstance(INVALID_FILENAMES, tuple)
    assert 'ortho' in INVALID_FILENAMES
