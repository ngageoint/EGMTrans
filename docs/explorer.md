# EGMTrans Explorer

The EGMTrans Explorer is a map project in two formats, ArcGIS Pro (`EGMTrans_Explorer.aprx`) and QGIS (`EGMTrans_Explorer.qgz`), for visualizing the geoids and checking datum transformations done by EGMTrans or other software.

**EGM2008 shaded relief**  
<img src="../img/EGM2008_shaded_relief.png" alt="EGM2008 shaded relief" width="800">

It renders the EGM2008 and EGM96 Cloud Optimized GeoTIFFs as grids and as color relief. The datum grids show the horizontal resolution of the one-arc-minute grids against the standard EGM products, 15 arc minutes for EGM96 and 2.5 arc minutes for EGM2008; zoomed out, a hillshade over the color relief gives the geoid models depth. A one-arc-minute delta grid, EGM2008 minus EGM96, compares the datums with each other and with any DEM loaded into the map. An OpenStreetMap basemap gives context; it, and the locators, need the internet.

The Explorer offers:

- an interactive display of the EGM96 and EGM2008 undulations;
- the difference between the two geoids;
- a comparison of original and transformed DEMs;
- a comparison of DEMs and point clouds against a reference elevation to find datum errors.

**EGM96 to EGM2008 delta**  
<img src="../img/EGM96_to_EGM2008_delta.png" alt="EGM96 to EGM2008 delta" width="800">

**EGM96 15' (black) and EGM2008 2.5' (gray) grids**  
<img src="../img/datum_grids_example.png" alt="EGM96 (black) and EGM2008 (gray) grids" width="400">

## Using it

1. Put the three Explorer grids in `datums/` beside the two transform grids (see the [README](../README.md), "Offline setup"): `egm96_to_egm2008_delta.tif`, `us_nga_egm08_25.tif`, `us_nga_egm96_15.tif`. A project opened before the grids are there shows red "!" icons on the geoid layers; close and reopen it once they are.
2. Open `EGMTrans_Explorer.aprx` in ArcGIS Pro or `EGMTrans_Explorer.qgz` in QGIS.
3. Load your elevation datasets (DEMs and point clouds) to compare them with the geoid heights.
4. Use the analysis tools of the application to compare datums, calculate differences and generate statistics; customize the symbology as you need; export with the standard tools.
5. In QGIS, the Value Tool plugin (<https://plugins.qgis.org/plugins/valuetool/>) queries every raster turned on in the map at the cursor.

**QGIS Value Tool plugin**  
<img src="../img/qgis_value_tool_plugin.png" alt="QGIS Value Tool plugin" width="600">
