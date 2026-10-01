"""DTED header support: the MIL-PRF-89020B schema, a header codec, a validator,
report renderers, the collection metadata index, the product profile and the
header assembler that fills a header from all of them.

The elevation records of a DTED file are read and written by GDAL. Everything
in this package works on the 3,428-byte header (UHL, DSI and ACC records)
directly, from raw bytes, so that no GDAL default or ``.aux.xml`` sidecar can
stand between the tool and the file.
"""

from egmtrans.dted.header import (
    AccSubregion,
    CellGeometry,
    DtedHeader,
    LevelDetection,
    cell_geometry,
    cell_id,
    detect_level,
    encode_header,
    new_header,
    parse_cell_id,
    parse_header,
    read_header,
    write_header,
)
from egmtrans.dted.index import DtedIndex, read_index, validate_index, write_index
from egmtrans.dted.profile import Profile, load_profile
from egmtrans.dted.report import header_rows, render_report
from egmtrans.dted.schema import HEADER_LENGTH, LEVELS, Field
from egmtrans.dted.validate import Issue, validate_file, validate_header, validate_records
from egmtrans.dted.writer import DtedMetadata, DtedMetadataSource, assemble_header

__all__ = [
    "AccSubregion",
    "CellGeometry",
    "DtedHeader",
    "DtedIndex",
    "DtedMetadata",
    "DtedMetadataSource",
    "Field",
    "HEADER_LENGTH",
    "Issue",
    "LEVELS",
    "LevelDetection",
    "Profile",
    "assemble_header",
    "cell_geometry",
    "cell_id",
    "detect_level",
    "encode_header",
    "header_rows",
    "load_profile",
    "new_header",
    "parse_cell_id",
    "parse_header",
    "read_header",
    "read_index",
    "render_report",
    "validate_file",
    "validate_header",
    "validate_index",
    "validate_records",
    "write_header",
    "write_index",
]
