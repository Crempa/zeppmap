# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sync command: marks for every tile the watch has a base map for.

Steps: read from the watch which base map tiles it has and what is in the
target layer now. Download the routes for those tiles (in blocks, cached on
disk), build the mark tiles and upload only those that differ from the ones
in the watch.

The target is either the ski layer (marks alone) or the contour layer. There
the marks are merged with Zepp's original contours, which they would
otherwise overwrite; the original tiles are therefore saved to disk first
(ContourStore), and every later merged tile is built from that saved copy.

The map in the watch can have thousands of tiles, so nothing is held in
memory as a whole: blocks are read one at a time together with their nearest
neighbours, and tiles are built block by block.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from array import array
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import ctlmerge, imginfo, routes, skitile, swatch
from .errors import ZeppmapError
from .ftpops import FtpError, layer_dir

Tile = Tuple[int, int]
Box = Tuple[float, float, float, float]  # south, west, north, east

# Routes are downloaded in blocks of this many tiles in width and height. The
# public Overpass servers refuse or time out on one query for a large area.
BLOCK = 4
# Increase when the way tiles are built changes (mkgmap, conversion to ski
# tiles); it forces all of them to be built again.
BUILD_VERSION = "1"
MANIFEST = "sync.json"        # which source each tile was built from
UPLOADED = "uploaded.json"    # what was last uploaded to the watch
TILE_LIST = "tiles.json"      # list of tiles last read from the watch
QUERY_KEY = "zeppmap_query"
WORKERS = 4                   # how many tiles are compiled at once
CONTOURS_DIR = "zepp-contours"  # original contour tiles downloaded from the watch
BACKUPS_DIR = "backups"         # where push and sync back up the tiles they overwrite


class SyncError(ZeppmapError):
    pass


def tile_path(tile: Tile) -> str:
    return "%d/%d/%d.img" % (swatch.ZOOM, tile[0], tile[1])


# -------------------------------------------------------------------- watch

def _columns(client, layer: str) -> Dict[int, Dict[int, int]]:
    """Tiles of a layer in the watch as {x: {y: size}}; empty files do not count."""
    base = "%s/%d" % (layer_dir(layer), swatch.ZOOM)
    try:
        folders = client.listdir(base)
    except FtpError:
        return {}  # the watch does not have the layer
    found: Dict[int, Dict[int, int]] = {}
    for folder in folders:
        if not folder.is_dir or not folder.name.isdigit():
            continue
        for entry in client.listdir(base + "/" + folder.name):
            stem, extension = os.path.splitext(entry.name)
            if entry.is_dir or extension.lower() != ".img" or not stem.isdigit() or not entry.size:
                continue
            found.setdefault(int(folder.name), {})[int(stem)] = entry.size
    return found


def watch_sizes(client, layer: str = "ski") -> Dict[Tile, int]:
    """Sizes of the tiles the watch has in a layer."""
    return {(x, y): size for x, column in _columns(client, layer).items() for y, size in column.items()}


def watch_tiles(client, layer: str = "scl") -> List[Tile]:
    """Tiles the watch has in a layer."""
    return sorted(watch_sizes(client, layer))


def read_tiles(client, tiles: Iterable[Tile], layer: str = "ski") -> Dict[Tile, bytes]:
    """Content of the given tiles of a layer; tiles not in the watch are missing from the result."""
    sizes = watch_sizes(client, layer)
    found: Dict[Tile, bytes] = {}
    for tile in sorted(set(tiles)):
        if sizes.get(tile):
            found[tile] = client.read("%s/%s" % (layer_dir(layer), tile_path(tile)), sizes[tile])
    return found


def save_tiles(cache_dir: str, tiles: Iterable[Tile]) -> str:
    """Save the list of tiles read from the watch; useful for a later run without it (--like)."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, TILE_LIST)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(sorted(set(tiles)), handle)
    return path


def tiles_from(source: str) -> List[Tile]:
    """List of tiles from a 11/<x>/<y>.img folder or from a file saved by save_tiles."""
    if os.path.isdir(source):
        return swatch.tiles_in(source)
    try:
        with open(source, "r", encoding="utf-8") as handle:
            return sorted({(int(x), int(y)) for x, y in json.load(handle)})
    except (OSError, ValueError, TypeError) as exc:
        raise SyncError("cannot read the tile list %s (%s)" % (source, exc)) from None


# -------------------------------------------------------- original contours

class ContourStore:
    """Zepp's original contour tiles saved on disk (11/<x>/<y>.img).

    A merged tile overwrites the original contours in the watch. Their first
    copy is therefore saved here and never changes; every later merged tile
    is built from it. Only tiles in the original format are saved, never our
    own (ski format). The folder has the same layout as a source for the push
    command, so it can be used to put the original contours back.
    """

    def __init__(self, folder: str) -> None:
        self.folder = folder

    def path(self, tile: Tile) -> str:
        return os.path.join(self.folder, *tile_path(tile).split("/"))

    def has(self, tile: Tile) -> bool:
        return os.path.exists(self.path(tile))

    def read_relative(self, relative: str) -> Optional[bytes]:
        """Content of a tile by its name "11/<x>/<y>.img"; None when it is not saved."""
        try:
            with open(os.path.join(self.folder, *relative.split("/")), "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def read(self, tile: Tile) -> Optional[bytes]:
        return self.read_relative(tile_path(tile))

    def add(self, tile: Tile, data: bytes) -> bool:
        """Save an original tile. Returns whether the store has the original of the tile.

        A tile already saved is not overwritten, and a tile in any format
        other than the original one is not saved.
        """
        if self.has(tile):
            return True
        try:
            if imginfo.container_kind(data) != "garmin":
                return False
        except imginfo.ImgError:
            return False
        path = self.path(tile)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "wb") as handle:
            handle.write(data)
        os.replace(path + ".part", path)
        return True


def original_from_backups(tile: Tile, root: str = BACKUPS_DIR) -> Optional[bytes]:
    """The original contour tile from the oldest backup holding it in the original format.

    Backups of the contour layer are in <time>-ctl folders; later ones may
    already contain our earlier tiles.
    """
    try:
        folders = sorted(name for name in os.listdir(root) if name.endswith("-ctl"))
    except OSError:
        return None
    for name in folders:
        try:
            with open(os.path.join(root, name, *tile_path(tile).split("/")), "rb") as handle:
                data = handle.read()
            if imginfo.container_kind(data) == "garmin":
                return data
        except (OSError, imginfo.ImgError):
            continue
    return None


def fetch_contours(
    client,
    tiles: Iterable[Tile],
    store: ContourStore,
    sizes: Dict[Tile, int],
    uploaded: Optional[Dict[str, list]] = None,
    report: Callable[[str], None] = print,
    contents: Optional[Dict[Tile, bytes]] = None,
    backup_root: str = BACKUPS_DIR,
) -> Tuple[int, int, List[Tile]]:
    """Add to the store the original contours of tiles not in it yet.

    sizes are the sizes of the contour layer tiles in the watch, uploaded is
    the record of what we uploaded there ourselves (UploadRecord.items),
    contents is tile content already read, if the caller has it.

    Returns (read from the watch, restored from backups, tiles with an
    unknown original). The last ones have our tile in the watch and the
    original was not found in the backups either. They must not be
    overwritten: the merged tile would come out without contours and those
    would disappear from the watch.
    """
    uploaded = uploaded or {}
    contents = contents or {}
    missing = [tile for tile in sorted(set(tiles)) if sizes.get(tile) and not store.has(tile)]
    if missing:
        report("Original contours are missing on disk for %d tiles (%.0f MB), reading them from the watch…" % (
            len(missing), sum(sizes[tile] for tile in missing) / 1e6))
    fetched = restored = 0
    unknown: List[Tile] = []
    for index, tile in enumerate(missing, 1):
        noted = uploaded.get(tile_path(tile))
        data = None
        if not (noted and noted[0] == sizes[tile]):  # otherwise the record says it is our tile
            data = contents.get(tile)
            if data is None:
                data = client.read("%s/%s" % (layer_dir("ctl"), tile_path(tile)), sizes[tile])
        if data is not None and store.add(tile, data):
            fetched += 1
        else:
            backup = original_from_backups(tile, backup_root)
            if backup is not None and store.add(tile, backup):
                restored += 1
            else:
                unknown.append(tile)
        if len(missing) > 20 and (index % 50 == 0 or index == len(missing)):
            report("  %d of %d" % (index, len(missing)))
    return fetched, restored, unknown


# ------------------------------------------------------------------- routes

def plan(tiles: Iterable[Tile]) -> Dict[Tile, List[Tile]]:
    """Tiles split into the blocks routes are downloaded by: {block number: tiles}."""
    groups: Dict[Tile, List[Tile]] = {}
    for x, y in sorted(set(tiles)):
        groups.setdefault((x // BLOCK, y // BLOCK), []).append((x, y))
    return groups


def blocks(tiles: Iterable[Tile]) -> List[List[Tile]]:
    groups = plan(tiles)
    return [groups[key] for key in sorted(groups)]


def cache_name(block: List[Tile]) -> str:
    xs = [x for x, _y in block]
    ys = [y for _x, y in block]
    return "routes-x%d-%d-y%d-%d.json" % (min(xs), max(xs), min(ys), max(ys))


def _fresh(path: str, max_age_days: float, now: float) -> bool:
    """Is the file in the cache, not too old and downloaded by the current query?

    The query version is appended at the very end of the file, so reading
    its tail is enough. An unfinished file does not have it and counts as
    missing.
    """
    try:
        if now - os.path.getmtime(path) > max_age_days * 86400:
            return False
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 64))
            tail = handle.read()
    except OSError:
        return False
    return tail.rstrip().endswith(('"%s": %d}' % (QUERY_KEY, routes.QUERY_VERSION)).encode("ascii"))


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise SyncError(
            "cannot read the downloaded routes %s (%s). Delete that file and run the command again." % (path, exc)
        ) from None


def stale_blocks(tiles: Iterable[Tile], cache_dir: str, max_age_days: float = 30.0,
                 refresh: bool = False, now: Optional[float] = None) -> List[List[Tile]]:
    """Blocks whose routes must be downloaded again."""
    now = time.time() if now is None else now
    return [block for block in blocks(tiles)
            if refresh or not _fresh(os.path.join(cache_dir, cache_name(block)), max_age_days, now)]


def download(
    tiles: Iterable[Tile],
    cache_dir: str,
    max_age_days: float = 30.0,
    refresh: bool = False,
    report: Callable[[str], None] = print,
    fetch: Optional[Callable] = None,
    now: Optional[float] = None,
    pause: Callable[[float], None] = time.sleep,
) -> int:
    """Download routes for the blocks missing from the cache or out of date. Returns their number."""
    fetch = fetch or routes.fetch
    todo = stale_blocks(tiles, cache_dir, max_age_days, refresh, now)
    for index, block in enumerate(todo):
        if index:
            pause(2.0)  # public servers do not like queries right after each other
        area = routes.area_of(block)
        report("Downloading routes, block %d of %d (%d tiles, lat %.3f to %.3f, lon %.3f to %.3f)…" % (
            index + 1, len(todo), len(block), area[0], area[2], area[1], area[3]))
        data = fetch(area, report)
        data[QUERY_KEY] = routes.QUERY_VERSION
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, cache_name(block))
        with open(path + ".part", "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(path + ".part", path)
    return len(todo)


def load_routes(
    tiles: Iterable[Tile],
    cache_dir: str,
    max_age_days: float = 30.0,
    refresh: bool = False,
    report: Callable[[str], None] = print,
    fetch: Optional[Callable] = None,
    now: Optional[float] = None,
    pause: Callable[[float], None] = time.sleep,
) -> dict:
    """Routes for the given tiles joined into one data set.

    Only for small areas: everything is held in memory at once. Large areas
    are handled block by block by build_from_cache().
    """
    tiles = sorted(set(tiles))
    stale = {cache_name(block) for block in stale_blocks(tiles, cache_dir, max_age_days, refresh, now)}
    download(tiles, cache_dir, max_age_days, refresh, report, fetch, now, pause)
    elements: Dict[tuple, dict] = {}
    for block in blocks(tiles):
        name = cache_name(block)
        if name not in stale:
            report("Routes for %d tiles taken from the cache." % len(block))
        # A route or way can be in several blocks; once is enough. A cable car
        # that is also part of a route comes twice: once with the geometry
        # only, once with the tags too. The one with tags must stay.
        for element in _load(os.path.join(cache_dir, name)).get("elements", []):
            key = (element.get("type"), element.get("id", id(element)))
            if key not in elements or ("tags" in element and "tags" not in elements[key]):
                elements[key] = element
    return {"elements": list(elements.values())}


class BlockData:
    """One downloaded block in compact form: way geometries as arrays of numbers.

    A plainly loaded Overpass reply takes tens of megabytes of memory per
    block; this way, a few.
    """
    __slots__ = ("relations", "ways", "lifts")

    def __init__(self, data: dict) -> None:
        self.relations: Dict[int, Tuple[routes.RouteAttrs, array]] = {}
        self.ways: Dict[int, Tuple[Box, array]] = {}
        self.lifts: Dict[int, Tuple[str, Box, array]] = {}
        for element in data.get("elements", []):
            kind = element.get("type")
            if kind == "relation":
                attrs = routes.route_attrs(element.get("tags") or {})
                if attrs is not None:
                    members = array("q", (member["ref"] for member in element.get("members", [])
                                          if member.get("type") == "way"))
                    self.relations[element["id"]] = (attrs, members)
            elif kind == "way" and element.get("geometry"):
                points = array("d")
                for node in element["geometry"]:
                    if node:
                        points.append(node["lat"])
                        points.append(node["lon"])
                if len(points) < 4:
                    continue
                lats, lons = points[0::2], points[1::2]
                box = (min(lats), min(lons), max(lats), max(lons))
                tags = element.get("tags") or {}
                if tags.get("aerialway") in routes.LIFT_KINDS:
                    self.lifts[element["id"]] = (routes.plain_label(tags.get("name", "")), box, points)
                self.ways.setdefault(element["id"], (box, points))


class BlockCache:
    """Recently used blocks; neighbouring blocks are read repeatedly while walking an area."""

    def __init__(self, capacity: int = 12) -> None:
        self._capacity = capacity
        self._items: "OrderedDict[str, BlockData]" = OrderedDict()

    def get(self, path: str) -> BlockData:
        if path in self._items:
            self._items.move_to_end(path)
            return self._items[path]
        block = BlockData(_load(path))
        self._items[path] = block
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return block


def _points(values: array) -> List[routes.Point]:
    return list(zip(values[0::2], values[1::2]))


def gather(key: Tile, todo: Dict[Tile, List[Tile]], cache_dir: str,
           loader: Callable[[str], BlockData]) -> Tuple[List[routes.RouteWay], List[routes.Lift]]:
    """Ways with marks and cable cars for a block and a band of one tile around it.

    The surroundings are needed so that a route crossing into the next block
    comes out the same from both sides and the lines meet at the block border.
    """
    xs = [x for x, _y in todo[key]]
    ys = [y for _x, y in todo[key]]
    south, west, north, east = routes.area_of([(min(xs) - 1, min(ys) - 1), (max(xs) + 1, max(ys) + 1)])

    def near(box: Box) -> bool:
        return not (box[2] < south or box[0] > north or box[3] < west or box[1] > east)

    shapes: Dict[int, List[routes.Point]] = {}
    relations: Dict[int, Tuple[routes.RouteAttrs, array]] = {}
    lifts: Dict[int, routes.Lift] = {}
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            neighbour = (key[0] + dx, key[1] + dy)
            if neighbour not in todo:
                continue
            block = loader(os.path.join(cache_dir, cache_name(todo[neighbour])))
            for way_id, (box, values) in block.ways.items():
                if way_id not in shapes and near(box):
                    shapes[way_id] = _points(values)
            for relation_id, relation in block.relations.items():
                relations.setdefault(relation_id, relation)
            for way_id, (name, box, values) in block.lifts.items():
                if way_id not in lifts and near(box):
                    lifts[way_id] = routes.Lift(name, _points(values))
    return routes.ways_of(relations.values(), shapes), list(lifts.values())


# -------------------------------------------------------------------- tiles

def make_tile(source: str, name: str) -> bytes:
    """Compile a tile source and turn it into a ski layer tile."""
    return skitile.to_ski_tile(swatch.compile_mp(source, name))


def make_contour_tile(source: str, name: str) -> bytes:
    """Compile the source of a contour tile with marks; contours stay as they are."""
    return skitile.to_ski_tile(swatch.compile_mp(source, name), plain=ctlmerge.CONTOUR_TYPES)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".part", "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=0, sort_keys=True)
    os.replace(path + ".part", path)


def _read_manifest(out_dir: str) -> Dict[str, str]:
    return _read_json(os.path.join(out_dir, MANIFEST))


def _write_manifest(out_dir: str, manifest: Dict[str, str]) -> None:
    _write_json(os.path.join(out_dir, MANIFEST), manifest)


# Recipe for a tile: from the lines in the tile it makes (a description of what
# the tile is made from, a function that builds it). The description tells
# whether the tile changed since last time; building comes only after that.
# None means the tile cannot be built.
Recipe = Callable[[Tile, List[routes.Line]], Optional[Tuple[str, Callable[[], bytes]]]]


def _mark_recipe(make: Callable[[str, str], bytes]) -> Recipe:
    """Tiles of marks alone, for the ski layer."""
    def recipe(tile: Tile, tile_lines: List[routes.Line]):
        source = routes.routes_mp(tile[0], tile[1], tile_lines)
        return source, lambda: make(source, swatch.tile_name(tile[0], tile[1]))
    return recipe


def _contour_recipe(make: Callable[[str, str], bytes], store: ContourStore, skipped: List[Tile]) -> Recipe:
    """Mark tile merged with the original contours from the store.

    A tile without saved contours (the watch has none for it) comes out with
    marks only. A tile whose contours have an unknown structure is not built
    and is added to skipped.
    """
    def recipe(tile: Tile, tile_lines: List[routes.Line]):
        original = store.read(tile)
        if original is not None and not ctlmerge.supported(original):
            skipped.append(tile)
            return None
        # The full source with contours is large and slow to prepare. The marks
        # and a digest of the original tile are enough to detect a change.
        marks = routes.routes_mp(tile[0], tile[1], tile_lines, levels=ctlmerge.LEVELS)
        # The text of the description is part of the digest kept in the manifest;
        # changing it would rebuild and upload every tile again.
        stamp = hashlib.sha256(original).hexdigest() if original is not None else "bez vrstevnic"
        described = "vrstevnice %s %s\n%s" % (list(ctlmerge.CONTOUR_SOURCE), stamp, marks)

        def produce() -> bytes:
            source = ctlmerge.merged_source(tile[0], tile[1], tile_lines, original)
            return make(source, swatch.tile_name(tile[0], tile[1]))
        return described, produce
    return recipe


def _build_tiles(lines: List[routes.Line], tiles: Iterable[Tile], out_dir: str, manifest: Dict[str, str],
                 recipe: Recipe, workers: int) -> Tuple[List[Tile], List[Tuple[str, int]]]:
    """Build the tiles whose source changed since last time.

    Returns (tiles with marks, [(name, size)] of the newly built ones). An
    unchanged tile stays the same byte for byte, so it is clear that the
    watch already has it.
    """
    present: List[Tile] = []
    jobs = []
    for tile in sorted(set(tiles)):
        tile_lines = routes.clip_lines(lines, tile[0], tile[1])
        if not tile_lines:
            continue  # no marked route runs through the tile
        planned = recipe(tile, tile_lines)
        if planned is None:
            continue  # the tile cannot be built; it stays in the watch as it is
        described, produce = planned
        digest = hashlib.sha256((BUILD_VERSION + "\n" + described).encode("utf-8")).hexdigest()
        relative = tile_path(tile)
        target = os.path.join(out_dir, *relative.split("/"))
        present.append(tile)
        if manifest.get(relative) != digest or not os.path.exists(target):
            jobs.append((relative, target, produce, digest))

    def run(job) -> bytes:
        return job[2]()

    if workers > 1 and len(jobs) > 1:
        # Compilation runs in a separate process; the threads only wait for its result.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run, jobs))
    else:
        results = [run(job) for job in jobs]

    built = []
    for (relative, target, _produce, digest), data in zip(jobs, results):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)
        manifest[relative] = digest
        built.append((relative, len(data)))
    return present, built


def build(
    ways: List[routes.RouteWay],
    tiles: Iterable[Tile],
    out_dir: str,
    report: Callable[[str], None] = print,
    make: Optional[Callable[[str, str], bytes]] = None,
    lifts: Iterable[routes.Lift] = (),
) -> Tuple[List[Tile], int]:
    """Build mark tiles from ways held in memory; for small areas.

    Returns (tiles with marks, number of newly built tiles).
    """
    lines = routes.spread(routes.merge_ways(ways)) + routes.lift_lines(lifts)
    manifest = _read_manifest(out_dir)
    present, built = _build_tiles(lines, tiles, out_dir, manifest, _mark_recipe(make or make_tile), 1)
    for relative, size in built:
        report("%s built (%d B)" % (relative, size))
    if built:
        _write_manifest(out_dir, manifest)
    return present, len(built)


def build_from_cache(
    tiles: Iterable[Tile],
    cache_dir: str,
    out_dir: str,
    report: Callable[[str], None] = print,
    make: Optional[Callable[[str, str], bytes]] = None,
    workers: int = WORKERS,
    contours: Optional[ContourStore] = None,
    skipped: Optional[List[Tile]] = None,
    leave: Iterable[Tile] = (),
) -> Tuple[List[Tile], int]:
    """Build mark tiles from the downloaded blocks, block by block.

    With the contours store, tiles for the contour layer are built: marks
    merged with the original contours. Tiles that could not be built because
    of an unknown contour structure are added to skipped. Tiles in leave are
    left out.

    Returns (tiles with marks, number of newly built tiles).
    """
    if contours is None:
        recipe = _mark_recipe(make or make_tile)
    else:
        recipe = _contour_recipe(make or make_contour_tile, contours, skipped if skipped is not None else [])
    leave = set(leave)
    todo = plan(tiles)
    cache = BlockCache()
    manifest = _read_manifest(out_dir)
    present: List[Tile] = []
    rebuilt = 0
    for index, key in enumerate(sorted(todo), 1):
        ways, lifts = gather(key, todo, cache_dir, cache.get)
        if contours is None:
            lines = routes.spread(routes.merge_ways(ways)) + routes.lift_lines(lifts)
        else:
            lines = ctlmerge.mark_lines(ways, lifts)
        wanted = [tile for tile in todo[key] if tile not in leave]
        have, built = _build_tiles(lines, wanted, out_dir, manifest, recipe, workers)
        present += have
        rebuilt += len(built)
        if built:
            # The manifest is saved as we go, so an interruption does not mean building everything again.
            _write_manifest(out_dir, manifest)
        if len(todo) > 1:
            report("Block %d of %d: %d tiles with marks, %d newly built." % (
                index, len(todo), len(have), len(built)))
    return sorted(present), rebuilt


# --------------------------------------------------------------- comparison

def _local(out_dir: str, tile: Tile) -> bytes:
    with open(os.path.join(out_dir, *tile_path(tile).split("/")), "rb") as handle:
        return handle.read()


def changed_tiles(out_dir: str, present: Iterable[Tile], remote: Dict[Tile, bytes]) -> List[Tile]:
    """Tiles whose content differs from what is in the watch (or that are missing there)."""
    return [tile for tile in present if _local(out_dir, tile) != remote.get(tile)]


class UploadRecord:
    """Record of what was uploaded to the watch: size and digest of every tile.

    Thanks to it, the next run only needs to read the sizes from the watch. A
    tile counts as unchanged when it has the same size in the watch as when
    it was uploaded and the local file has the same digest.
    """

    def __init__(self, out_dir: str) -> None:
        self._path = os.path.join(out_dir, UPLOADED)
        self.items: Dict[str, list] = _read_json(self._path)
        self._unsaved = 0

    def note(self, relative: str, data: bytes) -> None:
        self.items[relative] = [len(data), hashlib.sha256(data).hexdigest()]
        self._unsaved += 1
        if self._unsaved >= 25:  # if a long upload is interrupted, at most a few records are lost
            self.save()

    def save(self) -> None:
        if self._unsaved:
            _write_json(self._path, self.items)
            self._unsaved = 0

    def changed(self, out_dir: str, present: Iterable[Tile], sizes: Dict[Tile, int]) -> List[Tile]:
        """Tiles that need uploading, according to the sizes read from the watch."""
        changed = []
        for tile in present:
            data = _local(out_dir, tile)
            noted = self.items.get(tile_path(tile))
            same = (noted is not None and sizes.get(tile) == noted[0] == len(data)
                    and noted[1] == hashlib.sha256(data).hexdigest())
            if not same:
                changed.append(tile)
        return changed


def save_backups(backup_dir: str, tiles: Iterable[Tile], remote: Dict[Tile, bytes]) -> int:
    """Save the original content of the tiles about to be overwritten. Returns their number."""
    saved = 0
    for tile in tiles:
        if tile not in remote:
            continue
        path = os.path.join(backup_dir, *tile_path(tile).split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(remote[tile])
        saved += 1
    return saved
