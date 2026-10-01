#!/usr/bin/env python3
"""Deprecated: the DTED header report now lives in the egmtrans package.

Run ``egmtrans dted-header FILE...`` (or ``python EGMTrans.py dted-header
FILE...`` from a plain clone). This shim forwards to it and will be removed in
the release after 1.7.0.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from egmtrans.cli_dted import main  # noqa: E402

if __name__ == '__main__':
    print('crs/dted_header_parser.py is deprecated; use: egmtrans dted-header FILE...', file=sys.stderr)
    sys.exit(main(['dted-header', *sys.argv[1:]]))
