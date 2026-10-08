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


class TestHeaderCheck:
    """Every candidate, DTED included, is opened with GDAL and must carry a geotransform."""

    def test_geotiff_without_georeferencing_rejected(self, tmp_dir):
        from osgeo import gdal

        path = os.path.join(tmp_dir, "plain.tif")
        ds = gdal.GetDriverByName("GTiff").Create(path, 4, 4, 1, gdal.GDT_Float32)
        ds.FlushCache()
        ds = None
        assert is_valid_dem(path) is False

    def test_unreadable_dted_rejected(self, tmp_dir):
        path = os.path.join(tmp_dir, "n03e008.dt2")
        with open(path, "wb") as f:
            f.write(b"not a DTED file")
        assert is_valid_dem(path) is False

    def test_real_dted_accepted(self, tmp_dir):
        import numpy as np

        from tests.conftest import write_dted

        path = write_dted(os.path.join(tmp_dir, "n03e008.dt0"), np.full((121, 121), 40, dtype=np.int16), 8, 3)
        assert is_valid_dem(path) is True


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


class TestDemProblems:
    """What find_dems says about files it cannot open, and what the tree copy leaves out."""

    def test_dem_problem_tells_an_unreadable_file_from_a_file_that_is_not_a_dem(self, tmp_dir, synthetic_geotiff):
        from egmtrans.file_utils import dem_problem

        assert dem_problem(synthetic_geotiff) is None
        truncated = os.path.join(tmp_dir, 'truncated.tif')
        with open(synthetic_geotiff, 'rb') as source, open(truncated, 'wb') as target:
            target.write(source.read(64))
        assert dem_problem(truncated).startswith('could not be opened')
        text = os.path.join(tmp_dir, 'notes.tif')
        with open(text, 'w') as handle:
            handle.write('not a raster')
        assert dem_problem(text).startswith('could not be opened')
        assert dem_problem(os.path.join(tmp_dir, 'tile_mask.tif')) == 'is named like a mask or an image'
        assert dem_problem(os.path.join(tmp_dir, 'N40E047_01_HEM.tif')) == 'is named like an auxiliary layer'
        assert is_valid_dem(truncated) is False and is_valid_dem(text) is False

    def test_find_dems_reports_unreadable_files_and_folders(self, tmp_dir, synthetic_geotiff, log_lines):
        import shutil

        from egmtrans.file_utils import find_dems

        folder = os.path.join(tmp_dir, 'tiles')
        os.makedirs(os.path.join(folder, 'locked'))
        shutil.copy(synthetic_geotiff, os.path.join(folder, 'good.tif'))
        with open(os.path.join(folder, 'bad.tif'), 'wb') as handle:
            handle.write(b'II*\x00garbage')
        shutil.copy(synthetic_geotiff, os.path.join(folder, 'locked', 'hidden.tif'))
        skipped = []
        if os.name != 'nt' and os.geteuid() != 0:
            os.chmod(os.path.join(folder, 'locked'), 0)
        try:
            found = find_dems(folder, skipped=skipped)
        finally:
            os.chmod(os.path.join(folder, 'locked'), stat.S_IRWXU)
        assert [os.path.basename(path) for path in found] == ['good.tif']
        reasons = dict(skipped)
        assert os.path.join(folder, 'bad.tif') in reasons and reasons[os.path.join(folder, 'bad.tif')].startswith(
            'could not be opened')
        if os.name != 'nt' and os.geteuid() != 0:
            assert os.path.join(folder, 'locked') in reasons
            assert any('could not be read' in line and 'locked' in line for line in log_lines)
        assert any(line.startswith('Skipping bad.tif: it could not be opened') for line in log_lines)

    def test_copy_folder_structure_leaves_dems_sidecars_and_companions_out(self, tmp_dir, synthetic_geotiff):
        import shutil

        src = os.path.join(tmp_dir, 'in')
        os.makedirs(os.path.join(src, 'sub'))
        dem = shutil.copy(synthetic_geotiff, os.path.join(src, 'a.tif'))
        for name in ('a.tif.ovr', 'a.tif.aux.xml', 'a.rrd', 'a.aux', 'meta.xml', 'license.pdf'):
            with open(os.path.join(src, name), 'w') as handle:
                handle.write('x')
        dt0 = os.path.join(src, 'sub', 'b.dt0')
        for name in ('b.dt0', 'b.avg', 'b.min', 'b.max', 'b.xml'):
            with open(os.path.join(src, 'sub', name), 'w') as handle:
                handle.write('x')
        out = os.path.join(tmp_dir, 'out')
        companions = copy_folder_structure(src, out, skip=[dem, dt0])
        copied = sorted(os.path.relpath(os.path.join(root, f), out) for root, _, files in os.walk(out) for f in files)
        assert copied == ['license.pdf', 'meta.xml', os.path.join('sub', 'b.xml')]
        assert sorted(os.path.basename(path) for path in companions) == ['b.avg', 'b.max', 'b.min']
        assert os.path.isdir(os.path.join(out, 'sub'))
        # Without a skip list everything is copied, as before.
        everything = os.path.join(tmp_dir, 'everything')
        assert copy_folder_structure(src, everything) == []
        assert os.path.isfile(os.path.join(everything, 'a.tif.ovr'))
