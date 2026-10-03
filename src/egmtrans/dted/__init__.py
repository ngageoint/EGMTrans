"""DTED support: the MIL-PRF-89020B schema, a header codec, a record codec, a
validator, report renderers, the collection metadata index, the product
profile and the header assembler that fills a header from all of them.

Everything in this package works on the file directly, from raw bytes: the
3,428-byte header (UHL, DSI and ACC records) and the elevation records that
follow it, so that no GDAL default or ``.aux.xml`` sidecar can stand between
the tool and the file.
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
from egmtrans.dted.records import (
    DecodedRecords,
    RecordError,
    decode_records,
    encode_records,
    partial_cell_indicator,
    read_edges,
    read_records,
    write_dted_file,
)
from egmtrans.dted.report import header_rows, render_report
from egmtrans.dted.resample import (
    ResampleError,
    SourceGrid,
    carry_labels,
    master_row,
    regrid_bilinear,
    thin,
)
from egmtrans.dted.schema import HEADER_LENGTH, LEVELS, Field
from egmtrans.dted.validate import Issue, validate_file, validate_header, validate_records
from egmtrans.dted.writer import DtedMetadata, DtedMetadataSource, assemble_header

__all__ = [
    "AccSubregion",
    "CellGeometry",
    "DecodedRecords",
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
    "RecordError",
    "ResampleError",
    "SourceGrid",
    "assemble_header",
    "carry_labels",
    "cell_geometry",
    "cell_id",
    "decode_records",
    "detect_level",
    "encode_header",
    "encode_records",
    "header_rows",
    "load_profile",
    "master_row",
    "new_header",
    "parse_cell_id",
    "parse_header",
    "partial_cell_indicator",
    "read_edges",
    "read_header",
    "read_index",
    "read_records",
    "regrid_bilinear",
    "render_report",
    "thin",
    "validate_file",
    "validate_header",
    "validate_index",
    "validate_records",
    "write_dted_file",
    "write_header",
    "write_index",
]
