# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading map tiles in the Garmin IMG format.

Used to compare our own tiles with the ones Zepp downloads to the watch
(file parts, bounds, zoom levels, object type codes) and to read the objects
themselves, including the geometry of lines and polygons.

Type code: standard lines and polygons 0x01-0x7f, extended 0x1TTSS (TT type,
SS subtype), points 0xTTSS.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .errors import ZeppmapError

_SECTOR = 512

# Signatures in the file header (at offsets 0x10 and 0x41). Zepp's ski tiles
# have the same structure otherwise, only their own signatures.
SIGNATURES = {
    "garmin": (b"DSKIMG", b"GARMIN"),
    "ski": (b"HMSNOW", b"HMSKIM"),
}


def container_kind(data: bytes) -> str:
    """Kind of file according to the header signatures: "garmin" or "ski"."""
    for kind, (first, second) in SIGNATURES.items():
        if data[0x10:0x16] == first and data[0x41:0x47] == second:
            return kind
    raise ImgError("the file is not a map tile (unknown header)")


def with_signature(data: bytes, kind: str) -> bytes:
    """The tile with the signatures of the given kind; the content is otherwise unchanged."""
    container_kind(data)
    first, second = SIGNATURES[kind]
    return data[:0x10] + first + data[0x16:0x41] + second + data[0x47:]


class ImgError(ZeppmapError):
    pass


@dataclass
class TileInfo:
    kind: str = "garmin"  # kind of file according to the header: "garmin" or "ski"
    description: str = ""
    subfiles: Dict[str, int] = field(default_factory=dict)  # "RGN" -> size
    map_id: int = 0
    bounds: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # north, east, south, west
    levels: List[Tuple[int, bool, int, int]] = field(default_factory=list)  # zoom, inherited, bits, subdivisions
    draw_priority: int = 0
    poi_flags: int = 0
    label_encoding: int = 0
    codepage: int = 0
    lines: Dict[int, int] = field(default_factory=dict)  # type code -> highest level
    polygons: Dict[int, int] = field(default_factory=dict)
    points: Dict[int, int] = field(default_factory=dict)


def split_subfiles(data: bytes) -> Dict[str, bytes]:
    """Parts of the tile according to its directory: "11130692.RGN" -> content."""
    if len(data) < 2 * _SECTOR:
        raise ImgError("the file is not a map tile (it is too short)")
    container_kind(data)
    block_size = 1 << (data[0x61] + data[0x62])
    parts: Dict[str, Dict] = {}
    offset = _SECTOR
    while offset + _SECTOR <= len(data):
        entry = data[offset:offset + _SECTOR]
        if entry[0] == 0x01 and entry[1] != 0x20 and all(32 <= c < 127 for c in entry[1:12]):
            name = entry[1:9].decode("ascii").rstrip() + "." + entry[9:12].decode("ascii")
            part = parts.setdefault(name, {"size": 0, "blocks": {}})
            if entry[0x11] == 0:
                part["size"] = struct.unpack_from("<I", entry, 0x0C)[0]
            part["blocks"][entry[0x11]] = [b for b in struct.unpack_from("<240H", entry, 0x20) if b != 0xFFFF]
        elif entry[0] == 0x00 and offset > 2 * _SECTOR:
            break
        offset += _SECTOR
    result = {}
    for name, part in parts.items():
        blocks = [b for index in sorted(part["blocks"]) for b in part["blocks"][index]]
        result[name] = b"".join(data[b * block_size:(b + 1) * block_size] for b in blocks)[:part["size"]]
    return result


def subfile_spans(data: bytes) -> Dict[str, Tuple[int, int]]:
    """Position of the parts in the file: "11130692.RGN" -> (start, length).

    Only for tiles where every part is one contiguous run; that is how
    mkgmap writes them.
    """
    container_kind(data)
    block_size = 1 << (data[0x61] + data[0x62])
    parts: Dict[str, Dict] = {}
    offset = _SECTOR
    while offset + _SECTOR <= len(data):
        entry = data[offset:offset + _SECTOR]
        if entry[0] == 0x01 and entry[1] != 0x20 and all(32 <= c < 127 for c in entry[1:12]):
            name = entry[1:9].decode("ascii").rstrip() + "." + entry[9:12].decode("ascii")
            part = parts.setdefault(name, {"size": 0, "blocks": {}})
            if entry[0x11] == 0:
                part["size"] = struct.unpack_from("<I", entry, 0x0C)[0]
            part["blocks"][entry[0x11]] = [b for b in struct.unpack_from("<240H", entry, 0x20) if b != 0xFFFF]
        elif entry[0] == 0x00 and offset > 2 * _SECTOR:
            break
        offset += _SECTOR
    spans = {}
    for name, part in parts.items():
        blocks = [b for index in sorted(part["blocks"]) for b in part["blocks"][index]]
        if blocks != list(range(blocks[0], blocks[0] + len(blocks))):
            raise ImgError("part %s is not stored contiguously in the file" % name)
        spans[name] = (blocks[0] * block_size, part["size"])
    return spans


def _signed24(data: bytes, offset: int) -> int:
    value = int.from_bytes(data[offset:offset + 3], "little")
    return value - (1 << 24) if value & 0x800000 else value


def _degrees(units: int) -> float:
    return units * 360.0 / (1 << 24)


def read_tile(data: bytes) -> TileInfo:
    subfiles = split_subfiles(data)
    info = TileInfo(kind=container_kind(data), description=data[0x49:0x5D].decode("ascii", "replace").strip())
    info.subfiles = {name.rsplit(".", 1)[1]: len(content) for name, content in subfiles.items()}

    tre = next((content for name, content in subfiles.items() if name.endswith(".TRE")), None)
    if tre is None or tre[2:12] != b"GARMIN TRE":
        raise ImgError("the tile has no TRE part")
    header_length = struct.unpack_from("<H", tre, 0)[0]
    info.bounds = tuple(_degrees(_signed24(tre, at)) for at in (0x15, 0x18, 0x1B, 0x1E))
    info.poi_flags = tre[0x3F]
    info.draw_priority = _signed24(tre, 0x40)
    if header_length >= 0x78:
        info.map_id = struct.unpack_from("<I", tre, 0x74)[0]

    levels_at, levels_size = struct.unpack_from("<II", tre, 0x21)
    for index in range(levels_size // 4):
        zoom, bits, subdivisions = struct.unpack_from("<BBH", tre, levels_at + 4 * index)
        info.levels.append((zoom & 0x0F, bool(zoom & 0x80), bits, subdivisions))

    for target, at, is_point in ((info.lines, 0x4A, False), (info.polygons, 0x58, False), (info.points, 0x66, True)):
        start, size, record = struct.unpack_from("<IIH", tre, at)
        for index in range(size // record if record else 0):
            item = tre[start + index * record:start + (index + 1) * record]
            code = (item[0] << 8) | item[2] if is_point else item[0]
            target[code] = max(target.get(code, 0), item[1])

    if header_length >= 0x9A:  # overview of extended types: lines, polygons, points
        start, size, record = struct.unpack_from("<IIH", tre, 0x8A)
        counts = struct.unpack_from("<HHH", tre, 0x94)
        for index in range(size // record if record else 0):
            item = tre[start + index * record:start + (index + 1) * record]
            code = 0x10000 | (item[0] << 8) | item[2]
            if index < counts[0]:
                target = info.lines
            elif index < counts[0] + counts[1]:
                target = info.polygons
            else:
                target = info.points
            target[code] = max(target.get(code, 0), item[1])

    lbl = next((content for name, content in subfiles.items() if name.endswith(".LBL")), None)
    if lbl is not None and len(lbl) >= 0x1E:
        info.label_encoding = lbl[0x1E]
        if struct.unpack_from("<H", lbl, 0)[0] >= 0xAC:
            info.codepage = struct.unpack_from("<H", lbl, 0xAA)[0]
    return info


def format_code(code: int) -> str:
    return "0x%05x" % code if code >= 0x10000 else "0x%02x" % code


def describe(info: TileInfo) -> List[str]:
    north, east, south, west = info.bounds
    levels = ", ".join(
        "%d:%d%s" % (zoom, bits, " (overview)" if inherited else "") for zoom, inherited, bits, _n in info.levels
    )
    lines = [
        "description: %s, map ID: %d%s" % (
            info.description, info.map_id, ", ski tile (HMSNOW/HMSKIM signatures)" if info.kind == "ski" else ""),
        "parts: %s" % ", ".join("%s %d B" % item for item in sorted(info.subfiles.items())),
        "bounds: north %.5f, east %.5f, south %.5f, west %.5f" % (north, east, south, west),
        "levels: %s" % levels,
        "labels: encoding %d, codepage %d; draw priority %d" % (
            info.label_encoding, info.codepage, info.draw_priority),
    ]
    for title, table in (("lines", info.lines), ("polygons", info.polygons)):
        lines.append("%s (%d types): %s" % (title, len(table), " ".join(format_code(code) for code in sorted(table))))
    lines.append("points: %d types" % len(info.points))
    return lines


@dataclass
class Element:
    """One object from the RGN part: a point, line or polygon."""
    kind: str        # "point", "line" or "polygon"
    type_byte: int   # first byte of the record as it is in the file
    code: int        # type code: lower 7 bits for lines and polygons, type and subtype for points
    label: str
    level: int       # zoom level the object is in
    offset: int = 0  # position of the first byte of the record in the RGN part
    points: Optional[List[Tuple[float, float]]] = None  # (lat, lon); only with with_points=True


def _label_reader(subfiles: Dict[str, bytes]):
    lbl = next((content for name, content in subfiles.items() if name.endswith(".LBL")), b"")
    net = next((content for name, content in subfiles.items() if name.endswith(".NET")), b"")
    if len(lbl) < 0x1F:
        return lambda value, poi=False: ""
    data_at, data_size, multiplier = struct.unpack_from("<IIB", lbl, 0x15)
    # Points usually have their name in the POI properties table (LBL6); its record starts with a text reference.
    poi_at, poi_size, poi_shift = struct.unpack_from("<IIB", lbl, 0x57) if len(lbl) >= 0x60 else (0, 0, 0)
    roads_at, roads_shift = (0, 0)
    if len(net) >= 0x1E:
        roads_at, _size, roads_shift = struct.unpack_from("<IIB", net, 0x15)

    def text(offset: int) -> str:
        start = data_at + (offset << multiplier)
        if offset == 0 or start >= data_at + data_size:
            return ""
        end = lbl.find(b"\x00", start)
        return lbl[start:end if end >= 0 else None].decode("utf-8", "replace")

    def read(value: int, poi: bool = False) -> str:
        offset = value & 0x3FFFFF
        if poi:
            at = poi_at + (offset << poi_shift)
            if not poi_size or at + 3 > poi_at + poi_size:
                return ""
            return text(int.from_bytes(lbl[at:at + 3], "little") & 0x3FFFFF)
        if value & 0x800000 and net:  # the name is in the road record in the NET part
            at = roads_at + (offset << roads_shift)
            if at + 3 > len(net):
                return ""
            return text(int.from_bytes(net[at:at + 3], "little") & 0x3FFFFF)
        return text(offset)

    return read


_UNIT = 360.0 / (1 << 24)  # degrees per map unit


def _bits_for(base: int) -> int:
    """Number of bits per coordinate according to the base in the bitstream header."""
    return 2 + (base if base <= 9 else 2 * base - 9)


def decode_line(blob: bytes, start_lon: int, start_lat: int, shift: int, extra_bit: bool) -> List[Tuple[int, int]]:
    """Points of a line or polygon from the RGN bitstream, in map units.

    blob starts with a byte holding the bit-count bases (lower four bits
    longitude, upper latitude), followed by a bitstream read from the lowest
    bit of each byte: the signs and then the deltas between points. Deltas are
    multiplied by 2^shift according to the level.
    """
    bits = int.from_bytes(blob[1:], "little")
    total = 8 * (len(blob) - 1)
    position = 0

    def take(count: int) -> int:
        nonlocal position
        value = (bits >> position) & ((1 << count) - 1)
        position += count
        return value

    lon_bits, lat_bits = _bits_for(blob[0] & 0x0F), _bits_for(blob[0] >> 4)
    signs = []
    for _axis in range(2):
        if take(1):                 # all deltas have the same sign
            signs.append(-1 if take(1) else 1)
        else:
            signs.append(0)         # every delta has its own sign
    if not signs[0]:
        lon_bits += 1
    if not signs[1]:
        lat_bits += 1
    if extra_bit:
        take(1)

    def delta(count: int, sign: int) -> int:
        value = take(count)
        if sign:
            return sign * value
        top = 1 << (count - 1)
        if value != top:
            return value - (1 << count) if value & top else value
        # A value with only the top bit set extends the delta by another chunk of the same length.
        extra = 0
        while value == top:
            extra += top - 1
            value = take(count)
        tail = value - (1 << count) if value & top else value
        return tail - extra if tail < 0 else tail + extra

    points = [(start_lon, start_lat)]
    step = lon_bits + lat_bits + (1 if extra_bit else 0)
    while total - position >= step:
        dx = delta(lon_bits, signs[0])
        dy = delta(lat_bits, signs[1])
        if extra_bit:
            take(1)
        lon, lat = points[-1]
        points.append((lon + (dx << shift), lat + (dy << shift)))
    return points


def iter_elements(data: bytes, with_points: bool = False):
    """Walk the objects of standard types in all levels of the tile (extended types are skipped).

    With with_points=True, lines and polygons also carry their geometry in Element.points.
    """
    subfiles = split_subfiles(data)
    tre = next(content for name, content in subfiles.items() if name.endswith(".TRE"))
    rgn = next(content for name, content in subfiles.items() if name.endswith(".RGN"))
    label = _label_reader(subfiles)
    rgn_at, rgn_size = struct.unpack_from("<II", rgn, 0x15)

    levels_at, levels_size = struct.unpack_from("<II", tre, 0x21)
    levels = [struct.unpack_from("<BBH", tre, levels_at + 4 * i) for i in range(levels_size // 4)]
    subdivisions_at = struct.unpack_from("<I", tre, 0x29)[0]

    records = []  # (level, start in RGN, object kinds, centre lon, centre lat, shift)
    at = subdivisions_at
    for index, (zoom, bits_per_coord, count) in enumerate(levels):
        size = 14 if index == len(levels) - 1 else 16
        for _ in range(count):
            records.append((zoom & 0x0F, int.from_bytes(tre[at:at + 3], "little"), tre[at + 3],
                            _signed24(tre, at + 4), _signed24(tre, at + 7), 24 - bits_per_coord))
            at += size

    for index, (level, start, kinds, center_lon, center_lat, shift) in enumerate(records):
        end = records[index + 1][1] if index + 1 < len(records) else rgn_size
        present = [kind for kind, bit in (("point", 0x10), ("indexed", 0x20), ("line", 0x40), ("polygon", 0x80)) if kinds & bit]
        if not present or end <= start:
            continue
        segment = rgn[rgn_at + start:rgn_at + end]
        # Before the data there are two-byte pointers to the start of the second and further object kinds.
        pointers = [2 * (len(present) - 1)]
        for i in range(len(present) - 1):
            pointers.append(struct.unpack_from("<H", segment, 2 * i)[0])
        pointers.append(len(segment))
        for kind, begin, stop in zip(present, pointers, pointers[1:]):
            at = begin
            while at + 8 <= stop:
                type_byte = segment[at]
                info = int.from_bytes(segment[at + 1:at + 4], "little")
                if kind in ("point", "indexed"):
                    subtype = 0
                    size = 8
                    if info & 0x800000:
                        subtype = segment[at + 8]
                        size = 9
                    # Bit 22: the name is in the POI properties table, not directly in the texts.
                    name = label(info & 0x3FFFFF, poi=bool(info & 0x400000))
                    yield Element("point", type_byte, (type_byte << 8) | subtype, name, level,
                                  rgn_at + start + at)
                    at += size
                    continue
                if type_byte & 0x80:
                    length = struct.unpack_from("<H", segment, at + 8)[0]
                    head = 10
                else:
                    length = segment[at + 8]
                    head = 9
                size = head + 1 + length
                # For lines, bit 22 is a reference into NET; _label_reader expects this flag in bit 23.
                value = (info & 0x3FFFFF) | (0x800000 if kind == "line" and info & 0x400000 else 0)
                points = None
                if with_points:
                    dlon, dlat = struct.unpack_from("<hh", segment, at + 4)
                    units = decode_line(segment[at + head:at + size], center_lon + (dlon << shift),
                                        center_lat + (dlat << shift), shift, bool(info & 0x800000))
                    points = [(lat * _UNIT, lon * _UNIT) for lon, lat in units]
                yield Element(kind, type_byte, type_byte & 0x7F, label(value), level, rgn_at + start + at, points)
                at += size
