"""Tests for egmtrans.cli — argument handling, path dispatch, and exit codes."""

import argparse
import os
import sys

import pytest

from egmtrans import cli
from egmtrans.cli import datum_arg, delete_output_directory, process_file, str2bool


class TestStr2Bool:
    def test_true_values(self):
        for v in ('yes', 'true', 't', 'y', '1', 'YES', 'True', 'T', 'Y'):
            assert str2bool(v) is True

    def test_false_values(self):
        for v in ('no', 'false', 'f', 'n', '0', 'NO', 'False', 'F', 'N'):
            assert str2bool(v) is False

    def test_bool_passthrough(self):
        assert str2bool(True) is True
        assert str2bool(False) is False

    def test_none_returns_false(self):
        assert str2bool(None) is False

    def test_invalid_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            str2bool('maybe')

    def test_empty_string_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            str2bool('')


class TestProcessFileSignature:
    def test_callable(self):
        assert callable(process_file)


class TestDeleteOutputDirectory:
    def test_callable(self):
        assert callable(delete_output_directory)


class TestDatumArg:
    """``-s`` / ``-t`` used to be a fuzzy substring match with no validation."""

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("WGS84", "WGS84"), ("wgs 84", "WGS84"), ("WGS-84", "WGS84"),
            ("EGM96", "EGM96"), ("egm-96", "EGM96"),
            ("EGM2008", "EGM2008"), ("egm08", "EGM2008"), ("EGM_2008", "EGM2008"),
        ],
    )
    def test_accepts_the_usual_spellings(self, given, expected):
        assert datum_arg(given) == expected

    @pytest.mark.parametrize("given", ["EGM", "EGM6", "96", "8", "", "NAVD88"])
    def test_rejects_ambiguous_or_unknown(self, given):
        # 'EGM' matched both EGM96 and EGM2008 under the old substring test and
        # silently resolved to whichever key came last.
        with pytest.raises(argparse.ArgumentTypeError):
            datum_arg(given)


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Run main() without touching GDAL or the geoid grids."""
    calls = []

    def fake_process_file(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(cli, "ensure_grids", lambda **kw: [])
    monkeypatch.setattr(cli, "process_file", fake_process_file)
    return calls


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["egmtrans", *argv])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    code = excinfo.value.code
    return 0 if code is None else code


class TestMainOutputPaths:
    """Regression tests for the output path becoming a directory.

    Before the fix, ``setup_logger`` derived the log path as
    ``out.dt2/out.dt2_transform.log`` and created that directory, so the
    transform then tried to open a directory as a DTED file.
    """

    def test_dted_output_does_not_become_a_directory(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        out = os.path.join(tmp_dir, "out.dt2")

        assert _run(monkeypatch, "-i", src, "-o", out, "-s", "EGM96", "-t", "EGM2008") == 0

        assert not os.path.isdir(out), "the output path was turned into a directory"
        assert stub_pipeline[0][1] == out, "not dispatched as a single file"
        assert os.path.isfile(os.path.join(tmp_dir, "out_transform.log"))

    def test_geotiff_output_does_not_become_a_directory(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "in.tif")
        with open(src, "wb"):
            pass
        out = os.path.join(tmp_dir, "out.tif")

        assert _run(monkeypatch, "-i", src, "-o", out, "-s", "EGM2008", "-t", "EGM96") == 0

        assert not os.path.isdir(out)
        assert os.path.isfile(os.path.join(tmp_dir, "out_transform.log"))

    def test_folder_output_gets_the_input_basename(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "n39w077.dt2")
        with open(src, "wb"):
            pass
        outdir = os.path.join(tmp_dir, "results")

        assert _run(monkeypatch, "-i", src, "-o", outdir, "-s", "EGM96", "-t", "EGM2008") == 0

        assert stub_pipeline[0][1] == os.path.join(outdir, "n39w077.dt2")
        assert os.path.isdir(outdir)

    def test_folder_input_to_file_output_is_a_usage_error(self, tmp_dir, stub_pipeline, monkeypatch):
        indir = os.path.join(tmp_dir, "in")
        os.makedirs(indir)
        out = os.path.join(tmp_dir, "out.tif")

        assert _run(monkeypatch, "-i", indir, "-o", out, "-s", "EGM96", "-t", "EGM2008") == 2
        assert not os.path.exists(out), "a directory was created at the output file path"
        assert stub_pipeline == []


class TestBatchSkipsAuxiliaryLayers:
    def test_delivery_folder_with_auxiliary_layers(self, tmp_dir, stub_pipeline, monkeypatch):
        """A TanDEM-X style delivery folder holds mask and amplitude layers beside the DEM.

        A Byte WBM used to reach the transform, fail as an unsupported data type,
        and abort the whole batch.
        """
        from osgeo import gdal, osr

        indir = os.path.join(tmp_dir, "N40E047_01")
        os.makedirs(indir)
        layers = {
            "DEM": gdal.GDT_Float32,
            "HEM": gdal.GDT_Float32,
            "WBM": gdal.GDT_Byte,
            "EDM": gdal.GDT_Byte,
            "AMP": gdal.GDT_UInt16,
        }
        for code, gdal_type in layers.items():
            ds = gdal.GetDriverByName("GTiff").Create(
                os.path.join(indir, f"N40E047_01_{code}.tif"), 4, 4, 1, gdal_type
            )
            ds.SetGeoTransform((47.0, 0.25, 0.0, 41.0, 0.0, -0.25))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            ds.SetProjection(srs.ExportToWkt())
            ds = None
        outdir = os.path.join(tmp_dir, "out")

        assert _run(monkeypatch, "-i", indir, "-o", outdir, "-s", "EGM2008", "-t", "EGM96") == 0
        dispatched = [os.path.basename(call[0]) for call in stub_pipeline]
        assert dispatched == ["N40E047_01_DEM.tif"]


class TestMainExitCodes:
    """main() used to report success no matter what happened."""

    def test_success_is_zero(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        assert _run(
            monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.dt2"),
            "-s", "EGM96", "-t", "EGM2008",
        ) == 0

    def test_transform_failure_is_one(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        monkeypatch.setattr(cli, "process_file", lambda *a, **k: False)
        assert _run(
            monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.dt2"),
            "-s", "EGM96", "-t", "EGM2008",
        ) == 1

    def test_missing_input_is_a_usage_error(self, tmp_dir, stub_pipeline, monkeypatch):
        assert _run(
            monkeypatch, "-i", os.path.join(tmp_dir, "nope.dt2"),
            "-o", os.path.join(tmp_dir, "out.dt2"), "-s", "EGM96", "-t", "EGM2008",
        ) == 2

    def test_bad_datum_is_a_usage_error(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        assert _run(
            monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.dt2"),
            "-s", "EGM6", "-t", "EGM2008",
        ) == 2


class TestConfirm:
    def test_assume_yes_never_reads_stdin(self, monkeypatch):
        def no_input(*_):
            raise AssertionError("input() was called")

        monkeypatch.setattr("builtins.input", no_input)
        assert cli.confirm("Proceed?", assume_yes=True) is True

    def test_piped_answer_still_works(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_: "no")
        assert cli.confirm("Proceed?") is False

    def test_closed_stdin_raises_instead_of_crashing(self, monkeypatch):
        def eof(*_):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        with pytest.raises(cli.NonInteractiveError):
            cli.confirm("Proceed?")


class TestProcessFilePrompts:
    """A container has no terminal: input() used to die with EOFError."""

    @pytest.fixture
    def no_transform(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "verify_grids", lambda *a: None)
        monkeypatch.setattr(cli, "transform_vertical_datum", lambda *a, **k: calls.append(a))
        return calls

    def test_same_datum_prompt_without_terminal(self, synthetic_geotiff, tmp_dir, no_transform, monkeypatch):
        def eof(*_):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        out = os.path.join(tmp_dir, "out.tif")
        with pytest.raises(cli.NonInteractiveError):
            process_file(synthetic_geotiff, out, "EGM96", "EGM96", False, False, 16, "bilinear")
        assert no_transform == []

    def test_same_datum_prompt_with_assume_yes(self, synthetic_geotiff, tmp_dir, no_transform):
        out = os.path.join(tmp_dir, "out.tif")
        process_file(synthetic_geotiff, out, "EGM96", "EGM96", False, False, 16, "bilinear", assume_yes=True)
        assert len(no_transform) == 1


class TestMainYes:
    def test_yes_reaches_process_file(self, tmp_dir, monkeypatch):
        seen = {}

        def fake_process_file(*args, **kwargs):
            seen.update(kwargs)
            return True

        monkeypatch.setattr(cli, "ensure_grids", lambda **kw: [])
        monkeypatch.setattr(cli, "process_file", fake_process_file)
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        assert _run(
            monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.dt2"),
            "-s", "EGM2008", "-t", "EGM96", "--yes",
        ) == 0
        assert seen.get("assume_yes") is True

    def test_unanswerable_prompt_is_exit_code_two(self, tmp_dir, monkeypatch):
        def needs_an_answer(*args, **kwargs):
            raise cli.NonInteractiveError("Do you wish to proceed?")

        monkeypatch.setattr(cli, "ensure_grids", lambda **kw: [])
        monkeypatch.setattr(cli, "process_file", needs_an_answer)
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        assert _run(
            monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.dt2"),
            "-s", "EGM2008", "-t", "EGM96",
        ) == 2


class TestMainGridScope:
    """main() used to fetch all five grids, including the Explorer-only ones."""

    def _grids_requested(self, tmp_dir, monkeypatch, source, target):
        requested = {}

        def fake_ensure_grids(**kwargs):
            requested.update(kwargs)
            return []

        monkeypatch.setattr(cli, "ensure_grids", fake_ensure_grids)
        monkeypatch.setattr(cli, "process_file", lambda *a, **k: True)
        src = os.path.join(tmp_dir, "in.tif")
        with open(src, "wb"):
            pass
        _run(monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.tif"), "-s", source, "-t", target)
        return requested["filenames"]

    def test_geoid_to_geoid_needs_both_one_minute_grids(self, tmp_dir, monkeypatch):
        assert self._grids_requested(tmp_dir, monkeypatch, "EGM2008", "EGM96") == [
            "us_nga_egm08_1.tif", "us_nga_egm96_1.tif",
        ]

    def test_ellipsoid_to_geoid_needs_one_grid(self, tmp_dir, monkeypatch):
        assert self._grids_requested(tmp_dir, monkeypatch, "WGS84", "EGM96") == ["us_nga_egm96_1.tif"]
