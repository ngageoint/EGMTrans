#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# Project: EGMTrans
# Author: Eric Robeck
#
# Copyright (c) 2025, National Geospatial-Intelligence Agency
# Licensed under the MIT License
# ******************************************************************************

"""Backward-compatibility shim. Re-exports the public API from the egmtrans package."""

import sys
import os

_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from egmtrans import *  # noqa: F401, F403
from egmtrans.cli import main  # noqa: F401

if __name__ == "__main__":
    main()
