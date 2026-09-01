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
