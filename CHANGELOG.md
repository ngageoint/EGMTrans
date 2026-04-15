# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-04-15

### Added

- CLI auto-download of geoid grids: both `egmtrans` and `python EGMTrans.py` now fetch any missing grid files from GitHub Releases on first run, matching the behavior previously available only in the ArcGIS Pro toolbox.
- `tests/test_accuracy.py`: numerical regression tests that pin `create_datum_array` output at six global control points (Atlantic, Washington DC, Cape Town, Mt Everest, New Guinea, central Greenland) against the real geoid grids, plus a full round-trip EGM96 → EGM2008 → EGM96 sanity check. Tests skip cleanly if the grid files are absent.
- `SECURITY.md` documenting the HTTPS + SHA-256 grid download model, network egress expectations, and the static attack surface.
- `CONTRIBUTING.md` covering dev setup, test/lint commands, and the commit attribution rule for AI assistants.
- GitHub Actions CI (`.github/workflows/ci.yml`) running `ruff check` and `pytest` on Python 3.11 and 3.12 against a cached copy of the geoid grids.
- README "Grid provenance" subsection explaining how the 1-arc-minute grids were computed directly from the EGM96/EGM2008 spherical harmonic coefficients via NGA's Fortran executables (`hsynth_WGS84`, `f477_bin`, `clenqt_bin`) and validated against Nikolaos Pavlis's 1-arc-minute reference binary.

### Changed

- README: removed the stale hardcoded "current version is 1.1.0" line; the version badge and `src/egmtrans/_version.py` remain the single source of truth.

### Fixed

- Cleaned up ruff findings in `cli.py`, `download.py`, and several existing test files so that the new CI pipeline runs green.

## [1.2.0] - 2026-04-08

### Changed

- Moved geoid grid GeoTIFF files (~1.3 GB) from Git LFS to a dedicated GitHub Release ([datum-grids-v1](https://github.com/ngageoint/EGMTrans/releases/tag/datum-grids-v1)) to fix ZIP download issues and remove the Git LFS dependency.
- The `datums/` directory no longer contains `.tif` files in the repository. Grid files must be downloaded separately.

### Added

- `src/egmtrans/download.py` module for downloading grid files from GitHub Releases with SHA-256 checksum verification.
- `download_grids.py` CLI script to download all geoid grid files with a single command.
- ArcGIS Pro toolbox auto-downloads grid files on first run -- no terminal required.
- Runtime validation in `config.verify_grids()` checks for required grid files before processing and provides clear download instructions if they are missing.
- `datums/README.md` with download instructions and checksums.

### Removed

- `.gitattributes` (Git LFS tracking no longer needed).
- Grid `.tif` files from Git LFS tracking.

## [1.1.0] - 2026-03-16

### Changed

- Refactored monolithic `EGMTrans.py` into an installable `egmtrans` Python package under `src/egmtrans/`.
- Replaced module-level global state with `_state.py` getter/setter pattern.
- Extracted code into focused modules: `config`, `crs`, `interpolation`, `flattening`, `io`, `transform`, `cli`, `numba_utils`, `logging_setup`, `file_utils`, `arcpy_compat`.
- Root-level `EGMTrans.py` is now a thin backward-compatibility shim that re-exports from the package.

### Added

- `pyproject.toml` with hatchling build system and `egmtrans` console entry point.
- `src/egmtrans/__main__.py` for `python -m egmtrans` support.
- Comprehensive test suite under `tests/` (64 tests covering config, CRS, interpolation, flattening, I/O, CLI, numba utils).
- `EGMTRANS_BASE_PATH` environment variable to override project root detection.

### Fixed

- No functional changes — all transformation logic is preserved exactly as-is.

## [1.0.0] - 2025-09-23

### Added

- Initial release of EGMTrans.
- Support for vertical datum transformations between WGS84, EGM96, and EGM2008.
- Support for GeoTIFF and DTED file formats.
- Standalone script and ArcGIS Pro toolbox versions.
- Option to keep ocean at 0 elevation.
- Flattening of water bodies and other flat areas.
- Creation of flat masks.