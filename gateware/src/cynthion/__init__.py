# SPDX-License-Identifier: BSD-3-Clause

"""Minimal Cynthion package shim for standalone analyzer gateware builds."""

# Provide vendored amaranth_boards resources if the package is unavailable.
try:
    import amaranth_boards  # noqa: F401
except Exception:
    import sys
    from .gateware.vendor import amaranth_boards as amaranth_boards_vendor

    sys.modules["amaranth_boards"] = amaranth_boards_vendor

from . import gateware
from . import shared

__all__ = ["gateware", "shared"]
