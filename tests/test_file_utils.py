"""Tests for egmtrans.file_utils."""

import os

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
