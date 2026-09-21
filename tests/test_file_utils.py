"""Tests for egmtrans.file_utils."""

import os
import stat

import pytest

from egmtrans.file_utils import copy_folder_structure, is_valid_dem, is_valid_filename


class TestIsValidFilename:
    def test_normal_filename(self):
        assert is_valid_filename("output.tif") is True

    def test_empty_string(self):
        assert is_valid_filename("") is False

    def test_whitespace_only(self):
        assert is_valid_filename("   ") is False

    def test_invalid_characters(self):
        assert is_valid_filename("file<name>.tif") is False
        assert is_valid_filename('file"name".tif') is False
        assert is_valid_filename("file|name.tif") is False

    def test_too_long(self):
        assert is_valid_filename("a" * 256) is False
        assert is_valid_filename("a" * 255) is True

    def test_filename_with_spaces(self):
        assert is_valid_filename("my file.tif") is True


class TestIsValidDem:
    def test_valid_single_band(self, synthetic_geotiff):
        assert is_valid_dem(synthetic_geotiff) is True

    def test_multiband_rejected(self, multiband_tiff):
        assert is_valid_dem(multiband_tiff) is False

    def test_mask_filename_rejected(self, tmp_dir):
        # Even if the file doesn't exist, the name check happens first
        path = os.path.join(tmp_dir, "some_mask_file.dt1")
        assert is_valid_dem(path) is False

    def test_ortho_filename_rejected(self, tmp_dir):
        path = os.path.join(tmp_dir, "ortho_image.tif")
        assert is_valid_dem(path) is False

    def test_invalid_filenames_case_insensitive(self, tmp_dir):
        path = os.path.join(tmp_dir, "HEM_data.tif")
        assert is_valid_dem(path) is False


def _make_raster(tmp_dir, name, gdal_type):
    """Write a real single-band GeoTIFF, so the name check is what decides."""
    from osgeo import gdal, osr

    path = os.path.join(tmp_dir, name)
    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 4, 1, gdal_type)
    ds.SetGeoTransform((47.0, 0.25, 0.0, 41.0, 0.0, -0.25))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds = None
    return path


class TestAuxiliaryLayers:
    """The keyword list is upper case but the filename was lower-cased before the
    comparison, so HEM/WBM/EDM layers were never skipped and a Byte mask aborted
    the whole batch. The old test passed only because its file did not exist.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "N40E047_01_HEM.tif",
            "N40E047_01_EDM.tif",
            "n40e047_01_sdm.tif",
            "Copernicus_DSM_10_N06_00_E126_00_WBM.tif",
            "HEM.tif",
        ],
    )
    def test_float_layer_skipped_by_name(self, tmp_dir, name):
        from osgeo import gdal

        assert is_valid_dem(_make_raster(tmp_dir, name, gdal.GDT_Float32)) is False

    @pytest.mark.parametrize(
        "name",
        ["N40E047_01_DEM.tif", "Edmonton_DEM.tif", "Hampton_DEM.tif", "chemnitz.tif"],
    )
    def test_codes_inside_words_are_not_layers(self, tmp_dir, name):
        from osgeo import gdal

        assert is_valid_dem(_make_raster(tmp_dir, name, gdal.GDT_Float32)) is True

    def test_byte_band_skipped_whatever_its_name(self, tmp_dir):
        from osgeo import gdal

        assert is_valid_dem(_make_raster(tmp_dir, "water.tif", gdal.GDT_Byte)) is False

    def test_uint16_amplitude_skipped(self, tmp_dir):
        from osgeo import gdal

        assert is_valid_dem(_make_raster(tmp_dir, "scene_amplitude.tif", gdal.GDT_UInt16)) is False


class TestCopyFolderStructure:
    def test_copies_files_and_dirs(self, tmp_dir):
        src = os.path.join(tmp_dir, "src_folder")
        dst = os.path.join(tmp_dir, "dst_folder")
        os.makedirs(os.path.join(src, "subdir"))
        with open(os.path.join(src, "file.txt"), "w") as f:
            f.write("hello")
        with open(os.path.join(src, "subdir", "nested.txt"), "w") as f:
            f.write("world")

        copy_folder_structure(src, dst)

        assert os.path.isfile(os.path.join(dst, "file.txt"))
        assert os.path.isfile(os.path.join(dst, "subdir", "nested.txt"))
        with open(os.path.join(dst, "file.txt")) as f:
            assert f.read() == "hello"


class TestCopyFolderStructurePermissions:
    """copy2 preserves mode bits, so a read-only source produced a read-only
    copy that the transform then could not overwrite."""

    @pytest.mark.skipif(
        getattr(os, "geteuid", lambda: 1)() == 0,
        reason="root bypasses the permission bits this test relies on",
    )
    def test_copies_of_read_only_files_are_writable(self, tmp_dir):
        src_dir = os.path.join(tmp_dir, "in")
        os.makedirs(src_dir)
        src = os.path.join(src_dir, "locked.dt2")
        with open(src, "wb") as f:
            f.write(b"data")
        os.chmod(src, 0o444)

        out_dir = os.path.join(tmp_dir, "out")
        copy_folder_structure(src_dir, out_dir)

        copied = os.path.join(out_dir, "locked.dt2")
        assert os.path.isfile(copied)
        assert stat.S_IMODE(os.stat(copied).st_mode) & stat.S_IWUSR
