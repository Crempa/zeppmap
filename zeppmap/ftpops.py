# SPDX-License-Identifier: AGPL-3.0-or-later
"""Working with the watch files through its FTP server.

Paths are relative to the watch data partition (/mnt/data), so map tiles
live in "map/res/scl/11/<x>/<y>.img".
"""
from __future__ import annotations

import ftplib
import io
import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, List, Optional, Tuple

from .errors import ZeppmapError

log = logging.getLogger("zeppmap.ftp")

MAPS_DIR = "map/res/scl"
_LAYER_NAME = re.compile(r"^[a-z0-9_]{1,16}$")


def layer_dir(layer: str) -> str:
    """Folder of a map layer in the watch: scl base map, ctl contours, ski ski map.

    Any other name is used as it is; it serves for trying other layers.
    """
    if not _LAYER_NAME.match(layer):
        raise ValueError("a layer name may only contain lower-case letters, digits and underscores")
    return "map/res/" + layer

_TILE_PATH = re.compile(r"(?:^|/)(11/(\d+)/(\d+)\.img)$")
_LIST_TOTAL = re.compile(r"^total\s+\d+$", re.IGNORECASE)
# The watch server (FreeRTOS+FAT) returns an entry without a name for an empty folder.
_UNIX_LIST_NAMELESS = re.compile(r"^[\-dlbcps][rwxsStT\-]{9}\S*\s+\d+\s+\S+\s+\S+\s+\d+\s+\S+\s+\S+\s+\S+$")
_UNIX_LIST = re.compile(
    r"^([\-dlbcps])[rwxsStT\-]{9}\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+\S+\s+\S+\s+(.+)$"
)


class FtpError(ZeppmapError):
    pass


class FtpNotFound(FtpError):
    """The file is not on the server; unlike a broken transfer, retrying will not help."""


def describe_difference(sent: bytes, stored: bytes) -> str:
    """How what is in the watch differs from what was sent."""
    same = 0
    limit = min(len(sent), len(stored))
    while same < limit and sent[same] == stored[same]:
        same += 1
    if len(stored) != len(sent):
        text = "the watch has %d B instead of %d B" % (len(stored), len(sent))
        if same == limit:
            text += ", the common part matches"
        else:
            text += ", content differs from byte %d" % same
        return text
    return "size matches, content differs from byte %d" % same


def parse_list(lines: List[str]) -> Optional[List[Entry]]:
    """Entries from a LIST listing in Unix format.

    Returns None when the listing contains lines in another format. The
    "total N" line some servers start with and nameless entries are skipped.
    """
    entries = []
    for line in lines:
        text = line.strip()
        if not text or _LIST_TOTAL.match(text) or _UNIX_LIST_NAMELESS.match(text):
            continue
        match = _UNIX_LIST.match(text)
        if not match:
            return None
        if match.group(3) not in (".", ".."):
            entries.append(Entry(match.group(3), match.group(1) == "d", int(match.group(2))))
    return entries


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: Optional[int]


def human_size(size: int) -> str:
    if size < 1000:
        return "%d B" % size
    if size < 1000 * 1000:
        return "%.1f kB" % (size / 1e3)
    return "%.1f MB" % (size / 1e6)


def parse_address(address: str) -> Tuple[str, int]:
    """Address from the watch ("ftp://192.168.x.y:21/" and the like) as (host, port)."""
    host = address.strip()
    if host.startswith("ftp://"):
        host = host[len("ftp://"):]
    host = host.split("/", 1)[0]
    port = 21
    if ":" in host:
        host, port_text = host.rsplit(":", 1)
        port = int(port_text)
    if not host:
        raise FtpError("the watch returned an empty FTP address: %r" % address)
    return host, port


def iter_tiles(source: str) -> Iterator[Tuple[str, bytes]]:
    """Tiles from a zip or folder as ("11/<x>/<y>.img", content)."""
    if os.path.isdir(source):
        for root, _dirs, files in sorted(os.walk(source)):
            for name in sorted(files):
                full = os.path.join(root, name)
                match = _TILE_PATH.search(os.path.relpath(full, source).replace(os.sep, "/"))
                if match and _valid_tile(match):
                    with open(full, "rb") as handle:
                        yield match.group(1), handle.read()
    elif zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in sorted(archive.infolist(), key=lambda item: item.filename):
                match = _TILE_PATH.search(info.filename)
                if not info.is_dir() and match and _valid_tile(match):
                    yield match.group(1), archive.read(info)
    else:
        raise FtpError("%s is neither a zip nor a folder" % source)


def _valid_tile(match) -> bool:
    return 0 <= int(match.group(2)) < 2048 and 0 <= int(match.group(3)) < 2048


class WatchFtp:
    def __init__(self, host: str, port: int, username: str, timeout: float = 30.0) -> None:
        self._ftp = ftplib.FTP(timeout=timeout)
        if log.isEnabledFor(logging.DEBUG):
            self._ftp.set_debuglevel(1)  # with -v, print every FTP command and reply
        self._ftp.connect(host, port)
        self._ftp.login(username, "")
        self._ftp.set_pasv(True)
        self._ftp.voidcmd("TYPE I")
        self._mlsd_works: Optional[bool] = None
        self.attempts = 3
        self.retry_delay = 1.0
        # The server may take paths as absolute (/mnt/data/...) or relative to the root.
        self.base = "/mnt/data" if self._can_cwd("/mnt/data") else ""

    def close(self) -> None:
        try:
            self._ftp.quit()
        except Exception:
            self._ftp.close()

    def _full(self, path: str) -> str:
        return self.base + "/" + path.strip("/") if path.strip("/") else (self.base or "/")

    def _can_cwd(self, full_path: str) -> bool:
        try:
            self._ftp.cwd(full_path)
            return True
        except ftplib.error_perm:
            return False

    # ------------------------------------------------------------- reading

    def is_dir(self, path: str) -> bool:
        return self._can_cwd(self._full(path))

    def listdir(self, path: str) -> List[Entry]:
        full = self._full(path)
        try:
            self._ftp.cwd(full)
        except ftplib.error_perm as exc:
            raise FtpError("folder %s does not exist (%s)" % (path or "/", exc)) from None
        try:
            entries = self._list_current(full)
        except ftplib.Error as exc:
            raise FtpError("folder %s cannot be listed (%s)" % (path or "/", exc)) from None
        return sorted(entries, key=lambda e: (not e.is_dir, e.name))

    def _list_current(self, full: str) -> List[Entry]:
        """Entries of the current folder; tries MLSD, then LIST, then NLST."""
        if self._mlsd_works is not False:
            try:
                entries = [
                    Entry(name, facts.get("type") == "dir", int(facts["size"]) if "size" in facts else None)
                    for name, facts in self._ftp.mlsd()
                    if facts.get("type") in ("dir", "file")
                ]
                self._mlsd_works = True
                return entries
            except (ftplib.error_perm, ftplib.error_temp):
                if self._mlsd_works:
                    raise
                self._mlsd_works = False  # the server cannot do MLSD, go straight to LIST next time

        lines: List[str] = []
        self._ftp.retrlines("LIST", lines.append)
        log.debug("LIST %s: %r", full, lines[:5])
        entries = parse_list(lines)
        if entries is not None:
            return entries

        # Unknown listing format: what is left is a list of names and a query for each.
        try:
            names = [n for n in self._ftp.nlst() if n not in (".", "..")]
        except ftplib.Error as exc:
            raise FtpError(
                "the server returned a listing in an unknown format (%r) and cannot list names (%s)" % (lines[:3], exc)
            ) from None
        entries = []
        for name in names:
            child = full.rstrip("/") + "/" + name
            is_dir = self._can_cwd(child)
            entries.append(Entry(name, is_dir, None if is_dir else self._size(child)))
        self._ftp.cwd(full)
        return entries

    def describe_tree(
        self, path: str, report: Callable[[str], None] = print, list_limit: int = 40
    ) -> Tuple[int, int]:
        """Print a folder tree with sizes. Returns (number of files, bytes).

        Files are listed one by one; for folders with more than list_limit
        files (typically map tiles) only a summary and a few sample names. A
        folder that cannot be listed does not stop the walk.
        """
        files = total = 0
        pending = [path.strip("/")]
        while pending:
            folder = pending.pop(0)
            try:
                entries = self.listdir(folder)
            except FtpError as exc:
                report("%s/  (%s)" % (folder, exc))
                continue
            own = [entry for entry in entries if not entry.is_dir]
            size = sum(entry.size or 0 for entry in own)
            files += len(own)
            total += size
            report("%s/  (files: %d, %s)" % (folder, len(own), human_size(size)))
            if len(own) <= list_limit:
                for entry in own:
                    report("    %-36s %s" % (entry.name, "%d B" % entry.size if entry.size is not None else ""))
            else:
                report("    e.g. %s" % ", ".join(entry.name for entry in own[:3]))
            # Subfolders come next, in the same order.
            pending = [folder + "/" + entry.name for entry in entries if entry.is_dir] + pending
        return files, total

    def _size(self, full_path: str) -> Optional[int]:
        try:
            return self._ftp.size(full_path)
        except (ftplib.error_perm, ftplib.error_temp):
            return None

    def size(self, path: str) -> Optional[int]:
        """File size, or None when the file does not exist (or the server will not say)."""
        return self._size(self._full(path))

    def stat(self, path: str) -> Optional[Entry]:
        """Entry of a file from the listing of its folder, or None when it is not there."""
        folder, _slash, name = path.strip("/").rpartition("/")
        try:
            entries = self.listdir(folder)
        except FtpError:
            return None  # not even the folder exists
        return next((entry for entry in entries if entry.name == name and not entry.is_dir), None)

    def exists(self, path: str) -> bool:
        """Whether the file is in the watch, according to the listing of its folder."""
        return self.stat(path) is not None

    def _retrying(self, what: str, action: Callable[[], bytes]) -> bytes:
        """A transfer that is repeated on failure; the watch sometimes drops the connection."""
        problem: Exception = FtpError("unknown error")
        for attempt in range(1, self.attempts + 1):
            try:
                return action()
            except ftplib.error_perm:
                raise  # a permanent error (such as a missing file); retrying will not help
            except (ftplib.Error, OSError, EOFError) as exc:
                problem = exc
                log.debug("%s: attempt %d of %d failed (%s)", what, attempt, self.attempts, exc)
                time.sleep(self.retry_delay)
        raise FtpError("%s failed even after %d attempts (%s)" % (what, self.attempts, problem))

    def read(self, path: str, expected_size: Optional[int] = None) -> bytes:
        """File content. When the size is known, an incomplete transfer is detected and repeated."""
        def fetch() -> bytes:
            buffer = io.BytesIO()
            self._ftp.retrbinary("RETR " + self._full(path), buffer.write)
            data = buffer.getvalue()
            if expected_size is not None and len(data) != expected_size:
                raise EOFError("received %d B instead of %d B" % (len(data), expected_size))
            return data

        try:
            return self._retrying("downloading %s" % path, fetch)
        except ftplib.error_perm as exc:
            raise FtpNotFound("file %s cannot be downloaded (%s)" % (path, exc)) from None

    def _pull_file(self, remote: str, local: str, size: Optional[int], report: Callable[[str], None]) -> Tuple[int, int]:
        if size is not None and os.path.isfile(local) and os.path.getsize(local) == size:
            report("%s (already downloaded)" % remote)
            return 0, 0
        data = self.read(remote, size)
        os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
        with open(local, "wb") as handle:
            handle.write(data)
        report("%s (%d B)" % (remote, len(data)))
        return 1, len(data)

    def pull(self, remote: str, local: str, report: Callable[[str], None] = print) -> Tuple[int, int]:
        """Download a file or a whole folder. Returns (number of files downloaded, bytes).

        Files already on disk with the same size are skipped, so an
        interrupted download only needs to be started again.
        """
        if not self.is_dir(remote):
            if os.path.isdir(local):
                local = os.path.join(local, os.path.basename(remote.rstrip("/")))
            return self._pull_file(remote, local, self.size(remote), report)

        files = total = 0
        os.makedirs(local, exist_ok=True)
        for entry in self.listdir(remote):
            child_remote = remote.rstrip("/") + "/" + entry.name
            child_local = os.path.join(local, entry.name)
            if entry.is_dir:
                count, size = self.pull(child_remote, child_local, report)
            else:
                count, size = self._pull_file(child_remote, child_local, entry.size, report)
            files += count
            total += size
        return files, total

    # ------------------------------------------------------------- writing

    def mkdirs(self, path: str) -> None:
        current = self.base
        for part in path.strip("/").split("/"):
            current += "/" + part
            if self._can_cwd(current):
                continue
            try:
                self._ftp.mkd(current)
            except ftplib.error_perm as exc:
                raise FtpError("folder %s cannot be created (%s)" % (current, exc)) from None

    def write(self, path: str, data: bytes) -> None:
        def store() -> bytes:
            self._ftp.storbinary("STOR " + self._full(path), io.BytesIO(data))
            return b""

        self._retrying("uploading %s" % path, store)

    def push_tiles(
        self,
        source: str,
        verify: str = "full",
        backup_dir: Optional[str] = None,
        report: Callable[[str], None] = print,
        layer: str = "scl",
        only: Optional[Iterable[str]] = None,
        uploaded: Optional[Callable[[str, bytes], None]] = None,
        known: Optional[Callable[[str], Optional[bytes]]] = None,
    ) -> Tuple[int, int]:
        """Upload tiles into a layer: scl (base map), ctl (contours), ski (ski map).

        Returns (number of tiles, bytes). With only, just the tiles with these
        names ("11/<x>/<y>.img") are uploaded from the source. uploaded is
        called after every uploaded and verified tile with its name and
        content. known returns the original content of a tile if the caller
        already has it stored; such a tile is neither read from the watch
        nor backed up again before being overwritten, and if writing fails,
        this content is put back.

        verify: "full" downloads the tile again after upload and compares
        it, "size" compares the size only, "none" does not check. A damaged
        tile can crash the watch when it opens the map, so the default is the
        full check. Existing tiles are saved to backup_dir before being
        overwritten.
        """
        if verify not in ("full", "size", "none"):
            raise ValueError("verify must be full, size or none")
        target_dir = layer_dir(layer)
        wanted = None if only is None else set(only)
        created = set()
        tiles = total = 0
        for relative, data in iter_tiles(source):
            if wanted is not None and relative not in wanted:
                continue
            remote = target_dir + "/" + relative
            parent = remote.rsplit("/", 1)[0]
            if parent not in created:
                self.mkdirs(parent)
                created.add(parent)

            note = ""
            original: Optional[bytes] = known(relative) if known is not None else None
            if original is None and backup_dir:
                backup_path = os.path.join(backup_dir, relative)
                if os.path.exists(backup_path):
                    with open(backup_path, "rb") as handle:
                        original = handle.read()
                else:
                    # For a missing and for an empty file, the watch server answers the
                    # same way as for a broken transfer, so first find out what is there.
                    # A failed read attempt leaves an empty file behind.
                    current = self.stat(remote)
                    if current is not None and current.size != 0:
                        original = self.read(remote, current.size)
                    if original is not None:
                        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                        with open(backup_path, "wb") as handle:
                            handle.write(original)
                        note = ", original backed up"

            try:
                self._upload_verified(remote, data, verify)
            except FtpError as problem:
                # A failed write must not leave a damaged tile in the watch.
                if original is None:
                    self.remove(remote)
                    raise FtpError("%s. The tile was removed from the watch." % problem) from None
                try:
                    self._upload_verified(remote, original, verify)
                except FtpError as second:
                    raise FtpError(
                        "%s. Putting the original tile back failed (%s); its state in the watch is unknown."
                        % (problem, second)
                    ) from None
                raise FtpError("%s. The original tile was put back into the watch." % problem) from None
            tiles += 1
            total += len(data)
            if uploaded is not None:
                uploaded(relative, data)
            report("%s (%d B%s)" % (relative, len(data), note))
        if tiles == 0:
            raise FtpError("there are no 11/<x>/<y>.img tiles in %s" % source)
        return tiles, total

    def remove_dir(self, path: str) -> bool:
        """Delete an empty folder. Returns False when that is not possible."""
        try:
            self._ftp.rmd(self._full(path))
            return True
        except ftplib.Error as exc:
            log.debug("deleting folder %s: %s", path, exc)
            return False

    def remove_tree(self, path: str, report: Callable[[str], None] = print) -> int:
        """Delete a file, or a folder with its content. Returns the number of files deleted."""
        if not self.is_dir(path):
            if not self.remove(path):
                raise FtpNotFound("file %s is not in the watch or cannot be deleted" % path)
            report("deleted %s" % path)
            return 1
        count = 0
        for entry in self.listdir(path):
            count += self.remove_tree(path.rstrip("/") + "/" + entry.name, report)
        # The server cannot delete the folder it is currently in.
        self._can_cwd(self._full(""))
        if self.remove_dir(path):
            report("deleted folder %s" % path)
        return count

    def remove(self, path: str) -> bool:
        """Delete a file. Returns False when it does not exist or the server cannot do it."""
        try:
            self._ftp.delete(self._full(path))
            return True
        except ftplib.Error as exc:
            log.debug("deleting %s: %s", path, exc)
            return False

    def _upload_verified(self, remote: str, data: bytes, verify: str) -> None:
        problem = ""
        for _attempt in range(2):
            # The watch server does not truncate a file it overwrites: after a
            # smaller file, the end of the old one would remain. So delete first.
            self.remove(remote)
            self.write(remote, data)
            if verify == "none":
                return
            if verify == "size":
                size = self.size(remote)
                if size is None or size == len(data):
                    return
                problem = "size %d B instead of %d B" % (size, len(data))
            else:
                readback = self.read(remote)
                if readback == data:
                    return
                problem = describe_difference(data, readback)
        raise FtpError("tile %s was not uploaded correctly (%s)" % (remote, problem))
