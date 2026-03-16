#!/usr/bin/env python3
"""
Script to lookup EPSG codes from Esri WKT strings using GDAL's built-in
translation capabilities.
"""

import re
import sys
from osgeo import osr

def lookup_epsg_from_esri_wkt(wkt_string):
    """
    Main function to lookup EPSG code from Esri WKT string using GDAL.
    """
    try:
        # The WKT string from the command line might be wrapped in single quotes
        # and have its internal double quotes stripped. GDAL is flexible enough
        # to handle this if we pass it directly.
        wkt_string = wkt_string.strip("'")

        srs = osr.SpatialReference()
        result = srs.ImportFromWkt(wkt_string)
        
        if result != 0:
            return {"error": f"Failed to import WKT. GDAL error code: {result}"}

        srs.AutoIdentifyEPSG()
        epsg_code = srs.GetAuthorityCode(None)
        
        if epsg_code:
            # Try to parse the name for display purposes, but don't fail if it doesn't work.
            name = "Unknown"
            match = re.match(r'^\s*\'?(PROJCS|GEOGCS|VERTCS)\[([^,]+),', wkt_string)
            if match:
                name = match.group(2).strip("'")

            wkt2_string = srs.ExportToWkt(['FORMAT=WKT2_2019', 'MULTILINE=YES'])
            return {
                "epsg_code": f"EPSG:{epsg_code}",
                "wkt2_2019": wkt2_string,
                "name": name,
                "type": "PROJCS" if "PROJCS" in wkt_string else "GEOGCS",
                "method": "AutoIdentifyEPSG"
            }
        else:
            return {"error": "Could not identify EPSG code after import."}
            
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}

def main():
    """
    Command line interface for the script.
    """
    if len(sys.argv) != 2:
        print("Usage: python esri_wkt_lookup.py '<ESRI_WKT_STRING>'")
        sys.exit(1)
    
    wkt_string = sys.argv[1]
    
    result = lookup_epsg_from_esri_wkt(wkt_string)
    
    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)
    else:
        print(f"\n--- Success ---")
        print(f"Name: {result['name']}")
        print(f"Type: {result['type']}")
        print(f"EPSG Code: {result['epsg_code']}")
        print(f"Method: {result['method']}")
        print(f"WKT2_2019:")
        print(result['wkt2_2019'])

if __name__ == "__main__":
    main()