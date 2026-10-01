# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`egmtrans dted-header`** and the *DTED Header Report* tool in ArcGIS Pro: every field of the UHL, DSI and ACC records as a table (Start, End, Length, Title, Value, Description; one-based positions as in the specification, `--zero-based` on request) in text, JSON, CSV or Markdown, with the level evidence from four sources, a summary, and the MIL-PRF-89020B findings as errors, warnings and information (sentinels, bytes that are not printable, intervals against the latitude zone of Tables I to III, counts, corners, UHL/DSI agreement, security codes, accuracy forms and NA justification, dates, flags, accuracy subregions, file size). `--check-data` checks the elevation records too: sentinel, block and line counts, every checksum, and the null share against the partial cell indicator. Accuracy titles name the statistic (CE90, LE90). The reader works from raw bytes, so a `.aux.xml` sidecar cannot answer for the file. It replaces `crs/dted_header_parser.py`, which displayed the last of several reserved fields in every reserved row, never decoded the accuracy subregions (it looked up a field by the wrong name, and its subregion layout was 287 bytes with four corners where the specification has 284 with a 3-14 vertex outline), and had no validation; the script is now a shim that forwards to the subcommand and will be removed in the next release.
- **The DTED metadata index** (`egmtrans dted-index build|validate`): one row per one-degree cell in a GeoPackage or GeoParquet file, keyed by `cell_id`, holding the header values the raster cannot provide (accuracies, edition, dates, producer, security markings, free text) with the cell polygon, the accuracy subregions in a second layer, and a meta table; catalog columns for the collection's own use are kept. Rows are harvested from the headers of existing DTED files, from source rasters (one row per cell they cover, with values pulled from raster tags and XML sidecars through XPath mappings), or from a footprint layer, and an index can be updated in place. Values the build cannot find stay NULL, and `validate` lists them. GeoParquet needs `pyarrow` (the new `index` extra; ArcGIS Pro 3.7 ships it), GeoPackage only GDAL.
- **The product profile** (`samples/dted_profile_example.toml`): a TOML file of the header values that are the same for every cell of a product, plus the harvest mappings for the index builder.
- **`--dted-index` and `--dted-profile`** (and the matching toolbox parameters): the DTED header of every output is filled from the cell's index row and the profile, in a fixed order of precedence (derived from the geometry, the target datum and the data, then the index row, the profile, the `--abs_horiz_accuracy` fallback, the input header, the specification's fill), with every field's source logged. A cell the index does not hold stops the run before anything is written; a datum the index or profile states for another product is reported and the output keeps its own code. A NULL accuracy in the index means NA. A DTED output whose header cannot be completed is removed, since its records already hold the target datum's heights under the input's header.
- `egmtrans.dted`: the schema (`schema.py`, the single source of truth for every byte of the three records), the codec (`header.py`: exact round trip of any header, typed formatting, DMS and YYMM helpers, cell geometry for every level and latitude zone, level detection), `validate.py`, `report.py`, `index.py`, `profile.py`, `harvest.py` and `writer.py` (the header assembler). The `egmtrans` command routes the subcommands and keeps its flag-only form for the transform.
- `tests/data/srtm_n03e008_header.bin`, the first 3,428 bytes of the public SRTM sample, as the header fixture; the suite grows by 101 tests.

### Changed

- **Python 3.13 or later; the development environment is Python 3.14.** `environment.yml`, the `Dockerfile` and CI move to 3.14 (CI tests 3.13 and 3.14), the floors become numpy 2.0, scipy 1.15 and numba 0.61, and `pyarrow` and `lxml` are added for the index. ArcGIS Pro 3.7 (Python 3.13) is the supported toolbox host.
- **DTED-to-DTED headers go through the new codec.** The vertical datum is set as before, and the accuracy fields change in two ways: a value that is neither `0000`-`9999` nor NA becomes NA (as before), and NA is now written left justified (`NA  `) as MIL-PRF-89020B 3.13.5 requires for alpha values; 1.6.0 and earlier wrote `  NA` and the README called that compliant. The UHL absolute vertical accuracy now repeats the ACC value (it was never updated before), and the NUL bytes GDAL-written headers carry where the specification wants blanks become blanks. Everything else in the input header is carried over, gaps included; the validator's findings on the result are logged as warnings. The `--abs_horiz_accuracy` fallback still applies only when the field is NA.
- `check_file_datum` reads the vertical datum from the raw DTED header instead of GDAL metadata, which a stale `.aux.xml` sidecar could override.
- `configure_gdal` points GDAL at the PROJ directory that holds `proj.db` when the search path lists a user directory without one ahead of it (GDAL 3.13 with PROJ 9.9), which otherwise fails every EPSG lookup with `proj_create_from_database: Open of .../share/proj failed`.
- `SECURITY.md` describes the fixed-width ASCII header codec, which replaced the hardcoded byte offsets.

## [1.6.0] - 2026-09-30

### Fixed

- **DTED lakes were split into two levels by rounding.** DTED input had only its ocean flattened (`create_labeled_array_int` labeled nothing else), on the reasoning that the EGM96 to EGM2008 correction varies by much less than 1 m over moderately sized areas. It varies by a median of 0.59 m within a 1x1 degree cell and by more than 1 m in 28% of cells, so any lake whose corrected height crosses a half-meter boundary came out of the whole-meter rounding 1 m higher on one side of that boundary than on the other; 4 of the 57 flat patches in `samples/03n008e_SRTM.dt2` were split this way. Every whole-meter plateau of at least `-p` posts is now a patch, as it is for GeoTIFF (`tests/test_transform.py::test_dted_flat_patch_is_one_level`). GeoTIFF outputs were not affected. **DTED outputs with lakes or other flat areas produced by earlier versions should be regenerated.**
- **A flat patch took the mean of its transformed values, which left shore posts below the water.** The correction spans 2 to 5 m across the largest lakes (Superior 2.3, Victoria 2.0, Tanganyika 4.5, Baikal 4.6, Caspian 5.0 m over their bounding boxes), so a lake set to its mean level stood above its own shore at the high-correction end by up to half that range. A patch now takes the lowest of its transformed values. No land post is modified; a shore post is at least as high as the water in the input, the correction changes by millimeters between neighboring posts, and rounding is monotonic, so `round(h_shore - dN) >= round(h_water - dN) >= minimum` at every post and no shoreline can end up below its water body. The cost is that a large lake sits lower than its mean by up to the range of the correction across it (centimeters for most lakes). The log now counts, per file, the shore posts whose whole-meter step above the water was lost to rounding and any that fell below it (always 0). Affects GeoTIFF and, from this version, DTED. **Outputs with large water bodies should be regenerated; a level moves by up to the range of the correction across the body.** `tests/test_accuracy.py::test_pinned_lake_level_uses_patch_minimum` pins the rule.
- **A water body spanning several tiles got a different level in each tile.** Batch mode transformed one file at a time. It now runs in two passes: every tile is analyzed for flat patches that touch its edges, the patches are joined across the seams between tiles (the shared post row or column of DTED, the adjacent posts of abutting products such as Copernicus GeoTIFF tiles, matched by coordinate so that a coarser DTED neighbor above 50 degrees latitude matches every second post), and each water body takes one level over all its parts before any tile is written. The shared posts of neighboring DTED tiles come out bit for bit identical (`tests/test_batch.py::TestOverlappingDted::test_shared_dted_column_is_bit_identical`). A water body that reaches an edge with no neighbor in the run is listed in the log, since a neighbor transformed separately may give it a different level.
- A bare `-a` was accepted as `True`, passed the choices check and ran the spline branch; a bare `-p` meant one post. Both are now usage errors.

### Added

- `-c` / `--containment`: a flat area counts as a water body only when at least this share of its boundary posts (default 0.8) lie above it in the input. Flattening every plateau of whole meters turned the contour bands of every gentle DTED slope into "water bodies": on the sample DTED2 tile 54 of the 57 flat areas are such bands, with 40 to 80% of their boundary above them, and one is a hilltop with none; the two large basins and the coastal flats of the Copernicus tile all measure above 80%. Ocean neighbors are neutral, so lagoons and river mouths qualify; the share is summed over every part of a water body in a batch run, so both sides of a seam reach the same verdict; `-c 0` keeps every flat area. Areas left as terrain are counted in the log and stay out of the mask, the report and the table.
- `--context FOLDER` (repeatable): neighboring tiles analyzed in the first pass but not transformed, so a producer who holds the neighboring source tiles computes the level a run over all of them would. A whole delivery folder can be given: it is searched like `-i`, every DEM's placement is read from its header, and only the tiles that adjoin the run (directly or through other context tiles) are analyzed.
- `--export-water-levels FILE` and `--water-levels FILE`: a CSV of the level of every water body that touches a tile edge, keyed by the edge crossing, written by a run over a larger area and read by a later run over part of it, so that levels do not depend on the order of production. An imported level can lower a water body but never raise it above the level found in the run, and a table that disagrees with the run is reported. The ArcGIS Pro toolbox gains Context Folder, Water Levels Table and Minimum Containment parameters, refuses a non-bilinear algorithm for DTED input in validation, and its tool help (`arcgis/EGMTransToolbox.Tool.pyt.xml`) now describes every parameter, Save Log File included, and no longer calls the grids "one arc second".
- `batch.run_batch`, the two-pass runner, shared by the command line and the toolbox (which had its own copy of the loop); `tiling.py`, the seam matching, merging and report; `transform.analyze_tile` and the stages `load_input`, `compute_transformed`, `flatten_tile` and `write_output` it shares with the transform; `file_utils.find_dems`; `io.round_half_away`.
- A per-file containment line, and the report of water bodies touching the boundary of a run.

### Changed

- **DTED output requires the bilinear algorithm.** DTED tiles are edge-matched products, and only bilinear gives the same correction at a shared post whatever the tile extent: the thin plate spline is solved over the clipped grid of each tile, so it differs at every shared post, and Delaunay differs off grid lines. Even millimeters matter after rounding to whole meters: measured on a DTED2 tile, spline and bilinear disagree by 1 m at 0.067% of posts (about 9,000 per tile) and Delaunay and bilinear at 0.032%, from differences of 1 to 14 mm in the correction. `-a spline`, `delaunay` and `proj` are refused for DTED output (`cli.DTED_REQUIRES_BILINEAR` switches the refusal to a warning); GeoTIFF keeps all four, and a batch GeoTIFF run with another algorithm warns once that its tiles will not match bilinear tiles at shared posts.
- A batch no longer stops at the first tile that fails: the tile is reported, its untransformed copy is removed from the output tree, the run goes on, and the exit code is 1. The prompts a run can need are asked once, before anything is copied.
- `-p` applies to DTED as well as GeoTIFF.
- Every candidate DEM, DTED included, is opened with GDAL when a folder is searched, and must carry a geotransform; a file that cannot be opened is skipped instead of failing the run. The name-based exclusions stay, since a TanDEM-X Height Error Map is a single Float32 band with the DEM's georeferencing and only its name tells the two apart.
- The containment line also counts shore posts that were already below the water in the input (outlets, dam faces, dipping shores), which are left as they are.
- The DTED write rounds explicitly (`io.round_half_away`, halves away from zero, as GDAL does), so the value in the file is the one the log lines compared.
- The advice to run one container per tile is withdrawn where water bodies cross tile edges: the tiles that share a lake or river belong in one run, or the neighbors in `--context`.

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

- **CLI single-file output created a directory at the output path.** `main()` classified any legal output name as a folder (`is_valid_filename` accepts every legal basename), and tested that before the file case, so the log path was derived as `out.dt2/out.dt2_transform.log` and `setup_logger` created `out.dt2` as a **directory**. The transform then opened that directory in update mode, which GDAL reports on Windows as `<path>: Permission denied` — right after "Updating vertical datum to …". Present since 1.1.0; it affected every single-file CLI run with logging on (GeoTIFF as well as DTED) and went unnoticed because single-file testing went through the ArcGIS Pro toolbox, which had the correct ordering. Batch folder→folder mode was unaffected.
- **DTED void pixels were written as 0 m.** Nodata is converted to `NaN` during processing and GDAL maps `NaN` to 0 when writing a float array into an Int16 band, so voids came out at sea level. They are now restored to -32767 (`io.restore_nodata`). `samples/03n008e_SRTM.dt2` alone has 23,378 affected pixels — **DTED outputs produced by earlier versions should be regenerated.**
- **Scale/offset correction was silently discarded** for the `bilinear`, `spline`, and `delaunay` algorithms: `input_array` was read before `apply_scale_factor` ran and never re-read, so those paths operated on raw digital numbers. The `proj` algorithm was unaffected because it re-reads the file.
- **Half-pixel registration offset in the geoid resampling.** The clipped geoid points were built at cell centers but the DEM query points at cell corners, so every sample was taken half a DEM pixel to the north-west. Pinned accuracy values moved by 0.3 cm to 7.0 cm (largest at high-gradient locations) and now match the raw grid exactly at nodes; see `test_exact_node_matches_grid`.
- **Bilinear interpolation degraded to nearest-neighbor at grid edges** along *both* axes: when either axis fell outside the source grid, both were clamped and the in-range axis's interpolation was discarded. Each axis is now clamped independently.
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