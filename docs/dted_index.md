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

Layer `dted_cells`; dates are ISO dates and are written as YYMM, so they must fall in 1980-2079, the century the readers assume.

| Column | Header field | Notes |
|---|---|---|
| `cell_id`, `dted_level` | | The key (`N38E045`) and the level the row describes |
| `security_code` | UHL 33, DSI 4 | U, R, C or S; required |
| `security_control`, `security_handling` | DSI 5-6, 7-33 | Control and release markings, handling description |
| `unique_ref_uhl`, `unique_ref_dsi` | UHL 36-47, DSI 65-79 | Unique reference numbers |
| `data_edition`, `match_merge_version` | DSI 88-89, 90 | 1-99 and A-Z; required |
| `maintenance_date`, `match_merge_date`, `maintenance_code` | DSI 91-102 | NULL until used |
| `producer_code` | DSI 103-110 | FIPS 10-4 country code first; required |
| `product_spec`, `product_spec_amend`, `product_spec_date` | DSI 127-141 | `PRF89020B`, `00`, 2000-05 by default |
| `digitizing_system`, `compilation_date` | DSI 150-163 | Compilation date required |
| `abs_horiz_acc`, `abs_vert_acc`, `rel_horiz_acc`, `rel_vert_acc` | ACC 4-19 | Meters; NULL means NA |
| `acc_nima_reserved`, `dsi_nima_text`, `dsi_producer_text`, `dsi_free_text` | ACC 24, DSI 292-648 | Free text areas |
| `vertical_datum`, `horizontal_datum` | DSI 142-149 | Checked against the output, never written from here |
| `source_id`, `source_file`, `source_metadata_file`, `source_date`, `source_version`, `partial_cell`, `qc_status`, `notes`, `updated` | | Catalog columns the writer ignores |

Accuracy subregions (up to nine per cell, each with its four accuracies and an outline of 3 to 14 vertices) go in the layer `dted_acc_subregions` (`cell_id`, `seq`, the accuracies, a polygon); in a GeoParquet index they are the sibling file `<name>_subregions.parquet`. The table `dted_index_meta` (or the Parquet file's metadata) records the schema version, the level, the product and the generator. Columns the writer does not know are kept, so an index may carry whatever else a collection needs. A GeoPackage without the layer `dted_cells`, or a GeoParquet file with neither the index metadata nor a header column, is not an index and is refused.

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

Findings have three severities. An error breaks readers or a mandatory rule: a wrong sentinel, a byte that is not printable, an interval that does not match the latitude zone (Tables I to III of the specification), counts that do not match the interval, a UHL origin that differs from the DSI, security codes that differ between UHL and DSI, a UHL vertical accuracy that differs from the ACC, flags that disagree with the subregions, a malformed date or accuracy. A warning is a deviation that readers tolerate: NA right justified, `E08`, a product specification other than `PRF89020B`, a producer code that is not a FIPS 10-4 country code, an unset compilation date. Information notes free text in a reserved area, the elevation range, and an overall accuracy better than its worst subregion. With `--check-data`, the elevation records are checked too: the `0xAA` sentinel, the block and line counts, the checksum of every record, and the share of null posts against the partial cell indicator. Log messages go to stderr, the report to stdout (or `--out`), so a JSON or CSV report can be piped; the exit code is 1 when a file has errors (or warnings with `--strict`), 2 for a usage error.
