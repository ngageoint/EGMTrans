"""Tests for egmtrans.download — which grid files ensure_grids fetches."""

import os

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


def test_downloads_time_out_instead_of_hanging(tmp_dir, monkeypatch):
    import urllib.request

    seen = {}

    class Response:
        headers = {"Content-Length": "0"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            return b""

    def fake_urlopen(url, timeout=None):
        seen["timeout"] = timeout
        if seen.get("hang"):
            raise TimeoutError("timed out")
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        download.download_file("https://example.invalid/x.tif", f"{tmp_dir}/x.tif", "00", lambda m: None)
    assert seen["timeout"] == download.DOWNLOAD_TIMEOUT == 60
    seen["hang"] = True
    with pytest.raises(RuntimeError, match="No response .* within 60 seconds"):
        download.download_file("https://example.invalid/x.tif", f"{tmp_dir}/x.tif", "00", lambda m: None)
    assert not os.path.exists(f"{tmp_dir}/x.tif.part")
