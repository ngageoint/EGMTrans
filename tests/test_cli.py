"""Tests for egmtrans.cli — argument handling, path dispatch, and exit codes."""

import argparse
import os
import sys

import pytest

from egmtrans import batch, cli
from egmtrans.cli import datum_arg, process_file, str2bool


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
    """Run main() without the transform or the geoid grids.

    A batch of one DEM needs no first pass, so ``analyze_tile`` must not run.
    """
    calls = []

    def fake_process_file(*args, **kwargs):
        calls.append(args)
        return True

    def no_analysis(*args, **kwargs):
        raise AssertionError("pass 1 ran for a batch that needs no merge")

    monkeypatch.setattr(cli, "ensure_grids", lambda **kw: [])
    monkeypatch.setattr(cli, "verify_grids", lambda *a: None)
    monkeypatch.setattr(cli, "process_file", fake_process_file)
    monkeypatch.setattr(batch, "analyze_tile", no_analysis)
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

        assert _run(monkeypatch, "-i", src, "-o", out, "-s", "EGM2008", "-t", "EGM96") == 0

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

        assert _run(monkeypatch, "-i", src, "-o", outdir, "-s", "EGM2008", "-t", "EGM96") == 0

        assert stub_pipeline[0][1] == os.path.join(outdir, "n39w077.dt2")
        assert os.path.isdir(outdir)

    def test_folder_input_to_file_output_is_a_usage_error(self, tmp_dir, stub_pipeline, monkeypatch):
        indir = os.path.join(tmp_dir, "in")
        os.makedirs(indir)
        out = os.path.join(tmp_dir, "out.tif")

        assert _run(monkeypatch, "-i", indir, "-o", out, "-s", "EGM2008", "-t", "EGM96") == 2
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
            "-s", "EGM2008", "-t", "EGM96",
        ) == 0

    def test_transform_failure_is_one(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        monkeypatch.setattr(cli, "process_file", lambda *a, **k: False)
        assert _run(
            monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.dt2"),
            "-s", "EGM2008", "-t", "EGM96",
        ) == 1

    def test_missing_input_is_a_usage_error(self, tmp_dir, stub_pipeline, monkeypatch):
        assert _run(
            monkeypatch, "-i", os.path.join(tmp_dir, "nope.dt2"),
            "-o", os.path.join(tmp_dir, "out.dt2"), "-s", "EGM2008", "-t", "EGM96",
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


class TestDtedRequiresBilinear:
    """DTED tiles are edge-matched: only bilinear gives the same correction at a
    shared post whatever the tile extent, and any other algorithm moves posts
    by 1 m after rounding to whole meters."""

    @pytest.fixture
    def no_transform(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cli, "verify_grids", lambda *a: None)
        monkeypatch.setattr(cli, "transform_vertical_datum", lambda *a, **k: calls.append((a, k)))
        return calls

    @pytest.mark.parametrize("algorithm", ["spline", "delaunay", "proj"])
    def test_dted_output_rejects_other_algorithms(self, tmp_dir, no_transform, log_lines, algorithm):
        import numpy as np

        from tests.conftest import write_dted

        src = write_dted(os.path.join(tmp_dir, "n03e008.dt0"), np.full((121, 121), 40, dtype=np.int16), 8, 3)
        out = os.path.join(tmp_dir, "out.dt0")
        assert process_file(src, out, "EGM2008", "EGM96", True, False, 16, algorithm) is False
        assert no_transform == []
        assert any("DTED output requires the bilinear algorithm" in line for line in log_lines)

    def test_geotiff_output_still_accepts_spline(self, synthetic_geotiff, tmp_dir, no_transform):
        out = os.path.join(tmp_dir, "out.tif")
        assert process_file(synthetic_geotiff, out, "EGM2008", "EGM96", True, False, 16, "spline") is True
        assert no_transform[0][0][7] == "spline"

    def test_switch_downgrades_to_warning(self, tmp_dir, no_transform, log_lines, monkeypatch):
        import numpy as np

        from tests.conftest import write_dted

        monkeypatch.setattr(cli, "DTED_REQUIRES_BILINEAR", False)
        src = write_dted(os.path.join(tmp_dir, "n03e008.dt0"), np.full((121, 121), 40, dtype=np.int16), 8, 3)
        out = os.path.join(tmp_dir, "out.dt0")
        assert process_file(src, out, "EGM2008", "EGM96", True, False, 16, "spline",
                            check_for_wrong_datum=False) is True
        assert len(no_transform) == 1
        assert any("DTED output requires the bilinear algorithm" in line for line in log_lines)

    def test_tile_levels_reach_transform(self, synthetic_geotiff, tmp_dir, no_transform):
        from egmtrans.tiling import TileLevels

        levels = TileLevels({2: 149.4}, {2: 25})
        out = os.path.join(tmp_dir, "out.tif")
        process_file(synthetic_geotiff, out, "EGM2008", "EGM96", True, False, 16, "bilinear", tile_levels=levels)
        assert no_transform[0][1]["tile_levels"] is levels


class TestBareFlags:
    """A bare -a used to become True, pass the choices check and run the spline
    branch; a bare -p became True, that is one post."""

    @pytest.mark.parametrize("flag", ["-a", "-p"])
    def test_bare_flag_is_a_usage_error(self, tmp_dir, stub_pipeline, monkeypatch, flag):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        assert _run(
            monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.dt2"),
            "-s", "EGM2008", "-t", "EGM96", flag,
        ) == 2
        assert stub_pipeline == []


class TestContainmentOption:
    def test_out_of_range_is_a_usage_error(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        assert _run(
            monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.dt2"),
            "-s", "EGM2008", "-t", "EGM96", "-c", "1.5",
        ) == 2
        assert stub_pipeline == []

    def test_reaches_process_file(self, tmp_dir, monkeypatch):
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
            "-s", "EGM2008", "-t", "EGM96", "-c", "0.5",
        ) == 0
        assert seen.get("min_containment") == 0.5


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


class TestDtedLevelFlags:
    """--dted-level and --dted-naming: parsing, the versions line, and the dispatch to the batch."""

    def test_level_with_a_folder_output_goes_to_the_batch(self, tmp_dir, stub_pipeline, monkeypatch):
        from tests.conftest import lattice_geotransform, synthetic_cell, write_geotiff

        src = write_geotiff(os.path.join(tmp_dir, "tile.tif"), synthetic_cell(60),
                            lattice_geotransform(30, 85, 60, 60), nodata=-32767.0)
        outdir = os.path.join(tmp_dir, "out")
        profile = os.path.join(os.path.dirname(__file__), "data", "dted_profile.toml")
        argv = ["-i", src, "-o", outdir, "-s", "EGM2008", "-t", "EGM96", "--dted-level", "2",
                "--dted-naming", "cell", "--dted-profile", profile]
        # The header plan is shown before anything is written; a "no" ends the run.
        monkeypatch.setattr("builtins.input", lambda *_: "no")
        assert _run(monkeypatch, *argv) == 1
        assert stub_pipeline == []
        assert _run(monkeypatch, *argv, "-y") == 0
        assert stub_pipeline[0][1] == os.path.join(outdir, "N85E030.dt2")
        assert stub_pipeline[0][0] == src
        with open(os.path.join(outdir, "tile_transform.log")) as handle:
            log = handle.read()
        assert "EGMTrans " in log and "GDAL " in log and "numba " in log, "the versions line is missing"
        assert not os.path.exists(os.path.join(outdir, "tile.tif")), "the input was copied"

    def test_bad_level_and_naming_are_usage_errors(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "tile.tif")
        with open(src, "wb"):
            pass
        base = ["-i", src, "-o", os.path.join(tmp_dir, "out"), "-s", "EGM2008", "-t", "EGM96"]
        assert _run(monkeypatch, *base, "--dted-level", "3") == 2
        assert _run(monkeypatch, *base, "--dted-level", "2", "--dted-naming", "{lvl}") == 2
        assert _run(monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out.tif"), "-s", "EGM2008", "-t", "EGM96",
                    "--dted-level", "2") == 2
        assert stub_pipeline == []

    def test_dted_input_must_be_at_the_level_given(self, tmp_dir, stub_pipeline, monkeypatch, capsys):
        import numpy as np

        from tests.conftest import write_dted

        src = write_dted(os.path.join(tmp_dir, "n50w001.dt0"), np.full((121, 121), 5, dtype=np.int16), -1, 50)
        outdir = os.path.join(tmp_dir, "out")
        assert _run(monkeypatch, "-i", src, "-o", outdir, "-s", "EGM2008", "-t", "EGM96", "--dted-level", "2",
                    "-y") == 1
        assert "a DTED file keeps its level" in capsys.readouterr().out and stub_pipeline == []
        assert _run(monkeypatch, "-i", src, "-o", outdir, "-s", "EGM2008", "-t", "EGM96", "--dted-level", "0",
                    "-y") == 0
        assert stub_pipeline[0][1] == os.path.join(outdir, "n50w001.dt0")

    def test_an_egm2008_target_for_dted_is_refused_before_anything_runs(self, tmp_dir, stub_pipeline, monkeypatch,
                                                                       capsys):
        tiles = os.path.join(tmp_dir, "tiles")
        os.makedirs(tiles)
        src = os.path.join(tiles, "in.dt2")
        with open(src, "wb"):
            pass
        for args in (("-i", src, "-o", os.path.join(tmp_dir, "out.dt2"), "-s", "EGM96", "-t", "EGM2008"),
                     ("-i", src, "-o", os.path.join(tmp_dir, "out"), "-s", "EGM96", "-t", "WGS84"),
                     ("-i", tiles, "-o", os.path.join(tmp_dir, "out"), "-s", "EGM2008", "-t", "EGM2008",
                      "--dted-level", "2")):
            assert _run(monkeypatch, *args) == 2, args
            assert "EGM96 only" in capsys.readouterr().err
        assert stub_pipeline == []
        assert not os.path.exists(os.path.join(tmp_dir, "out"))


class TestDtedSet:
    """--dted-set: parsed once, carried to every file; a bad one is a usage error."""

    def test_overrides_reach_the_metadata(self, tmp_dir, monkeypatch):
        import datetime as dt

        seen = {}

        def fake_process_file(*args, **kwargs):
            seen.update(kwargs)
            return True

        monkeypatch.setattr(cli, "ensure_grids", lambda **kw: [])
        monkeypatch.setattr(cli, "process_file", fake_process_file)
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        base = ["-i", src, "-o", os.path.join(tmp_dir, "out.dt2"), "-s", "EGM2008", "-t", "EGM96", "--yes"]
        assert _run(monkeypatch, *base, "--dted-set", "producer_code=USNGA",
                    "--dted-set", "compilation_date=2026-10") == 0
        metadata = seen["dted_metadata"]
        assert not metadata.empty
        assert metadata.overrides == {"producer_code": "USNGA", "compilation_date": dt.date(2026, 10, 1)}
        assert _run(monkeypatch, *base, "--dted-set", "dted_level=2") == 2
        assert _run(monkeypatch, *base, "--dted-set", "security_code") == 2

    def test_dted_to_dted_plan_asks_only_with_metadata(self, tmp_dir, monkeypatch, log_lines):
        import numpy as np

        from egmtrans.dted.writer import DtedMetadataSource, parse_overrides
        from tests.conftest import write_dted

        calls = []
        monkeypatch.setattr(cli, "verify_grids", lambda *a: None)
        monkeypatch.setattr(cli, "transform_vertical_datum", lambda *a, **k: calls.append(a))
        src = write_dted(os.path.join(tmp_dir, "n06e126.dt0"), np.full((121, 121), 3, dtype=np.int16), 126, 6)
        out = os.path.join(tmp_dir, "out.dt0")

        def no_input(*_):
            raise AssertionError("input() was called")

        # Without an index, a profile or overrides there is nothing to confirm.
        monkeypatch.setattr("builtins.input", no_input)
        assert process_file(src, out, "EGM2008", "EGM96", False, False, 16, "bilinear",
                            check_for_wrong_datum=False) is True
        assert len(calls) == 1 and "DTED header plan" not in log_lines

        source = DtedMetadataSource(None, None, parse_overrides(["producer_code=USNGA"]))
        monkeypatch.setattr("builtins.input", lambda *_: "no")
        assert process_file(src, out, "EGM2008", "EGM96", False, False, 16, "bilinear",
                            check_for_wrong_datum=False, dted_metadata=source) is False
        assert len(calls) == 1 and "DTED header plan" in log_lines
        assert any("dsi.producer_code" in line and "(override)" in line for line in log_lines)
        assert "  example: cell N06E126, n06e126.dt0 -> out.dt0" in log_lines
        monkeypatch.setattr("builtins.input", lambda *_: "yes")
        assert process_file(src, out, "EGM2008", "EGM96", False, False, 16, "bilinear",
                            check_for_wrong_datum=False, dted_metadata=source) is True
        assert len(calls) == 2


class TestLogsAndTracebacks:
    def test_a_traceback_goes_to_the_log_file_only(self, tmp_dir, stub_pipeline, monkeypatch, capsys):
        def explode(*args, **kwargs):
            raise RuntimeError("boom in the transform")

        monkeypatch.setattr(cli, "process_file", explode)
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        out = os.path.join(tmp_dir, "out.dt2")
        assert _run(monkeypatch, "-i", src, "-o", out, "-s", "EGM2008", "-t", "EGM96") == 1
        printed = capsys.readouterr().out
        assert "EGMTrans stopped: boom in the transform" in printed and "Traceback" not in printed
        with open(os.path.join(tmp_dir, "out_transform.log"), encoding="utf-8") as handle:
            log = handle.read()
        assert "Traceback (for the record)" in log and "boom in the transform" in log

    def test_the_log_is_appended_with_a_banner_and_written_in_utf_8(self, tmp_dir, stub_pipeline, monkeypatch):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        out = os.path.join(tmp_dir, "sortie_été.dt2")
        for _ in range(2):
            assert _run(monkeypatch, "-i", src, "-o", out, "-s", "EGM2008", "-t", "EGM96") == 0
        with open(os.path.join(tmp_dir, "sortie_été_transform.log"), encoding="utf-8") as handle:
            log = handle.read()
        assert log.count("run started") == 2, "the second run appended to the first run's log"
        assert "sortie_été.dt2" in log, "a path outside cp1252 reaches the log"
        assert log.index("run started") < log.index("Processing completed.")

    def test_an_unwritable_export_path_is_an_argument_error(self, tmp_dir, stub_pipeline, monkeypatch, capsys):
        src = os.path.join(tmp_dir, "in.dt2")
        with open(src, "wb"):
            pass
        code = _run(monkeypatch, "-i", src, "-o", os.path.join(tmp_dir, "out"), "-s", "EGM2008", "-t", "EGM96",
                    "--export-water-levels", os.path.join(tmp_dir, "nowhere", "levels.csv"))
        assert code == 2 and "does not exist" in capsys.readouterr().err
        assert stub_pipeline == []


def test_a_patch_size_below_one_is_refused(tmp_dir, log_lines, monkeypatch):
    import numpy as np

    from tests.conftest import write_dted

    monkeypatch.setattr(cli, "verify_grids", lambda *a: None)
    src = write_dted(os.path.join(tmp_dir, "n06e126.dt0"), np.full((121, 121), 3, dtype=np.int16), 126, 6)
    for bad in (0, None, -4):
        assert process_file(src, os.path.join(tmp_dir, "out.dt0"), "EGM2008", "EGM96", True, False, bad,
                            "bilinear", check_for_wrong_datum=False) is False
    assert sum("minimum patch size must be at least 1 post" in line for line in log_lines) == 3
