"""Download geoid grid files from GitHub Releases.

Uses only the Python standard library (urllib, hashlib) so it works in
environments that have not yet installed EGMTrans's dependencies.  The
module is importable from both the standalone ``download_grids.py`` CLI
wrapper and the ArcGIS Pro toolbox.
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.request

RELEASE_TAG = "datum-grids-v1"
RELEASE_URL = (
    f"https://github.com/ngageoint/EGMTrans/releases/download/{RELEASE_TAG}"
)

GRID_FILES: dict[str, dict[str, str | int]] = {
    "us_nga_egm96_1.tif": {
        "sha256": "bb1d699ff17f6d641116c70109521c234396f68095d49234655d8cd519997b03",
        "size_mb": 323,
    },
    "us_nga_egm08_1.tif": {
        "sha256": "916b9a59a9590bcaea022cc4e3aec78d4c55653c30b06b9969380d1872de4b4e",
        "size_mb": 374,
    },
    "egm96_to_egm2008_delta.tif": {
        "sha256": "ce33cddc49d2bf964bcec80bd0ed065bca9cc4034dc3d2e29eae6addcc7c4181",
        "size_mb": 468,
    },
    "us_nga_egm08_25.tif": {
        "sha256": "2fceeebc3f1e43719a7f350cf5162ca75187d8ea5732dd9871d63f688e00a1a3",
        "size_mb": 96,
    },
    "us_nga_egm96_15.tif": {
        "sha256": "856a47a31af9195f8b3e565ae364924cbca3e7dd329f874595a2e6e0487ed678",
        "size_mb": 3,
    },
}

# Chunk size for streaming downloads (1 MB).
_CHUNK_SIZE = 1024 * 1024


def _default_datums_dir() -> str:
    """Return the datums/ directory relative to the project root."""
    # This file lives at src/egmtrans/download.py — three levels up is the
    # project root, same convention as config.py.
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return os.path.join(project_root, "datums")


def verify_checksum(file_path: str, expected_sha256: str) -> bool:
    """Return True if *file_path* exists and its SHA-256 matches *expected_sha256*."""
    if not os.path.isfile(file_path):
        return False
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            sha.update(chunk)
    return sha.hexdigest() == expected_sha256


def download_file(
    url: str,
    dest_path: str,
    expected_sha256: str,
    message_func: object = print,
) -> None:
    """Download a single file from *url* to *dest_path* with checksum verification.

    Progress is reported via *message_func* (``print`` for CLI,
    ``arcpy.AddMessage`` for ArcGIS Pro).

    Raises:
        RuntimeError: If the downloaded file fails checksum verification.
        urllib.error.URLError: On network errors.
    """
    filename = os.path.basename(dest_path)
    tmp_path = dest_path + ".part"

    try:
        with urllib.request.urlopen(url) as response:
            total = int(response.headers.get("Content-Length", 0))
            total_mb = total / (1024 * 1024) if total else 0
            downloaded = 0
            sha = hashlib.sha256()

            with open(tmp_path, "wb") as out:
                while True:
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    out.write(chunk)
                    sha.update(chunk)
                    downloaded += len(chunk)
                    dl_mb = downloaded / (1024 * 1024)
                    if total:
                        message_func(
                            f"  {filename}  {dl_mb:.0f} / {total_mb:.0f} MB"
                        )
                    else:
                        message_func(f"  {filename}  {dl_mb:.0f} MB")

        if sha.hexdigest() != expected_sha256:
            os.remove(tmp_path)
            raise RuntimeError(
                f"Checksum mismatch for {filename}. "
                f"Expected {expected_sha256}, got {sha.hexdigest()}. "
                f"The partial download was deleted."
            )

        os.replace(tmp_path, dest_path)
    except BaseException:
        # Clean up partial download on any failure (including KeyboardInterrupt).
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def ensure_grids(
    datums_dir: str | None = None,
    message_func: object = print,
) -> list[str]:
    """Ensure all geoid grid files are present, downloading any that are missing.

    Args:
        datums_dir: Path to the ``datums/`` directory.  Defaults to the
            standard location relative to the project root.
        message_func: Callable used for progress messages.  Pass
            ``arcpy.AddMessage`` when running inside ArcGIS Pro.

    Returns:
        List of filenames that were downloaded (empty if all were already
        present).
    """
    if datums_dir is None:
        datums_dir = _default_datums_dir()
    os.makedirs(datums_dir, exist_ok=True)

    # Determine which files need downloading.
    missing = []
    for filename, info in GRID_FILES.items():
        path = os.path.join(datums_dir, filename)
        if not verify_checksum(path, info["sha256"]):
            missing.append(filename)

    if not missing:
        return []

    total_mb = sum(GRID_FILES[f]["size_mb"] for f in missing)
    message_func(
        f"Downloading {len(missing)} geoid grid file(s) "
        f"({total_mb:,} MB) from GitHub Releases..."
    )

    downloaded = []
    for filename in missing:
        info = GRID_FILES[filename]
        url = f"{RELEASE_URL}/{filename}"
        dest = os.path.join(datums_dir, filename)
        message_func(f"  Starting {filename} ({info['size_mb']} MB)")
        download_file(url, dest, info["sha256"], message_func)
        downloaded.append(filename)

    message_func("All geoid grid files are ready.")
    return downloaded


if __name__ == "__main__":
    ensure_grids()
