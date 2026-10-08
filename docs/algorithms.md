# Interpolation algorithms

The geoid correction at every post is resampled from NGA's one-arc-minute EGM96 and EGM2008 grids (about 1.8 km between nodes), then applied to the heights.

- **bilinear** (default): fast, memory-efficient, and the one method DTED output accepts.
- **spline**: thin plate spline, for a single GeoTIFF that need not match its neighbors, in an area with complex geoid variations; it follows the curvature of the geoid between grid nodes and differs from bilinear by a few millimeters. Significantly slower.
- **delaunay**: triangulation-based linear interpolation; differs from bilinear off the grid lines. Slower than bilinear.
- **proj** (command line only): GDAL's own vertical transformation, forced onto the same one-arc-minute grids; it agrees with bilinear to within the 1 cm output rounding. Not available in ArcGIS Pro.

**DTED output accepts only bilinear.** DTED tiles are edge-matched products, and only bilinear gives the same correction at a shared post whatever the tile extent: the thin plate spline is solved over the clipped grid of each tile, so its result differs at every shared post, and Delaunay differs off the grid lines. Even millimeters matter once heights are rounded to whole meters: on a DTED2 tile, spline and bilinear disagree by 1 m at 0.067% of posts (about 9,000 per tile) and Delaunay and bilinear at 0.032%, from differences of 1 to 14 mm in the correction, so tiles transformed with different algorithms would not edge-match. GeoTIFF tiles that must edge-match (DGED, Copernicus, TanDEM-X) should use bilinear for the same reason; a batch run with another algorithm says so once.

For a DTED cell made from a GeoTIFF on the whole-degree lattice, the correction is evaluated on the arc-minute lattice of the grids with whole-number weights, so the value is exact and the same on every computer (see [determinism.md](determinism.md)).

## The grids

The one-arc-minute EGM96 and EGM2008 grids shipped with EGMTrans were computed directly from the published spherical harmonic coefficients with NGA's own Fortran executables (`hsynth_WGS84`, `f477_bin`, `clenqt_bin`, distributed by NGA's Office of Geomatics at <https://earth-info.nga.mil>), not interpolated up from the lower-resolution published grids, and written as Cloud Optimized GeoTIFFs; geoid undulations are rounded to the nearest millimeter. The EGM2008 grid was validated against the independent one-arc-minute file provided to NGA by a lead author of EGM2008; the two agree to within millimeters globally.

PROJ registers lower-resolution grids for EPSG:5773 and EPSG:3855 (15 arc minutes for EGM96, 2.5 for EGM2008; errors of more than 0.5 m have been observed between the sparse EGM96 posts), so left to itself the `proj` option would resample those. EGMTrans overrides the grids it uses, so every algorithm reads the one-arc-minute grids. Each grid file is pinned to a SHA-256 hash in `src/egmtrans/download.py` and listed in `datums/README.md`; the download verifies the hash and deletes a file that fails, and a DTED conversion checks the grids it reads before a cell is written.
