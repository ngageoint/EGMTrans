# DTED headers: the metadata index and the product profile

The geometry fields of a DTED header follow from the raster, but the accuracies (CE90 and LE90), the edition, the dates, the producer, the security markings and the free text do not, and they differ per cell. EGMTrans takes them from two files that a producer prepares once for a whole collection:

- A **metadata index**, a GeoPackage (`.gpkg`) or GeoParquet (`.parquet`) file with one row per one-degree cell, keyed by `cell_id` (`N38E045`), built and checked with `egmtrans dted-index`, or exported by a catalog. The index is also a catalog of the collection that other services can read, filter and style: every row carries the cell polygon.
- A **product profile**, a TOML file of the values that are the same for every cell of a product. Start from [`dted_profile_template.toml`](dted_profile_template.toml): copy it, replace every `<...>` placeholder, and give the result to `--dted-profile`. A profile that still holds a placeholder is refused, so nothing of the template can reach a header.

## Precedence

When a header is written, its fields are filled in order of precedence:

1. values derived from the cell geometry, the target datum and the data (sentinels, origin, intervals, counts, corners, series, vertical and horizontal datum, partial cell indicator, the multiple-accuracy flags, the UHL copies of the security code and the vertical accuracy) can never be overridden;
2. the run's overrides (`--dted-set FIELD=VALUE`, or the DTED Header Overrides table in ArcGIS Pro: one value for every cell, for a date that must be today's or a producer code that has changed);
3. the cell's index row;
4. the profile;
5. the input file's header (for a DTED input);
6. the `--abs_horiz_accuracy` fallback, which fills its field only when it is still NA;
7. the specification's fill (NA, `0000`, blanks; the DSI unique reference is zero filled).

A cell the index does not hold, or holds twice, stops the run before anything is written, and so does an index or profile made for another DTED level. For a DTED cell made from a GeoTIFF there is no input header: the security code, the edition, the match/merge version, the producer code, the compilation date and the four accuracies must come from the index, the profile or `--abs_horiz_accuracy`, and the run stops before writing when one of them has no source (an explicit NA counts; a blank does not). A `vertical_datum` or `horizontal_datum` the profile or index states for another product is reported as a warning and the output keeps its own code. A column the index does not have is supplied by the profile: an index built by `dted-index build` leaves out every column that is NULL in all its rows. A NULL among an accuracy column's values means NA; to write NA in every cell, say so in the profile (`rel_horiz_acc = "NA"`). Every field's source is logged, the validator's warnings on the result are logged, and a DTED output whose header cannot be completed is removed rather than left with the wrong datum code over transformed heights.

Before a run writes anything, the **header plan** is logged: the index, profile and overrides in use, the first cell as the example, and every supplied field with its value and source (for a DTED-to-DTED run with an index, a profile or overrides, every field that changes). On the command line the run then asks `Write the DTED headers as planned?`; `-y` answers it. In ArcGIS Pro the plan is the first thing in the messages, and the DTED Header Summary box shows the summary as soon as an index or a profile is chosen, so a wrong value is seen before a long run starts.

## The producer code

The first two characters of the producer code (DSI 103-110) are the producing nation's FIPS 10-4 country code (MIL-PRF-89020B 3.13.4.1 i): US, UK, GM, FR, SP, NL, NO, IT, BE and so on, not the ISO code. Four ISO codes are other countries in FIPS 10-4, and EGMTrans warns about them with both readings: AU is Austria (Australia is AS), GB is Gabon (the United Kingdom is UK), SE is Seychelles (Sweden is SW), CH is China (Switzerland is SZ); DE is no FIPS 10-4 code at all (Germany is GM; the specification's own example table writes GE, which is accepted). An unknown code is a warning too. The warnings appear in the header report, in `dted-index validate`, in the header plan and on the toolbox parameters.

## Index columns

Dates are ISO dates and are written as YYMM, so they must fall in 1980-2079, the century the readers assume. The two tables are what `egmtrans dted-index columns` prints (`--format csv` gives the index columns as data for another system); a test keeps them in step with the code.

<!-- index-columns:start -->
**Index columns** (layer `dted_cells`; the number is the order the index writes them in)

| # | Column | Header field | Record | Characters | Status | Required | Index note |
|---|---|---|---|---|---|---|---|
| 1 | `cell_id` |  |  |  | key | required | text, up to 7 characters; N or S and two digits, then E or W and three digits (N38E045); the key |
| 2 | `dted_level` |  |  |  | key | optional | integer 0-2; 0, 1 or 2, the level the row describes; checked against the run |
| 3 | `security_code` | Security classification code | DSI | 4 | written | required | one character; U, R, C or S; also written to UHL 33-35 |
| 4 | `security_control` | Security control and release markings | DSI | 5-6 | written | optional | text, up to 2 characters; blank allowed |
| 5 | `security_handling` | Security handling description | DSI | 7-33 | written | optional | text, up to 27 characters |
| 6 | `unique_ref_uhl` | Unique reference number | UHL | 36-47 | written | optional | text, up to 12 characters; blank allowed |
| 7 | `unique_ref_dsi` | Unique reference number | DSI | 65-79 | written | optional | text, up to 15 characters; zero filled when nothing supplies it |
| 8 | `data_edition` | Data edition number | DSI | 88-89 | written | required | integer 1-99 |
| 9 | `match_merge_version` | Match/merge version | DSI | 90 | written | required | one character; one letter A-Z |
| 10 | `maintenance_date` | Maintenance date | DSI | 91-94 | written | optional | date YYYY-MM, 1980-2079, written as YYMM; NULL writes 0000 |
| 11 | `match_merge_date` | Match/merge date | DSI | 95-98 | written | optional | date YYYY-MM, 1980-2079, written as YYMM; NULL writes 0000 |
| 12 | `maintenance_code` | Maintenance description code | DSI | 99-102 | written | optional | text, up to 4 characters; 0000, or a letter and three digits |
| 13 | `producer_code` | Producer code | DSI | 103-110 | written | required | text, up to 8 characters; a FIPS 10-4 country code first (US, UK, GM, FR, ...), then up to six characters of the producer |
| 14 | `product_spec` | Product specification | DSI | 127-135 | written | optional | text, up to 9 characters; PRF89020B for this specification |
| 15 | `product_spec_amend` | Product specification amendment and change | DSI | 136-137 | written | optional | text, up to 2 characters; two digits; 00 |
| 16 | `product_spec_date` | Product specification date | DSI | 138-141 | written | optional | date YYYY-MM, 1980-2079, written as YYMM; 2000-05 for this specification |
| 17 | `vertical_datum` | Vertical datum code | DSI | 142-144 | check only | optional | text, up to 3 characters; MSL, E96 or E08; compared with the output, which is always E96 |
| 18 | `horizontal_datum` | Horizontal datum code | DSI | 145-149 | check only | optional | text, up to 5 characters; WGS84; compared with the output |
| 19 | `digitizing_system` | Digitizing/collection system | DSI | 150-159 | written | optional | text, up to 10 characters |
| 20 | `compilation_date` | Compilation date | DSI | 160-163 | written | required | date YYYY-MM, 1980-2079, written as YYMM; the month the cell is made; usually given at run time with --dted-set compilation_date=today |
| 21 | `abs_horiz_acc` | Absolute horizontal accuracy (CE90, m) | ACC | 4-7 | written | required | whole meters 0-9999; NULL means NA and never falls through to the profile |
| 22 | `abs_vert_acc` | Absolute vertical accuracy (LE90, m) | ACC | 8-11 | written | required | whole meters 0-9999; NULL means NA and never falls through to the profile; also written to UHL 29-32 |
| 23 | `rel_horiz_acc` | Point-to-point horizontal accuracy (CE90, m) | ACC | 12-15 | written | required | whole meters 0-9999; NULL means NA and never falls through to the profile |
| 24 | `rel_vert_acc` | Point-to-point vertical accuracy (LE90, m) | ACC | 16-19 | written | required | whole meters 0-9999; NULL means NA and never falls through to the profile |
| 25 | `acc_nima_reserved` | Reserved for NIMA use | ACC | 24 | written | optional | one character |
| 26 | `dsi_nima_text` | Reserved for NIMA use | DSI | 292-392 | written | optional | text, up to 101 characters |
| 27 | `dsi_producer_text` | Reserved for producing nation use | DSI | 393-492 | written | optional | text, up to 100 characters |
| 28 | `dsi_free_text` | Free text comments | DSI | 493-648 | written | optional | text, up to 156 characters |
| 29 | `partial_cell` |  |  |  | catalog | optional | integer 0-99; the header gets its own value from the data |
| 30 | `source_id` |  |  |  | catalog | optional | text, up to 64 characters |
| 31 | `source_file` |  |  |  | catalog | optional | text, up to 255 characters |
| 32 | `source_metadata_file` |  |  |  | catalog | optional | text, up to 255 characters |
| 33 | `source_date` |  |  |  | catalog | optional | date, any year |
| 34 | `source_version` |  |  |  | catalog | optional | text, up to 32 characters |
| 35 | `qc_status` |  |  |  | catalog | optional | text, up to 32 characters |
| 36 | `notes` |  |  |  | catalog | optional | text, up to 1024 characters |
| 37 | `updated` |  |  |  | catalog | optional | text, up to 32 characters; ISO time of the row's last write |

**Header fields EGMTrans fills itself** (no index column: the value comes from the cell, the data, the subregions layer or the standard)

| # | Header field | Record | Characters | Value |
|---|---|---|---|---|
| 1 | Recognition sentinel | UHL | 1-3 | fixed by the standard: `UHL` |
| 2 | Fixed by standard | UHL | 4 | fixed by the standard: `1` |
| 3 | Longitude of origin | UHL | 5-12 | derived from the cell geometry |
| 4 | Latitude of origin | UHL | 13-20 | derived from the cell geometry |
| 5 | Longitude data interval | UHL | 21-24 | derived from the cell geometry |
| 6 | Latitude data interval | UHL | 25-28 | derived from the cell geometry |
| 7 | Absolute vertical accuracy (LE90, m) | UHL | 29-32 | copied from the ACC absolute vertical accuracy |
| 8 | Security code | UHL | 33-35 | copied from the DSI security code |
| 9 | Number of longitude lines | UHL | 48-51 | derived from the cell geometry |
| 10 | Number of latitude points | UHL | 52-55 | derived from the cell geometry |
| 11 | Multiple accuracy | UHL | 56 | from the subregions layer `dted_acc_subregions`; the no-subregion value without one |
| 12 | Reserved | UHL | 57-80 | blank (reserved) |
| 13 | Recognition sentinel | DSI | 1-3 | fixed by the standard: `DSI` |
| 14 | Reserved | DSI | 34-59 | blank (reserved) |
| 15 | NIMA series designator | DSI | 60-64 | derived from the level: `DTED0`, `DTED1` or `DTED2` |
| 16 | Reserved | DSI | 80-87 | blank (reserved) |
| 17 | Reserved | DSI | 111-126 | blank (reserved) |
| 18 | Reserved | DSI | 164-185 | blank (reserved) |
| 19 | Latitude of origin | DSI | 186-194 | derived from the cell geometry |
| 20 | Longitude of origin | DSI | 195-204 | derived from the cell geometry |
| 21 | Latitude of SW corner | DSI | 205-211 | derived from the cell geometry |
| 22 | Longitude of SW corner | DSI | 212-219 | derived from the cell geometry |
| 23 | Latitude of NW corner | DSI | 220-226 | derived from the cell geometry |
| 24 | Longitude of NW corner | DSI | 227-234 | derived from the cell geometry |
| 25 | Latitude of NE corner | DSI | 235-241 | derived from the cell geometry |
| 26 | Longitude of NE corner | DSI | 242-249 | derived from the cell geometry |
| 27 | Latitude of SE corner | DSI | 250-256 | derived from the cell geometry |
| 28 | Longitude of SE corner | DSI | 257-264 | derived from the cell geometry |
| 29 | Clockwise orientation angle | DSI | 265-273 | fixed by the standard: `0000000.0` |
| 30 | Latitude interval | DSI | 274-277 | derived from the cell geometry |
| 31 | Longitude interval | DSI | 278-281 | derived from the cell geometry |
| 32 | Number of latitude lines | DSI | 282-285 | derived from the cell geometry |
| 33 | Number of longitude lines | DSI | 286-289 | derived from the cell geometry |
| 34 | Partial cell indicator | DSI | 290-291 | derived from the data: the share of void posts |
| 35 | Recognition sentinel | ACC | 1-3 | fixed by the standard: `ACC` |
| 36 | Reserved | ACC | 20-23 | blank (reserved) |
| 37 | Reserved | ACC | 25-55 | blank (reserved) |
| 38 | Multiple accuracy outline flag | ACC | 56-57 | from the subregions layer `dted_acc_subregions`; the no-subregion value without one |
| 39 | Accuracy subregion descriptions | ACC | 58-2613 | from the subregions layer `dted_acc_subregions`; the no-subregion value without one |
| 40 | Reserved for NIMA use | ACC | 2614-2631 | blank (reserved for NIMA use) |
| 41 | Reserved | ACC | 2632-2700 | blank (reserved) |
<!-- index-columns:end -->

Accuracy subregions (up to nine per cell, each with its four accuracies and an outline of 3 to 14 vertices) go in the layer `dted_acc_subregions` (`cell_id`, `seq`, the accuracies, a polygon); in a GeoParquet index they are the sibling file `<name>_subregions.parquet`. The table `dted_index_meta` (or the Parquet file's metadata) records the schema version, the level, the product and the generator. Columns the writer does not know are kept, so an index may carry whatever else a collection needs. A GeoPackage without the layer `dted_cells`, or a GeoParquet file with neither the index metadata nor a header column, is not an index and is refused.

### A complete index

An index may hold every column, NULL included, so that a producer sees in a GIS which fields have values and which the profile or the run must supply, and so that one file can serve as the template of a product. `egmtrans dted-index build --out template.gpkg --from-table cells.csv --all-columns` writes the 37 columns for the cells of `cells.csv` (a `cell_id` column; any OGR table or footprint layer does). With `--profile product.toml` the profile's constants are filled into every row where the cell has no value of its own, so the table shows what the header gets, and a header written from the complete table alone equals the one written from the sparse index and the profile.

Two rules follow from the precedence above. A NULL in one of the four accuracy columns means NA whatever the profile says: an accuracy is per-cell evidence, so a cell without a value must not inherit a product-wide number, and the build leaves an accuracy column alone when some cell fills it. A value in a row wins over the profile's constant; `dted-index validate` warns, naming the cells, when the two disagree.

The GeoPackage declares the text widths, so an edit in a GIS cannot overrun a header field. After editing, run `dted-index validate` again with the profile and the run's `--dted-set` overrides.

## Building an index

```bash
# Rows from the headers of an existing DTED collection (subregions included)
egmtrans dted-index build --out collection.gpkg --from-dted /data/dted --product DTED2

# Rows for every cell the source rasters cover, with values the profile's harvest
# mappings pull from raster tags and XML sidecars
egmtrans dted-index build --out collection.parquet --from-rasters /data/tiles --profile product.toml

# Rows from a footprint layer, then add what the DTED headers say, keeping the rest
egmtrans dted-index build --out collection.gpkg --from-table footprints.gpkg --cell-field item_name
egmtrans dted-index build --out collection.gpkg --from-dted /data/dted --update

# Rows from any attribute table (a catalog, an export): a table column named like an index
# column fills it, --map names the others, --set fills a column with one value, --prefer
# keeps one row per cell, and the import is reported (what mapped, what was dropped, what
# the profile must supply)
egmtrans dted-index build --out collection.gpkg --from-table catalog.parquet --prefer tile_version \
  --map compilation_date=creation_date --set security_code=U --profile product.toml

egmtrans dted-index validate collection.gpkg --profile product.toml --level 2

# A field the run gives every cell (the compilation date of a profile without one) is given
# to validate the same way, so the index and the profile check as the run will see them
egmtrans dted-index validate collection.gpkg --profile product.toml --level 2 --dted-set compilation_date=today
```

`dted-index validate` checks the index, the profile and the `--dted-set` overrides of the run they are meant for: the values, the keys and the subregions, the level, the producer codes against FIPS 10-4, and that every required header field comes from one of them. A field an override supplies is not reported as missing from the index.

The profile's `[harvest.tags.fields]` map index columns to raster metadata tags and `[harvest.xml.fields]` to XPath expressions in a sidecar found through `[harvest.xml] sidecar` (`{stem}`, `{name}`, `{cell}` and `{dir}` are replaced); a mapping may be a table with a `pattern` whose first group is the value, and an XML mapping may list several XPaths, tried in order until one yields a value. XPath with namespaces and predicates needs `lxml`; a sidecar that declares a DOCTYPE or entities is refused. Harvested accuracies are rounded up to whole meters. Values the build cannot find stay NULL, to be filled in any GIS or with a script, and `dted-index validate` lists what is missing.

## DTED-to-DTED transforms

When transforming DTED files, EGMTrans rewrites the output file's 3,428-byte header (the UHL, DSI and ACC records) from the input's header. The header is read and written as raw bytes, never through GDAL, so a `.aux.xml` sidecar or a driver default cannot stand between the tool and the file. Without a metadata index or profile, the header changes only in:

- **Vertical datum** (DSI characters 142-144): `E96`, the one code DTED is written in.
- **Accuracies** (ACC characters 4-19 and the UHL copy at 29-32): a value that is neither `0000`-`9999` nor NA becomes NA, and NA is written left justified (`NA  `), as section 3.13.5 of the specification requires for alpha values. The UHL absolute vertical accuracy always repeats the ACC value.
- **Absolute horizontal accuracy**: the `--abs_horiz_accuracy` value fills the field only when it is NA.
- **Bytes that are not printable**: the NUL bytes that GDAL-written headers carry where the specification wants blanks become blanks.

Every change is logged with its source, and the findings of the validator are logged as warnings, so a problem the input header had and nothing corrected is visible.

## The header report

`egmtrans dted-header` (and the *DTED Header Report* tool in ArcGIS Pro) reports every field of a DTED header as a table with the columns Start, End, Length, Title, Value and Description, one section per record, followed by the decoded accuracy subregions, a summary and the findings. Byte positions are the specification's one-based character positions, so a row can be checked against the MIL-PRF-89020B tables as printed; `--zero-based` counts from 0 as a hex editor does. The level is taken from four sources (the extension, the DSI series designator, the UHL latitude interval and the UHL latitude point count) and a disagreement is reported. A DTED0 companion file (`.avg`, `.min`, `.max`) is reported as a DTED0 file.

```bash
egmtrans dted-header N55.dt2                               # text report on stdout
egmtrans dted-header N55.dt2 --check-data                  # also check every elevation record
egmtrans dted-header E038/*.dt2 --format csv --out headers.csv
egmtrans dted-header N55.dt2 --format json --strict        # exit 1 on warnings too
```

Findings have three severities. An error breaks readers or a mandatory rule: a wrong sentinel, an interval that does not match the latitude zone (Tables I to III of the specification), counts that do not match the interval, a UHL origin that differs from the DSI, security codes that differ between UHL and DSI, a UHL vertical accuracy that differs from the ACC, flags that disagree with the subregions, a malformed date or accuracy. A warning is a deviation that readers tolerate: bytes that are not printable characters where the specification blank-fills (NUL bytes from other software, read as blanks; the elevations are not affected), NA right justified, `E08`, a product specification other than `PRF89020B`, a producer code that is not a FIPS 10-4 country code, an unset compilation date. Information notes free text in a reserved area, the elevation range, and an overall accuracy better than its worst subregion. With `--check-data`, the elevation records are checked too: the `0xAA` sentinel, the block and line counts, the checksum of every record, and the share of null posts against the partial cell indicator. Log messages go to stderr, the report to stdout (or `--out`), so a JSON or CSV report can be piped; the exit code is 1 when a file has errors (or warnings with `--strict`), 2 for a usage error.
