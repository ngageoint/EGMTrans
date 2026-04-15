"""Tests for egmtrans.transform — basic structural tests."""


from egmtrans.transform import (
    create_datum_array,
    create_gdal_warp_array,
    create_interp_array,
    transform_vertical_datum,
)


def test_transform_functions_exist():
    """Verify all expected transform functions are importable."""
    assert callable(create_gdal_warp_array)
    assert callable(create_interp_array)
    assert callable(create_datum_array)
    assert callable(transform_vertical_datum)
