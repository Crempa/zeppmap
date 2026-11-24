# SPDX-License-Identifier: AGPL-3.0-or-later
"""Swatches: test tiles that show how the watch draws what.

Tiles carry no style definition; the firmware decides how lines and areas
look by their type code. A swatch therefore covers the tile with a repeating
"page" on which every tested type appears once, with a label. Wherever you are
in the tile, a whole page is within reach.

Pages:

    lines     standard line types 0x01-0x3f and the extended types Zepp uses
    areas     areas, line overlaps and extended line types
    contours  all standard line types in the contour layer (ctl folder); goes
              with the areas page, which has headers and scale stubs ready for them
    overlay   the lines page with the structure of a contour tile, to test an
              overlay layer over the original base map
    ski       line types 0x40-0x7e and areas of the ski layer (ski folder)
    ctl       a separate swatch of the contour layer (ctl folder)
    zepp      areas, points and lines Zepp uses in its tiles; the types are
              taken from the official tiles in the source folder

A short stretch of an ordinary street at each row serves as a reference: if
the row has only that, the watch does not draw the type. The source for
mkgmap is the "Polish format".
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import imginfo, skitile
from .errors import ZeppmapError
from .imginfo import format_code

ZOOM = 11

STANDARD_LINES = list(range(0x01, 0x40))
# Extended line types from all tiles downloaded from the watch (17 Nov 2026).
EXTENDED_LINES = [
    0x10401, 0x10601, 0x10602, 0x10603, 0x10604, 0x10700, 0x10701, 0x10702,
    0x10802, 0x10803, 0x10804, 0x1080A, 0x10813,
    0x10D01, 0x10D02, 0x10D03, 0x10D04, 0x10D05, 0x10D0A, 0x10D0B,
    0x10D11, 0x10D12, 0x10D13, 0x10D16, 0x10D19, 0x10D1A, 0x10D1B,
    0x10E03, 0x1351E,
]
# Extended area types from the same tiles.
EXTENDED_POLYGONS = [
    0x10701, 0x10702, 0x10907, 0x10B0E, 0x10C02, 0x10C03, 0x10C06, 0x10C07, 0x10C10, 0x10C11,
]
STANDARD_POLYGONS = list(range(0x01, 0x60))
# Ski layer: line types 0x40 to 0x7e (0x7f cannot be made, see skitile) and areas from 0x50.
SKI_LINES = list(range(skitile.FIRST_LINE, skitile.LAST_LINE + 1))
SKI_POLYGONS = list(range(0x50, 0x80))
SKI_FRAME = 0x46      # blue piste, the most common line type in ski tiles

BACKGROUND = 0x4B
STREET = 0x06         # ordinary street: headers and reference stubs
TOWN = 0x0B00

BASE_LEVELS = [24, 22, 20, 18, 17]         # as Zepp's base map tiles
CONTOUR_LEVELS = [24, 22, 20, 18, 16, 15]  # as Zepp's contour tiles

GROUP_SIZE = 8        # lines in one cell
ROW_SPACING = 25.0    # metres between lines in a cell
HEADER_HEIGHT = 40.0  # room above the first line for the header
GAP = 20.0
STUB_LENGTH = 40.0    # length of the reference stub on the left

_METERS_PER_DEGREE = 111_320.0
_UNITS_PER_DEGREE = (1 << 24) / 360.0


class SwatchError(ZeppmapError):
    pass


@dataclass
class Shape:
    """A line, rectangular area or point in metres from the top left corner of the page."""
    kind: str            # "line", "polygon" or "point"
    code: int
    label: str
    end_level: int
    x0: float
    y0: float
    x1: float = 0.0
    y1: float = 0.0


@dataclass
class Page:
    name: str
    width: float
    height: float
    levels: List[int]
    shapes: List[Shape] = field(default_factory=list)
    legend: List[str] = field(default_factory=list)
    container: str = "garmin"  # "ski": a ski layer tile

    def line(self, code: int, label: str, x0: float, x1: float, y: float, end_level: int = 1) -> None:
        self.shapes.append(Shape("line", code, label, end_level, x0, y, x1, y))

    def polygon(self, code: int, label: str, x0: float, y0: float, x1: float, y1: float, end_level: int = 1) -> None:
        self.shapes.append(Shape("polygon", code, label, end_level, x0, y0, x1, y1))

    def point(self, code: int, label: str, x: float, y: float, end_level: int = 1) -> None:
        self.shapes.append(Shape("point", code, label, end_level, x, y))

    def codes(self, kind: str) -> List[int]:
        return [shape.code for shape in self.shapes if shape.kind == kind]


# ------------------------------------------------------------------ pages

def groups(codes: List[int]) -> List[List[int]]:
    return [codes[start:start + GROUP_SIZE] for start in range(0, len(codes), GROUP_SIZE)]


def group_title(codes: List[int]) -> str:
    return "%s-%s" % (format_code(codes[0]), format_code(codes[-1]))


def _line_cells(
    page: Page, cells: List[List[int]], columns: int, left: float, top: float, line_length: float,
    frame: bool = True, lines: bool = True, prefix: str = "", frame_code: int = STREET,
    label_of: Optional[Callable[[int], str]] = None, title_of: Optional[Callable[[int], str]] = None,
) -> float:
    """Cells with lines one below another; returns y below the last row of cells.

    frame draws the headers and reference stubs, lines the tested lines. They
    are drawn separately when the frame belongs to one map layer and the
    lines to another.
    """
    cell_width = STUB_LENGTH + GAP + line_length + GAP
    cell_height = HEADER_HEIGHT + GROUP_SIZE * ROW_SPACING + GAP
    for index, codes in enumerate(cells):
        x = left + (index % columns) * cell_width
        y = top + (index // columns) * cell_height
        line_x = x + STUB_LENGTH + GAP
        title = title_of(index) if title_of else prefix + group_title(codes)
        if frame:
            page.line(frame_code, title, x, line_x + line_length, y + HEADER_HEIGHT / 2, end_level=2)
            if frame_code == STREET:
                page.point(TOWN, title, (x + line_x + line_length) / 2, y + HEADER_HEIGHT / 2)
        for row, code in enumerate(codes):
            row_y = y + HEADER_HEIGHT + row * ROW_SPACING
            if frame:
                page.line(frame_code, "", x, x + STUB_LENGTH, row_y)
            if lines:
                page.line(code, label_of(code) if label_of else format_code(code),
                          line_x, line_x + line_length, row_y, end_level=2 if not frame else 1)
    return top + math.ceil(len(cells) / columns) * cell_height


def lines_page() -> Page:
    """Standard and extended line types, 4 cells side by side."""
    cells = groups(STANDARD_LINES) + groups(EXTENDED_LINES)
    columns, line_length = 4, 220.0
    page = Page("lines", columns * (STUB_LENGTH + GAP + line_length + GAP), 0.0, BASE_LEVELS)
    page.height = _line_cells(page, cells, columns, 0.0, 0.0, line_length)
    page.legend = [
        "Line type swatch",
        "",
        "A %d x %d m page repeats over the whole tile; every type is on it once." % (page.width, page.height),
        "At the top of a cell is the header (a street labelled with the code range), below it the types",
        "from top to bottom, %d m apart. Each row starts on the left with a short stretch of ordinary" % ROW_SPACING,
        "street; if the row has only that, the watch does not draw the type. The tested line is labelled",
        "with its code.",
        "",
        "Cells row by row from left to right, %d side by side, and their content from top to bottom:" % columns,
    ]
    page.legend += ["    %-17s %s" % (group_title(codes), "  ".join(format_code(c) for c in codes)) for codes in cells]
    return page


# Overlaps: (row name, [(line type, offset in metres downwards), ...]).
OVERLAPS = [
    ("0a+01", [(0x0A, 0), (0x01, 0)]),
    ("01+0a", [(0x01, 0), (0x0A, 0)]),
    ("0a+03", [(0x0A, 0), (0x03, 0)]),
    ("0a+14", [(0x0A, 0), (0x14, 0)]),
    ("06+01", [(0x06, 0), (0x01, 0)]),
    ("0a 01 6m", [(0x0A, 0), (0x01, 6)]),
    ("0a 14 6m", [(0x0A, 0), (0x14, 6)]),
    ("01 03 14 8m", [(0x01, -8), (0x03, 0), (0x14, 8)]),
    ("0a 01 03 8m", [(0x0A, 0), (0x01, 8), (0x03, 16)]),
]

_OVERLAY_LINE_LENGTH = 160.0


def _areas() -> Tuple[Page, float]:
    """The areas page and the y at which its rows for the contour layer start."""
    page = Page("areas", 1000.0, 0.0, BASE_LEVELS)
    left = 20.0

    # A: standard areas in a grid, column = last digit of the code, row = first.
    y = 20.0
    page.line(STREET, "areas 0x01-0x5f", left, left + 960.0, y, end_level=2)
    top = y + 20.0
    for code in STANDARD_POLYGONS:
        x = left + (code & 0x0F) * 60.0
        cell_y = top + (code >> 4) * 50.0
        page.polygon(code, "%02x" % code, x + 5, cell_y, x + 55, cell_y + 30)
        page.line(STREET, "%02x" % code, x + 5, x + 55, cell_y + 38)
    y = top + 6 * 50.0 + 10.0

    # Extended areas, each with its full code.
    for index, code in enumerate(EXTENDED_POLYGONS):
        x = left + index * 96.0
        page.polygon(code, format_code(code), x + 8, y, x + 88, y + 30)
        page.line(STREET, format_code(code), x + 8, x + 88, y + 38)
    y += 80.0

    # B: line overlaps, a named street on the left, the tested lines on the right.
    page.line(STREET, "overlaps", left, left + 960.0, y, end_level=2)
    y += 35.0
    rows = math.ceil(len(OVERLAPS) / 2)
    for index, (name, parts) in enumerate(OVERLAPS):
        x = left + (index // rows) * 500.0
        row_y = y + (index % rows) * 40.0
        page.line(STREET, name, x, x + 100.0, row_y)
        for code, shift in parts:
            page.line(code, "", x + 120.0, x + 420.0, row_y + shift)
    y += rows * 40.0 + 10.0

    # E: extended line types.
    page.line(STREET, "extended lines", left, left + 960.0, y, end_level=2)
    y = _line_cells(page, groups(EXTENDED_LINES), 4, left, y + 10.0, 160.0)

    # D: cells for the contour layer. The base has only headers and stubs here;
    # the lines of all standard types are drawn by the contours page in the ctl layer.
    page.line(STREET, "ctl layer", left, left + 960.0, y, end_level=2)
    contour_top = y + 10.0
    page.height = _line_cells(page, groups(STANDARD_LINES), 4, left, contour_top, _OVERLAY_LINE_LENGTH,
                              lines=False, prefix="ctl ")

    page.legend = [
        "Swatch of areas, overlaps and extended lines",
        "",
        "A %d x %d m page repeats over the whole tile. From top to bottom:" % (page.width, page.height),
        "",
        "1. Areas 0x01-0x5f in a grid of 16 columns x 6 rows. The column is the last digit of the code",
        "   (0 to f from the left), the row the first digit (0 to 5 from the top). Below each area is a",
        "   short street labelled with the code; it is there even when the watch does not draw the area.",
        "2. Extended areas: %s." % ", ".join(format_code(c) for c in EXTENDED_POLYGONS),
        "3. Overlaps: a street with the row name on the left, the tested lines on the right.",
    ]
    page.legend += ["       %-12s %s" % (name, _describe_overlap(parts)) for name, parts in OVERLAPS]
    page.legend += ["4. Extended line types in groups of eight, as in the line swatch:"]
    page.legend += ["       %-17s %s" % (group_title(codes), "  ".join(format_code(c) for c in codes))
                    for codes in groups(EXTENDED_LINES)]
    page.legend += [
        "5. Contour layer: cells with the headers \"ctl 0x01-0x08\" and so on. The base map draws only",
        "   the headers and the stubs on the left there. Lines of types 0x01 to 0x3f, again in groups",
        "   of eight from top to bottom, are drawn by the contour layer if the contours page is",
        "   uploaded (push --layer ctl). It shows which types, and in what colour, the watch can draw",
        "   as an overlay over the base map.",
    ]
    return page, contour_top


def _describe_overlap(parts: List[Tuple[int, int]]) -> str:
    if all(shift == 0 for _code, shift in parts):
        return "lines %s on top of each other, in this order" % " and ".join(format_code(code) for code, _s in parts)
    return "lines side by side: " + ", ".join(
        "%s %d m %s" % (format_code(code), abs(shift), "lower" if shift > 0 else "higher") if shift
        else format_code(code) for code, shift in parts
    )


def areas_page() -> Page:
    """Areas, line overlaps, extended line types and room for the contour layer lines."""
    return _areas()[0]


def contours_page() -> Page:
    """Lines for the contour layer; they lie in the room reserved on the areas page."""
    base, contour_top = _areas()
    page = Page("contours", base.width, base.height, CONTOUR_LEVELS)
    _line_cells(page, groups(STANDARD_LINES), 4, 20.0, contour_top, _OVERLAY_LINE_LENGTH, frame=False)
    page.legend = [
        "Swatch for the contour layer: lines of all standard types 0x01 to 0x3f in the cells",
        "\"ctl 0x01-0x08\" to \"ctl 0x39-0x3f\" of the areas page, in groups of eight from top to bottom.",
        "Upload it to the ctl folder: push --layer ctl.",
    ]
    return page


def overlay_page() -> Page:
    """Line swatch for an overlay layer over the ordinary map.

    The content is the same as on the lines page, the tile structure as for
    contours. Useful to test what a layer (ctl, or the ski layer) draws over
    the original base map.
    """
    page = lines_page()
    page.name = "overlay"
    page.levels = CONTOUR_LEVELS
    page.legend = ["Overlay layer: the same line swatch, drawn over the ordinary map.", ""] + page.legend[2:]
    return page


CONTOUR_FRAME = 0x22   # a contour; we know the watch draws it in the contour layer


def feet_label(meters: float) -> str:
    """A label the watch turns into the given number in the contour layer.

    Contour labels are in feet in the tiles and the watch converts them to metres.
    """
    return str(int(round(meters / 0.3048)))


def ctl_page() -> Page:
    """What the contour layer draws: all standard line types in a frame of contours.

    The contour layer is the only overlay the watch draws during an ordinary
    activity too. The page needs no other layer: the frame is made of
    contours and the lines are recognised by position and by the number in
    the label.
    """
    cells = groups(STANDARD_LINES)
    columns, line_length = 4, 220.0
    page = Page("ctl", columns * (STUB_LENGTH + GAP + line_length + GAP), 0.0, CONTOUR_LEVELS)
    page.height = _line_cells(
        page, cells, columns, 0.0, 0.0, line_length, frame_code=CONTOUR_FRAME,
        label_of=lambda code: feet_label(code * 10), title_of=lambda index: feet_label((index + 1) * 1000))
    page.legend = [
        "Contour layer swatch",
        "",
        "A %d x %d m page repeats over the whole tile and is drawn over the ordinary map." % (page.width, page.height),
        "Cell headers and the short stubs on the left are contours (type 0x22). If a row has only the",
        "stub, the watch does not draw that type in the contour layer.",
        "",
        "Labels are numbers, because the watch shows only elevations in this layer: a cell header has",
        "1000, 2000 to %d000 by position, a line has ten times its code in decimal" % len(cells),
        "(type 0x2a is 42, so 420).",
        "",
        "Cells row by row from left to right, %d side by side, and their content from top to bottom:" % columns,
    ]
    page.legend += ["    %5d  %-12s %s" % ((index + 1) * 1000, group_title(codes),
                                         "  ".join("%s=%d" % (format_code(c), c * 10) for c in codes))
                    for index, codes in enumerate(cells)]
    return page


def ski_page() -> Page:
    """Line and area types of the ski layer; drawn over the ordinary map."""
    columns, line_length = 4, 220.0
    page = Page("ski", columns * (STUB_LENGTH + GAP + line_length + GAP), 0.0, BASE_LEVELS, container="ski")
    y = _line_cells(page, groups(SKI_LINES), columns, 0.0, 0.0, line_length, frame_code=SKI_FRAME)

    # Areas in a grid: column = last digit of the code, row = first (5, 6, 7).
    page.line(SKI_FRAME, "areas 0x50-0x7f", 20.0, 980.0, y + 20.0, end_level=2)
    top = y + 40.0
    for code in SKI_POLYGONS:
        x = 20.0 + (code & 0x0F) * 60.0
        cell_y = top + ((code >> 4) - 5) * 50.0
        page.polygon(code, "%02x" % code, x + 5, cell_y, x + 55, cell_y + 30)
        page.line(SKI_FRAME, "%02x" % code, x + 5, x + 55, cell_y + 38)
    page.height = top + 3 * 50.0 + GAP

    cells = groups(SKI_LINES)
    page.legend = [
        "Ski layer swatch",
        "",
        "A %d x %d m page repeats over the whole tile and is drawn over the ordinary map." % (page.width, page.height),
        "Cell headers and the short stubs on the left are type 0x46 (blue piste). If a row has only",
        "the stub, the watch does not draw that type.",
        "",
        "Lines: cells row by row from left to right, %d side by side, content from top to bottom:" % columns,
    ]
    page.legend += ["    %-12s %s" % (group_title(codes), "  ".join(format_code(c) for c in codes)) for codes in cells]
    page.legend += [
        "",
        "Areas 0x50-0x7f below the lines: 16 columns (last digit of the code, 0 to f from the left)",
        "and 3 rows (first digit 5, 6, 7 from the top). Below each area is a short line labelled with the code.",
        "",
        "What these types mean in Zepp's ski maps (by the names in the Krkonose tiles):",
        "    0x40 black piste, 0x43 red, 0x46 blue, 0x49 nursery and practice slopes,",
        "    0x51 to 0x60 cross-country trails, 0x63 lifts and cable cars, area 0x71 ski resort.",
    ]
    return page


# ------------------------------------------------ types from official tiles

FOREST = 0x50  # forest: a green area on which a line in the background colour shows too


def point_code(code: int) -> str:
    """A point code as the Polish format writes it (type and subtype, e.g. 0x2c04)."""
    return format_code(code) if code >= 0x10000 else "0x%04x" % code


def official_types(folder: str) -> Dict[str, Dict[int, dict]]:
    """Line, area and point types in the tiles in a folder.

    For each type: how many tiles it is in and its most common names (only
    for standard types; names of extended types are not read). For lines the
    code is without the direction bit.
    """
    found: Dict[str, Dict[int, dict]] = {"lines": {}, "polygons": {}, "points": {}}
    names = {kind: defaultdict(Counter) for kind in found}
    for x, y in tiles_in(folder):
        with open(os.path.join(folder, str(ZOOM), str(x), "%d.img" % y), "rb") as handle:
            data = handle.read()
        info = imginfo.read_tile(data)
        for kind in found:
            for code in getattr(info, kind):
                entry = found[kind].setdefault(code, {"tiles": 0, "names": []})
                entry["tiles"] += 1
        for element in imginfo.iter_elements(data):
            kind = {"line": "lines", "polygon": "polygons", "point": "points"}[element.kind]
            code = element.code & 0x3F if kind == "lines" else element.code
            # Some road names come out garbled (references through the NET part); those are skipped.
            if element.label and "\ufffd" not in element.label:
                names[kind][code][element.label] += 1
    for kind, table in found.items():
        for code, entry in table.items():
            entry["names"] = [name for name, _count in names[kind][code].most_common(2)]
    return found


_STYLE_FILES = {
    "points": ("points", "inc/compat_points"),
    "lines": ("lines", "inc/compat_lines"),
    "polygons": ("polygons", "inc/compat_polygons"),
}
_STYLE_RULE = re.compile(r"([^\[\]{}\n][^\[\]{}]*?)\s*(?:\{[^}]*\}\s*)?\[\s*(0x[0-9a-fA-F]+)[^\]]*\]")


def style_meanings(jar: Optional[str] = None) -> Dict[str, Dict[int, str]]:
    """What each type means in mkgmap's default style: the first OpenStreetMap tags leading to it.

    Zepp builds its tiles with mkgmap but its own style, so for some types
    (mainly points) the default style says nothing. Without mkgmap it returns
    empty tables.
    """
    meanings: Dict[str, Dict[int, str]] = {kind: {} for kind in _STYLE_FILES}
    try:
        archive = zipfile.ZipFile(jar or find_mkgmap())
    except (SwatchError, OSError, zipfile.BadZipFile):
        return meanings
    with archive:
        for kind, files in _STYLE_FILES.items():
            for name in files:
                try:
                    text = archive.read("styles/default/" + name).decode("utf-8", "replace")
                except KeyError:
                    continue
                text = re.sub(r"#[^\n]*", "", text)  # comments, also after a rule
                for match in _STYLE_RULE.finditer(text):
                    code = int(match.group(2), 16)
                    if kind == "points" and code < 0x100:
                        code <<= 8  # [0x2c] means type 0x2c without a subtype
                    rule = " ".join(match.group(1).split()).strip()
                    first = rule.split(" | ")[0].split(" & ")[0].strip(" ()")
                    if first.startswith("include") or not first:
                        continue
                    meanings[kind].setdefault(code, first[:40])
    return meanings


def zepp_page(folder: str, jar: Optional[str] = None) -> Page:
    """A swatch of the types Zepp actually uses in the tiles in folder.

    Areas and points have a short street labelled with the code below them,
    by which they are recognised even when the watch does not show their
    name. Lines are in cells of eight and run over a forest with their right
    half, so that a line in the background colour shows too.
    """
    types = official_types(folder)
    if not any(types.values()):
        raise SwatchError("there are no 11/<x>/<y>.img tiles in %s" % folder)
    meanings = style_meanings(jar)
    page = Page("zepp", 1000.0, 0.0, BASE_LEVELS)
    left = 20.0
    columns = 12
    cell = 80.0

    polygons = sorted(code for code in types["polygons"] if code != BACKGROUND)
    points = sorted(types["points"])
    lines = sorted(types["lines"])

    # 1. areas
    y = 20.0
    page.line(STREET, "zepp areas", left, left + 960.0, y, end_level=2)
    top = y + 20.0
    for index, code in enumerate(polygons):
        x = left + (index % columns) * cell
        cell_y = top + (index // columns) * 50.0
        page.polygon(code, format_code(code), x + 5, cell_y, x + cell - 5, cell_y + 30)
        page.line(STREET, format_code(code), x + 5, x + cell - 5, cell_y + 38)
    y = top + math.ceil(len(polygons) / columns) * 50.0 + 20.0

    # 2. points
    page.line(STREET, "zepp points", left, left + 960.0, y, end_level=2)
    top = y + 25.0
    for index, code in enumerate(points):
        x = left + (index % columns) * cell
        cell_y = top + (index // columns) * 55.0
        page.point(code, point_code(code), x + cell / 2, cell_y + 10)
        page.line(STREET, point_code(code), x + 5, x + cell - 5, cell_y + 38)
    y = top + math.ceil(len(points) / columns) * 55.0 + 20.0

    # 3. lines, the right half over forest
    page.line(STREET, "zepp lines", left, left + 960.0, y, end_level=2)
    line_top = y + 10.0
    line_length = 160.0
    cells = groups(lines)
    page.height = _line_cells(page, cells, 4, left, line_top, line_length)
    cell_width = STUB_LENGTH + GAP + line_length + GAP
    cell_height = HEADER_HEIGHT + GROUP_SIZE * ROW_SPACING + GAP
    for index, codes in enumerate(cells):
        x = left + (index % 4) * cell_width + STUB_LENGTH + GAP
        cell_y = line_top + (index // 4) * cell_height + HEADER_HEIGHT
        page.polygon(FOREST, "", x + line_length / 2, cell_y - 10, x + line_length,
                     cell_y + (len(codes) - 1) * ROW_SPACING + 10)

    def describe(kind: str, code: int, shown: str) -> str:
        entry = types[kind][code]
        parts = ["%-8s in %2d tiles" % (shown, entry["tiles"])]
        if meanings[kind].get(code):
            parts.append("mkgmap: " + meanings[kind][code])
        if entry["names"]:
            parts.append("e.g. " + "; ".join(entry["names"]))
        return "    " + "  ".join(parts)

    page.legend = [
        "Swatch of the types Zepp uses in its tiles",
        "",
        "The types come from %d tiles in %s. A %d x %d m page repeats over the whole tile."
        % (len(tiles_in(folder)), folder, page.width, page.height),
        "From top to bottom:",
        "",
        "1. Areas (%d), %d side by side in rows. Below each is a short street labelled with the code;" % (len(polygons), columns),
        "   it is there even when the watch does not draw the area. Background 0x4b covers the whole tile.",
        "2. Points (%d), %d side by side. A point is labelled with its code and has a street with the" % (len(points), columns),
        "   same label below it, so it is recognised even without an icon or a point name.",
        "3. Lines (%d) in cells of eight, as in the line swatch. The right half of each cell lies on" % len(lines),
        "   forest (0x50): a line the watch draws in the background colour shows only there.",
        "",
        "The meaning by mkgmap's default style is only a hint: Zepp has its own style. The names",
        "are samples from the official tiles.",
        "",
        "Areas:",
    ]
    page.legend += [describe("polygons", code, format_code(code)) for code in polygons]
    page.legend += ["", "Points:"]
    page.legend += [describe("points", code, point_code(code)) for code in points]
    page.legend += ["", "Lines by cell:"]
    for codes in cells:
        page.legend.append("  %s" % group_title(codes))
        page.legend += [describe("lines", code, format_code(code)) for code in codes]
    return page


PAGES = {"lines": lines_page, "areas": areas_page, "contours": contours_page, "overlay": overlay_page,
         "ctl": ctl_page,
         "ski": ski_page}


# ------------------------------------------------------------------ tiles

def tile_bounds(x: int, y: int, zoom: int = ZOOM) -> Tuple[float, float, float, float]:
    """Tile bounds as (north, east, south, west) in degrees."""
    count = 2 ** zoom

    def latitude(row: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / count))))

    west = x / count * 360.0 - 180.0
    east = (x + 1) / count * 360.0 - 180.0
    return latitude(y), east, latitude(y + 1), west


def map_bounds(x: int, y: int) -> Tuple[float, float, float, float]:
    """Bounds with which mkgmap makes a map exactly within the tile.

    Zepp's tiles have their bounds rounded to the nearest map unit (360/2^24
    degrees). mkgmap rounds outwards, so the bounds are moved a quarter unit
    inwards.
    """
    north, east, south, west = tile_bounds(x, y)
    inset = 0.25
    return (
        (round(north * _UNITS_PER_DEGREE) - inset) / _UNITS_PER_DEGREE,
        (round(east * _UNITS_PER_DEGREE) - inset) / _UNITS_PER_DEGREE,
        (round(south * _UNITS_PER_DEGREE) + inset) / _UNITS_PER_DEGREE,
        (round(west * _UNITS_PER_DEGREE) + inset) / _UNITS_PER_DEGREE,
    )


def tile_name(x: int, y: int) -> str:
    """Map name as Zepp gives it to tiles: x and y, four digits each."""
    return "%04d%04d" % (x, y)


def _scales(north: float, south: float) -> Tuple[float, float]:
    lat_per_meter = 1.0 / _METERS_PER_DEGREE
    lon_per_meter = 1.0 / (_METERS_PER_DEGREE * math.cos(math.radians((north + south) / 2)))
    return lat_per_meter, lon_per_meter


def page_origins(x: int, y: int, page: Page) -> List[Tuple[float, float]]:
    """Top left corners (latitude, longitude) of all whole pages in the tile."""
    north, east, south, west = map_bounds(x, y)
    lat_per_meter, lon_per_meter = _scales(north, south)
    origins = []
    top = north
    while top - page.height * lat_per_meter > south:
        left = west
        while left + page.width * lon_per_meter < east:
            origins.append((top, left))
            left += page.width * lon_per_meter
        top -= page.height * lat_per_meter
    return origins


def swatch_mp(x: int, y: int, page: Optional[Page] = None) -> str:
    """Swatch source for one tile in the Polish format."""
    page = page or lines_page()
    north, east, south, west = map_bounds(x, y)
    lat_per_meter, lon_per_meter = _scales(north, south)
    out: List[str] = ["[IMG ID]", "ID=%s" % tile_name(x, y), "Name=OSM street map", "Elevation=M",
                      "Transparent=N", "Levels=%d" % len(page.levels)]
    out += ["Level%d=%d" % (index, bits) for index, bits in enumerate(page.levels)]
    out += ["[END-IMG ID]", ""]

    def point(lat: float, lon: float) -> str:
        return "(%.7f,%.7f)" % (lat, lon)

    # A background exactly over the whole tile keeps the map bounds on the tile bounds.
    out += ["[POLYGON]", "Type=0x%02x" % BACKGROUND, "EndLevel=%d" % (len(page.levels) - 1),
            "Data0=%s,%s,%s,%s" % (point(north, west), point(north, east), point(south, east), point(south, west)),
            "[END]", ""]

    for top, left in page_origins(x, y, page):
        for shape in page.shapes:
            lat0 = top - shape.y0 * lat_per_meter
            lon0 = left + shape.x0 * lon_per_meter
            lat1 = top - shape.y1 * lat_per_meter
            lon1 = left + shape.x1 * lon_per_meter
            extra: List[str] = []
            if shape.kind == "line":
                section, code = "[POLYLINE]", format_code(shape.code)
                if page.container == "ski":  # types above 0x3f are passed to mkgmap in a roundabout way
                    compiled, direction = skitile.compile_type(shape.code)
                    code = format_code(compiled)
                    extra = ["DirIndicator=1"] if direction else []
                data = "%s,%s" % (point(lat0, lon0), point(lat1, lon1))
            elif shape.kind == "polygon":
                section, code = "[POLYGON]", format_code(shape.code)
                data = "%s,%s,%s,%s" % (point(lat0, lon0), point(lat0, lon1), point(lat1, lon1), point(lat1, lon0))
            else:
                section, code, data = "[POI]", "0x%04x" % shape.code, point(lat0, lon0)
            out += [section, "Type=%s" % code]
            if shape.label:
                out.append("Label=%s" % shape.label)
            out += extra + ["EndLevel=%d" % shape.end_level, "Data0=%s" % data, "[END]", ""]
    return "\n".join(out)


def legend(page: Optional[Page] = None) -> str:
    """Description of the swatch, for reading it off the watch display."""
    return "\n".join((page or lines_page()).legend) + "\n"


# -------------------------------------------------------------- compiling

def find_java() -> str:
    candidates = [
        os.environ.get("ZEPPMAP_JAVA"),
        "/opt/homebrew/opt/openjdk/bin/java",
        "/usr/local/opt/openjdk/bin/java",
        shutil.which("java"),
    ]
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        # /usr/bin/java exists on macOS even without Java and only prints an install prompt.
        probe = subprocess.run([candidate, "-version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return candidate
    hint = " (brew install openjdk)" if sys.platform == "darwin" else ""
    raise SwatchError("Java is not installed%s, or point ZEPPMAP_JAVA to it" % hint)


def find_mkgmap() -> str:
    explicit = os.environ.get("ZEPPMAP_MKGMAP")
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise SwatchError("ZEPPMAP_MKGMAP points to a file that does not exist: %s" % explicit)
    tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    if os.path.isdir(tools):
        for name in sorted(os.listdir(tools), reverse=True):
            jar = os.path.join(tools, name, "mkgmap.jar")
            if name.startswith("mkgmap") and os.path.isfile(jar):
                return jar
    raise SwatchError("mkgmap.jar is neither in the tools/ folder nor in the ZEPPMAP_MKGMAP variable")


def compile_mp(source: str, map_name: str) -> bytes:
    """Compile a source in the Polish format with mkgmap and return the tile content."""
    java, mkgmap = find_java(), find_mkgmap()
    with tempfile.TemporaryDirectory() as work:
        with open(os.path.join(work, "tile.mp"), "w", encoding="ascii") as handle:
            handle.write(source)
        result = subprocess.run(
            [java, "-jar", mkgmap, "--unicode", "--mapname=%s" % map_name,
             "--description=OSM street map", "--output-dir=out", "tile.mp"],
            cwd=work, capture_output=True, text=True,
        )
        produced = os.path.join(work, "out", map_name + ".img")
        if result.returncode != 0 or not os.path.isfile(produced):
            detail = (result.stdout + result.stderr).strip().splitlines()
            raise SwatchError("mkgmap did not produce tile %s: %s" % (map_name, " | ".join(detail[-4:])))
        with open(produced, "rb") as handle:
            return handle.read()


def tiles_in(folder: str) -> List[Tuple[int, int]]:
    """Numbers (x, y) of the tiles found in a folder with the 11/<x>/<y>.img structure."""
    found = []
    for root, _dirs, files in os.walk(folder):
        parts = os.path.normpath(root).split(os.sep)
        if len(parts) < 2 or parts[-2] != str(ZOOM) or not parts[-1].isdigit():
            continue
        for name in files:
            stem, extension = os.path.splitext(name)
            if extension.lower() == ".img" and stem.isdigit():
                found.append((int(parts[-1]), int(stem)))
    return sorted(found)


def build(
    tiles: Iterable[Tuple[int, int]],
    out_dir: str,
    report: Callable[[str], None] = print,
    compiler: Optional[Callable[[str, str], bytes]] = None,
    page: Optional[Page] = None,
) -> int:
    """Build the swatch for the given tiles into out_dir/11/<x>/<y>.img. Returns their number."""
    compiler = compiler or compile_mp
    page = page or lines_page()
    count = 0
    for x, y in tiles:
        data = compiler(swatch_mp(x, y, page), tile_name(x, y))
        if page.container == "ski":
            data = skitile.to_ski_tile(data)
        target = os.path.join(out_dir, str(ZOOM), str(x), "%d.img" % y)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        report("%d/%d/%d.img (%d B)" % (ZOOM, x, y, len(data)))
        count += 1
    if count == 0:
        raise SwatchError("no tile was given")
    return count
