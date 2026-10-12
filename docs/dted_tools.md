# The DTED tools: parameters, messages and exit codes

Five tools of the toolbox prepare and check a DTED delivery beside the EGMTrans Tool: DTED Self-Test, Build DTED Index, Validate DTED Index, DTED Header Report and Build DMED. Each is also a subcommand of `egmtrans` with its own `--help`; the help beside each parameter in ArcGIS Pro and the `--help` text describe the same options. On the command line, reports go to stdout (or `--out`) and messages to stderr, so a JSON or CSV report can be piped; the exit code is 0 when clean, 1 on findings or failures, 2 on a usage error. In ArcGIS Pro the same messages land in the messages pane.

```text
egmtrans dted-selftest [--keep FOLDER] [--print-reference]
egmtrans dted-index build --out INDEX (--from-dted PATH... | --from-rasters PATH...
                     | --from-table FILE [--layer NAME] [--cell-field NAME]
                       [--map INDEX_COLUMN=TABLE_COLUMN]... [--prefer TABLE_COLUMN])
                     [--set INDEX_COLUMN=VALUE]... [--profile FILE] [--level N] [--product NAME]
                     [--update] [--all-columns]
egmtrans dted-index validate INDEX [--profile FILE] [--level N] [--dted-set FIELD=VALUE]...
egmtrans dted-header PATH... [--format text|json|csv|md] [--out PATH]
                     [--zero-based] [--check-data] [--strict]
egmtrans dmed FOLDER [--out PATH] [--check]
```

The metadata index, the product profile, the precedence of the header sources and the two column tables are explained in [dted_index.md](dted_index.md). `egmtrans dted-index columns [--format markdown|csv]` prints those tables and has no tool in ArcGIS Pro.

## DTED Self-Test

Converts two built-in synthetic tiles to DTED2, DTED1 and DTED0 from EGM2008 to EGM96 and compares the SHA-256 of every header and record block with the reference pinned in EGMTrans. The tiles are built from integer formulas and hold a lake with a low spot behind a one-post dam, an outlet to a lower lake, ocean, a void block, a flat hilltop and a row on a longitude-spacing boundary, so the resampler, the water rules, the geoid lattice, the rounding and the record writer are all exercised. A match shows that this computer reproduces the reference bytes, so the DTED it makes from real tiles matches any other computer that matches (see [determinism.md](determinism.md)). The geoid grids must be present; they are downloaded on the first run of the EGMTrans Tool. Run it after installing or updating EGMTrans, and whenever ArcGIS Pro, GDAL or numpy changes.

| ArcGIS Pro | Command line | Meaning |
|---|---|---|
| Keep Files in Folder | `--keep FOLDER` | A folder to write the synthetic tiles and the DTED files to and keep them; without it they go to a temporary folder that is removed afterwards. |

Command line only: `--print-reference` prints the hashes of this run as the reference mapping the package stores, for whoever re-pins the reference after an intended change.

**Messages.** The first line names the versions two producers compare first when their bytes differ: `EGMTrans <version>, Python <version>, GDAL <version>, numpy <version>, numba <version>` (`numba absent` without it). Then one line per file, `N50E006.dt2: header b6dda3601a86e94e..., records 3e6f7d3befac435f...: matches the reference`, or `DIFFERS from the reference`, or `no reference`; with Keep Files in Folder, `Files kept under <folder>`; and last `Self-test passed: this host reproduces the reference bytes.` or `Self-test FAILED: this host does not reproduce the reference bytes.` A test that cannot start says `The self-test could not run: <reason>`.

**Exit codes.** 0 when every file matches; 1 when a file differs or the test could not run; 2 when the geoid grids are missing.

## Build DTED Index

Builds the DTED metadata index of a delivery from the headers of its DTED files: one row per one-degree cell, keyed by `cell_id` (`N49E006`), holding the security markings, references, edition, dates, producer, product specification, datum codes, accuracies and free text the UHL, DSI and ACC records carry, with the accuracy subregions of the ACC in a second layer and the file each row came from. The Index Overrides set a value in every row. After the write, the index is validated as a run would see it, and the messages say where each required header field comes from. The index can be edited in ArcGIS Pro or any GIS; run Validate DTED Index afterwards, then give it to the EGMTrans Tool as the DTED Metadata Index.

| ArcGIS Pro | Command line | Meaning |
|---|---|---|
| DTED File or Folder | `--from-dted PATH...` | A DTED file (`.dt0`, `.dt1`, `.dt2`) or a folder of them: a delivery laid out as `DTED/E006/N49.dt2`, or any tree. Every file under the folder is found whatever the case of its name; files of another level than the one chosen are counted and skipped. In ArcGIS Pro, browse to a DTED file and click OK, or choose a DTED layer of the map; a band chosen in the browse dialog (`Band_1`) is read as its file. |
| DTED Level | `--level 0\|1\|2` | The level of the cells to index: only the files of that level are read, and an index describes one level. Left blank, the one level found under the input is taken; when several are found the build stops and reports their counts, so a folder that holds DTED1 and DTED2 cells gives one index per level. The level of a product profile stands in for a blank choice. |
| Output Index | `--out INDEX` | The index to write: a GeoPackage (`.gpkg`) or a GeoParquet file (`.parquet`, with its subregions in a sibling `_subregions.parquet`). Every column is written, NULL included, so the table can be reviewed and completed in a GIS; a NULL accuracy means NA, as the header said. |
| Index Overrides | `--set COLUMN=VALUE` (may be repeated) | Values written in every row, over what the headers hold: an index column and its value per row. Any column can be set except `cell_id`, `dted_level` and `updated`, which the build fills itself. A date takes `YYYY-MM`, `YYYY-MM-DD` or `today`; an accuracy takes `NA` or whole meters; a blank clears an optional text column. A row `vertical_datum = E96` records the datum the cells will carry after a transform to EGM96, the datum EGMTrans writes; a row `producer_code = <code>` stamps the producer's own code, FIPS 10-4 country code first. |
| DTED Product Profile | `--profile FILE` | A TOML file of the header constants of the product, as the EGMTrans Tool takes it. Its values fill the text and date fields a header left blank (never an accuracy: a header's NA is a value), its level stands in for a blank DTED Level, and the validation after the build compares the rows with it. |

Command line only: `--from-rasters` (rows for the cells that source rasters cover, with the profile's harvest mappings), `--from-table` with `--layer`, `--cell-field`, `--map` and `--prefer` (rows from any attribute table), `--product NAME` (recorded in the index), `--update` (add to an existing index instead of replacing it) and `--all-columns` (always on with `--from-dted`); see [Building an index](dted_index.md#building-an-index).

**Messages.** The first lines count the files found by level, `Found 12 DTED file(s) under <folder>: 8 .dt1, 4 .dt2`, and say what is read, `Reading 4 .dt2 file(s) for level 2; skipped: 8 .dt1`. Several levels without a DTED Level stop the build: `The input holds several DTED levels (8 .dt1, 4 .dt2); an index describes one level: give --level 1 or --level 2 (the DTED Level parameter in ArcGIS Pro)`. A file that cannot be read is an error with no row, `<file>: is too short for a DTED header (100 bytes); no row`, and two files of one cell at the level are an error naming both, `N49E006: 2 files for one cell at level 2: a/N49.dt2, b/N49.dt2; no row for the cell`; the index is still written for the other cells, `2 file(s) have no row in the index; the index holds the 10 other cell(s)`. A header value the index cannot hold is left NULL and reported once per problem with the cells concerned, `In 2 cell(s) (N49E006, N50E006): data_edition '00' is NULL in the index: ...`, and the findings of the source headers (bytes that are not printable, a producer code without a country prefix) are summarized the same way, `Source header finding in 2 cell(s) (...): WARNING DSI.security_control: ...`; neither stops the build. Every Index Overrides row is confirmed, `set in 12 cell(s): vertical_datum = E96`, and a profile's fills are listed, `filled from the profile: producer_code (12 cell(s))`. Then the index itself, `collection.gpkg: 12 cell(s), 3 subregion(s), level 2`, the validation findings (`ERROR`, `WARNING` or `INFO INDEX.<column>: ...`), a warning when the rows carry a datum code other than E96 (`vertical_datum is 'E08' in 12 cell(s) (...); the DTED EGMTrans writes is E96: --set vertical_datum=E96 (an Index Overrides row in ArcGIS Pro) records that in the index`), and the supply lines: `Required header fields the index supplies for every cell: ...`, `<field>: NULL in 3 cell(s); the profile supplies it there`, `<field>: NULL in 3 cell(s) and nothing supplies it there; a run stops at those cells`, `Required header fields the profile supplies: ...`, `Required header fields nothing supplies yet (a profile, or --dted-set at run time, must): ...`, or `Every required header field has a source.` In ArcGIS Pro the parameters are echoed first, a file without a row adds `<n> file(s) gave no row; see the messages above.`, and the run ends with `Processing completed.` or `Processing stopped with errors.`

**Exit codes.** 0 when every file gave a row and the validation found no error; 1 when the index was not written (no level chosen among several, no DTED file, no cell with a row), when the validation found an error, or when a file gave no row; 2 when no source is given, when `--map`, `--prefer`, `--layer` or `--cell-field` comes without `--from-table`, when a `--set` value is refused, or when the profile cannot be loaded.

## Validate DTED Index

Checks a DTED metadata index as a run with the same profile and overrides would see it: two rows for one cell, cell ids, values out of range or of the wrong form, the level, the accuracy subregions (count, order, vertices, outlines inside the cell), producer codes against FIPS 10-4, rows that disagree with a profile constant, and whether every required header field comes from the index, the profile or an override. Run it after editing the index, before the EGMTrans Tool reads it.

| ArcGIS Pro | Command line | Meaning |
|---|---|---|
| DTED Metadata Index | `INDEX` | The GeoPackage or GeoParquet index to check: one row per cell, as Build DTED Index writes it, a catalog exports it, or an edit in a GIS left it. |
| DTED Product Profile | `--profile FILE` | The product profile the run will use with this index: its constants stand behind the NULL rows, so the check says what nothing supplies, and the rows are compared with them. |
| DTED Header Overrides | `--dted-set FIELD=VALUE` (may be repeated) | The header overrides the run will use, the same rows as the DTED Header Overrides of the EGMTrans Tool: a header field and its value per row, counted as a source for that field. `compilation_date = today` is the usual row. |
| DTED Level | `--level 0\|1\|2` | The level the run will write. The index and the profile must be for it; left blank, the level of the index is taken. |

**Messages.** An error is what stops a run before anything is written: two rows for one cell, a bad cell id, a value out of range, a level other than the one chosen (`ERROR INDEX.dted_level: the index is for level 0, not 2`), a subregion rule broken. A warning does not stop a run: a required field NULL in some rows that the profile supplies, a producer code without a country prefix, a row that differs from a profile constant, an accuracy left NULL when the profile holds a number (the header will say NA). A profile written for another level stops the check: `The profile is for level 2, not 0`. With a profile, one error names every required header field that nothing supplies, `Neither the index, the profile nor --dted-set supplies: compilation_date (NULL in 12 of 12 cells)` (in ArcGIS Pro, `... nor the DTED Header Overrides supplies: ...`); a `compilation_date = today` row is the usual way to supply the date. The report ends with `collection.gpkg: 12 cell(s), level 2, 3 subregion(s)` and `0 error(s), 2 warning(s)`; in ArcGIS Pro with `The index is valid.` or `The index is not valid; see the messages above.`

**Exit codes.** 0 when there is no error; 1 when the check found an error, a required field nothing supplies, or a profile for another level; 2 when a `--dted-set` value is refused or the index or the profile cannot be read.

## DTED Header Report

Reports every field of the User Header Label (UHL), Data Set Identification (DSI) and Accuracy Description (ACC) records of DTED files as a table (Start, End, Length, Title, Value, Description), with the level evidence, a summary and the MIL-PRF-89020B findings (errors, warnings, information). Byte positions are the specification's one-based character positions unless zero-based counting is chosen. Accuracy subregions are decoded, and the elevation records can be checked for sentinels, block and line counts, checksums and the share of null posts. The header is read from raw bytes, so a `.aux.xml` sidecar or a GDAL default cannot hide a problem. The findings and their three severities are explained in [The header report](dted_index.md#the-header-report).

| ArcGIS Pro | Command line | Meaning |
|---|---|---|
| DTED File or Folder | `PATH...` | A DTED file (`.dt0`, `.dt1`, `.dt2`), a DTED0 companion file (`.avg`, `.min`, `.max`), or a folder of them, searched for all of those. In ArcGIS Pro, browse to a DTED file and click OK, or choose a DTED layer of the map; a band chosen in the browse dialog (`Band_1`) is read as its file. |
| Report Format | `--format text\|json\|csv\|md` | `text` (aligned columns, the default), `json` (every field with its raw and decoded value), `csv` (one row per field) or `md` (Markdown tables). |
| Report File | `--out PATH` | Where to write the report. Without it, the report goes to stdout, or to the messages in ArcGIS Pro. |
| Check Elevation Records | `--check-data` | Also check the elevation records: the `0xAA` sentinel, block and line counts, the checksum of every record, and the share of null posts against the partial cell indicator. |
| Count Byte Positions From 0 | `--zero-based` | Count byte positions from 0 as a hex editor does, instead of the specification's 1. |

Command line only: `--strict` exits with 1 on warnings as well as errors.

**Messages.** A folder is announced with its counts, `Found 12 file(s) under <folder>: 3 .dt0, 3 .avg, 3 .min, 3 .max`, then one line per file, `N49.dt2: 0 error(s), 2 warning(s)`, with the findings themselves in the report (indented under that line in ArcGIS Pro). A file that cannot be read is reported, `<path>: <reason>`, and the others are still checked. More than one file ends with `Checked 12 file(s): 2 with errors, 3 with warnings, 7 clean` (and `, 1 not readable` when it applies); a report file is confirmed with `Report written to <path>`. A missing path says `Not a file or folder: <path>`, an empty folder `No DTED file (.dt0, .dt1, .dt2, or a DTED0 .avg, .min, .max) under <folder>`.

**Exit codes.** 0 when no file has an error; 1 when a file has errors or could not be read, or has warnings with `--strict`; 2 on a usage error, a missing path or a folder without DTED files.

## Build DMED

Writes the DMED volume file of MIL-PRF-89020B 3.9.5 for a DTED delivery laid out as `DTED/E006/N49.dt2`: a header record with the bounding rectangle of the cells, then one 394-byte record per cell of the rectangle, columns west to east and south to north within a column, holding the cell's edition and match/merge version and, for each of its 16 areas of 15 minutes, the minimum, maximum and mean elevation and the standard deviation of the posts in whole meters. A cell of the rectangle that is not in the delivery is its coordinates and blanks. Void posts are left out of the statistics and an area with no valid post is blank. Run it on a finished delivery: every cell under `DTED/` is read, so the DMED describes the delivery as it stands. Rerun it after cells are added or replaced.

| ArcGIS Pro | Command line | Meaning |
|---|---|---|
| Delivery Folder | `FOLDER` | The delivery: the folder that holds the `DTED` folder of cells laid out as `DTED/E006/N49.dt2`, or that `DTED` folder itself. Masks, logs and DTED0 companion files in the tree are ignored; a cell whose name disagrees with its header, or cells of several levels, are reported. |
| DMED File | `--out PATH` | Where to write the DMED. By default `DMED` at the root of the delivery, beside the `DTED` folder, as MIL-PRF-89020B 3.9 places it. |
| Check Only | `--check` | Do not write: compare the existing DMED with what the cells give and report the differences. |

**Messages.** A write ends with `Wrote <path>: 6 records of 394 bytes for 4 cell(s) in a rectangle of 3 x 2 degrees.`; a check with `The DMED matches the cells.` or, after one line per difference, `The DMED does not match the cells.` A delivery that cannot be used (no `DTED` folder, a cell whose name and header disagree, several levels) is reported before anything is written; a DMED that cannot be written says `The DMED could not be written: <reason>`.

**Exit codes.** 0 when the DMED was written or matches; 1 when it does not match or could not be written; 2 when the delivery cannot be used.
