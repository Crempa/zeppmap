# SPDX-License-Identifier: AGPL-3.0-or-later
"""Marks in the contour layer.

During an ordinary activity the watch draws only the base map and the
contours; the ski layer only for skiing sports. A tile in the ski layer
format (HMSNOW/HMSKIM signatures) uploaded into the contour layer is drawn
exactly as in the ski layer though, during any activity (verified on a
Balance 3 Ti on 22 Nov 2026).

So that the contours do not disappear, the tile is put together from the
official contours read from Zepp's tile and from the marks. Contours stay
types 0x20 to 0x23 without the direction flag.

The watch picks the tile level by its position counted from the overview
level, not by its number of bits. At the 8 to 100 m scales it draws the
level four places below the overview: in a tile with five levels (base map,
our marks) the most detailed one, in a tile with six levels (Zepp's contours)
only the second one. The most detailed level of a six-level tile was not
drawn even at 8 m (verified on 22 Nov 2026 with two test tiles).

This matters because mkgmap simplifies lines in levels below 24 bits: of
6,561 mark points it kept 1,503 in a 23-bit level, and routes looked jagged
at 8 m. The merged tile therefore has the same structure as a mark tile:
five levels and the full 24 bits at 8 to 100 m.

    level 0  8 to 100 m      contours from level 0 of Zepp's tile
    level 1  200 to 750 m    contours from level 2
    level 2  1 and 1.5 km    contours from level 3
    level 3  not drawn       contours from level 4
    level 4  overview without data

The contours are placed in the levels by the scales Zepp prepared them for;
only at 8 to 100 m they come from a more detailed level. On photos from
22 Nov 2026 they show as thin olive lines.

From the original tiles, though, the watch draws contours only close up: at
25 and 50 m yes, at 100 m no longer (verified on 22 Nov 2026; 75 m not
checked). The merged tile behaved the same as the original one on Černá hora
in the Krkonoše mountains, so the contours stay in all levels (decided on
22 Nov 2026). Should they ever clutter the map, the coarser levels can be
left without them (see place_contours).

From the 2 km scale on, the watch does not draw a ski-format tile even in the
contour layer (as in the ski layer), so level 3 is never visible. It stays
because of the tile structure: without it the levels would shift by one
scale.
"""
from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Sequence

from . import imginfo, routes, skitile, swatch

CONTOUR_TYPES = (0x20, 0x21, 0x22, 0x23)

# Level bits of the merged tile, the same as for mark tiles.
LEVELS = list(swatch.BASE_LEVELS)
# Level of Zepp's tile the contours for levels 0 to 3 are taken from.
CONTOUR_SOURCE = [0, 2, 3, 4]
# Spacing of parallel marks in metres for levels 0 to 2; level 3 is the overview.
# Each level serves several scales at once and a line has a fixed width on
# the display, so it covers a different width on the ground at each scale.
# Widths in metres measured with the width swatch (width_tile) on 22 Nov
# 2026; "wide" are the cycle route, educational trail, yellow and black,
# "solid" are red, blue and green:
#
#   scale     8 m   15 m   25 m   50 m   75 m   100 m   200 m   300 m   500 m   1.5 km
#   solid     0.8   1.4    2.4    5.1    7.1    9.3     19      29      39      40
#   wide      0.9   1.7    2.9    6.1    9.0    11.3    19      29      39      40
#
# (300 and 500 m estimated from photos, the rest from the swatch; a cable car
# is about half as wide as a solid mark.)
#   level 0: at 6 m the marks just touch at 50 m and the path shows between
#            them at 8 to 25 m. At 75 m they overlap by a third, at 100 m by
#            half: an overlapped cycle route loses its white dashes there,
#            which would stay from 7 m on.
#   level 1: at 28 m there is a gap of about half a line width at 200 m,
#            they touch at 300 m and overlap by a quarter at 500 m.
#   level 2: at 40 m the marks touch at 1.5 km.
SPACING = [6.0, 28.0, 40.0]


def contour_levels(data: bytes) -> List[int]:
    """Level bits of a tile from the most detailed; the last one is the overview without data."""
    info = imginfo.read_tile(data)
    by_zoom = {zoom: bits for zoom, _inherited, bits, _count in info.levels}
    return [by_zoom[zoom] for zoom in sorted(by_zoom)]


def read_contours(data: bytes) -> List[routes.Line]:
    """Contours from Zepp's tile as lines (type, elevation in feet, points, level)."""
    lines = []
    for element in imginfo.iter_elements(data, with_points=True):
        code = element.type_byte & 0x3F
        if element.kind == "line" and code in CONTOUR_TYPES and len(element.points) >= 2:
            lines.append((code, element.label, element.points, element.level))
    return lines


def place_contours(contours: List[routes.Line], sources: Optional[Sequence[int]] = None) -> List[routes.Line]:
    """Contours moved from the levels of Zepp's tile to the levels of the merged tile.

    sources are the levels of Zepp's tile for levels 0, 1, ... of the merged
    tile (default CONTOUR_SOURCE). A shorter list leaves the coarser levels
    without contours; [0], for example, gives them only to the 8 to 100 m
    scales.
    """
    target = {source: level for level, source in enumerate(CONTOUR_SOURCE if sources is None else sources)}
    return [(code, label, points, target[level]) for code, label, points, level in contours if level in target]


def mark_lines(ways: List[routes.RouteWay], lifts: Iterable[routes.Lift] = ()) -> List[routes.Line]:
    """Lines of marks and cable cars for the levels of the merged tile, not clipped yet."""
    data_levels = len(LEVELS) - 1
    return (routes.spread(routes.merge_ways(ways), SPACING, data_levels)
            + routes.lift_lines(lifts, data_levels))


def supported(ctl_data: bytes) -> bool:
    """Does the contour tile have the structure and content the merge expects?

    That is six levels as Zepp's tiles have, holding only contours and the
    background. Anything else would be lost in the merge, so such a tile is
    not merged.
    """
    info = imginfo.read_tile(ctl_data)
    return (len(info.levels) == len(swatch.CONTOUR_LEVELS)
            and set(info.lines) <= set(CONTOUR_TYPES)
            and set(info.polygons) <= {swatch.BACKGROUND}
            and not info.points)


def merged_source(x: int, y: int, marks: List[routes.Line], ctl_data: Optional[bytes],
                  contour_source: Optional[Sequence[int]] = None) -> str:
    """Source of a contour tile with marks, for mkgmap.

    marks are lines from mark_lines(). Contours go to the level drawn at the
    same scales as their level in Zepp's tile (contour_source, default
    CONTOUR_SOURCE; see place_contours). Without ctl_data (the watch has no
    contours for the tile) the tile holds only marks.
    """
    contours: List[routes.Line] = []
    if ctl_data is not None:
        if not supported(ctl_data):
            raise skitile.SkiTileError("the contour tile has a different structure or content than Zepp's "
                                       "(%d levels instead of %d, or objects other than contours)" % (
                                           len(contour_levels(ctl_data)), len(swatch.CONTOUR_LEVELS)))
        contours = place_contours(read_contours(ctl_data), contour_source)
    north, east, south, west = swatch.map_bounds(x, y)
    clipped = [(code, label, piece, level) for code, label, points, level in marks
               for piece in routes.clip_line(points, north, east, south, west)]
    return routes.routes_mp(x, y, clipped, levels=LEVELS, plain=contours)


def merged_tile(
    x: int, y: int, marks: List[routes.Line], ctl_data: Optional[bytes],
    compiler: Optional[Callable[[str, str], bytes]] = None,
    contour_source: Optional[Sequence[int]] = None,
) -> bytes:
    """A contour tile with marks, in the ski layer format."""
    compiler = compiler or swatch.compile_mp
    data = compiler(merged_source(x, y, marks, ctl_data, contour_source), swatch.tile_name(x, y))
    return skitile.to_ski_tile(data, plain=CONTOUR_TYPES)


# ------------------------------------------------------------ level probe

# Every level gets a different ski layer line.
PROBE_TYPES = [0x43, 0x46, 0x49, 0x5C, 0x40, 0x54, 0x5A, 0x57, 0x51]
# Result from 22 Nov 2026 for these seven levels: at 8, 15, 25, 50, 75 and
# 100 m the watch drew L22 (four places below the overview), at 200 to 750 m
# L21 (three places below it). L24 and L23 were never drawn, so extra levels
# do not add a separate step for the closest scales. At 1.5 km nothing from
# level L20 showed; its type 0x40 is probably not drawn there.
PROBE_LEVELS = [24, 23, 22, 21, 20, 19, 17]
PROBE_SPACING = 25.0  # metres between lines; coarser levels round it to their grid


def probe_source(x: int, y: int, levels: List[int] = PROBE_LEVELS) -> str:
    """A tile in which every level has its own lines with the number of bits in the label.

    Lines are dense in all levels, so at any scale at least one is on the
    display, and its label tells which level the watch draws.
    """
    north, east, south, west = swatch.map_bounds(x, y)
    step = PROBE_SPACING / 111_320.0
    lines: List[routes.Line] = []
    for level, bits in enumerate(levels[:-1]):
        code = PROBE_TYPES[level % len(PROBE_TYPES)]
        lat = south + step / 2
        while lat < north:
            lines.append((code, "L%d" % bits, [(lat, west), (lat, east)], level))
            lat += step
    return routes.routes_mp(x, y, lines, levels=levels)


def probe_tile(x: int, y: int, compiler: Optional[Callable[[str, str], bytes]] = None) -> bytes:
    compiler = compiler or swatch.compile_mp
    return skitile.to_ski_tile(compiler(probe_source(x, y), swatch.tile_name(x, y)))


def describe_probe(levels: List[int] = PROBE_LEVELS) -> List[str]:
    """Legend lines: label, level and the line it is drawn with."""
    names = {0x43: "solid red", 0x46: "solid blue", 0x49: "solid green", 0x5C: "orange with yellow edge",
             0x40: "solid black", 0x54: "red with white dashes", 0x5A: "green with white dashes",
             0x57: "blue with white dashes", 0x51: "black with white dashes"}
    return ["    L%d  level %d (%d. from the overview), %s" % (
        bits, level, len(levels) - 1 - level, names[PROBE_TYPES[level % len(PROBE_TYPES)]])
        for level, bits in enumerate(levels[:-1])]


# ------------------------------------------------------------ width swatch

# All lines we put in the layer: marks in MARK_ORDER and the cable car.
WIDTH_TYPES = [routes.MARK_TYPES[mark] for mark in routes.MARK_ORDER] + [routes.LIFT_TYPE]
# Distance of neighbouring lines in grid steps of the level. The lines lie
# exactly on the grid, so mkgmap does not move them, and both the scale and
# the line width can be read from a photo.
WIDTH_STEPS = [12, 8, 5, 4]


def width_spacing(level: int) -> float:
    """Distance of neighbouring swatch lines in degrees of latitude."""
    return WIDTH_STEPS[level] * 360.0 / (1 << LEVELS[level])


def width_source(x: int, y: int) -> str:
    """Width swatch: horizontal lines of all types in all levels, labelled with the type code.

    It has the same structure as the merged tile, so each scale shows the
    level the watch draws for marks. Neighbouring lines are several times
    their width apart; from a photo with the scale bar you can measure how
    much each type covers at that scale and set SPACING accordingly.
    """
    north, east, south, west = swatch.map_bounds(x, y)
    lines: List[routes.Line] = []
    for level in range(len(WIDTH_STEPS)):
        step = width_spacing(level)
        row = int(south / step) + 1
        while row * step < north:
            code = WIDTH_TYPES[row % len(WIDTH_TYPES)]
            lines.append((code, "%02X" % code, [(row * step, west), (row * step, east)], level))
            row += 1
    return routes.routes_mp(x, y, lines, levels=LEVELS)


def width_tile(x: int, y: int, compiler: Optional[Callable[[str, str], bytes]] = None) -> bytes:
    compiler = compiler or swatch.compile_mp
    return skitile.to_ski_tile(compiler(width_source(x, y), swatch.tile_name(x, y)))


def describe_widths() -> List[str]:
    """Legend of the width swatch: line codes and line distances in each level."""
    names = [routes.MARK_NAMES[mark] for mark in routes.MARK_ORDER] + ["cable car"]
    rows = ["    %02X  %s" % (code, name) for code, name in zip(WIDTH_TYPES, names)]
    scales = ["8 to 100 m", "200 to 750 m", "1 and 1.5 km", "2 km and more"]
    rows += ["    level %d (%s): lines every %.1f m" % (level, scales[level], width_spacing(level) * 111_320.0)
             for level in range(len(WIDTH_STEPS))]
    return rows
