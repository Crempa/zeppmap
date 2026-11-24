# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn a tile compiled by mkgmap into a tile of the ski layer.

Zepp's ski tiles have the same structure as the others and differ in two
things:

- the signatures in the file header are HMSNOW/HMSKIM instead of DSKIMG/GARMIN,
- lines have types 0x40 to 0x64 (pistes, cross-country trails, lifts). They
  are stored as type 0x00-0x3f with bit 6 set, which elsewhere means
  "one way".

mkgmap rejects line types above 0x3f. A line of type 0x41-0x7e is therefore
compiled as type 0x01-0x3e with a direction, which produces the same byte.
Type 0x40 cannot be written like that (there is no type 0x00); it is stood in
for by type 0x3f without a direction and fixed in the finished tile. Type
0x7f is not available for this reason.
"""
from __future__ import annotations

import struct
from typing import Iterable, Tuple

from . import imginfo
from .errors import ZeppmapError

FIRST_LINE = 0x40
LAST_LINE = 0x7E
_PLACEHOLDER = 0x3F  # stands in for type 0x40 during compilation


class SkiTileError(ZeppmapError):
    pass


def compile_type(code: int) -> Tuple[int, bool]:
    """Line type and direction flag under which a ski type is passed to mkgmap."""
    if not FIRST_LINE <= code <= LAST_LINE:
        raise SkiTileError("the ski layer has line types 0x40 to 0x7e, not 0x%02x" % code)
    if code == FIRST_LINE:
        return _PLACEHOLDER, False
    return code & 0x3F, True


def to_ski_tile(data: bytes, plain: Iterable[int] = ()) -> bytes:
    """Turn a tile compiled according to compile_type() into a ski tile.

    Lines of the types in plain (without a direction, such as contours
    0x20-0x23) are left as they are. A ski type stored as the same byte must
    then not be in the tile.
    """
    plain = frozenset(plain)
    spans = imginfo.subfile_spans(data)
    rgn_at = next(start for name, (start, _size) in spans.items() if name.endswith(".RGN"))
    tre_at = next(start for name, (start, _size) in spans.items() if name.endswith(".TRE"))
    out = bytearray(data)

    for element in imginfo.iter_elements(data):
        if element.kind != "line":
            continue
        at = rgn_at + element.offset
        low = element.type_byte & 0x7F
        if low in plain:
            continue
        if low == _PLACEHOLDER:
            out[at] = (element.type_byte & 0x80) | FIRST_LINE
        elif low < FIRST_LINE:
            raise SkiTileError("line of type 0x%02x has no direction set; it does not belong in a ski tile" % low)
        elif low & 0x3F in plain:
            raise SkiTileError("ski type 0x%02x is stored the same way as line type 0x%02x" % (low, low & 0x3F))

    # Overview of line types in the TRE part: the same renumbering, records stay sorted.
    start, size, record = struct.unpack_from("<IIH", data, tre_at + 0x4A)
    entries = []
    for index in range(size // record if record else 0):
        entry = bytearray(data[tre_at + start + index * record:tre_at + start + (index + 1) * record])
        if entry[0] not in plain:
            entry[0] = FIRST_LINE if entry[0] == _PLACEHOLDER else entry[0] | 0x40
        entries.append(bytes(entry))
    entries.sort()
    out[tre_at + start:tre_at + start + len(entries) * record] = b"".join(entries)

    return imginfo.with_signature(bytes(out), "ski")
