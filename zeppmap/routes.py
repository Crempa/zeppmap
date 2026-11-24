# SPDX-License-Identifier: AGPL-3.0-or-later
"""Marked routes: hiking marks and cycle routes from OpenStreetMap as an
overlay drawn with the line types of the watch's ski layer.

Steps: download the routes (route relations) for an area from the Overpass
service, give every way the marks that run along it, spread parallel marks
side by side, clip the lines to tiles and compile those as ski tiles (see
skitile).
"""
from __future__ import annotations

import bisect
import json
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__, skitile, swatch
from .errors import ZeppmapError
from .imginfo import format_code

Point = Tuple[float, float]  # (latitude, longitude)

# How the watch draws the ski layer types is in docs/. The layer has no
# yellow, hence orange with a yellow edge.
MARK_TYPES = {
    "red": 0x43,       # solid red
    "blue": 0x46,      # solid blue
    "green": 0x49,     # solid bluish green
    "yellow": 0x5C,    # orange with a yellow edge
    "black": 0x50,     # black with a yellow edge (Polish marking)
    "learning": 0x5A,  # green with white dashes
    "cycle": 0x54,     # red with white dashes
}
MARK_ORDER = ["red", "blue", "green", "yellow", "black", "learning", "cycle"]
MARK_NAMES = {"red": "red", "blue": "blue", "green": "green", "yellow": "yellow", "black": "black",
              "learning": "educational trail", "cycle": "cycle route"}
KCT_COLOURS = ("red", "blue", "green", "yellow")

# Values of the kct_<colour> tag: the stripe mark and the shaped marks of side
# trails. Other values (KČT ski, horse riding and cycling marks) do not belong
# in the hiking layer.
_KCT_HIKING = {"major", "local", "peak", "ruin", "spring", "interesting_object"}
_KCT_LEARNING = "learning"
# A KČT mark in osmc:symbol: a colour on a white background, stripe or side trail shape.
# Marking in Poland looks the same and has black in addition.
_KCT_SYMBOL = re.compile(
    r"^(red|blue|green|yellow|black):white:(red|blue|green|yellow|black)_(bar|corner|turned_T|bowl|triangle|L|backslash)$"
)

# Alpine marking (Austria, Bavaria, Italy): a red-white-red mark, red with a
# white stripe for the Italian CAI. Trails differ by number, not by colour.
# The first symbol field (the colour for drawing on a map) varies and says
# nothing about the mark on the ground; the shape may be followed by text.
_ALPINE_SYMBOL = re.compile(r"^[a-z_]*:red:white_(bar|stripe)(:.*)?$")
# A longer trail "number" tends to be an abbreviated name that would only clutter the map.
_TRAIL_REF_LENGTH = 8
# The most route numbers shown on one line.
_LABEL_REFS = 4

# Spacing of parallel marks in metres for zoom levels 0 (most detailed) to 2,
# for tiles of the ski layer (the contour layer has its own, see
# ctlmerge.SPACING). A line has a width on the display given by the scale, so
# when zooming out it covers more metres and marks with the same spacing
# would merge. Each level therefore has its own line positions. From photos
# of the watch:
#   level 0: scales 8 to 100 m, line about 10 pixels
#   level 1: scales 200, 300, 500 and 750 m, line narrows from 10 to 3 pixels
#   level 2: scales 1 km and 1.5 km, line only about 2 pixels
# From the 2 km scale the watch does not draw the ski layer at all.
# From 200 m the line narrows as fast as the map zooms out, so levels 1 and 2
# come out almost the same.
LEVEL_SPACING = [9.0, 36.0, 40.0]
# Further levels are the overview for the largest zoom-out: the route network
# is so dense there that marks are not spread. The watch does not show it so
# far (see above); it is here in case the firmware changes. Each way is
# represented by one hiking mark; only long-distance cycle routes are drawn,
# and only where no hiking mark runs.
OVERVIEW_HIKING = ("red", "blue", "green", "yellow", "black")
OFFSET_SPACING = LEVEL_SPACING[0]

# Cable cars that carry people through the air. Drag lifts are not drawn; they
# do not run in summer and there would be too many around ski resorts.
LIFT_KINDS = ("cable_car", "gondola", "chair_lift", "mixed_lift")
# A thin ochre line with purple cross ticks, the same as 0x62 and 0x63, which
# Zepp's map uses for lifts. Type 0x63 is stored the same way as contour 0x23
# though and could not be told apart in the contour layer; 0x64 is stored as
# 0x24, which contours do not use.
LIFT_TYPE = 0x64
_LIFT_NAME_LENGTH = 40

# Increase when the query changes, so that data downloaded earlier is not taken as complete.
QUERY_VERSION = 2

OVERPASS_SERVERS = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

_METERS_PER_DEGREE = 111_320.0


class RoutesError(ZeppmapError):
    pass


# --------------------------------------------------------------- download

def area_of(tiles: Iterable[Tuple[int, int]]) -> Tuple[float, float, float, float]:
    """Rectangle (south, west, north, east) covering the given tiles."""
    bounds = [swatch.tile_bounds(x, y) for x, y in tiles]
    if not bounds:
        raise RoutesError("no tile was given")
    return (min(b[2] for b in bounds), min(b[3] for b in bounds), max(b[0] for b in bounds), max(b[1] for b in bounds))


def overpass_query(area: Tuple[float, float, float, float]) -> str:
    box = "%.5f,%.5f,%.5f,%.5f" % area
    # Relations carry only the mark and the member list. Geometry is downloaded
    # with the ways, in full: the same way then comes out the same in every
    # area that contains it, and the data of neighbouring areas can be joined.
    return (
        "[out:json][timeout:180];\n"
        'relation["type"="route"]["route"~"^(hiking|foot|bicycle|mtb)$"](%s)->.r;\n'
        ".r out body;\n"
        "way(r.r)(%s);\n"
        "out ids geom;\n"
        'way["aerialway"~"^(%s)$"](%s);\n'
        "out tags geom;\n" % (box, box, "|".join(LIFT_KINDS), box)
    )


def fetch(
    area: Tuple[float, float, float, float],
    report: Callable[[str], None] = print,
    servers: Sequence[str] = OVERPASS_SERVERS,
    rounds: int = 4,
    opener: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Download the routes for an area. Public servers are often busy, so it retries."""
    body = urllib.parse.urlencode({"data": overpass_query(area)}).encode("ascii")
    problem = ""
    for attempt in range(rounds):
        for server in servers:
            host = urllib.parse.urlparse(server).netloc
            request = urllib.request.Request(server, data=body, headers={"User-Agent": "zeppmap/%s (+https://github.com/Crempa/zeppmap)" % __version__})
            try:
                with opener(request, timeout=200) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if "elements" in data:
                    report("downloaded from %s: %d routes" % (
                        host, sum(1 for element in data["elements"] if element.get("type") == "relation")))
                    return data
                problem = "%s returned a reply without data" % host
            except (urllib.error.URLError, OSError, ValueError) as exc:
                problem = "%s: %s" % (host, exc)
            report("failed (%s), trying the next one…" % problem)
        if attempt + 1 < rounds:
            sleep(10.0 * 2 ** attempt)
    raise RoutesError("could not download the routes: %s" % problem)


# ------------------------------------------------------------- processing

@dataclass
class RouteWay:
    """One way and the marks that run along it."""
    geometry: List[Point]
    marks: set = field(default_factory=set)       # "red", "blue", "green", "yellow", "cycle"
    cycle_refs: set = field(default_factory=set)  # cycle route numbers
    cycle_major: bool = False                     # a long-distance cycle route runs here, see major_cycle()
    trail_refs: set = field(default_factory=set)  # numbers of hiking trails with alpine marking


def mark_of(tags: Dict[str, str]) -> Optional[str]:
    """The mark of a route by its tags, or None when we cannot draw it."""
    route = tags.get("route", "")
    if route in ("bicycle", "mtb"):
        return "cycle"
    if route not in ("hiking", "foot"):
        return None
    # Only KČT marking counts. Other routes (pilgrim ways with a shell, local
    # loops with their own symbol) also have a colour in osmc:symbol but are not KČT marks.
    for colour in KCT_COLOURS:
        value = tags.get("kct_" + colour)
        if value == _KCT_LEARNING:
            return "learning"
        if value in _KCT_HIKING:
            return colour
        if value:
            return None
    symbol = tags.get("osmc:symbol", "").strip()
    match = _KCT_SYMBOL.match(symbol)
    if match and match.group(1) == match.group(2):
        return "learning" if match.group(3) == "backslash" else match.group(1)
    if _ALPINE_SYMBOL.match(symbol):
        return "red"
    return None


def trail_ref(tags: Dict[str, str]) -> str:
    """Number of a hiking trail to show on the line, or an empty string.

    Only for alpine marking: there all trails have the same mark and differ
    by number. KČT marks are told apart by colour and have no number.
    """
    if tags.get("route") not in ("hiking", "foot") or any(tags.get("kct_" + colour) for colour in KCT_COLOURS):
        return ""
    if not _ALPINE_SYMBOL.match(tags.get("osmc:symbol", "").strip()):
        return ""
    ref = tags.get("ref", "").strip()
    return ref if len(ref) <= _TRAIL_REF_LENGTH else ""


@dataclass
class Lift:
    """A cable car: name without diacritics and geometry."""
    name: str
    geometry: List[Point]


def plain_label(text: str, limit: int = _LIFT_NAME_LENGTH) -> str:
    """A name converted to ASCII.

    The watch turns capitals into lower case only for letters without
    diacritics, so a name with carons would come out half in capitals (like
    "ŠpindlerŮv Mlýn" in the official map). Without diacritics it reads better.
    """
    replaced = text.translate(_PLAIN_CHARS)
    stripped = "".join(ch for ch in unicodedata.normalize("NFKD", replaced) if not unicodedata.combining(ch))
    plain = " ".join(stripped.encode("ascii", "ignore").decode("ascii").split())
    return plain[:limit].rstrip()


_PLAIN_CHARS = str.maketrans({"ł": "l", "Ł": "L", "ß": "ss", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
                              "–": "-", "—": "-", "⇔": "-", "↔": "-", "→": "-", "⇒": "-", "„": '"', "“": '"', "”": '"'})


def parse_lifts(data: dict) -> List[Lift]:
    """Cable cars from an Overpass reply (older data does not contain them)."""
    lifts: Dict[int, Lift] = {}
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        if element.get("type") != "way" or tags.get("aerialway") not in LIFT_KINDS:
            continue
        points = [(node["lat"], node["lon"]) for node in element.get("geometry") or [] if node]
        if len(points) >= 2:
            lifts[element.get("id", len(lifts))] = Lift(plain_label(tags.get("name", "")), points)
    return list(lifts.values())


def lift_lines(lifts: Iterable[Lift], data_levels: Optional[int] = None) -> List["Line"]:
    """Cable car lines for all zoom levels; they run through the air, so they are not spread apart from anything."""
    count = len(swatch.BASE_LEVELS) - 1 if data_levels is None else data_levels
    return [(LIFT_TYPE, lift.name, list(lift.geometry), level) for level in range(count) for lift in lifts]


def major_cycle(tags: Dict[str, str]) -> bool:
    """Is the cycle route a long-distance one (international, national or regional)?"""
    network = tags.get("network")
    if network in ("icn", "ncn"):
        return True
    # Four-digit routes are local in Czech marking, even when OpenStreetMap lists them as regional.
    ref = tags.get("ref", "").strip()
    return network == "rcn" and not (ref.isdigit() and len(ref) >= 4)


def parse(data: dict) -> List[RouteWay]:
    """Turn an Overpass reply into a list of ways with marks.

    The geometry is either with the ways (query from overpass_query) or, in
    older data, directly with the relation members, clipped to the downloaded area.
    """
    elements = data.get("elements", [])
    shapes: Dict[int, List[Point]] = {}
    for element in elements:
        if element.get("type") == "way" and element.get("geometry"):
            shapes[element["id"]] = [(node["lat"], node["lon"]) for node in element["geometry"] if node]

    ways: Dict[tuple, RouteWay] = {}
    for element in elements:
        if element.get("type") != "relation":
            continue
        attrs = route_attrs(element.get("tags", {}))
        if attrs is None:
            continue
        for member in element.get("members", []):
            if member.get("type") != "way":
                continue
            runs: List[List[Point]] = [[]]
            if member.get("geometry"):
                # Outside the downloaded area the way has gaps instead of points; contiguous runs are taken.
                for node in member["geometry"]:
                    if node is None:
                        runs.append([])
                    else:
                        runs[-1].append((node["lat"], node["lon"]))
            elif member.get("ref") in shapes:
                runs = [shapes[member["ref"]]]
            for index, run in enumerate(run for run in runs if len(run) >= 2):
                _mark_way(ways, (member["ref"], index), run, attrs)
    return list(ways.values())


@dataclass(frozen=True)
class RouteAttrs:
    """What a route adds to the ways it runs along."""
    mark: str
    cycle_ref: str = ""    # cycle route number
    major: bool = False    # long-distance cycle route
    trail: str = ""        # number of a hiking trail with alpine marking


def route_attrs(tags: Dict[str, str]) -> Optional[RouteAttrs]:
    """Mark and numbers of a route by its tags, or None when the route is not drawn."""
    mark = mark_of(tags)
    if mark is None:
        return None
    if mark == "cycle":
        return RouteAttrs(mark, tags.get("ref", "").strip(), major_cycle(tags))
    return RouteAttrs(mark, trail=trail_ref(tags) if mark == "red" else "")


def _mark_way(ways: Dict[tuple, RouteWay], key: tuple, run: List[Point], attrs: RouteAttrs) -> None:
    way = ways.setdefault(key, RouteWay(run))
    way.marks.add(attrs.mark)
    if attrs.cycle_ref:
        way.cycle_refs.add(attrs.cycle_ref)
    if attrs.major:
        way.cycle_major = True
    if attrs.trail:
        way.trail_refs.add(attrs.trail)


def ways_of(relations: Iterable[Tuple[RouteAttrs, Iterable[int]]], shapes: Dict[int, List[Point]]) -> List[RouteWay]:
    """Ways with marks from routes given as (attributes, way ids) and from way geometries.

    A memory-saving counterpart of parse() for large areas: geometries are
    supplied only for the ways needed right now.
    """
    ways: Dict[tuple, RouteWay] = {}
    for attrs, members in relations:
        for ref in members:
            run = shapes.get(ref)
            if run is not None and len(run) >= 2:
                _mark_way(ways, (ref, 0), run, attrs)
    return list(ways.values())


def summary(ways: List[RouteWay]) -> Dict[str, float]:
    """Length of marked routes in kilometres by mark."""
    totals = {mark: 0.0 for mark in MARK_ORDER}
    for way in ways:
        length = sum(_distance(a, b) for a, b in zip(way.geometry, way.geometry[1:]))
        for mark in way.marks:
            totals[mark] += length / 1000.0
    return totals


def _distance(a: Point, b: Point) -> float:
    dy = (b[0] - a[0]) * _METERS_PER_DEGREE
    dx = (b[1] - a[1]) * _METERS_PER_DEGREE * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


# --------------------------------------------------------------- geometry

def oriented(points: List[Point]) -> List[Point]:
    """The way turned to run from west to east (if equal, from south to north).

    Parallel marks are offset to the right in the direction of the way. A
    uniform orientation keeps the order of marks the same even where ways
    drawn in opposite directions meet.
    """
    first, last = points[0], points[-1]
    return points if (first[1], first[0]) <= (last[1], last[0]) else points[::-1]


def offset_line(points: List[Point], meters: float) -> List[Point]:
    """The line offset sideways by the given number of metres, positive to the right.

    The outer side of a sharp bend is rounded so that the corner does not
    shoot far from the route. On the inner side, points that would lie closer
    to the original line than the offset are dropped; otherwise small bends
    would make loops in the offset line.
    """
    if meters == 0 or len(points) < 2:
        return list(points)
    scale_x = _METERS_PER_DEGREE * math.cos(math.radians(sum(p[0] for p in points) / len(points)))
    flat: List[Tuple[float, float]] = []
    for p in points:
        xy = (p[1] * scale_x, p[0] * _METERS_PER_DEGREE)
        if not flat or xy != flat[-1]:
            flat.append(xy)
    if len(flat) < 2:
        return list(points)

    directions = []  # unit direction of each segment
    lengths = []     # length of each segment
    reached = [0.0]  # line length from the start to each point
    for (x0, y0), (x1, y1) in zip(flat, flat[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        directions.append(((x1 - x0) / length, (y1 - y0) / length))
        lengths.append(length)
        reached.append(reached[-1] + length)
    distance = abs(meters)

    def beside(index: int, nx: float, ny: float, factor: float = 1.0) -> Tuple[int, float, float]:
        x, y = flat[index]
        return index, x + nx * meters * factor, y + ny * meters * factor

    moved = [beside(0, directions[0][1], -directions[0][0])]
    for index in range(1, len(flat) - 1):
        (ax, ay), (bx, by) = directions[index - 1], directions[index]
        before, after = (ay, -ax), (by, -bx)  # normals to the right
        turn = ax * bx + ay * by              # cosine of the bend angle
        outer = (ax * by - ay * bx) * meters > 0
        mx, my = before[0] + after[0], before[1] + after[1]
        length = math.hypot(mx, my)
        if length < 1e-9:  # a U-turn: the line goes round the end
            x, y = flat[index]
            moved += [beside(index, *before), (index, x + ax * distance, y + ay * distance), beside(index, *after)]
            continue
        mx, my = mx / length, my / length
        if outer and turn < -0.2:
            moved += [beside(index, *before), beside(index, mx, my), beside(index, *after)]
            continue
        if not outer:
            # An inner corner cuts a piece off both segments; if it is longer than the segment, there is no corner.
            cut = distance * math.sqrt(max(1.0 - turn, 0.0) / max(1.0 + turn, 1e-9))
            if cut > min(lengths[index - 1], lengths[index]):
                continue
        moved.append(beside(index, mx, my, 1.0 / max(mx * after[0] + my * after[1], 0.1)))
    moved.append(beside(len(flat) - 1, directions[-1][1], -directions[-1][0]))

    result = []
    for position, (index, x, y) in enumerate(moved):
        if 0 < position < len(moved) - 1 and _near_line(flat, reached, index, x, y, distance):
            continue
        result.append((y / _METERS_PER_DEGREE, x / scale_x))
    return result


def _near_line(flat, reached, index: int, x: float, y: float, distance: float) -> bool:
    """Does the point lie closer to the line than it should? Only near the point it came from is searched."""
    window = 8.0 * distance
    limit = (0.92 * distance) ** 2
    first = bisect.bisect_left(reached, reached[index] - window)
    last = bisect.bisect_right(reached, reached[index] + window)
    for i in range(max(first - 1, 0), min(last, len(flat) - 1)):
        (x0, y0), (x1, y1) = flat[i], flat[i + 1]
        dx, dy = x1 - x0, y1 - y0
        along = ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)
        along = min(max(along, 0.0), 1.0)
        if (x - x0 - along * dx) ** 2 + (y - y0 - along * dy) ** 2 < limit:
            return True
    return False


def _clip_segment(a: Point, b: Point, north: float, east: float, south: float, west: float):
    """The part of a segment inside a rectangle (Liang-Barsky), or None."""
    t0, t1 = 0.0, 1.0
    d_lat, d_lon = b[0] - a[0], b[1] - a[1]
    for delta, low, high in ((d_lat, south - a[0], north - a[0]), (d_lon, west - a[1], east - a[1])):
        if delta == 0:
            if low > 0 or high < 0:
                return None
            continue
        enter, leave = low / delta, high / delta
        if enter > leave:
            enter, leave = leave, enter
        t0, t1 = max(t0, enter), min(t1, leave)
        if t0 > t1:
            return None
    start = a if t0 == 0.0 else (a[0] + d_lat * t0, a[1] + d_lon * t0)
    end = b if t1 == 1.0 else (a[0] + d_lat * t1, a[1] + d_lon * t1)
    return start, end


def clip_line(points: List[Point], north: float, east: float, south: float, west: float) -> List[List[Point]]:
    """The pieces of a line that lie inside a rectangle."""
    pieces: List[List[Point]] = []
    current: List[Point] = []

    def close() -> None:
        nonlocal current
        if len(current) >= 2:
            pieces.append(current)
        current = []

    for a, b in zip(points, points[1:]):
        clipped = _clip_segment(a, b, north, east, south, west)
        if clipped is None or clipped[0] == clipped[1]:
            close()
            continue
        start, end = clipped
        if current and current[-1] == start:
            current.append(end)
        else:
            close()
            current = [start, end]
        if end != b:  # the line leaves the rectangle here
            close()
    close()
    return pieces


def simplify(points: List[Point], tolerance: float) -> List[Point]:
    """The line without points deviating less than tolerance metres from their neighbours' chord (Douglas-Peucker)."""
    if len(points) < 3 or tolerance <= 0:
        return list(points)
    scale_x = _METERS_PER_DEGREE * math.cos(math.radians(points[0][0]))
    flat = [(p[1] * scale_x, p[0] * _METERS_PER_DEGREE) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        (x0, y0), (x1, y1) = flat[first], flat[last]
        length = math.hypot(x1 - x0, y1 - y0)
        worst, index = 0.0, -1
        for i in range(first + 1, last):
            x, y = flat[i]
            if length == 0:
                distance = math.hypot(x - x0, y - y0)
            else:
                distance = abs((x1 - x0) * (y0 - y) - (x0 - x) * (y1 - y0)) / length
            if distance > worst:
                worst, index = distance, i
        if worst > tolerance:
            keep[index] = True
            stack += [(first, index), (index, last)]
    return [p for p, kept in zip(points, keep) if kept]


def merge_ways(ways: List[RouteWay]) -> List[RouteWay]:
    """Join connecting ways with the same marks into continuous routes.

    Parallel marks are then offset along the whole route at once, so there
    are no gaps or teeth where one way passes into the next.
    """
    groups: Dict[tuple, List[RouteWay]] = {}
    for way in ways:
        groups.setdefault((frozenset(way.marks), frozenset(way.cycle_refs), way.cycle_major,
                           frozenset(way.trail_refs)), []).append(way)

    merged: List[RouteWay] = []
    for (marks, refs, major, trails), members in groups.items():
        touching: Dict[Point, List[int]] = {}
        for index, way in enumerate(members):
            touching.setdefault(way.geometry[0], []).append(index)
            touching.setdefault(way.geometry[-1], []).append(index)
        used = [False] * len(members)

        def next_at(point: Point):
            # A route continues only where exactly two ways meet; it ends at a junction.
            candidates = touching.get(point, [])
            if len(candidates) != 2:
                return None
            free = [i for i in candidates if not used[i]]
            return free[0] if len(free) == 1 else None

        for start, way in enumerate(members):
            if used[start]:
                continue
            used[start] = True
            chain = list(way.geometry)
            for forward in (True, False):
                while True:
                    end = chain[-1] if forward else chain[0]
                    following = next_at(end)
                    if following is None:
                        break
                    used[following] = True
                    geometry = members[following].geometry
                    if forward:
                        chain += (geometry if geometry[0] == end else geometry[::-1])[1:]
                    else:
                        chain = (geometry if geometry[-1] == end else geometry[::-1])[:-1] + chain
            merged.append(RouteWay(chain, set(marks), set(refs), major, set(trails)))
    return merged


# ------------------------------------------------------------------ tiles

Line = Tuple[int, str, List[Point], int]  # (ski layer line type, label, points, zoom level)


def spread(ways: List[RouteWay], spacings: Sequence[float] = LEVEL_SPACING,
           data_levels: Optional[int] = None) -> List[Line]:
    """Mark lines for all zoom levels, not clipped to tiles yet.

    Parallel marks are spread by spacings (metres for level 0, 1, ...); the
    remaining levels up to data_levels are an overview with one line per way.
    The defaults apply to tiles with the structure of the base map. Ways are
    best joined with merge_ways() beforehand.
    """
    lines: List[Line] = []
    # The last level in BASE_LEVELS is the empty overview of the whole tile; no lines go there.
    for level in range(len(swatch.BASE_LEVELS) - 1 if data_levels is None else data_levels):
        overview = level >= len(spacings)
        spacing = 0.0 if overview else spacings[level]
        placed = []  # [mark, label, points, start of way, end of way]
        for way in ways:
            marks = overview_marks(way) if overview else [mark for mark in MARK_ORDER if mark in way.marks]
            if not marks:
                continue
            geometry = oriented(way.geometry)
            length = sum(_distance(a, b) for a, b in zip(geometry, geometry[1:]))
            # A piece shorter than two spacings would look like a tooth when spread; marks on it stay on top of each other.
            apart = spacing if len(marks) > 1 and length >= 2 * spacing else 0.0
            # With a larger spacing, small bends would make the offset line wavy for nothing.
            base = simplify(geometry, spacing / 3.0) if level and apart else geometry
            labels = {"cycle": _refs_label(way.cycle_refs), "red": _refs_label(way.trail_refs)}
            for index, mark in enumerate(marks):
                shift = (index - (len(marks) - 1) / 2.0) * apart
                placed.append([mark, labels.get(mark, ""), offset_line(base, shift), geometry[0], geometry[-1]])
        _join_ends(placed)
        lines += [(MARK_TYPES[mark], label, points, level) for mark, label, points, _start, _end in placed]
    return lines


def overview_marks(way: RouteWay) -> List[str]:
    """The mark representing the way in the overview, or nothing when the way is not drawn there."""
    for mark in OVERVIEW_HIKING:
        if mark in way.marks:
            return [mark]
    return ["cycle"] if "cycle" in way.marks and way.cycle_major else []


def _join_ends(placed: list) -> None:
    """Connect the ends of lines of the same mark that continue each other on the ground.

    Where the number of parallel marks changes, the line is offset
    differently before and after the change. Both ends are therefore pulled
    to a point halfway between them.
    """
    ends: Dict[tuple, List[Tuple[int, bool]]] = {}
    for index, (mark, _label, _points, start, end) in enumerate(placed):
        ends.setdefault((mark, start), []).append((index, True))
        ends.setdefault((mark, end), []).append((index, False))
    for meeting in ends.values():
        if len(meeting) != 2:  # end of a route or a junction of routes with the same mark
            continue
        (first, first_at_start), (second, second_at_start) = meeting
        a = placed[first][2][0 if first_at_start else -1]
        b = placed[second][2][0 if second_at_start else -1]
        if a == b:
            continue
        middle = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        for index, at_start in meeting:
            if at_start:
                placed[index][2].insert(0, middle)
            else:
                placed[index][2].append(middle)


def clip_lines(lines: List[Line], x: int, y: int) -> List[Line]:
    """The parts of lines that lie in the tile."""
    north, east, south, west = swatch.map_bounds(x, y)
    clipped: List[Line] = []
    for code, label, points, level in lines:
        if (max(p[0] for p in points) < south or min(p[0] for p in points) > north
                or max(p[1] for p in points) < west or min(p[1] for p in points) > east):
            continue
        for piece in clip_line(points, north, east, south, west):
            clipped.append((code, label, piece, level))
    return clipped


def tile_lines(ways: List[RouteWay], x: int, y: int) -> List[Line]:
    """Mark lines for one tile and all zoom levels."""
    return clip_lines(spread(ways), x, y)


def _ref_key(ref: str):
    # Equal numbers (6 and 06) are ordered by how they are written; otherwise the
    # order would be random and the tile would be built and uploaded on every run.
    return (0, int(ref), ref) if ref.isdigit() else (1, 0, ref)


def _refs_label(refs: Iterable[str]) -> str:
    """Route numbers sorted into a line label; when there are too many, a plus replaces the rest."""
    ordered = sorted(refs, key=_ref_key)
    return ", ".join(ordered[:_LABEL_REFS]) + (" +" if len(ordered) > _LABEL_REFS else "")


def routes_mp(x: int, y: int, lines: List[Line], levels: Optional[List[int]] = None,
              plain: Iterable[Line] = ()) -> str:
    """Source of a tile with marks in the Polish format.

    levels are the level bits (default as the base map). Lines in plain are
    written as they are, without converting to ski layer types (contours).
    """
    levels = list(levels or swatch.BASE_LEVELS)
    north, east, south, west = swatch.map_bounds(x, y)

    def point(p: Point) -> str:
        return "(%.7f,%.7f)" % p

    # Contour elevations are in feet in the tiles and stay that way.
    out = ["[IMG ID]", "ID=%s" % swatch.tile_name(x, y), "Name=OSM street map", "Elevation=F",
           "Transparent=N", "Levels=%d" % len(levels)]
    out += ["Level%d=%d" % (index, bits) for index, bits in enumerate(levels)]
    out += ["[END-IMG ID]", "",
            "[POLYGON]", "Type=0x%02x" % swatch.BACKGROUND, "EndLevel=%d" % (len(levels) - 1),
            "Data0=%s,%s,%s,%s" % (point((north, west)), point((north, east)), point((south, east)), point((south, west))),
            "[END]", ""]
    for code, label, points, level in plain:
        out += ["[POLYLINE]", "Type=%s" % format_code(code)]
        if label:
            out.append("Label=%s" % label.encode("ascii", "replace").decode("ascii"))
        out += ["EndLevel=%d" % level, "Data%d=%s" % (level, ",".join(point(p) for p in points)), "[END]", ""]
    for code, label, points, level in lines:
        compiled, direction = skitile.compile_type(code)
        out += ["[POLYLINE]", "Type=%s" % format_code(compiled)]
        if label:
            # Labels go into the file in ASCII; route numbers rarely have other characters.
            out.append("Label=%s" % label.encode("ascii", "replace").decode("ascii"))
        if direction:
            out.append("DirIndicator=1")
        # Data<N> together with EndLevel=<N> put the line in level N only.
        out += ["EndLevel=%d" % level, "Data%d=%s" % (level, ",".join(point(p) for p in points)), "[END]", ""]
    return "\n".join(out)


def build(
    ways: List[RouteWay],
    tiles: Iterable[Tuple[int, int]],
    out_dir: str,
    report: Callable[[str], None] = print,
    compiler: Optional[Callable[[str, str], bytes]] = None,
    lifts: Iterable[Lift] = (),
) -> int:
    """Build ski tiles with marks into out_dir/11/<x>/<y>.img. Returns their number."""
    compiler = compiler or swatch.compile_mp
    spread_lines = spread(merge_ways(ways)) + lift_lines(lifts)
    count = 0
    for x, y in tiles:
        lines = clip_lines(spread_lines, x, y)
        if not lines:
            report("%d/%d/%d.img: no marked routes, tile not built" % (swatch.ZOOM, x, y))
            continue
        data = skitile.to_ski_tile(compiler(routes_mp(x, y, lines), swatch.tile_name(x, y)))
        target = os.path.join(out_dir, str(swatch.ZOOM), str(x), "%d.img" % y)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        report("%d/%d/%d.img (%d B, %d lines)" % (
            swatch.ZOOM, x, y, len(data), sum(1 for line in lines if line[3] == 0)))
        count += 1
    if count == 0:
        raise RoutesError("there are no marked routes in the given tiles")
    return count


def legend(title: str = "Marked routes layer (shown as the ski layer)",
           spacings: Sequence[float] = LEVEL_SPACING) -> str:
    rows = [title, ""]
    looks = {"red": "solid red", "blue": "solid blue", "green": "solid bluish green",
             "yellow": "orange with a yellow edge", "black": "black with a yellow edge (Polish marking)",
             "learning": "green with white dashes",
             "cycle": "red with white dashes, route number in the label"}
    for mark in MARK_ORDER:
        rows.append("    %-18s %s  %s" % (MARK_NAMES[mark], format_code(MARK_TYPES[mark]), looks[mark]))
    rows.append("    %-18s %s  %s" % ("cable car", format_code(LIFT_TYPE),
                                      "thin ochre with purple cross ticks, name without diacritics in the label"))
    rows += ["", "Hiking routes without a KČT mark (pilgrim ways, local loops with their own symbol) are not drawn.",
             "Alpine red-white-red marking is drawn red with the trail number in the label."]
    rows += ["", "Parallel marks run side by side in this order: %s." % ", ".join(MARK_NAMES[mark] for mark in MARK_ORDER),
             "The spacing grows when zooming out so that marks do not merge: %s m by zoom level." % (
                 ", ".join("%d" % spacing for spacing in spacings)),
             "At the largest zoom-out one hiking mark stands for each way and only long-distance cycle routes remain."]
    return "\n".join(rows) + "\n"
