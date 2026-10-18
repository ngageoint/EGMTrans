"""Tests for egmtrans.download: which grid files ensure_grids fetches, and how often a grid is hashed."""

import hashlib
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


@pytest.fixture
def hashed(monkeypatch):
    """The files hashed, with the digest registry starting empty."""
    monkeypatch.setattr(download, "_digests", {})
    real = download._digest
    calls = []
    monkeypatch.setattr(download, "_digest", lambda path: (calls.append(path), real(path))[1])
    return calls


def _write(path, content):
    with open(path, "wb") as handle:
        handle.write(content)
    return hashlib.sha256(content).hexdigest()


def test_a_file_is_hashed_once_per_process(tmp_dir, hashed):
    path = os.path.join(tmp_dir, "grid.tif")
    sha = _write(path, b"x" * 5000)
    assert download.verify_checksum(path, sha) and download.verify_checksum(path, sha)
    assert len(hashed) == 1
    assert not download.verify_checksum(path, "00"), "a wrong expectation fails"
    assert len(hashed) == 1, "without reading the file again"
    assert not download.verify_checksum(os.path.join(tmp_dir, "missing.tif"), sha)
    assert len(hashed) == 1


def test_a_rewritten_file_is_hashed_again(tmp_dir, hashed):
    path = os.path.join(tmp_dir, "grid.tif")
    old = _write(path, b"x" * 5000)
    assert download.verify_checksum(path, old)
    new = _write(path, b"y" * 6000)   # another length: the stamp changes whatever the clock's resolution
    assert download.verify_checksum(path, new) and not download.verify_checksum(path, old)
    assert len(hashed) == 2


def test_ensure_grids_and_a_later_check_share_one_hash(tmp_dir, hashed, monkeypatch):
    path = os.path.join(tmp_dir, "x.tif")
    sha = _write(path, b"z" * 4096)
    monkeypatch.setattr(download, "GRID_FILES", {"x.tif": {"sha256": sha, "size_mb": 0}})
    assert download.ensure_grids(datums_dir=tmp_dir, filenames=["x.tif"], message_func=lambda m: None) == []
    assert download.verify_checksum(path, sha)
    assert len(hashed) == 1


def test_a_download_seeds_the_digest(tmp_dir, hashed, monkeypatch):
    import urllib.request

    body = b"grid bytes" * 300
    sha = hashlib.sha256(body).hexdigest()

    class Response:
        headers = {"Content-Length": str(len(body))}

        def __init__(self):
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            if self.sent:
                return b""
            self.sent = True
            return body

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: Response())
    dest = os.path.join(tmp_dir, "x.tif")
    download.download_file("https://example.invalid/x.tif", dest, sha, lambda m: None)
    assert download.verify_checksum(dest, sha) and hashed == []
