# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The example product profile, the README examples and the tests use the public SRTM and Copernicus samples.

### Removed

- An unreferenced PDF under `samples/`.

## [1.8.0] - 2026-10-03

### Added

- **DTED made directly from GeoTIFF.** `--dted-level {0,1,2}` (and the DTED Level parameter in ArcGIS Pro) writes every GeoTIFF on the whole-degree lattice, such as TanDEM-X tiles, as DTED of that level, one file per whole one-degree cell; a single GeoTIFF written to a `.dt0`, `.dt1` or `.dt2` name needs no flag. The heights are resampled onto the DTED posts with exact whole-number weights, the water bodies are found on the source grid and carried to the cell, the geoid correction is evaluated on the arc-minute lattice, and DTED1 and DTED0 are the finished DTED2 thinned, so the three levels agree at every common post. On a TanDEM-X longitude-spacing boundary the shared row is rebuilt from the coarser tile's data, so the two cells agree post for post. The header comes from the product profile and the metadata index. See "Creating DTED from GeoTIFF" in the README.
- **The same bytes on every computer.** A conversion gives the same DTED file from the same version, grids and inputs with or without Numba, in a terminal or in ArcGIS Pro. The log records the versions of EGMTrans, Python, GDAL, numpy and Numba, the SHA-256 of the grids read, and the size and SHA-256 of every DTED written; after a batch run the shared posts of neighboring DTED outputs are read back and compared.
- `egmtrans dted-selftest` and the *DTED Self-Test* tool: convert built-in synthetic tiles and compare the bytes with the pinned reference, so a producer can prove an installation before a run.
- `--dted-naming` (and the DTED Output Naming parameter): `stem`, `cell`, `dted`, or a template with `{stem}`, `{dir}`, `{cell}`, `{lat}`, `{lon}` and `{level}` for the names of converted cells.
- `egmtrans.dted.records`: EGMTrans reads and writes DTED elevation records itself (sentinels, counts, signed-magnitude posts, checksums), and `egmtrans dted-header --check-data` checks them with the same code. Posts outside the specification's -12,000 to 9,000 m range are reported.

### Changed

- Enclosed low spots beside a water body are raised to its level in floating-point GeoTIFF outputs: posts below the water beside them, bounded by the water and by higher ground, with fewer posts than `-p`. A low spot that reaches lower water (an outlet), the tile edge or a void is left as it is, and so is every post of DTED and whole-meter input. The raised posts join the water body in the mask, and the log counts the spots and lists every raise of a meter or more with its position.
- The flat-area and bilinear kernels are compiled without fast-math, so a host with Numba and one without label the same flat areas. Spline interpolation keeps it. The labeling kernel no longer runs in parallel.
- With `--dted-level`, a batch run copies nothing of the input tree: only the DTED files, their masks and the log are written.
- The ArcGIS Pro toolbox warns when the EGMTrans loaded in the session is not the version on disk.

### Fixed

- Flat areas are matched by whole centimeters. The former tolerance test could join posts exactly 1 cm apart, and its result could differ between hosts. Water bodies now join across tile seams, and match a water-levels table, only at the same centimeter. DTED outputs are unchanged. GeoTIFF outputs can differ from earlier versions by a centimeter or two where the terrain steps by exactly 1 cm; they need not be regenerated.
- A DTED header built from the index and profile now requires every required field to come from them (or `--abs_horiz_accuracy`), reports the validator's warnings, writes accuracy subregion outlines clockwise from their southwest vertex, and refuses an index row or profile made for another DTED level.
- The ArcGIS Pro toolbox checks the bilinear requirement for every DTED output, not only for DTED input, and its tool help shows the entries added in 1.7.0 as formatted text.
- After a batch run that transformed nothing, the offer to delete the output folder is not made when that folder is, or holds, the input folder.
- A DTED input is refused when the output name is another DTED level.

### Removed

- `crs/dted_header_parser.py`, the shim kept in 1.7.0; use `egmtrans dted-header`.

## [1.7.0] - 2026-10-01

### Added

- `egmtrans dted-header` and the *DTED Header Report* tool in ArcGIS Pro: every field of the UHL, DSI and ACC records as a table, with the level evidence, a summary and the MIL-PRF-89020B findings as errors, warnings and information, in text, JSON, CSV or Markdown. `--check-data` checks the elevation records too. It replaces `crs/dted_header_parser.py`, kept as a shim for one release.
- `egmtrans dted-index build|validate`: a metadata index of a DTED collection, one row per cell in a GeoPackage or GeoParquet file, holding the header values the raster cannot provide, with the accuracy subregions in a second layer. Rows come from existing DTED headers, from source rasters through the profile's harvest mappings, or from a footprint layer.
- A product profile (`samples/dted_profile_example.toml`): a TOML file of the header values that are the same for every cell of a product.
- `--dted-index` and `--dted-profile` (and the matching toolbox parameters) fill the DTED header of every output from the cell's index row and the profile, with every field's source logged. A cell the index does not hold stops the run before anything is written.
- The `egmtrans.dted` package: the header schema, the codec, the validator, the report, the index, the profile, the harvester and the header assembler.

### Changed

- Python 3.13 or later; CI tests 3.13 and 3.14. The floors are numpy 2.0, scipy 1.15 and numba 0.61; `pyarrow` and `lxml` serve the index.
- DTED-to-DTED headers go through the new codec: NA is written left justified (`NA  `) as the specification requires, the UHL vertical accuracy repeats the ACC value, and NUL bytes where the specification wants blanks become blanks. Everything else in the input header is carried over, and the validator's findings on the result are logged.
- The vertical datum of a DTED input is read from its header rather than from GDAL metadata, which a sidecar file could override.
- GDAL is pointed at the PROJ directory that holds `proj.db` when the search path lists one without it first.

## [1.6.0] - 2026-09-30

### Fixed

- DTED lakes were split into two levels by whole-meter rounding: only the ocean was flattened in DTED input. Every whole-meter plateau of at least `-p` posts is now a flat area, as for GeoTIFF. **DTED outputs with lakes or other flat areas produced by earlier versions should be regenerated.**
- A flat area took the mean of its transformed values, which could leave shore posts below the water. A flat area now takes the lowest of its transformed values, so no land post changes and no shore post ends below the water beside it. **Outputs with large water bodies should be regenerated.**
- A water body spanning several tiles got a different level in each tile. Batch mode now runs in two passes, joins the flat areas across tile seams and sets each water body to one level before any tile is written; the shared posts of neighboring DTED tiles come out identical. A water body that reaches an edge with no neighbor in the run is listed in the log.
- A bare `-a` or `-p` was accepted; both are now usage errors.

### Added

- `-c` / `--containment`: a flat area counts as a water body only when at least this share of its boundary posts (default 0.8) lie above it in the input, which keeps contour bands on slopes and flat hilltops as terrain. Ocean neighbors are neutral; `-c 0` keeps every flat area.
- `--context FOLDER` (repeatable): neighboring tiles analyzed in the first pass but not transformed, so a water body that continues into them gets the level a run over all of them would give.
- `--export-water-levels FILE` and `--water-levels FILE`: a CSV of the level of every water body that touches a tile edge, written by a run over a larger area and read by a later run over part of it. An imported level can lower a water body but never raise it.
- The ArcGIS Pro toolbox gains Context Folder, Water Levels Table and Minimum Containment parameters, and its tool help describes every parameter.
- The log reports the water bodies touching the boundary of a run and, per file, how the shore posts stand to the water after the transform.

### Changed

- DTED output requires the bilinear algorithm, the only one that gives the same correction at a shared post whatever the tile extent. GeoTIFF keeps all four algorithms; a batch run with another algorithm warns once.
- A batch no longer stops at the first tile that fails: the tile is reported, the run goes on, and the exit code is 1. The prompts a run can need are asked once, before anything is copied.
- `-p` applies to DTED as well as GeoTIFF.
- Every candidate DEM is opened with GDAL when a folder is searched and must carry a geotransform; a file that cannot be opened is skipped.
- The DTED write rounds explicitly, halves away from zero, as GDAL does.

## [1.5.0] - 2026-09-21

### Fixed

- **Whole rows of ocean in GeoTIFF outputs were written as voids.** The output was assembled in a new GeoTIFF whose NoData value (NaN) was set only after the heights were written. A new GeoTIFF skips any block equal to the NoData value current at write time (0 when none is set) and fills skipped blocks with the NoData value when it closes, so every row that was entirely 0 m (open sea) came out as NaN. The sample Copernicus tile over Mindanao lost its southern 969 rows this way. NoData is now set before the heights are written, and the dataset is closed (not just dereferenced) before it is converted to the COG. DTED outputs were not affected. **GeoTIFF outputs of coastal tiles produced by earlier versions should be regenerated.** The new benchmark script found this by checking that ocean stays at 0 m.
- **Voids in GeoTIFF inputs were written as 0 m when flattening was on (the default).** The Numba kernels were compiled with `fastmath=True`, which includes the `nnan` flag: LLVM may then assume no value is NaN and delete the `np.isnan()` tests, so every void was labeled ocean and set to 0. The kernels now use every fastmath flag except `nnan` and `ninf` (`numba_utils.FASTMATH_FLAGS`), and `transform_vertical_datum` restores input voids after flattening as a second line of defense. DTED inputs were not affected. **GeoTIFF outputs with voids produced by earlier versions should be regenerated.**
- **TanDEM-X and DGED auxiliary layers were not skipped in batch mode.** `is_valid_dem` lower-cased the filename but compared it with upper-case codes (`HEM`, `WBM`, `EDM`, ...), so the check never matched. A Height Error Map was transformed as if it were a DEM, and a Byte Water Body Mask aborted the whole batch as an unsupported data type. The codes now match case-insensitively as whole filename tokens (so `Edmonton_DEM.tif` is still a DEM), the Copernicus Filling Mask (`FLM`) is recognized, and any GeoTIFF whose band cannot hold heights (Byte, UInt16) is skipped. The old test passed only because its file did not exist.
- A confirmation prompt with no terminal to answer it (a container, a scheduler) crashed with `EOFError`. It now stops with exit code 2 and says to re-run with `--yes`.

### Added

- `-y` / `--yes`: proceed without asking when the input header's vertical datum disagrees with `-s`, or when `-s` equals `-t` for a GeoTIFF. It never answers the prompt to delete an output folder.
- `Dockerfile`, `.dockerignore` and `docker/smoke_test.sh`: an image with the two 1-arc-minute grids (SHA-256 verified at build time) and precompiled Numba kernels that runs with `--network none` as a non-root user. See "Run in a Container" in the README.
- `benchmarks/benchmark_tiles.py`: times real tiles cold, warm, single-threaded, without Numba and through the full CLI, checks every output (voids, ocean, DTED header), optionally cross-checks the bilinear transform against PROJ, and extrapolates to a global run in core-hours. It runs from a plain clone as well as an installed package, and fetches or verifies the two grids it needs before timing anything.
- `config.required_grids()`, and a `filenames` argument to `download.ensure_grids()`.

### Changed

- The command line checks and downloads only the grids its source and target datums need. It used to fetch all five, including the three used only by the Explorer maps, so an installation with just the two transform grids reached for the network on every run. `download_grids.py` and the ArcGIS Pro toolbox still fetch the full set.
- The command line resolves the grid folder through `EGMTRANS_BASE_PATH` when that is set, like the rest of the package.

## [1.4.0] - 2026-08-31

### Fixed

- **CLI single-file output created a directory at the output path.** `main()` classified any legal output name as a folder (`is_valid_filename` accepts every legal basename), and tested that before the file case, so the log path was derived as `out.dt2/out.dt2_transform.log` and `setup_logger` created `out.dt2` as a **directory**. The transform then opened that directory in update mode, which GDAL reports on Windows as `<path>: Permission denied` – right after "Updating vertical datum to …". Present since 1.1.0; it affected every single-file CLI run with logging on (GeoTIFF as well as DTED) and went unnoticed because single-file testing went through the ArcGIS Pro toolbox, which had the correct ordering. Batch folder→folder mode was unaffected.
- **DTED void pixels were written as 0 m.** Nodata is converted to `NaN` during processing and GDAL maps `NaN` to 0 when writing a float array into an Int16 band, so voids came out at sea level. They are now restored to -32767 (`io.restore_nodata`). `samples/03n008e_SRTM.dt2` alone has 23,378 affected pixels – **DTED outputs produced by earlier versions should be regenerated.**
- **Scale/offset correction was silently discarded** for the `bilinear`, `spline`, and `delaunay` algorithms: `input_array` was read before `apply_scale_factor` ran and never re-read, so those paths operated on raw digital numbers. The `proj` algorithm was unaffected because it re-reads the file.
- **Half-pixel registration offset in the geoid resampling.** The clipped geoid points were built at cell centers but the DEM query points at cell corners, so every sample was taken half a DEM pixel to the north-west. Pinned accuracy values moved by 0.3 cm to 7.0 cm (largest at high-gradient locations) and now match the raw grid exactly at nodes; see `test_exact_node_matches_grid`.
- **Bilinear interpolation degraded to nearest-neighbor at grid edges** along *both* axes: when either axis fell outside the source grid, both were clamped and the in-range axis's interpolation was discarded. Each axis is now clamped independently.
- **`-a proj` did not use the 1-arc-minute geoid grid.** `get_proj4` substituted the grid path with `str.replace` on an assumed basename, but PROJ emits its own registered grid (`us_nga_egm96_15.tif`, the 15-arc-minute model) for EPSG:5773, so the substitution silently did nothing and GDAL Warp used whatever it could find. The `+geoidgrids=` value is now parsed and replaced, and a missing token is reported.
- **The CLI exited 0 on failure.** `main()` now returns 0 on success, 1 on a runtime failure, and 2 on a usage or path error.
- **Read-only sources produced unwritable outputs.** `shutil.copy`/`copy2` carry the source's mode bits, so DTED delivered on read-only media yielded a read-only output that the next `GA_Update` open could not write – the same "Permission denied" from a different cause. Copies are now made writable (`file_utils.copy_as_writable`, `ensure_writable`).
- **One non-DEM file aborted an entire batch.** A `*_mask.tif` – a file EGMTrans itself produces – ended the run and offered to delete an output directory that already held good results. Non-DEMs are now skipped and the batch continues.
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
- ArcGIS Pro toolbox auto-downloads grid files on first run – no terminal required.
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

- No functional changes – all transformation logic is preserved exactly as-is.

## [1.0.0] - 2025-09-23

### Added

- Initial release of EGMTrans.
- Support for vertical datum transformations between WGS84, EGM96, and EGM2008.
- Support for GeoTIFF and DTED file formats.
- Standalone script and ArcGIS Pro toolbox versions.
- Option to keep ocean at 0 elevation.
- Flattening of water bodies and other flat areas.
- Creation of flat masks.