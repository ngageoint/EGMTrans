"""Tests for egmtrans.download — which grid files ensure_grids fetches."""

import pytest

from egmtrans import download


@pytest.fixture
def fetched(monkeypatch):
    """Record downloads instead of making them."""
    calls = []
    monkeypatch.setattr(download, "download_file", lambda url, dest, sha, msg: calls.append(url))
    return calls


def test_subset_fetches_only_the_named_grids(tmp_dir, fetched):
    got = download.ensure_grids(
        datums_dir=tmp_dir, filenames=["us_nga_egm96_1.tif"], message_func=lambda m: None
    )
    assert got == ["us_nga_egm96_1.tif"]
    assert fetched == [f"{download.RELEASE_URL}/us_nga_egm96_1.tif"]


def test_default_is_every_grid(tmp_dir, fetched):
    got = download.ensure_grids(datums_dir=tmp_dir, message_func=lambda m: None)
    assert sorted(got) == sorted(download.GRID_FILES)


def test_empty_subset_touches_nothing(tmp_dir, fetched):
    assert download.ensure_grids(datums_dir=tmp_dir, filenames=[], message_func=lambda m: None) == []
    assert fetched == []


def test_unknown_grid_is_rejected(tmp_dir, fetched):
    with pytest.raises(ValueError, match="nope.tif"):
        download.ensure_grids(datums_dir=tmp_dir, filenames=["nope.tif"], message_func=lambda m: None)
    assert fetched == []
