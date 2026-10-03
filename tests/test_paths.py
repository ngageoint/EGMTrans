"""Tests for output-path resolution — egmtrans.file_utils.

These are the regression tests for the bug where the CLI created a *directory*
at the output file's path.  ``is_valid_filename`` accepts any legal basename, so
the old ``output_is_folder = isdir(out) or is_valid_filename(basename(out))`` was
true for every output, and the log path was derived as
``out.dt2/out.dt2_transform.log`` — which ``setup_logger`` then created.  GDAL
later opened that directory in update mode and reported, on Windows,
``<path>.dt2: Permission denied``.

Everything here is pure path arithmetic: no GDAL, no geoid grids, no Windows.
"""

from __future__ import annotations

import os
import stat

import pytest

from egmtrans.file_utils import (
    IOPaths,
    copy_as_writable,
    derive_log_path,
    ensure_writable,
    prepare_output_target,
    resolve_io_paths,
)

skip_if_root = pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="root bypasses the permission bits this test relies on",
)


class TestDeriveLogPath:
    def test_file_output_logs_beside_the_file(self):
        # The bug: this used to return '/x/out.dt2/out.dt2_transform.log'.
        assert derive_log_path(os.path.join("/x", "out.dt2"), "file") == os.path.join(
            "/x", "out_transform.log"
        )

    def test_geotiff_file_output(self):
        assert derive_log_path(os.path.join("/x", "dem.tif"), "file") == os.path.join(
            "/x", "dem_transform.log"
        )

    def test_folder_output_logs_inside_the_folder(self):
        assert derive_log_path(os.path.join("/x", "outdir"), "folder") == os.path.join(
            "/x", "outdir", "outdir_transform.log"
        )

    def test_folder_output_tolerates_a_trailing_separator(self):
        assert derive_log_path(os.path.join("/x", "outdir") + os.sep, "folder") == os.path.join(
            "/x", "outdir", "outdir_transform.log"
        )

    def test_never_creates_anything(self, tmp_dir):
        target = os.path.join(tmp_dir, "out.dt2")
        derive_log_path(target, "file")
        derive_log_path(os.path.join(tmp_dir, "outdir"), "folder")
        assert os.listdir(tmp_dir) == []


class TestResolveIoPaths:
    def _make_input(self, tmp_dir, name="in.dt2"):
        path = os.path.join(tmp_dir, name)
        with open(path, "wb"):
            pass
        return path

    def test_file_to_raster_filename_stays_a_file(self, tmp_dir):
        src = self._make_input(tmp_dir)
        out = os.path.join(tmp_dir, "out.dt2")
        paths = resolve_io_paths(src, out)
        assert paths.mode == "file"
        assert paths.output_path == out
        assert paths.log_path == os.path.join(tmp_dir, "out_transform.log")

    @pytest.mark.parametrize("name", ["out.DT2", "out.Tif", "out.tiff", "out.dt0", "out.dt1"])
    def test_extension_match_is_case_insensitive(self, tmp_dir, name):
        src = self._make_input(tmp_dir)
        paths = resolve_io_paths(src, os.path.join(tmp_dir, name))
        assert paths.mode == "file"

    def test_file_to_existing_folder_joins_the_input_basename(self, tmp_dir):
        src = self._make_input(tmp_dir)
        outdir = os.path.join(tmp_dir, "results")
        os.makedirs(outdir)
        paths = resolve_io_paths(src, outdir)
        assert paths.mode == "file"
        assert paths.output_path == os.path.join(outdir, "in.dt2")

    def test_file_to_nonexistent_extensionless_folder(self, tmp_dir):
        """The ArcGIS-parity case: the folder does not exist yet."""
        src = self._make_input(tmp_dir)
        paths = resolve_io_paths(src, os.path.join(tmp_dir, "results"))
        assert paths.mode == "file"
        assert paths.output_path == os.path.join(tmp_dir, "results", "in.dt2")

    def test_folder_to_folder(self, tmp_dir):
        indir = os.path.join(tmp_dir, "in")
        os.makedirs(indir)
        outdir = os.path.join(tmp_dir, "out")
        paths = resolve_io_paths(indir, outdir)
        assert paths.mode == "folder"
        assert paths.output_path == outdir
        assert paths.log_path == os.path.join(outdir, "out_transform.log")

    def test_folder_input_to_file_output_is_rejected(self, tmp_dir):
        indir = os.path.join(tmp_dir, "in")
        os.makedirs(indir)
        with pytest.raises(ValueError, match="single file"):
            resolve_io_paths(indir, os.path.join(tmp_dir, "out.tif"))

    def test_missing_input_is_rejected(self, tmp_dir):
        with pytest.raises(ValueError, match="does not exist"):
            resolve_io_paths(os.path.join(tmp_dir, "nope.dt2"), os.path.join(tmp_dir, "out.dt2"))

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_empty_paths_are_rejected(self, tmp_dir, bad):
        src = self._make_input(tmp_dir)
        with pytest.raises(ValueError):
            resolve_io_paths(src, bad)
        with pytest.raises(ValueError):
            resolve_io_paths(bad, os.path.join(tmp_dir, "out.dt2"))


class TestPrepareOutputTarget:
    def test_creates_a_missing_parent_directory(self, tmp_dir):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        paths = resolve_io_paths(src, os.path.join(tmp_dir, "deep", "nested", "out.dt2"))
        prepare_output_target(paths)
        assert os.path.isdir(os.path.join(tmp_dir, "deep", "nested"))
        assert not os.path.exists(paths.output_path)

    def test_rejects_a_file_output_that_is_already_a_directory(self, tmp_dir):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        collision = os.path.join(tmp_dir, "out.dt2")
        os.makedirs(collision)
        # resolve_io_paths treats an existing directory as a folder, so build the
        # file-mode case directly — this is the state the old bug left on disk.
        paths = IOPaths(src, collision, "file", derive_log_path(collision, "file"))
        with pytest.raises(IsADirectoryError):
            prepare_output_target(paths)

    @skip_if_root
    def test_rejects_a_read_only_existing_output(self, tmp_dir):
        src = os.path.join(tmp_dir, "in.dt2")
        out = os.path.join(tmp_dir, "out.dt2")
        for path in (src, out):
            with open(path, "wb"):
                pass
        os.chmod(out, 0o444)
        try:
            with pytest.raises(PermissionError):
                prepare_output_target(resolve_io_paths(src, out))
        finally:
            os.chmod(out, 0o644)


class TestEnsureWritable:
    def test_clears_the_read_only_bit(self, tmp_dir):
        path = os.path.join(tmp_dir, "ro.dt2")
        with open(path, "wb"):
            pass
        os.chmod(path, 0o444)
        ensure_writable(path)
        assert stat.S_IMODE(os.stat(path).st_mode) & stat.S_IWUSR

    def test_missing_path_is_a_no_op(self, tmp_dir):
        ensure_writable(os.path.join(tmp_dir, "absent.dt2"))


class TestCopyAsWritable:
    def test_destination_is_writable_even_from_a_read_only_source(self, tmp_dir):
        src = os.path.join(tmp_dir, "src.dt2")
        with open(src, "wb") as f:
            f.write(b"payload")
        os.chmod(src, 0o444)
        dst = os.path.join(tmp_dir, "dst.dt2")

        copy_as_writable(src, dst)

        assert open(dst, "rb").read() == b"payload"
        assert stat.S_IMODE(os.stat(dst).st_mode) & stat.S_IWUSR, (
            "shutil.copy would have carried the source's read-only bit across"
        )

    def test_overwrites_a_read_only_destination(self, tmp_dir):
        src = os.path.join(tmp_dir, "src.dt2")
        dst = os.path.join(tmp_dir, "dst.dt2")
        with open(src, "wb") as f:
            f.write(b"new")
        with open(dst, "wb") as f:
            f.write(b"old")
        os.chmod(dst, 0o444)
        copy_as_writable(src, dst)
        assert open(dst, "rb").read() == b"new"

    def test_refuses_a_directory_destination(self, tmp_dir):
        src = os.path.join(tmp_dir, "src.dt2")
        with open(src, "wb"):
            pass
        dst = os.path.join(tmp_dir, "dst.dt2")
        os.makedirs(dst)
        # shutil.copy would silently write into the directory instead.
        with pytest.raises(IsADirectoryError):
            copy_as_writable(src, dst)


class TestDtedLevelPaths:
    """resolve_io_paths with a DTED level, and the naming of converted cells."""

    def test_geotiff_file_to_folder_takes_the_level_extension(self, tmp_path):
        src = tmp_path / 'TDF_N50W001_01_DEM.tif'
        src.write_bytes(b'')
        paths = resolve_io_paths(str(src), str(tmp_path / 'out'), dted_level=2)
        assert paths.mode == 'file' and paths.output_path.endswith('TDF_N50W001_01_DEM.dt2')
        assert paths.output_folder == str(tmp_path / 'out') and paths.dted_level == 2
        dted = tmp_path / 'n50w001.dt1'
        dted.write_bytes(b'')
        paths = resolve_io_paths(str(dted), str(tmp_path / 'out'), dted_level=2)
        assert paths.output_path.endswith('n50w001.dt1'), 'a DTED input keeps its name and level'

    def test_a_level_against_an_output_file_of_another_kind_is_an_error(self, tmp_path):
        src = tmp_path / 'tile.tif'
        src.write_bytes(b'')
        with pytest.raises(ValueError, match='not a DTED file'):
            resolve_io_paths(str(src), str(tmp_path / 'out.tif'), dted_level=2)
        with pytest.raises(ValueError, match='not DTED level 2'):
            resolve_io_paths(str(src), str(tmp_path / 'out.dt1'), dted_level=2)
        with pytest.raises(ValueError, match='must be 0, 1 or 2'):
            resolve_io_paths(str(src), str(tmp_path / 'out.dt2'), dted_level=3)
        paths = resolve_io_paths(str(src), str(tmp_path / 'out.dt2'), dted_level=2)
        assert paths.mode == 'file' and paths.output_folder == str(tmp_path)
        folder = resolve_io_paths(str(tmp_path), str(tmp_path / 'out'), dted_level=1)
        assert folder.mode == 'folder' and folder.output_folder == str(tmp_path / 'out')

    def test_naming_presets_and_templates(self):
        from egmtrans.file_utils import dted_naming_template, dted_output_name

        assert dted_naming_template('stem') == '{dir}/{stem}'
        assert dted_naming_template('{cell}_v2') == '{cell}_v2'
        with pytest.raises(ValueError, match='uses {lvl}'):
            dted_naming_template('{lvl}')
        with pytest.raises(ValueError, match='not a valid template'):
            dted_naming_template('{cell')
        root = os.path.join('data', 'tdf')
        tile = os.path.join(root, 'band1', 'TDF_N49E006_03_DEM.tif')
        assert dted_output_name('stem', tile, root, 'N49E006', 2) == 'band1/TDF_N49E006_03_DEM.dt2'
        assert dted_output_name('cell', tile, root, 'N49E006', 1) == 'N49E006.dt1'
        assert dted_output_name('dted', tile, root, 'N49E006', 0) == 'E006/N49.dt0'
        assert dted_output_name('TDF-DTED{level}_{lon}{lat}', tile, root, 'S06E030', 2) == 'TDF-DTED2_E030S06.dt2'
        assert dted_output_name('stem', os.path.join(root, 'a.tif'), root, 'N49E006', 2) == 'a.dt2'
        with pytest.raises(ValueError, match='leaves the output folder'):
            dted_output_name('../{cell}', tile, root, 'N49E006', 2)
        with pytest.raises(ValueError, match='empty name'):
            dted_output_name('{dir}', os.path.join(root, 'a.tif'), root, 'N49E006', 2)
