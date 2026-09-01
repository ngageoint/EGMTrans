# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] - 2026-08-31

### Fixed

- **CLI single-file output created a directory at the output path.** `main()` classified any legal output name as a folder (`is_valid_filename` accepts every legal basename), and tested that before the file case, so the log path was derived as `out.dt2/out.dt2_transform.log` and `setup_logger` created `out.dt2` as a **directory**. The transform then opened that directory in update mode, which GDAL reports on Windows as `<path>: Permission denied` — right after "Updating vertical datum to …". Present since 1.1.0; it affected every single-file CLI run with logging on (GeoTIFF as well as DTED) and went unnoticed because single-file testing went through the ArcGIS Pro toolbox, which had the correct ordering. Batch folder→folder mode was unaffected.
- **DTED void pixels were written as 0 m.** Nodata is converted to `NaN` during processing and GDAL maps `NaN` to 0 when writing a float array into an Int16 band, so voids came out at sea level. They are now restored to -32767 (`io.restore_nodata`). `samples/03n008e_SRTM.dt2` alone has 23,378 affected pixels — **DTED outputs produced by earlier versions should be regenerated.**
- **Scale/offset correction was silently discarded** for the `bilinear`, `spline`, and `delaunay` algorithms: `input_array` was read before `apply_scale_factor` ran and never re-read, so those paths operated on raw digital numbers. The `proj` algorithm was unaffected because it re-reads the file.
- **Half-pixel registration offset in the geoid resampling.** The clipped geoid points were built at cell centres but the DEM query points at cell corners, so every sample was taken half a DEM pixel to the north-west. Pinned accuracy values moved by 0.3 cm to 7.0 cm (largest at high-gradient locations) and now match the raw grid exactly at nodes; see `test_exact_node_matches_grid`.
- **Bilinear interpolation degraded to nearest-neighbour at grid edges** along *both* axes: when either axis fell outside the source grid, both were clamped and the in-range axis's interpolation was discarded. Each axis is now clamped independently.
- **`-a proj` did not use the 1-arc-minute geoid grid.** `get_proj4` substituted the grid path with `str.replace` on an assumed basename, but PROJ emits its own registered grid (`us_nga_egm96_15.tif`, the 15-arc-minute model) for EPSG:5773, so the substitution silently did nothing and GDAL Warp used whatever it could find. The `+geoidgrids=` value is now parsed and replaced, and a missing token is reported.
- **The CLI exited 0 on failure.** `main()` now returns 0 on success, 1 on a runtime failure, and 2 on a usage or path error.
- **Read-only sources produced unwritable outputs.** `shutil.copy`/`copy2` carry the source's mode bits, so DTED delivered on read-only media yielded a read-only output that the next `GA_Update` open could not write — the same "Permission denied" from a different cause. Copies are now made writable (`file_utils.copy_as_writable`, `ensure_writable`).
- **One non-DEM file aborted an entire batch.** A `*_mask.tif` — a file EGMTrans itself produces — ended the run and offered to delete an output directory that already held good results. Non-DEMs are now skipped and the batch continues.
- Invalid `-s`/`-t` values are rejected up front instead of silently resolving by substring match (a bare `EGM` matched both EGM96 and EGM2008) or surfacing as a `KeyError` mid-transform.
- `-i <folder> -o <file>.tif` no longer copies the input tree into a folder literally named `out.tif`.
- GDAL datasets in `create_datum_array`, `create_gdal_warp_array`, and `transform_vertical_datum` are now closed via context managers or a `finally` block. The scratch `temp_<hex>/` directory is removed even when a transform raises, instead of being left in the user's output folder.
- Logging failures can no longer break a transform: `setup_logger` warns and continues console-only rather than propagating an `OSError`.

### Changed

- Output-path resolution is now a single shared helper (`file_utils.resolve_io_paths`, `derive_log_path`, `prepare_output_target`) used by both the CLI and the ArcGIS Pro toolbox, replacing two independent copies of the logic. Unwritable or directory destinations are detected before any GDAL work, with an actionable message.
- ArcGIS Pro: for a file input written to a folder output, the log is now named after the output file (`<folder>/<input name>_transform.log`) rather than after the folder, matching the file→file case.
- Batch mode clears the read-only attribute on every copied file, so re-runs and output-directory cleanup work on Windows.
- Removed a redundant full-size intermediate raster from the GeoTIFF path: `<base>_warp.tif` was created and fully populated but never read.
- The `proj` algorithm is no longer discouraged in the README: it was documented as unreliable because "the PROJ engine relies on the lower-resolution versions", which was the `get_proj4` substitution failing rather than a PROJ limitation. It now resamples the same 1 arc minute grids as the other algorithms and agrees with `bilinear` to within the 1 cm output rounding.

### Added

- `tests/test_paths.py`: regression coverage for output-path resolution, including the assertion that a `.dt2` output never becomes a directory.
- `tests/test_accuracy.py::test_exact_node_matches_grid`: every control point lies on a 1-arc-minute grid node, so interpolation there must reproduce the stored value exactly. This pins the *registration* of the resampling rather than a historical output, and would have caught the half-pixel offset.
- CLI dispatch and exit-code tests, `restore_nodata` tests, and bilinear edge-handling tests.

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