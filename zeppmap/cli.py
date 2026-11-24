# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command line: python -m zeppmap <command>."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from typing import List, Optional

from .errors import ZeppmapError
from .ftpops import WatchFtp, human_size, iter_tiles, parse_address
from .session import (
    ENDPOINT_FTP,
    ENDPOINT_MAPS,
    ENDPOINT_NAMES,
    ENDPOINT_WIFI,
    ZeppSession,
    parse_auth_key,
    random_hotspot_credentials,
)

DEFAULT_KEY_FILE = "~/.zeppmap-key"
KEY_ENV = "ZEPPMAP_KEY"
BUILD_DIR = "build"            # tiles built by sync: build/contours, build/ski
FROM_WATCH_DIR = "from-watch"  # default target of pull


class UsageError(ZeppmapError):
    pass


def say(text: str) -> None:
    print(text, flush=True)


def load_key(args) -> bytes:
    text = os.environ.get(KEY_ENV)
    source = "variable %s" % KEY_ENV
    if not text:
        path = os.path.expanduser(args.key_file or DEFAULT_KEY_FILE)
        source = path
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            raise UsageError(
                "the auth key is missing. Save it to %s (32 hex digits) or to the %s variable."
                % (path, KEY_ENV)
            ) from None
    try:
        return parse_auth_key(text)
    except ValueError as exc:
        raise UsageError("the auth key (%s) is not valid: %s" % (source, exc)) from None


async def bluetooth_ready(ble) -> None:
    await ble.ensure_bluetooth(lambda: say(
        "Waiting for macOS to make Bluetooth available. If a permission prompt appeared, allow it…"
    ))


@contextlib.asynccontextmanager
async def connected_session(args):
    from . import ble  # bleak is loaded only here; commands without Bluetooth do not need it

    key = load_key(args)
    await bluetooth_ready(ble)
    say("Looking for the watch (up to %d s)…" % (args.scan_timeout or 30.0))
    device = await ble.find_watch(args.name, args.address, args.scan_timeout or 30.0)
    say("Connecting to %s…" % (device.name or device.address))

    holder: List[ZeppSession] = []
    link = ble.BleLink(device, lambda data: holder[0].on_packet(data))
    session = ZeppSession(link.write_packet, link.write_ack, key, args.max_packet)
    holder.append(session)
    await link.open()
    try:
        say("Authenticating…")
        await session.authenticate()
        await session.request_services()
        yield session
    finally:
        await link.close()
        await asyncio.sleep(1.0)
        # A command that connects to the watch twice keeps the notice for the very end.
        if getattr(args, "defer_hold_notice", False):
            args.held_address = device.address
        else:
            hold_notice(device.address)


def hold_notice(address: str) -> None:
    from . import ble

    if ble.held_by_system(address=address):
        say("The watch stays connected to this Mac. To get it back to your phone, turn Bluetooth off "
            "on the Mac, turn it on on the phone, and once the watch connects, turn it on on the Mac again.")


PORT_TIMEOUT = 60.0          # after joining the hotspot automatically
MANUAL_WIFI_TIMEOUT = 300.0  # for the user to join the hotspot by hand


@contextlib.asynccontextmanager
async def watch_ftp(args):
    from . import wifi

    async with connected_session(args) as session:
        client: Optional[WatchFtp] = None
        hotspot_requested = False
        join_attempted = False
        ftp_requested = False
        manual = not args.no_hotspot and getattr(args, "manual_wifi", False)
        ssid = ""
        try:
            if not args.no_hotspot:
                ssid, password = random_hotspot_credentials()
                if manual:
                    # sync starts the hotspot twice; the same network is easier to join again.
                    ssid, password = getattr(args, "hotspot_credentials", None) or (ssid, password)
                    args.hotspot_credentials = (ssid, password)
                say("Starting the watch hotspot (%s)…" % ssid)
                hotspot_requested = True
                await session.start_hotspot(ssid, password)
                if manual:
                    say("Join this Wi-Fi network on the computer yourself; your usual Wi-Fi disconnects for a while.")
                    say("  network:  %s" % ssid)
                    say("  password: %s" % password)
                else:
                    say("Joining the hotspot; your usual Wi-Fi disconnects for a while…")
                    join_attempted = True
                    await asyncio.to_thread(wifi.join, ssid, password)

            say("Starting the watch FTP server…")
            ftp_requested = True
            address, username = await session.start_ftp()
            host, port = parse_address(address)
            say("FTP is running at %s:%d (user %s)." % (host, port, username))
            if manual:
                say("Waiting up to %d minutes until the computer reaches it…" % (MANUAL_WIFI_TIMEOUT // 60))
            try:
                await asyncio.to_thread(
                    wifi.wait_for_port, host, port, MANUAL_WIFI_TIMEOUT if manual else PORT_TIMEOUT)
            except wifi.WifiError as exc:
                if join_attempted:
                    raise wifi.WifiError("%s. Check that the computer is on the %s network, or %s." % (
                        exc, ssid, wifi.MANUAL_HINT)) from None
                if manual:
                    raise wifi.WifiError("%s. Join the %s network on the computer sooner, or check that "
                                         "it really joined it." % (exc, ssid)) from None
                raise
            client = await asyncio.to_thread(WatchFtp, host, port, username)
            yield client
        finally:
            if client is not None:
                await asyncio.to_thread(client.close)
            for requested, stop in ((ftp_requested, session.stop_ftp), (hotspot_requested, session.stop_hotspot)):
                if requested:
                    try:
                        await stop()
                    except Exception as exc:
                        logging.getLogger("zeppmap").debug("cleanup failed: %s", exc)
            if join_attempted:
                await asyncio.to_thread(wifi.forget, ssid)
            if manual and hotspot_requested:
                say("Hotspot stopped. If the computer does not return to your usual Wi-Fi by itself, join it again.")
            elif hotspot_requested:
                say("Hotspot stopped; the computer returns to its usual Wi-Fi by itself.")


# ---------------------------------------------------------------- commands

async def cmd_check(args) -> int:
    """Check the prerequisites without connecting to the watch."""
    import platform
    import shutil

    from . import swatch, wifi

    problems = 0

    def report(ok: bool, what: str, detail: str) -> None:
        nonlocal problems
        problems += not ok
        say("%-5s %-12s %s" % ("OK" if ok else "FAIL", what, detail))

    def note(what: str, detail: str) -> None:
        say("%-5s %-12s %s" % ("NOTE", what, detail))

    report(sys.version_info >= (3, 9), "Python", "%s (3.9 or newer needed)" % platform.python_version())
    system = wifi.system()
    if system == "macos":
        report(True, "System", "macOS %s" % platform.mac_ver()[0])
    elif system in ("linux", "windows"):
        note("System", "%s %s: supported, but not yet tested with a real watch. If joining the watch "
             "hotspot fails, add --manual-wifi and join it yourself."
             % ("Linux" if system == "linux" else "Windows", platform.release()))
    else:
        report(False, "System", "%s is not supported: the Bluetooth library has no backend for it"
               % (platform.system() or sys.platform))
    tool = wifi.join_tool()
    if tool and shutil.which(tool):
        report(True, "Wi-Fi", "joins the watch hotspot with %s" % tool)
    elif tool:
        note("Wi-Fi", "%s not found; add --manual-wifi and join the watch hotspot yourself" % tool)
    pip_python = r".venv\Scripts\python" if system == "windows" else ".venv/bin/python"
    for module in ("bleak", "cryptography"):
        try:
            __import__(module)
            report(True, module, "installed")
        except ImportError:
            report(False, module, "missing; run: %s -m pip install -r requirements.txt" % pip_python)
    try:
        report(True, "Java", swatch.find_java())
    except ZeppmapError as exc:
        report(False, "Java", str(exc))
    try:
        report(True, "mkgmap", swatch.find_mkgmap())
    except ZeppmapError as exc:
        report(False, "mkgmap", str(exc))
    try:
        load_key(args)
        report(True, "Auth key", "found and well-formed (whether the watch accepts it shows on connecting)")
    except UsageError as exc:
        report(False, "Auth key", str(exc))
    if system == "macos" and not args.no_bluetooth:
        try:
            from . import ble

            await bluetooth_ready(ble)
            report(True, "Bluetooth", "available to this application")
        except Exception as exc:
            report(False, "Bluetooth", str(exc))
    elif not args.no_bluetooth:
        note("Bluetooth", "not checked on this system; the scan command shows whether the watch is found")
    if problems:
        say("%d problem%s found. Fix them and run check again." % (problems, "" if problems == 1 else "s"))
        return 1
    say("Everything is ready. Next: turn Bluetooth off on your phone and run the services command.")
    return 0


async def cmd_scan(args) -> int:
    from . import ble

    await bluetooth_ready(ble)
    held = ble.held_by_system(args.name)
    for device in held:
        say("%-28s %s  connected to this Mac  <- watch" % (device.name or "(no name)", device.address))
    timeout = args.scan_timeout or 10.0
    say("Scanning for %d s…" % timeout)
    found = await ble.scan(timeout)
    shown = [item for item in found if item[3] or args.all]
    if held and not any(item[3] for item in shown):
        if args.all:
            for device, name, rssi, _is_watch, details in shown:
                say("%-28s %s  %4d dBm%s" % (
                    name or "(no name)", device.address, rssi, "  [%s]" % details if details else ""))
        return 0
    if not shown:
        say("No watch is advertising (devices in range: %d). It keeps only one connection: turn "
            "Bluetooth off on your phone (do not unpair it in Zepp) and try again. "
            "scan --all lists every device." % len(found))
        return 1
    for device, name, rssi, is_watch, details in shown:
        say("%-28s %s  %4d dBm%s%s" % (
            name or "(no name)", device.address, rssi,
            "  <- watch" if is_watch else "",
            "  [%s]" % details if args.all and details else "",
        ))
    if not any(item[3] for item in shown):
        say("None of these looks like an Amazfit watch.")
        return 1
    return 0


async def cmd_services(args) -> int:
    async with connected_session(args) as session:
        say("Authenticated. Watch services:")
        for endpoint in sorted(session.services):
            say("  0x%04x  %-20s %s" % (
                endpoint,
                ENDPOINT_NAMES.get(endpoint, ""),
                "encrypted" if session.services[endpoint] else "not encrypted",
            ))
        for endpoint, label in ((ENDPOINT_WIFI, "Wi-Fi hotspot"), (ENDPOINT_FTP, "FTP server"), (ENDPOINT_MAPS, "maps")):
            say("%s: %s" % (label, "yes" if endpoint in session.services else "NO"))
    return 0


async def cmd_ls(args) -> int:
    async with watch_ftp(args) as client:
        if args.recursive:
            files, total = await asyncio.to_thread(client.describe_tree, args.path, say)
            say("Files in total: %d, %s." % (files, human_size(total)))
            return 0
        entries = await asyncio.to_thread(client.listdir, args.path)
        say("Contents of %s:" % (args.path or "/"))
        for entry in entries:
            if entry.is_dir:
                say("  %s/" % entry.name)
            else:
                say("  %-40s %s" % (entry.name, "%d B" % entry.size if entry.size is not None else ""))
        if not entries:
            say("  (empty)")
    return 0


async def cmd_pull(args) -> int:
    local = args.local or os.path.join(FROM_WATCH_DIR, os.path.basename(args.remote.rstrip("/")) or "data")
    async with watch_ftp(args) as client:
        files, total = await asyncio.to_thread(client.pull, args.remote, local, say)
    say("Downloaded %d files, %.1f MB to %s." % (files, total / 1e6, local))
    return 0


async def cmd_push(args) -> int:
    from . import sync

    tiles = [(relative, len(data)) for relative, data in iter_tiles(args.source)]
    if not tiles:
        raise UsageError("there are no 11/<x>/<y>.img tiles in %s" % args.source)
    total = sum(size for _relative, size in tiles)
    say("To upload: %d tiles, %.1f MB." % (len(tiles), total / 1e6))
    if args.dry_run:
        for relative, size in tiles:
            say("  %s (%d B)" % (relative, size))
        return 0

    backup_dir = None
    if not args.no_backup:
        backup_dir = args.backup_dir or os.path.join(
            sync.BACKUPS_DIR, time.strftime("%Y%m%d-%H%M%S") + ("" if args.layer == "scl" else "-" + args.layer))
    async with watch_ftp(args) as client:
        count, size = await asyncio.to_thread(
            client.push_tiles, args.source, args.verify, backup_dir, say, args.layer)
    say("Uploaded %d tiles, %.1f MB." % (count, size / 1e6))
    if backup_dir and os.path.isdir(backup_dir):
        say("The original tiles are backed up in %s." % backup_dir)
    say("Close and reopen the map on the watch so that it loads the tiles again.")
    return 0


async def cmd_sync(args) -> int:
    from . import ctlmerge, routes, sync, wifi

    layer = args.layer
    merged = layer == "ctl"   # marks are merged with the contours and uploaded to their layer
    out = args.out or os.path.join(BUILD_DIR, "contours" if merged else "ski")
    store = sync.ContourStore(args.contours) if merged else None
    record = sync.UploadRecord(out)
    backup_dir = args.backup_dir or os.path.join(sync.BACKUPS_DIR, time.strftime("%Y%m%d-%H%M%S") + "-" + layer)

    def listed(found) -> str:
        names = [sync.tile_path(tile) for tile in found]
        return ", ".join(names[:8]) + (" and more" if len(names) > 8 else "")

    sizes = contents = None
    unknown: list = []
    if args.like:
        tiles = sync.tiles_from(args.like)
        say("According to %s there is a base map for %d tiles." % (args.like, len(tiles)))
    else:
        args.defer_hold_notice = True
        async with watch_ftp(args) as client:
            tiles = await asyncio.to_thread(sync.watch_tiles, client)
            say("The watch has a base map for %d tiles." % len(tiles))
            if tiles:
                say("Reading what is in the %s layer now…" % ("contour" if merged else "ski"))
                if args.full_check:
                    contents = await asyncio.to_thread(sync.read_tiles, client, tiles, layer)
                else:
                    sizes = await asyncio.to_thread(sync.watch_sizes, client, layer)
                if merged:
                    # The original contours must be on disk before a merged tile overwrites them.
                    in_watch = sizes if sizes is not None else {tile: len(data) for tile, data in contents.items()}
                    fetched, restored, unknown = await asyncio.to_thread(
                        sync.fetch_contours, client, tiles, store, in_watch, record.items, say, contents,
                        os.path.dirname(backup_dir) or ".")   # older backups sit next to today's
                    if fetched or restored:
                        say("Original contours saved to %s: %d from the watch, %d from backups." % (
                            args.contours, fetched, restored))
        args.defer_hold_notice = False
        if tiles:
            sync.save_tiles(args.cache, tiles)
    held_address = getattr(args, "held_address", None)

    def finish(code: int) -> int:
        if held_address:
            hold_notice(held_address)
        return code

    if not tiles:
        say("There is nothing to build marks for. Download a map to the watch in the Zepp app first.")
        return finish(1)
    if unknown:
        say("%d tiles in the watch hold our marks, but their original contours are not on disk or in "
            "the backups. Leaving them unchanged: %s." % (len(unknown), listed(unknown)))

    total_blocks = len(sync.blocks(tiles))
    stale = sync.stale_blocks(tiles, args.cache, args.max_age, args.refresh)
    if stale:
        if not args.like:
            say("Waiting for the computer to get back online…")
        await asyncio.to_thread(
            wifi.wait_for_internet, [server.split("/")[2] for server in routes.OVERPASS_SERVERS])
        say("Routes need downloading for %d of %d blocks." % (len(stale), total_blocks))
        await asyncio.to_thread(sync.download, tiles, args.cache, args.max_age, args.refresh, say)
    else:
        say("Routes for all %d blocks are in the cache." % total_blocks)

    say("Building %s…" % ("contour tiles with marks" if merged else "mark tiles"))
    skipped: list = []
    present, rebuilt = await asyncio.to_thread(
        sync.build_from_cache, tiles, args.cache, out, say, None, max(1, args.jobs), store, skipped, unknown)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "legend.txt"), "w", encoding="utf-8") as handle:
        if merged:
            handle.write(routes.legend("Marked routes in the contour layer (switched with the contours toggle)",
                                       ctlmerge.SPACING))
        else:
            handle.write(routes.legend())
    say("Tiles with marks: %d, newly built: %d (folder %s)." % (len(present), rebuilt, out))
    if skipped:
        say("%d tiles have contours with an unknown structure; leaving them unchanged: %s." % (
            len(skipped), listed(skipped)))

    if contents is not None:
        changed = sync.changed_tiles(out, present, contents)
    elif sizes is not None:
        changed = record.changed(out, present, sizes)
    else:
        changed = present
    if not changed:
        say("The watch is up to date, nothing to upload.")
        return finish(0)
    names = [sync.tile_path(tile) for tile in changed]
    total = sum(os.path.getsize(os.path.join(out, *name.split("/"))) for name in names)
    say("To upload: %d tiles, %.1f MB%s." % (
        len(changed), total / 1e6, "" if len(changed) > 8 else " (%s)" % ", ".join(names)))
    if args.dry_run:
        say("Dry run, nothing is uploaded to the watch.")
        return finish(0)
    if merged and args.like:
        # Without reading the watch it is not certain that all original contours are on disk;
        # a tile built without them would overwrite them in the watch.
        say("The tile list did not come from the watch, so nothing is uploaded to the contour layer. "
            "Run the command without --like.")
        return finish(0)

    if contents is not None:
        # The original content is already read; saved to disk it serves as the upload backup.
        # Tiles whose original contours are in the store need no further backup.
        keep = [tile for tile in changed if store is None or not store.has(tile)]
        await asyncio.to_thread(sync.save_backups, backup_dir, keep, contents)

    done = [0]

    def progress(text: str) -> None:
        # For a large map, printing every tile would flood the terminal.
        done[0] += 1
        if len(names) <= 20 or done[0] % 50 == 0 or done[0] == len(names):
            say("  %d of %d: %s" % (done[0], len(names), text))

    try:
        async with watch_ftp(args) as client:
            count, size = await asyncio.to_thread(
                client.push_tiles, out, "full", backup_dir, progress, layer, names, record.note,
                store.read_relative if store is not None else None)
    finally:
        # What got uploaded is recorded even on interruption; the next run continues where this one stopped.
        record.save()
    say("Uploaded %d tiles, %.1f MB." % (count, size / 1e6))
    if os.path.isdir(backup_dir):
        say("The original tiles are backed up in %s." % backup_dir)
    if merged:
        say("The original contours are in the %s folder. Do not delete it: every later tile is built from "
            "it, and push %s --layer ctl puts them back into the watch." % (args.contours, args.contours))
    say("Close and reopen the map on the watch so that it loads the tiles again.")
    return 0


# Only map tiles may be deleted, and never whole layers the watch manages itself.
_PROTECTED_LAYERS = ("scl", "ctl", "ski")


async def cmd_rm(args) -> int:
    path = args.path.strip("/")
    parts = path.split("/")
    if len(parts) < 3 or parts[:2] != ["map", "res"] or ".." in parts:
        raise UsageError("only paths under map/res/<layer> can be deleted, for example map/res/ski/11/1113/694.img")
    if parts[2] in _PROTECTED_LAYERS and len(parts) < 6:
        raise UsageError(
            "in the %s layer only a single tile can be deleted, for example map/res/%s/11/1113/694.img"
            % (parts[2], parts[2]))
    async with watch_ftp(args) as client:
        count = await asyncio.to_thread(client.remove_tree, path, say)
    say("Files deleted: %d." % count)
    return 0


async def cmd_info(args) -> int:
    from . import imginfo

    for index, path in enumerate(args.files):
        with open(path, "rb") as handle:
            info = imginfo.read_tile(handle.read())
        if index:
            say("")
        say(path)
        for line in imginfo.describe(info):
            say("  " + line)
    return 0


async def cmd_swatch(args) -> int:
    from . import swatch

    tiles = _selected_tiles(args)
    # The zepp swatch takes the types from the official tiles in the whole source folder.
    page = swatch.zepp_page(args.like) if args.page == "zepp" else swatch.PAGES[args.page]()
    say('Building the "%s" swatch for %d tiles…' % (args.page, len(tiles)))
    count = await asyncio.to_thread(swatch.build, tiles, args.out, say, None, page)
    with open(os.path.join(args.out, "legend.txt"), "w", encoding="utf-8") as handle:
        handle.write(swatch.legend(page))
    layer = {"contours": " --layer ctl", "overlay": " --layer ctl", "ski": " --layer ski",
             "ctl": " --layer ctl"}.get(args.page, "")
    say("Done, %d tiles in %s. Upload them with: push %s%s" % (count, args.out, args.out, layer))
    return 0


def _selected_tiles(args) -> list:
    from . import swatch

    tiles = swatch.tiles_in(args.like)
    if not tiles:
        raise UsageError("there are no 11/<x>/<y>.img tiles in %s" % args.like)
    if not args.only:
        return tiles
    wanted = set()
    for text in args.only:
        try:
            x, y = text.split("/")
            wanted.add((int(x), int(y)))
        except ValueError:
            raise UsageError("a tile is given as x/y, for example 1113/694") from None
    missing = sorted(wanted - set(tiles))
    if missing:
        raise UsageError("%s has no tile %s" % (args.like, ", ".join("%d/%d" % t for t in missing)))
    return sorted(wanted)


async def cmd_fetch_routes(args) -> int:
    import json

    from . import routes

    tiles = _selected_tiles(args)
    area = routes.area_of(tiles)
    say("Downloading marked routes from OpenStreetMap for %d tiles (lat %.4f to %.4f, lon %.4f to %.4f)…" % (
        len(tiles), area[0], area[2], area[1], area[3]))
    data = await asyncio.to_thread(routes.fetch, area, say)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    ways = routes.parse(data)
    say("Saved to %s: %d ways with marks." % (args.out, len(ways)))
    for mark, length in routes.summary(ways).items():
        say("  %-18s %5.0f km" % (routes.MARK_NAMES[mark], length))
    return 0


async def cmd_routes(args) -> int:
    import json

    from . import routes

    tiles = _selected_tiles(args)
    try:
        with open(args.data, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        ways = routes.parse(data)
        lifts = routes.parse_lifts(data)
    except (OSError, ValueError) as exc:
        raise UsageError("cannot read the route data %s (%s); fetch-routes downloads it" % (args.data, exc)) from None
    say("Building the marks layer for %d tiles from %d ways and %d cable cars…" % (len(tiles), len(ways), len(lifts)))
    count = await asyncio.to_thread(routes.build, ways, tiles, args.out, say, None, lifts)
    with open(os.path.join(args.out, "legend.txt"), "w", encoding="utf-8") as handle:
        handle.write(routes.legend())
    say("Done, %d tiles in %s. Upload them with: push %s --layer ski" % (count, args.out, args.out))
    return 0


MANUAL_WIFI_HELP = ("do not join the watch hotspot automatically: print its name and password and wait "
                    "until you join it yourself (when automatic joining fails on your system)")


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="zeppmap",
        description="Hiking marks, cycle routes and cable cars for the offline maps of Zepp OS watches.",
    )
    parser.add_argument("--version", action="version", version="zeppmap " + __version__)
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="log the communication with the watch; -vv adds the Bluetooth library log")
    parser.add_argument("--key-file", help="file with the auth key (default %s)" % DEFAULT_KEY_FILE)
    parser.add_argument("--name", help="part of the watch name, when there are several in range")
    parser.add_argument("--address", help="watch address from the scan command")
    parser.add_argument("--scan-timeout", type=float,
                        help="how long to look for the watch, in seconds (default 30, 10 for scan)")
    parser.add_argument("--max-packet", type=int, default=20,
                        help="Bluetooth packet size; 20 always works")
    parser.add_argument("--manual-wifi", action="store_true", help=MANUAL_WIFI_HELP)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="check that everything needed is installed (no watch needed)")
    check.add_argument("--no-bluetooth", action="store_true", help="skip the Bluetooth permission check")
    check.set_defaults(run=cmd_check)

    scan = commands.add_parser("scan", help="find watches in range")
    scan.add_argument("--all", action="store_true", help="list all Bluetooth LE devices")
    scan.set_defaults(run=cmd_scan)

    services = commands.add_parser("services", help="authenticate and list the watch services")
    services.set_defaults(run=cmd_services)

    def ftp_command(name: str, text: str, run):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--no-hotspot", action="store_true",
                         help="do not start the hotspot; try FTP over a network the watch and computer are already on")
        # Also accepted after the command; SUPPRESS keeps the value given before it.
        sub.add_argument("--manual-wifi", action="store_true", default=argparse.SUPPRESS, help=MANUAL_WIFI_HELP)
        sub.set_defaults(run=run)
        return sub

    ls = ftp_command("ls", "list a folder in the watch", cmd_ls)
    ls.add_argument("path", nargs="?", default="map", help="path in the data partition (default map)")
    ls.add_argument("-r", "--recursive", action="store_true", help="list subfolders with sizes too")

    pull = ftp_command("pull", "download a file or folder from the watch", cmd_pull)
    pull.add_argument("remote", help="path in the watch, e.g. map/res/scl/11/1105")
    pull.add_argument("local", nargs="?", help="where to save it (default %s/…)" % FROM_WATCH_DIR)

    push = ftp_command("push", "upload tiles to the watch", cmd_push)
    push.add_argument("source", help="zip or folder with 11/<x>/<y>.img tiles")
    push.add_argument("--verify", choices=("full", "size", "none"), default="full",
                      help="check after upload (default full: download again and compare)")
    push.add_argument("--no-backup", action="store_true", help="do not back up the tiles being overwritten")
    push.add_argument("--backup-dir", help="where to back up the tiles being overwritten")
    push.add_argument("--dry-run", action="store_true", help="only list what would be uploaded")
    push.add_argument("--layer", default="scl", metavar="LAYER",
                      help="layer in the watch: scl base map (default), ctl contours, ski ski map; "
                           "any other name uploads to map/res/<name>")

    sync = ftp_command("sync", "add marks to every base map tile in the watch", cmd_sync)
    sync.add_argument("--layer", choices=("ctl", "ski"), default="ctl",
                      help="where to put the marks: ctl into the contour layer, merged with the original "
                           "contours (default; visible during any activity), ski into the ski layer (marks "
                           "only; the watch shows it only for skiing sports)")
    sync.add_argument("--contours", default="zepp-contours", metavar="FOLDER",
                      help="where to keep the original contour tiles read from the watch (default zepp-contours)")
    sync.add_argument("--out", metavar="FOLDER",
                      help="where to keep the built tiles (default build/contours, build/ski for the ski layer)")
    sync.add_argument("--cache", default=os.path.join("data", "sync"), metavar="FOLDER",
                      help="cache of downloaded routes (default data/sync)")
    sync.add_argument("--max-age", type=float, default=30.0, metavar="DAYS",
                      help="after how many days to download routes again (default 30)")
    sync.add_argument("--refresh", action="store_true", help="download routes again now")
    sync.add_argument("--like", metavar="SOURCE",
                      help="take the tile list from a 11/<x>/<y>.img folder or the tiles.json file sync keeps "
                           "in its cache instead of from the watch; all built tiles are then uploaded to the "
                           "ski layer, and nothing to the contour layer")
    sync.add_argument("--jobs", type=int, default=4, metavar="N",
                      help="how many tiles to compile at once (default 4)")
    sync.add_argument("--full-check", action="store_true",
                      help="compare the content of every tile with the watch, not just the size and upload "
                           "record; takes long for a large map")
    sync.add_argument("--dry-run", action="store_true", help="prepare everything but do not upload")
    sync.add_argument("--backup-dir",
                      help="where to back up the tiles being overwritten (default backups/<time>-<layer>)")

    remove = ftp_command("rm", "delete test tiles from the watch", cmd_rm)
    remove.add_argument("path", help="path under map/res, for example map/res/ski/11/1113/694.img")
    info = commands.add_parser("info", help="describe a tile (parts, levels, object types)")
    info.add_argument("files", nargs="+", help=".img files")
    info.set_defaults(run=cmd_info)

    swatch = commands.add_parser("swatch", help="build a swatch of line types to try on the watch")
    swatch.add_argument("like", help="folder with 11/<x>/<y>.img tiles to build the swatch for")
    swatch.add_argument("out", help="where to save the swatch")
    swatch.add_argument("--only", action="append", metavar="X/Y",
                        help="build only this tile (can be given several times)")
    swatch.add_argument("--page", choices=("lines", "areas", "contours", "overlay", "ski", "ctl", "zepp"),
                        default="lines",
                        help="what to try: lines (default), areas and overlaps, lines in the contour layer, "
                             "lines as an overlay over the base map, ski layer types, a separate contour "
                             "layer swatch (ctl), or areas, points and lines from the official tiles in "
                             "the source folder (zepp)")
    swatch.set_defaults(run=cmd_swatch)

    fetch = commands.add_parser("fetch-routes", help="download marked hiking and cycle routes from OpenStreetMap")
    fetch.add_argument("like", help="folder with 11/<x>/<y>.img tiles whose area to download")
    fetch.add_argument("out", help="file to save the data to (JSON)")
    fetch.add_argument("--only", action="append", metavar="X/Y", help="only this tile (can be given several times)")
    fetch.set_defaults(run=cmd_fetch_routes)

    routes = commands.add_parser("routes", help="build the marks layer for the watch's ski layer")
    routes.add_argument("data", help="data downloaded by fetch-routes")
    routes.add_argument("like", help="folder with 11/<x>/<y>.img tiles to build the layer for")
    routes.add_argument("out", help="where to save the tiles")
    routes.add_argument("--only", action="append", metavar="X/Y", help="only this tile (can be given several times)")
    routes.set_defaults(run=cmd_routes)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # -v shows only the communication with the watch; while scanning, bleak logs
    # every device around, so that is left for -vv.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else logging.WARNING,
        format="%(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.verbose:
        logging.getLogger("zeppmap").setLevel(logging.DEBUG)
    try:
        return asyncio.run(args.run(args))
    except KeyboardInterrupt:
        say("Interrupted.")
        return 130
    except Exception as exc:
        # With -v, unexpected errors are printed in full; otherwise one line is enough.
        expected = isinstance(exc, (ZeppmapError, OSError))
        if args.verbose and not expected:
            raise
        say("Error: %s" % exc)
        return 1
