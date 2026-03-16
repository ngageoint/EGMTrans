#!/usr/bin/env python3
"""
Complete DTED Header Parser

This script reads DTED files and extracts ALL header metadata according to
MIL-PRF-89020B standard (STNAG 3809). It documents and displays:
- User Header Label (UHL) - 80 bytes
- Data Set Identification (DSI) - 648 bytes
- Accuracy Description (ACC) - 2700 bytes

Each field is documented with position, length, name, and detailed description.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from typing import List, Dict, Any

# --- Data Structures for Parsed Data ---

@dataclass
class FieldDefinition:
    """Defines a single field in a DTED header record."""
    start: int
    length: int
    name: str
    description: str

@dataclass
class UHLData:
    """Holds parsed data from the User Header Label (UHL) record."""
    raw_data: bytes
    parsed_fields: Dict[str, Any]

@dataclass
class DSIData:
    """Holds parsed data from the Data Set Identification (DSI) record."""
    raw_data: bytes
    parsed_fields: Dict[str, Any]

@dataclass
class ACCSubregion:
    """Holds parsed data for a single accuracy subregion."""
    raw_data: bytes
    parsed_fields: Dict[str, Any]

@dataclass
class ACCData:
    """Holds parsed data from the Accuracy Description (ACC) record."""
    raw_data: bytes
    parsed_fields: Dict[str, Any]
    subregions: List[ACCSubregion]

# --- Field Definitions ---

UHL_FIELDS: List[FieldDefinition] = [
    FieldDefinition(0, 3, "Recognition Sentinel", "User Header Label (UHL) record"),
    FieldDefinition(3, 1, "Fixed", "Fixed by standard"),
    FieldDefinition(4, 8, "Origin Longitude", "Longitude of SW corner (DDDMMSSH)"),
    FieldDefinition(12, 8, "Origin Latitude", "Latitude of SW corner (DDDMMSSH)"),
    FieldDefinition(20, 4, "Longitude Interval", "Longitude interval in tenths of seconds"),
    FieldDefinition(24, 4, "Latitude Interval", "Latitude interval in tenths of seconds"),
    FieldDefinition(28, 4, "Absolute Vertical Accuracy", "Accuracy in meters (0000-9999 or '  NA', right justified)"),
    FieldDefinition(32, 3, "Security Classification", "Security classification code (R, U, C, S), left justified"),
    FieldDefinition(35, 12, "Unique Reference", "Unique reference number"),
    FieldDefinition(47, 4, "Longitude Lines", "Number of longitude (profiles) lines for full one-degree cell"),
    FieldDefinition(51, 4, "Latitude Points", "Number of latitude data points per longitude line for full one-degree cell"),
    FieldDefinition(55, 1, "Multiple Accuracy", "Multiple accuracy flag (0=single, 1=multiple)"),
    FieldDefinition(56, 24, "Reserved", "Reserved for future use (spaces)")
]

DSI_FIELDS: List[FieldDefinition] = [
    FieldDefinition(0, 3, "Recognition Sentinel", "Data Set Identification (DSI) record"),
    FieldDefinition(3, 1, "Security Classification", "Security classification code (R, U, C, S), left justified"),
    FieldDefinition(4, 2, "Security Control Markings", "Security control and release markings (for DoD use only)"),
    FieldDefinition(6, 27, "Security Handling Description", "Security handling description or other security description (free text or blank filled)"),
    FieldDefinition(33, 26, "Reserved", "Reserved for future use (blank filled)"),
    FieldDefinition(59, 5, "NIMA Series", "NIMA series designator for product series"),
    FieldDefinition(64, 15, "Unique Reference Number", "Unique reference number for producing nations' own use (free text or blank filled)"),
    FieldDefinition(79, 8, "Reserved", "Reserved for future use (blank filled)"),
    FieldDefinition(87, 2, "Data Edition Number", "Data edition number"),
    FieldDefinition(89, 1, "Match/Merge Version", "Match/merge version character"),
    FieldDefinition(90, 4, "Maintenance Date", "Date of last maintenance (zero filled until used)"),
    FieldDefinition(94, 4, "Match/Merge Date", "Match/merge date (zero filled until used)"),
    FieldDefinition(98, 4, "Maintenance Description Code", "Maintenance description code (zero filled until used)"),
    FieldDefinition(102, 8, "Producer Code", "Producer code (country-free text, FIPS 10-4 Country Codes used for first 2 characters)"),
    FieldDefinition(110, 16, "Reserved", "Reserved for future use (blank filled)"),
    FieldDefinition(126, 9, "Product Specification", "Product specification (alphanumeric field)"),
    FieldDefinition(135, 2, "Product Specification Version", "First digit is Product Specification Amendment Number and second digit is the Change Number."),
    FieldDefinition(137, 4, "Product Specification Date", "Date of product specification (YYMM)"),
    FieldDefinition(141, 3, "Vertical Datum", "Vertical datum code"),
    FieldDefinition(144, 5, "Horizontal Datum", "Horizontal datum code"),
    FieldDefinition(149, 10, "Digitizing Collection System", "Collection system used for digitizing (free text)"),
    FieldDefinition(159, 4, "Compilation Date", "Date of compilation, most descriptive year/month (YYMM)"),
    FieldDefinition(163, 22, "Reserved", "Reserved for future use (blank filled)"),
    FieldDefinition(185, 9, "Origin Latitude", "Latitude of SW corner (DDMMSS.SH)"),
    FieldDefinition(194, 10, "Origin Longitude", "Longitude of SW corner (DDDMMSS.SH)"),
    FieldDefinition(204, 7, "Southwest Corner Latitude", "SW corner latitude (DDMMSS)"),
    FieldDefinition(211, 8, "Southwest Corner Longitude", "SW corner longitude (DDDMMSS)"),
    FieldDefinition(219, 7, "Northwest Corner Latitude", "NW corner latitude (DDMMSS)"),
    FieldDefinition(226, 8, "Northwest Corner Longitude", "NW corner longitude (DDDMMSS)"),
    FieldDefinition(234, 7, "Northeast Corner Latitude", "NE corner latitude (DDMMSS)"),
    FieldDefinition(241, 8, "Northeast Corner Longitude", "NE corner longitude (DDDMMSS)"),
    FieldDefinition(249, 7, "Southeast Corner Latitude", "SE corner latitude (DDMMSS)"),
    FieldDefinition(256, 8, "Southeast Corner Longitude", "SE corner longitude (DDDMMSS)"),
    FieldDefinition(264, 9, "Clockwise Orientation", "Clockwise orientation angle"),
    FieldDefinition(273, 4, "Latitude Interval", "Latitude interval in tenths of seconds"),
    FieldDefinition(277, 4, "Longitude Interval", "Longitude interval in tenths of seconds"),
    FieldDefinition(281, 4, "Number of Latitude Lines", "Number of latitude lines"),
    FieldDefinition(285, 4, "Number of Longitude Lines", "Number of longitude lines"),
    FieldDefinition(289, 2, "Partial Cell Indicator", "00 = Complete one-degree cell, 01-99 = Percentage of data coverage."),
    FieldDefinition(291, 101, "Reserved", "Reserved for NIMA use only (free text or blank filled)"),
    FieldDefinition(392, 100, "Reserved", "Reserved for producing nation use only (free text or blank filled)"),
    FieldDefinition(492, 156, "Reserved", "Reserved for free text coments (free text or blank filled)")
]

ACC_FIELDS: List[FieldDefinition] = [
    FieldDefinition(0, 3, "Recognition Sentinel", "Accuracy Description (ACC) record"),
    FieldDefinition(3, 4, "Absolute Horizontal Accuracy", "Absolute horizontal accuracy (meters)"),
    FieldDefinition(7, 4, "Absolute Vertical Accuracy", "Absolute vertical accuracy (meters)"),
    FieldDefinition(11, 4, "Relative Horizontal Accuracy", "Relative (point-to-point) horizontal accuracy (meters)"),
    FieldDefinition(15, 4, "Relative Vertical Accuracy", "Relative (point-to-point) vertical accuracy (meters)"),
    FieldDefinition(19, 4, "Reserved", "Reserved for future use (blank filled)"),
    FieldDefinition(23, 1, "Reserved", "Reserved for NIMA use only (blank filled)"),
    FieldDefinition(24, 31, "Reserved", "Reserved for future use (blank filled)"),
    FieldDefinition(55, 2, "Multiple Accuracy Flag", "Multiple accuracy outline flag (00 = No accuracy subregions provided, 02-09 = Number of subregions per one-degree cell (maximum 9)."),
]

# Each subregion is 287 bytes. This definition is repeated for each subregion.
# NOTE: These subregion fields have NOT been validated against the standard because data that use them are not available.
ACC_SUBREGION_FIELDS: List[FieldDefinition] = [
    FieldDefinition(0, 4, "Absolute Horizontal Accuracy", "Absolute horizontal accuracy (meters)"),
    FieldDefinition(4, 4, "Absolute Vertical Accuracy", "Absolute vertical accuracy (meters)"),
    FieldDefinition(8, 4, "Relative Horizontal Accuracy", "Relative (point-to-point) horizontal accuracy (meters)"),
    FieldDefinition(12, 4, "Relative Vertical Accuracy", "Relative (point-to-point) vertical accuracy (meters)"),
    FieldDefinition(16, 8, "SW Corner Latitude", "Subregion SW corner latitude (DDMMSS)"),
    FieldDefinition(24, 8, "SW Corner Longitude", "Subregion SW corner longitude (DDDMMSS)"),
    FieldDefinition(32, 8, "NW Corner Latitude", "Subregion NW corner latitude (DDMMSS)"),
    FieldDefinition(40, 8, "NW Corner Longitude", "Subregion NW corner longitude (DDDMMSS)"),
    FieldDefinition(48, 8, "NE Corner Latitude", "Subregion NE corner latitude (DDMMSS)"),
    FieldDefinition(56, 8, "NE Corner Longitude", "Subregion NE corner longitude (DDDMMSS)"),
    FieldDefinition(64, 8, "SE Corner Latitude", "Subregion SE corner latitude (DDMMSS)"),
    FieldDefinition(72, 8, "SE Corner Longitude", "Subregion SE corner longitude (DDDMMSS)"),
    # The remaining 207 bytes are reserved.
    FieldDefinition(80, 207, "Reserved", "Reserved for future use."),
]

# --- Helper Functions ---

def _extract_ascii(data: bytes, start: int, length: int) -> str:
    """Extracts and decodes an ASCII field from a byte string."""
    try:
        field = data[start : start + length]
        return field.decode('ascii').strip()
    except (UnicodeDecodeError, IndexError):
        return "INVALID_ASCII"

# --- Parsing Logic ---

def _parse_generic_record(data: bytes, field_defs: List[FieldDefinition]) -> Dict[str, Any]:
    """A generic parser for any record type given field definitions."""
    parsed_data = {}
    for field in field_defs:
        raw_value = _extract_ascii(data, field.start, field.length)
        parsed_data[field.name] = raw_value if raw_value else "[BLANK]"
    return parsed_data

def parse_uhl_record(uhl_data: bytes) -> UHLData:
    """Parses the User Header Label (UHL) record."""
    return UHLData(
        raw_data=uhl_data,
        parsed_fields=_parse_generic_record(uhl_data, UHL_FIELDS)
    )

def parse_dsi_record(dsi_data: bytes) -> DSIData:
    """Parses the Data Set Identification (DSI) record."""
    return DSIData(
        raw_data=dsi_data,
        parsed_fields=_parse_generic_record(dsi_data, DSI_FIELDS)
    )

def parse_acc_record(acc_data: bytes) -> ACCData:
    """
    Parses the Accuracy Description (ACC) record, including all subregions.
    """
    header_data = acc_data[:57]
    parsed_fields = _parse_generic_record(header_data, ACC_FIELDS)
    
    subregions = []
    num_subregions_str = parsed_fields.get("Accuracy Subregions", "00")
    if num_subregions_str.isdigit():
        num_subregions = int(num_subregions_str)
        subregion_block = acc_data[57:] # Data starts after the 57-byte header
        
        for i in range(num_subregions):
            start = i * 287
            end = start + 287
            if end > len(subregion_block):
                break # Avoid reading past the end of the data
            
            subregion_raw = subregion_block[start:end]
            subregion_parsed = _parse_generic_record(subregion_raw, ACC_SUBREGION_FIELDS)
            subregions.append(ACCSubregion(raw_data=subregion_raw, parsed_fields=subregion_parsed))

    return ACCData(
        raw_data=acc_data,
        parsed_fields=parsed_fields,
        subregions=subregions
    )

# --- Presentation Logic ---

def _display_record(
    title: str,
    total_bytes: int,
    field_defs: List[FieldDefinition],
    parsed_data: Dict[str, Any]
) -> None:
    """Generic function to display a parsed record."""
    print("\n" + "=" * 80)
    print(f"{title.upper()} - {total_bytes} bytes")
    print("=" * 80)
    
    for field in field_defs:
        value = parsed_data.get(field.name, "NOT_FOUND")
        end_byte = field.start + field.length - 1
        print(f"Bytes {field.start:3d}-{end_byte:3d} ({field.length:3d}): {field.name:<40} = {value}")
        if field.description:
            print(f"        Description: {field.description}")

def display_uhl_record(uhl: UHLData) -> None:
    """Displays the parsed UHL data."""
    _display_record("User Header Label (UHL)", 80, UHL_FIELDS, uhl.parsed_fields)

def display_dsi_record(dsi: DSIData) -> None:
    """Displays the parsed DSI data."""
    _display_record("Data Set Identification (DSI)", 648, DSI_FIELDS, dsi.parsed_fields)

def display_acc_record(acc: ACCData) -> None:
    """Displays the parsed ACC data, including subregions."""
    _display_record("Accuracy Description (ACC)", 2700, ACC_FIELDS, acc.parsed_fields)
    
    if acc.subregions:
        print("\n" + "-" * 80)
        print(f"ACCURACY SUBREGIONS ({len(acc.subregions)} found)")
        print("-" * 80)
        for i, subregion in enumerate(acc.subregions):
            title = f"Subregion {i + 1}"
            print(f"\n--- {title} ---")
            for field in ACC_SUBREGION_FIELDS:
                value = subregion.parsed_fields.get(field.name, "NOT_FOUND")
                # Don't print the large reserved field to keep output clean
                if field.name == "Reserved":
                    continue
                print(f"    {field.name:<30} = {value}")

# --- Main Application Logic ---

def analyze_dted_file(filepath: str) -> None:
    """
    Analyzes a complete DTED file and extracts all header information.
    """
    print(f"\n{'='*80}")
    print(f"DTED FILE ANALYSIS: {os.path.basename(filepath)}")
    print(f"Full Path: {filepath}")
    print(f"{'='*80}")

    try:
        with open(filepath, 'rb') as f:
            file_size = os.fstat(f.fileno()).st_size
            print(f"File Size: {file_size:,} bytes")

            # Read and parse UHL
            uhl_data_raw = f.read(80)
            if len(uhl_data_raw) < 80:
                print("ERROR: File too short for UHL record.", file=sys.stderr)
                return
            uhl_data = parse_uhl_record(uhl_data_raw)
            display_uhl_record(uhl_data)

            # Read and parse DSI
            dsi_data_raw = f.read(648)
            if len(dsi_data_raw) < 648:
                print("ERROR: File too short for DSI record.", file=sys.stderr)
                return
            dsi_data = parse_dsi_record(dsi_data_raw)
            display_dsi_record(dsi_data)

            # Read and parse ACC
            acc_data_raw = f.read(2700)
            if len(acc_data_raw) < 2700:
                print("ERROR: File too short for ACC record.", file=sys.stderr)
                return
            acc_data = parse_acc_record(acc_data_raw)
            display_acc_record(acc_data)

            # Display Summary
            print(f"\n{'='*80}")
            print("SUMMARY")
            print(f"{'='*80}")
            print(f"UHL Recognition Sentinel: {uhl_data.parsed_fields.get('Recognition Sentinel', 'N/A')}")
            print(f"DSI Recognition Sentinel: {dsi_data.parsed_fields.get('Recognition Sentinel', 'N/A')}")
            print(f"ACC Recognition Sentinel: {acc_data.parsed_fields.get('Recognition Sentinel', 'N/A')}")
            print(f"Security Classification: {dsi_data.parsed_fields.get('Security Classification', 'N/A')}")
            print(f"Vertical Datum: {dsi_data.parsed_fields.get('Vertical Datum', 'N/A')}")
            print(f"Horizontal Datum: {dsi_data.parsed_fields.get('Horizontal Datum', 'N/A')}")
            print(f"Absolute Horizontal Accuracy: {acc_data.parsed_fields.get('Absolute Horizontal Accuracy', 'N/A')}")
            print(f"Absolute Vertical Accuracy: {acc_data.parsed_fields.get('Absolute Vertical Accuracy', 'N/A')}")
            print(f"Relative Horizontal Accuracy: {acc_data.parsed_fields.get('Relative Horizontal Accuracy', 'N/A')}")
            print(f"Relative Vertical Accuracy: {acc_data.parsed_fields.get('Relative Vertical Accuracy', 'N/A')}")

    except FileNotFoundError:
        print(f"ERROR: File not found at {filepath}", file=sys.stderr)
    except IOError as e:
        print(f"ERROR: Cannot read file {filepath}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: An unexpected error occurred while processing {filepath}: {e}", file=sys.stderr)

def main() -> None:
    """
    Main function to parse command-line arguments and initiate analysis.
    """
    parser = argparse.ArgumentParser(
        description="A tool to parse and display headers of DTED (Digital Terrain Elevation Data) files.",
        epilog="Example: python dted_full_header_parser.py /path/to/your/dted_file.dt2"
    )
    parser.add_argument(
        "dted_file",
        type=str,
        help="The path to the DTED file to be analyzed."
    )
    
    args = parser.parse_args()
    
    if not os.path.isfile(args.dted_file):
        print(f"ERROR: The file '{args.dted_file}' does not exist or is not a file.", file=sys.stderr)
        sys.exit(1)
        
    analyze_dted_file(args.dted_file)

if __name__ == "__main__":
    main()
