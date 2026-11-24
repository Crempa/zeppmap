# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests without a watch: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import argparse
import asyncio
import ftplib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
import zlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_watch import FakeWatch  # noqa: E402

from zeppmap import btstate, cli, ctlmerge, ecdh_b163, ftpops, imginfo, routes, skitile, swatch, sync  # noqa: E402
from zeppmap.chunked import ChunkedDecoder, ChunkedEncoder  # noqa: E402
from zeppmap.crypto import aes_decrypt  # noqa: E402
from zeppmap.session import WrongAuthKey, ZeppError, ZeppSession, parse_auth_key  # noqa: E402

try:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer
except ImportError:  # the FTP tests are skipped then
    FTPServer = None

KEY = bytes(range(0x10, 0x20))
# macOS alone keeps the watch connected at the system level and has CoreBluetooth;
# the tests of that behaviour check the opposite elsewhere.
ON_MACOS = sys.platform == "darwin"


def _openssl_with_b163():
    for binary in ("/usr/bin/openssl", "/opt/homebrew/bin/openssl"):
        try:
            out = subprocess.run([binary, "ecparam", "-list_curves"], capture_output=True, text=True).stdout
        except OSError:
            continue
        if "sect163r2" in out:
            return binary
    return None


class EcdhTest(unittest.TestCase):
    def test_both_sides_agree(self):
        a, b = ecdh_b163.generate_private(), ecdh_b163.generate_private()
        self.assertEqual(
            ecdh_b163.shared_secret(a, ecdh_b163.generate_public(b)),
            ecdh_b163.shared_secret(b, ecdh_b163.generate_public(a)),
        )

    def test_rejects_point_off_curve(self):
        with self.assertRaises(ValueError):
            ecdh_b163.shared_secret(ecdh_b163.generate_private(), bytes(48))

    def test_matches_openssl(self):
        openssl = _openssl_with_b163()
        if openssl is None:
            self.skipTest("openssl with the sect163r2 curve is not available")

        def field(text, name):
            block = re.search(name + r":\s*\n((?:\s+[0-9a-f:]+\n)+)", text).group(1)
            return re.sub(r"[^0-9a-f]", "", block)

        for _ in range(3):
            pem = subprocess.run(
                [openssl, "ecparam", "-name", "sect163r2", "-genkey", "-noout"],
                capture_output=True, text=True, check=True,
            ).stdout
            text = subprocess.run(
                [openssl, "ec", "-text", "-noout"], input=pem, capture_output=True, text=True
            ).stdout
            scalar = int(field(text, "priv"), 16)
            point = field(text, "pub")[2:]
            expected = (int(point[:len(point) // 2], 16), int(point[len(point) // 2:], 16))
            self.assertEqual(ecdh_b163._point_mul(ecdh_b163._BASE_X, ecdh_b163._BASE_Y, scalar), expected)


class ChunkedTest(unittest.TestCase):
    def test_plain_packet_layout(self):
        payload = bytes(range(52))
        packets = ChunkedEncoder(20).encode(0x0082, payload, False)
        self.assertEqual(packets[0], bytes([0x03, 0x01, 0x00, 0x01, 0x00, 52, 0, 0, 0, 0x82, 0x00]) + payload[:9])
        self.assertEqual(packets[1], bytes([0x03, 0x00, 0x00, 0x01, 0x01]) + payload[9:24])
        self.assertEqual(packets[2], bytes([0x03, 0x00, 0x00, 0x01, 0x02]) + payload[24:39])
        self.assertEqual(packets[3], bytes([0x03, 0x06, 0x00, 0x01, 0x03]) + payload[39:52])
        self.assertEqual(len(packets), 4)
        self.assertTrue(all(len(p) <= 20 for p in packets))

    def test_encrypted_packet_layout(self):
        session_key = bytes(range(16))
        encoder = ChunkedEncoder(244)
        encoder.set_encryption(0xFFFFFFFF, session_key)
        payload = b"\x11hello\x00"
        packets = encoder.encode(0x0003, payload, True)
        self.assertEqual(len(packets), 1)
        packet = packets[0]
        # first+last+ack+encrypted, handle 1, length of the unencrypted payload
        self.assertEqual(packet[:11], bytes([0x03, 0x0F, 0x00, 0x01, 0x00, 7, 0, 0, 0, 0x03, 0x00]))
        body = aes_decrypt(bytes(b ^ 1 for b in session_key), packet[11:])
        self.assertEqual(len(body), 16)
        self.assertEqual(body[:7], payload)
        self.assertEqual(body[7:11], bytes([0xFF, 0xFF, 0xFF, 0xFF]))
        self.assertEqual(int.from_bytes(body[11:15], "little"), zlib.crc32(body[:11]))
        self.assertEqual(body[15:], b"\x00")
        # the sequence number wraps to zero, the handle grows
        second = encoder.encode(0x0003, payload, True)[0]
        self.assertEqual(second[3], 2)
        self.assertEqual(aes_decrypt(bytes(b ^ 2 for b in session_key), second[11:])[7:11], bytes(4))

    def test_roundtrip(self):
        session_key = os.urandom(16)
        for max_packet in (20, 23, 185, 244):
            for size in (1, 8, 9, 15, 16, 24, 52, 200, 1000):
                for encrypt in (False, True):
                    encoder, decoder = ChunkedEncoder(max_packet), ChunkedDecoder()
                    encoder.set_encryption(7, session_key)
                    decoder.set_session_key(session_key)
                    payload = os.urandom(size)
                    result, acks = None, []
                    for packet in encoder.encode(0x1234, payload, encrypt):
                        self.assertLessEqual(len(packet), max_packet)
                        result, ack = decoder.feed(packet)
                        if ack:
                            acks.append(ack)
                    self.assertEqual(result, (0x1234, payload))
                    self.assertEqual(len(acks), 1)
                    self.assertEqual(acks[0][:4], bytes([0x04, 0x00, 0x01, 0x01]))


def make_pair(host_key=KEY, watch_key=KEY, **watch_options):
    """A session and a simulated watch connected in memory."""
    loop = asyncio.get_running_loop()
    holder = []
    watch = FakeWatch(watch_key, lambda packet: loop.call_soon(holder[0].on_packet, packet), **watch_options)

    async def write_packet(packet):
        watch.on_data_packet(packet)

    async def write_ack(packet):
        watch.on_ack_packet(packet)

    session = ZeppSession(write_packet, write_ack, host_key)
    holder.append(session)
    return session, watch


class SessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_login_and_services(self):
        session, watch = make_pair()
        await session.authenticate(timeout=5)
        self.assertTrue(session.authenticated and watch.authenticated)
        services = await session.request_services(timeout=5)
        self.assertEqual(services, {0x0000: False, 0x0003: True, 0x0006: True, 0x0046: True, 0x0082: False})
        # authentication and the service query go unencrypted
        self.assertEqual([encrypted for _e, _p, encrypted in watch.log], [False, False, False])
        self.assertEqual(watch.log[0][1][:4], bytes([0x04, 0x02, 0x00, 0x02]))
        await asyncio.sleep(0)
        self.assertEqual(len(watch.acks), 3)  # every watch reply acknowledged

    async def test_wrong_key(self):
        session, _watch = make_pair(host_key=bytes(16))
        with self.assertRaises(WrongAuthKey):
            await session.authenticate(timeout=5)
        self.assertFalse(session.authenticated)

    async def test_hotspot_and_ftp(self):
        session, watch = make_pair(ftp_address="ftp://192.168.4.1:2121/", ftp_username="admin")
        await session.authenticate(timeout=5)
        await session.request_services(timeout=5)

        await session.start_hotspot("ZM-0001", "abcdefgh1234", timeout=5)
        self.assertEqual(watch.hotspot, ("ZM-0001", "abcdefgh1234", "gadgetbridge"))
        self.assertEqual(watch.log[-1], (0x0003, b"\x11ZM-0001\x00abcdefgh1234\x00gadgetbridge\x00", True))

        address, username = await session.start_ftp(timeout=5)
        self.assertEqual((address, username), ("ftp://192.168.4.1:2121/", "admin"))
        self.assertEqual(watch.log[-1], (0x0006, b"\x01/mnt/data\x00", True))
        self.assertEqual(ftpops.parse_address(address), ("192.168.4.1", 2121))

        await session.stop_ftp(timeout=5)
        await session.stop_hotspot(timeout=5)
        self.assertIsNone(watch.ftp_root)
        self.assertIsNone(watch.hotspot)

    async def test_missing_service_and_timeout(self):
        session, _watch = make_pair(services=[(0x0000, False), (0x0082, False)])
        await session.authenticate(timeout=5)
        await session.request_services(timeout=5)
        with self.assertRaises(ZeppError):
            await session.start_ftp(timeout=1)

        session, _watch = make_pair(services=[])  # empty list: it tries and gets no answer
        await session.authenticate(timeout=5)
        await session.request_services(timeout=5)
        with self.assertRaises(ZeppError):
            await session.expect(0x0046, lambda p: True, 0.2, "an answer")

    async def test_foreign_messages_are_not_logged_or_kept(self):
        """Messages for other services (e.g. a weather request with the account token) stay out of the log."""
        session, watch = make_pair()
        await session.authenticate(timeout=5)
        await session.request_services(timeout=5)
        secret = b"GET https://example.invalid/weather apptoken TAJNY-TOKEN-1234"
        with self.assertLogs("zeppmap.session", level="DEBUG") as captured:
            watch._reply(0x0001, secret, True)
            watch._reply(0x0003, bytes([0x13, 0x00]), True)
            await asyncio.sleep(0.05)
        text = "\n".join(captured.output)
        self.assertNotIn(secret.hex(), text)
        self.assertNotIn("TAJNY", text)
        self.assertIn("content hidden", text)
        self.assertIn("<- 0003 1300", text)            # our own services are still logged
        self.assertNotIn(0x0001, session._queues)       # and foreign messages are not even kept

    async def test_hotspot_password_is_not_logged(self):
        session, _watch = make_pair()
        await session.authenticate(timeout=5)
        await session.request_services(timeout=5)
        password = "SecretHotspot99"
        with self.assertLogs("zeppmap.session", level="DEBUG") as captured:
            await session.start_hotspot("ZM-0001", password, timeout=5)
        text = "\n".join(captured.output)
        self.assertNotIn(password, text)
        self.assertNotIn(password.encode("utf-8").hex(), text)
        self.assertIn("hotspot credentials hidden", text)

    def test_key_parsing(self):
        self.assertEqual(parse_auth_key(" 0x" + KEY.hex() + "\n"), KEY)
        self.assertEqual(parse_auth_key(KEY.hex().upper()), KEY)
        for bad in ("", "1234", "zz" * 16, KEY.hex() + "00"):
            with self.assertRaises(ValueError):
                parse_auth_key(bad)


class BluetoothStateTest(unittest.TestCase):
    """Decisions on the Bluetooth state; the system query itself is replaced."""

    def _run(self, states, timeout=5.0):
        sequence = list(states)
        waits = []
        notes = []

        def read_state():
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]

        btstate.wait_for_state(read_state, waits.append, timeout, lambda: notes.append(1), notify_after=0.0)
        return waits, notes

    def test_ready_immediately(self):
        self.assertEqual(self._run([btstate.STATE_ON]), ([], []))

    def test_waits_for_permission_prompt(self):
        waits, notes = self._run([btstate.STATE_UNKNOWN, btstate.STATE_UNKNOWN, btstate.STATE_ON])
        self.assertEqual(len(waits), 2)
        self.assertEqual(notes, [1])  # the notice only once

    def test_reports_real_reason(self):
        cases = (
            ([btstate.STATE_OFF], "is off"),
            ([btstate.STATE_UNKNOWN, btstate.STATE_UNAUTHORIZED], "not allowed"),
            ([btstate.STATE_UNSUPPORTED], "does not offer"),
        )
        for states, expected in cases:
            with self.assertRaises(btstate.BluetoothUnavailable) as caught:
                self._run(states)
            self.assertIn(expected, str(caught.exception))

    @unittest.skipUnless(ON_MACOS, "CoreBluetooth exists only on macOS")
    def test_matches_corebluetooth(self):
        import CoreBluetooth

        self.assertEqual(
            (btstate.STATE_UNKNOWN, btstate.STATE_RESETTING, btstate.STATE_UNSUPPORTED,
             btstate.STATE_UNAUTHORIZED, btstate.STATE_OFF, btstate.STATE_ON),
            (CoreBluetooth.CBManagerStateUnknown, CoreBluetooth.CBManagerStateResetting,
             CoreBluetooth.CBManagerStateUnsupported, CoreBluetooth.CBManagerStateUnauthorized,
             CoreBluetooth.CBManagerStatePoweredOff, CoreBluetooth.CBManagerStatePoweredOn),
        )
        first, second = btstate.make_delegate(), btstate.make_delegate()
        self.assertIs(type(first), type(second))
        self.assertTrue(first.respondsToSelector_(b"centralManagerDidUpdateState:"))
        first.centralManagerDidUpdateState_(None)
        self.assertTrue(first.changed.is_set())
        self.assertFalse(second.changed.is_set())

    def test_times_out_when_system_stays_silent(self):
        with self.assertRaises(btstate.BluetoothUnavailable) as caught:
            self._run([btstate.STATE_UNKNOWN], timeout=0.0)
        self.assertIn("Terminal", str(caught.exception))
        self.assertNotIn("is off", str(caught.exception))


class AdvertisementTest(unittest.TestCase):
    def test_recognizes_watch(self):
        from zeppmap import ble

        self.assertTrue(ble.looks_like_watch("Balance 3", []))
        self.assertTrue(ble.looks_like_watch(None, ["0000FEE0-0000-1000-8000-00805F9B34FB"]))
        self.assertTrue(ble.looks_like_watch(None, [], [0x004C, 0x0157]))
        self.assertFalse(ble.looks_like_watch("LG TV", ["0000180f-0000-1000-8000-00805f9b34fb"], [0x004C]))
        self.assertFalse(ble.looks_like_watch(None, []))
        self.assertEqual(
            ble.describe_advertisement(
                ["0000fee0-0000-1000-8000-00805f9b34fb", "00001530-0000-3512-2118-0009af100700"], [0x0157]
            ),
            "services fee0,00001530-0000-3512-2118-0009af100700; manufacturer 0x0157",
        )
        self.assertEqual(ble.describe_advertisement([], []), "")


class FindWatchTest(unittest.IsolatedAsyncioTestCase):
    """A watch connected to the system takes precedence over scanning."""

    async def asyncSetUp(self):
        from zeppmap import ble

        self.ble = ble
        saved = (ble.connected_watches, ble.BleakScanner)
        self.addCleanup(lambda: (setattr(ble, "connected_watches", saved[0]), setattr(ble, "BleakScanner", saved[1])))
        self.scans = []

        class Scanner:
            @staticmethod
            async def find_device_by_filter(matches, timeout):
                self.scans.append(("filter", timeout))
                return None

            @staticmethod
            async def find_device_by_address(address, timeout):
                self.scans.append(("address", address))
                return None

        ble.BleakScanner = Scanner

    async def test_uses_device_held_by_macos(self):
        watch = argparse.Namespace(name="Balance 3 Ti", address="D079F294")
        other = argparse.Namespace(name="Amazfit Bip", address="AAAA")
        self.ble.connected_watches = lambda name=None: [
            d for d in (other, watch) if not name or name.lower() in d.name.lower()
        ]
        if not ON_MACOS:
            # elsewhere the system does not hold the watch, so the tool scans
            with self.assertRaises(self.ble.LinkError):
                await self.ble.find_watch(None, None, 5)
            self.assertEqual(self.scans, [("filter", 5)])
            return
        self.assertIs(await self.ble.find_watch(None, None, 5), other)
        self.assertIs(await self.ble.find_watch("balance", None, 5), watch)
        self.assertIs(await self.ble.find_watch(None, "d079f294", 5), watch)
        self.assertEqual(self.scans, [])

    async def test_falls_back_to_scanning(self):
        def broken(name=None):
            raise RuntimeError("the system did not answer")

        self.ble.connected_watches = broken
        with self.assertRaises(self.ble.LinkError) as caught:
            await self.ble.find_watch(None, None, 5)
        self.assertIn("did not answer", str(caught.exception))
        with self.assertRaises(self.ble.LinkError):
            await self.ble.find_watch(None, "AAAA", 5)
        self.assertEqual(self.scans, [("filter", 5), ("address", "AAAA")])


def _mkgmap_available() -> bool:
    try:
        swatch.find_java()
        swatch.find_mkgmap()
        return True
    except swatch.SwatchError:
        return False


class SwatchTest(unittest.TestCase):
    def test_tile_geometry(self):
        north, east, south, west = swatch.tile_bounds(1113, 692)
        # bounds of an original tile from Zepp, rounded to five places
        self.assertEqual(
            tuple(round(v, 5) for v in (north, east, south, west)), (50.28934, 15.82031, 50.1769, 15.64453)
        )
        self.assertEqual(swatch.tile_name(1113, 692), "11130692")
        inner = swatch.map_bounds(1113, 692)
        self.assertTrue(inner[0] < north + 1e-5 and inner[2] > south - 1e-5 and inner[1] < east and inner[3] > west)

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_zepp_page_from_official_types(self):
        north, east, south, west = swatch.map_bounds(1113, 694)
        lat, lon = (north + south) / 2, (east + west) / 2
        source = "\n".join([
            "[IMG ID]", "ID=11130694", "Name=OSM street map", "Levels=2", "Level0=24", "Level1=22", "[END-IMG ID]",
            "[POLYGON]", "Type=0x50", "Label=PRIMSKY LES", "EndLevel=0",
            "Data0=(%.5f,%.5f),(%.5f,%.5f),(%.5f,%.5f)" % (lat, lon, lat + 0.01, lon, lat, lon + 0.01), "[END]",
            "[POI]", "Type=0x2c02", "Label=MUZEUM", "EndLevel=0", "Data0=(%.5f,%.5f)" % (lat, lon), "[END]",
            "[POLYLINE]", "Type=0x13", "Label=SEIFERTOVA", "EndLevel=0",
            "Data0=(%.5f,%.5f),(%.5f,%.5f)" % (lat, lon, lat + 0.01, lon + 0.01), "[END]", "",
        ])
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        os.makedirs(os.path.join(folder, "11", "1113"))
        data = swatch.compile_mp(source, "11130694")
        with open(os.path.join(folder, "11", "1113", "694.img"), "wb") as handle:
            handle.write(data)
        # the point name is in the POI properties table and is read through it
        self.assertIn(("point", 0x2C02, "MUZEUM"),
                      [(e.kind, e.code, e.label) for e in imginfo.iter_elements(data)])

        found = swatch.official_types(folder)
        self.assertEqual(sorted(found["polygons"]), [swatch.BACKGROUND, 0x50])   # mkgmap adds the background itself
        self.assertEqual(sorted(found["points"]), [0x2C02])
        self.assertEqual(sorted(found["lines"]), [0x13])
        self.assertEqual(found["points"][0x2C02], {"tiles": 1, "names": ["MUZEUM"]})

        meanings = swatch.style_meanings()
        self.assertEqual(meanings["lines"].get(0x01), "highway=motorway")
        self.assertEqual(meanings["points"].get(0x2C02), "historic=museum")

        page = swatch.zepp_page(folder)
        shapes = {(s.kind, s.code) for s in page.shapes}
        self.assertTrue({("polygon", 0x50), ("point", 0x2C02), ("line", 0x13)} <= shapes)
        self.assertNotIn(("polygon", swatch.BACKGROUND), shapes)            # the swatch has its own background
        # the line runs over the forest with its right half
        line = next(s for s in page.shapes if s.kind == "line" and s.code == 0x13)
        forest = [s for s in page.shapes if s.kind == "polygon" and s.code == swatch.FOREST and not s.label]
        self.assertTrue(any(f.x0 > line.x0 and f.x1 == line.x1 and f.y0 < line.y0 < f.y1 for f in forest))
        legend = swatch.legend(page)
        self.assertIn("0x2c02   in  1 tiles  mkgmap: historic=museum  e.g. MUZEUM", legend)
        self.assertIn("SEIFERTOVA", legend)
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        with self.assertRaises(swatch.SwatchError):
            swatch.zepp_page(empty)

    def test_lines_page(self):
        page = swatch.lines_page()
        full = swatch.STANDARD_LINES + swatch.EXTENDED_LINES
        self.assertEqual(len(set(full)), len(full))
        self.assertEqual((page.width, page.height), (1200.0, 780.0))
        tested = [s for s in page.shapes if s.kind == "line" and s.label.startswith("0x") and "-" not in s.label]
        stubs = [s for s in page.shapes if s.kind == "line" and not s.label]
        self.assertEqual([s.code for s in tested], full)      # every type once, in cell order
        self.assertEqual(len(stubs), len(tested))             # each with a reference stub
        for line, stub in zip(tested, stubs):
            self.assertEqual((stub.code, stub.y0), (swatch.STREET, line.y0))
            self.assertLess(stub.x1, line.x0)
        self.assertEqual(tested[1].y0 - tested[0].y0, swatch.ROW_SPACING)
        self.assertEqual(tested[8].y0, tested[0].y0)           # the second cell next to the first
        self.assertGreater(tested[8].x0, tested[0].x1)
        headers = [s.label for s in page.shapes if s.kind == "line" and "-" in s.label]
        self.assertEqual(headers[:9], ["0x01-0x08", "0x09-0x10", "0x11-0x18", "0x19-0x20", "0x21-0x28",
                                       "0x29-0x30", "0x31-0x38", "0x39-0x3f", "0x10401-0x10702"])
        for shape in page.shapes:
            self.assertTrue(0 <= shape.x0 <= page.width and 0 <= shape.y0 <= page.height)

    def test_areas_and_contours_pages_fit_together(self):
        areas, contours = swatch.areas_page(), swatch.contours_page()
        self.assertEqual((areas.width, areas.height), (contours.width, contours.height))
        self.assertEqual(sorted(areas.codes("polygon")), sorted(swatch.STANDARD_POLYGONS + swatch.EXTENDED_POLYGONS))
        for code in swatch.EXTENDED_LINES:
            self.assertIn(code, areas.codes("line"))
        # area 0x17 lies in column 7 and row 1 of the grid
        cell = next(s for s in areas.shapes if s.kind == "polygon" and s.code == 0x17)
        first = next(s for s in areas.shapes if s.kind == "polygon" and s.code == 0x01)
        self.assertEqual((cell.x0 - first.x0, cell.y0 - first.y0), (6 * 60.0, 50.0))
        # ctl layer lines lie in the rows where the base page has only stubs, and to their right
        self.assertEqual(contours.codes("line"), swatch.STANDARD_LINES)
        self.assertEqual(contours.codes("polygon") + contours.codes("point"), [])
        stubs = {(s.x0, s.y0): s for s in areas.shapes if s.kind == "line" and not s.label and s.code == swatch.STREET}
        for line in contours.shapes:
            stub = stubs[(line.x0 - swatch.STUB_LENGTH - swatch.GAP, line.y0)]
            self.assertLess(stub.x1, line.x0)
            crossing = [s for s in areas.shapes if s.kind == "line" and s.y0 == line.y0
                        and s.x0 < line.x1 and s.x1 > line.x0]
            self.assertEqual(crossing, [])  # the base map is empty at that place
        self.assertIn("ctl 0x01-0x08", [s.label for s in areas.shapes])
        self.assertEqual(contours.levels, [24, 22, 20, 18, 16, 15])
        # overlaps: lines "on top of each other" share the position, "side by side" are offset
        rows = {}
        for shape in areas.shapes:
            if shape.kind == "line" and not shape.label and shape.x1 - shape.x0 == 300.0:
                rows.setdefault(round(shape.y0 / 40.0), []).append(shape)
        self.assertTrue(any(len({s.y0 for s in group}) == 1 and len(group) == 2 for group in rows.values()))
        self.assertTrue(any(len({s.y0 for s in group}) == 3 for group in rows.values()))

    def test_source_and_build(self):
        source = swatch.swatch_mp(1113, 694)
        source.encode("ascii")  # ASCII only, so that the codepage does not matter
        self.assertIn("ID=11130694", source)
        self.assertIn("Levels=5", source)
        self.assertIn("Level4=17", source)
        for code in (0x01, 0x16, 0x3F):
            self.assertIn("Type=0x%02x\nLabel=0x%02x\n" % (code, code), source)
        self.assertIn("Type=0x10d01\nLabel=0x10d01\n", source)
        self.assertIn("Type=0x06\nLabel=0x01-0x08\n", source)  # the header is a named street
        self.assertEqual(source.count("[POLYGON]"), 1)  # jen podklad
        pages = len(swatch.page_origins(1113, 694, swatch.lines_page()))
        self.assertGreater(pages, 100)
        self.assertEqual(source.count("Label=0x16\n"), pages)

        contour_source = swatch.swatch_mp(1113, 694, swatch.contours_page())
        self.assertIn("Levels=6", contour_source)
        self.assertIn("Level5=15", contour_source)
        self.assertIn("Type=0x22\nLabel=0x22\n", contour_source)
        swatch.swatch_mp(1113, 694, swatch.areas_page()).encode("ascii")

        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        seen = []

        def fake_compiler(text, name):
            seen.append(name)
            return b"IMG" + name.encode()

        lines = []
        self.assertEqual(swatch.build([(1113, 692), (1114, 694)], out, lines.append, fake_compiler), 2)
        self.assertEqual(seen, ["11130692", "11140694"])
        with open(os.path.join(out, "11", "1114", "694.img"), "rb") as handle:
            self.assertEqual(handle.read(), b"IMG11140694")
        self.assertEqual(swatch.tiles_in(out), [(1113, 692), (1114, 694)])
        self.assertEqual(swatch.tiles_in(os.path.join(out, "11")), [(1113, 692), (1114, 694)])
        with self.assertRaises(swatch.SwatchError):
            swatch.build([], out, lines.append, fake_compiler)
        legend = swatch.legend()
        for code in swatch.STANDARD_LINES + swatch.EXTENDED_LINES:
            self.assertIn(imginfo.format_code(code), legend)
        self.assertIn("0x10c02", swatch.legend(swatch.areas_page()))
        self.assertIn("0a 01 6m", swatch.legend(swatch.areas_page()))
        overlay = swatch.overlay_page()
        self.assertEqual(overlay.levels, swatch.CONTOUR_LEVELS)
        self.assertEqual(overlay.codes("line"), swatch.lines_page().codes("line"))

    def test_not_an_img(self):
        with self.assertRaises(imginfo.ImgError):
            imginfo.read_tile(b"x" * 4096)

    def test_ski_page_and_type_encoding(self):
        page = swatch.ski_page()
        self.assertEqual(page.container, "ski")
        self.assertEqual(page.levels, swatch.BASE_LEVELS)
        tested = [s.code for s in page.shapes if s.kind == "line" and s.label.startswith("0x") and "-" not in s.label]
        self.assertEqual(tested, list(range(0x40, 0x7F)))
        self.assertEqual({s.code for s in page.shapes if s.kind == "line"}, set(range(0x40, 0x7F)))  # the frame too
        self.assertEqual(page.codes("polygon"), list(range(0x50, 0x80)))
        self.assertEqual(page.codes("point"), [])

        self.assertEqual(skitile.compile_type(0x43), (0x03, True))
        self.assertEqual(skitile.compile_type(0x7E), (0x3E, True))
        self.assertEqual(skitile.compile_type(0x40), (0x3F, False))  # a stand-in type, fixed in the tile
        for bad in (0x06, 0x3F, 0x7F):
            with self.assertRaises(skitile.SkiTileError):
                skitile.compile_type(bad)
        source = swatch.swatch_mp(1113, 694, page)
        self.assertIn("Type=0x03\nLabel=0x43\nDirIndicator=1\n", source)
        self.assertIn("Type=0x3f\nLabel=0x40\nEndLevel", source)
        self.assertNotIn("Type=0x43", source)  # mkgmap would reject a type above 0x3f

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_compiled_ski_tile(self):
        """A ski tile: header signatures and line types as in the tiles from Zepp."""
        x, y = 1114, 694
        plain = swatch.compile_mp(swatch.swatch_mp(x, y, swatch.ski_page()), swatch.tile_name(x, y))
        self.assertEqual(imginfo.container_kind(plain), "garmin")
        ski = skitile.to_ski_tile(plain)
        self.assertEqual(len(ski), len(plain))
        self.assertEqual((ski[0x10:0x16], ski[0x41:0x47]), (b"HMSNOW", b"HMSKIM"))
        info = imginfo.read_tile(ski)
        self.assertEqual(info.kind, "ski")
        self.assertEqual(sorted(info.lines), swatch.SKI_LINES)       # the type overview in the TRE part
        self.assertEqual(sorted(info.polygons), sorted(set(swatch.SKI_POLYGONS + [swatch.BACKGROUND])))
        self.assertEqual([level[:3] for level in info.levels],
                         [(4, True, 17), (3, False, 18), (2, False, 20), (1, False, 22), (0, False, 24)])
        elements = list(imginfo.iter_elements(ski))
        lines = [e for e in elements if e.kind == "line"]
        self.assertEqual({e.type_byte & 0x7F for e in lines}, set(swatch.SKI_LINES))   # typy v datech
        # mkgmap stores labels in capitals; every tested line carries the code of its type
        named = [e for e in lines if e.label.startswith("0X") and "-" not in e.label]
        self.assertTrue(named and all(e.label == "0X%02X" % (e.type_byte & 0x7F) for e in named))
        black = [e for e in lines if e.label == "0X40"]
        self.assertTrue(black and all(e.type_byte & 0x7F == 0x40 for e in black))
        # the parts are contiguous in the file and their positions match the content
        spans = imginfo.subfile_spans(ski)
        for name, content in imginfo.split_subfiles(ski).items():
            start, size = spans[name]
            self.assertEqual(ski[start:start + size], content)
        # an ordinary tile cannot be turned into a ski tile
        normal = swatch.compile_mp(swatch.swatch_mp(x, y, swatch.contours_page()), swatch.tile_name(x, y))
        with self.assertRaises(skitile.SkiTileError):
            skitile.to_ski_tile(normal)
        self.assertEqual(imginfo.container_kind(imginfo.with_signature(ski, "garmin")), "garmin")

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_compiled_tile_matches_zepp_layout(self):
        """A real compilation: the tile has the properties of the tiles from Zepp."""
        x, y = 1114, 694
        info = imginfo.read_tile(swatch.compile_mp(swatch.swatch_mp(x, y), swatch.tile_name(x, y)))
        self.assertEqual(info.description, "OSM street map")
        self.assertEqual(info.map_id, 11140694)
        self.assertEqual([level[:3] for level in info.levels],
                         [(4, True, 17), (3, False, 18), (2, False, 20), (1, False, 22), (0, False, 24)])
        self.assertEqual((info.label_encoding, info.codepage, info.draw_priority), (10, 65001, 25))
        self.assertEqual(sorted(info.lines), sorted(swatch.STANDARD_LINES + swatch.EXTENDED_LINES))
        self.assertEqual(sorted(info.polygons), [swatch.BACKGROUND])
        units = (1 << 24) / 360.0
        expected = [round(value * units) for value in swatch.tile_bounds(x, y)]
        self.assertEqual([round(value * units) for value in info.bounds], expected)

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_compiled_areas_and_contour_tiles(self):
        x, y = 1114, 694
        areas = imginfo.read_tile(swatch.compile_mp(swatch.swatch_mp(x, y, swatch.areas_page()), swatch.tile_name(x, y)))
        self.assertEqual(sorted(areas.polygons),
                         sorted(set(swatch.STANDARD_POLYGONS + swatch.EXTENDED_POLYGONS + [swatch.BACKGROUND])))
        for code in swatch.EXTENDED_LINES + [0x01, 0x03, 0x0A, 0x14]:
            self.assertIn(code, areas.lines)
        contours = imginfo.read_tile(
            swatch.compile_mp(swatch.swatch_mp(x, y, swatch.contours_page()), swatch.tile_name(x, y)))
        # the same structure as Zepp's contour tiles
        self.assertEqual([level[:3] for level in contours.levels],
                         [(5, True, 15), (4, False, 16), (3, False, 18), (2, False, 20), (1, False, 22), (0, False, 24)])
        self.assertEqual(sorted(contours.lines), swatch.STANDARD_LINES)
        self.assertEqual(sorted(contours.polygons), [swatch.BACKGROUND])
        self.assertEqual(contours.map_id, 11140694)
        units = (1 << 24) / 360.0
        self.assertEqual([round(value * units) for value in contours.bounds],
                         [round(value * units) for value in swatch.tile_bounds(x, y)])

    def test_find_java_checks_intel_homebrew(self):
        intel_java = "/usr/local/opt/openjdk/bin/java"
        ok = subprocess.CompletedProcess([intel_java, "-version"], 0, "", "")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZEPPMAP_JAVA", None)
            with mock.patch.object(os.path, "exists", side_effect=lambda path: path == intel_java):
                with mock.patch.object(shutil, "which", return_value=None):
                    with mock.patch.object(subprocess, "run", return_value=ok) as run:
                        self.assertEqual(swatch.find_java(), intel_java)
        run.assert_called_once()


def _relation(tags, *members):
    return {"type": "relation", "id": 1, "tags": dict(tags, type="route"), "members": list(members)}


def _way(ref, *points):
    return {"type": "way", "ref": ref, "role": "",
            "geometry": [None if p is None else {"lat": p[0], "lon": p[1]} for p in points]}


class CtlMergeTest(unittest.TestCase):
    """Reading line geometry, and contour tiles with marks."""

    def _bounds(self):
        north, east, south, west = swatch.map_bounds(1113, 694)
        return north, east, south, west

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_reads_line_geometry(self):
        import math
        import random
        north, east, south, west = self._bounds()
        rng = random.Random(4)
        source = ["[IMG ID]", "ID=11130694", "Name=OSM street map", "Levels=3", "Level0=24", "Level1=22",
                  "Level2=20", "[END-IMG ID]"]
        lines = []
        for index in range(10):
            lat, lon = south + (north - south) * rng.random(), west + (east - west) * rng.random()
            points = [(lat, lon)]
            for _ in range(rng.randint(2, 30)):
                step = rng.choice([0.00002, 0.0003, 0.004, 0.02])   # big jumps too
                lat = min(max(lat + rng.uniform(-step, step), south), north)
                lon = min(max(lon + rng.uniform(-step, step), west), east)
                points.append((lat, lon))
            level = index % 2
            lines.append((level, points))
            source += ["[POLYLINE]", "Type=0x22", "Label=%d" % (100 + index), "EndLevel=%d" % level,
                       "Data%d=%s" % (level, ",".join("(%.7f,%.7f)" % p for p in points)), "[END]"]
        data = swatch.compile_mp("\n".join(source) + "\n", "11130694")
        decoded = [e for e in imginfo.iter_elements(data, with_points=True) if e.kind == "line"]
        self.assertEqual(len(decoded), len(lines))
        unit = 360.0 / (1 << 24)

        def distance(p, polyline):
            best = float("inf")
            for a, b in zip(polyline, polyline[1:]):
                dx, dy = b[1] - a[1], b[0] - a[0]
                t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((p[1] - a[1]) * dx + (p[0] - a[0]) * dy) / (dx * dx + dy * dy)))
                best = min(best, math.hypot(p[1] - a[1] - t * dx, p[0] - a[0] - t * dy))
            return best

        for element in decoded:
            level, points = lines[int(element.label) - 100]
            self.assertEqual(element.level, level)
            grid = unit * (1 << (2 * level))          # 24 and 22 bits
            self.assertLess(max(distance(p, points) for p in element.points), grid)
            self.assertLess(abs(element.points[0][0] - points[0][0]), grid)
            self.assertLess(abs(element.points[-1][1] - points[-1][1]), grid)

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_merged_tile_keeps_contours(self):
        north, east, south, west = self._bounds()
        lat, lon = (north + south) / 2, (east + west) / 2
        contour = lambda code, label, level, shift: [
            "[POLYLINE]", "Type=0x%02x" % code, "Label=%s" % label, "EndLevel=%d" % level,
            "Data%d=(%.6f,%.6f),(%.6f,%.6f),(%.6f,%.6f)" % (level, lat + shift, lon, lat + shift + 0.01, lon + 0.01,
                                                         lat + shift, lon + 0.02), "[END]"]
        source = ["[IMG ID]", "ID=11130694", "Name=OSM street map", "Elevation=F", "Levels=6"]
        source += ["Level%d=%d" % (i, bits) for i, bits in enumerate(swatch.CONTOUR_LEVELS)] + ["[END-IMG ID]"]
        # the watch draws level 1 of Zepp's tile at the same scales as our level 0; it is not merged
        source += contour(0x22, "787", 1, 0.002) + contour(0x22, "689", 2, 0.003)
        source += contour(0x21, "820", 0, 0.0) + contour(0x23, "755", 0, 0.005) + contour(0x23, "755", 4, 0.0)
        ctl = swatch.compile_mp("\n".join(source) + "\n", "11130694")
        self.assertEqual(ctlmerge.contour_levels(ctl), swatch.CONTOUR_LEVELS)
        contours = ctlmerge.read_contours(ctl)
        self.assertEqual(sorted((c, l, lvl) for c, l, _p, lvl in contours),
                         [(0x21, "820", 0), (0x22, "689", 2), (0x22, "787", 1), (0x23, "755", 0), (0x23, "755", 4)])
        self.assertEqual(sorted((c, l, lvl) for c, l, _p, lvl in ctlmerge.place_contours(contours)),
                         [(0x21, "820", 0), (0x22, "689", 1), (0x23, "755", 0), (0x23, "755", 3)])

        red = [(lat - 0.01, lon - 0.01), (lat - 0.01, lon + 0.03)]
        way = routes.RouteWay(red, {"red", "blue"})
        # a winding path with a point about every 5 m: it must stay whole at the closest scales
        wiggly = [(lat - 0.015 + (0.00002 if index % 2 else 0.0), lon + index * 0.00007) for index in range(60)]
        lift = routes.Lift("LANOVKA", [(lat - 0.02, lon), (lat - 0.02, lon + 0.02)])
        marks = ctlmerge.mark_lines([way, routes.RouteWay(wiggly, {"green"})], [lift])
        self.assertEqual(sorted({level for _c, _l, _p, level in marks}), [0, 1, 2, 3])

        def gap(level):
            """Distance of red and blue in the given level, in metres."""
            ends = {code: points[0][0] for code, _l, points, lvl in marks if lvl == level and code in (0x43, 0x46)}
            return abs(ends[0x43] - ends[0x46]) * 111320

        for level, spacing in enumerate(ctlmerge.SPACING):
            self.assertAlmostEqual(gap(level), spacing, delta=0.1)
        self.assertEqual(sorted(c for c, _l, _p, lvl in marks if lvl == 3), [0x43, 0x49, routes.LIFT_TYPE])   # overview

        data = ctlmerge.merged_tile(1113, 694, marks, ctl)
        info = imginfo.read_tile(data)
        self.assertEqual(info.kind, "ski")
        # the same structure as mark tiles: at 8 to 100 m the watch draws the 24-bit level
        self.assertEqual([bits for _z, _i, bits, _n in sorted(info.levels)], swatch.BASE_LEVELS)
        self.assertEqual(sorted(info.lines), [0x21, 0x22, 0x23, 0x43, 0x46, 0x49, 0x64])   # contours without direction
        elements = [e for e in imginfo.iter_elements(data, with_points=True) if e.kind == "line"]
        found = sorted((e.type_byte & 0x7F, e.label, e.level) for e in elements)
        self.assertIn((0x21, "820", 0), found)                         # the elevation stayed in feet
        self.assertIn((0x22, "689", 1), found)
        self.assertNotIn("787", [label for _c, label, _l in found])
        self.assertIn((0x23, "755", 3), found)
        self.assertIn((0x43, "", 3), found)                            # the mark in the farthest level too
        self.assertIn((0x64, "LANOVKA", 0), found)
        green = {e.level: len(e.points) for e in elements if e.type_byte & 0x7F == 0x49}
        self.assertEqual(green[0], len(wiggly))                        # no point missing in the most detailed level
        self.assertLess(green[1], len(wiggly) // 2)                    # mkgmap simplifies the coarser levels
        # contours only for the closest scales: the coarser levels keep only marks
        near = ctlmerge.merged_tile(1113, 694, marks, ctl, contour_source=[0])
        near_lines = [e for e in imginfo.iter_elements(near) if e.kind == "line"]
        self.assertEqual({e.level for e in near_lines if e.type_byte & 0x7F in ctlmerge.CONTOUR_TYPES}, {0})
        self.assertEqual({e.level for e in near_lines}, {0, 1, 2, 3})
        with self.assertRaises(skitile.SkiTileError):                  # a different contour structure
            ctlmerge.merged_tile(1113, 694, marks, swatch.compile_mp(routes.routes_mp(1113, 694, []), "11130694"))

        # a ski type stored the same way as a contour must not end up in the same tile
        clash = swatch.compile_mp(routes.routes_mp(1113, 694, [(0x63, "", red, 0)], levels=swatch.CONTOUR_LEVELS,
                                                   plain=[(0x23, "755", red, 0)]), "11130694")
        with self.assertRaises(skitile.SkiTileError):
            skitile.to_ski_tile(clash, plain=ctlmerge.CONTOUR_TYPES)

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_level_probe(self):
        data = ctlmerge.probe_tile(1113, 693)
        info = imginfo.read_tile(data)
        self.assertEqual([bits for _z, _i, bits, _n in sorted(info.levels)], ctlmerge.PROBE_LEVELS)
        labels = {e.level: e.label for e in imginfo.iter_elements(data) if e.kind == "line"}
        self.assertEqual(labels, {level: "L%d" % bits for level, bits in enumerate(ctlmerge.PROBE_LEVELS[:-1])})
        # the lines are dense in all levels
        rows = {e.level for e in imginfo.iter_elements(data) if e.kind == "line"}
        self.assertEqual(rows, set(range(len(ctlmerge.PROBE_LEVELS) - 1)))
        self.assertEqual(len(ctlmerge.describe_probe()), len(ctlmerge.PROBE_LEVELS) - 1)

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_width_swatch(self):
        data = ctlmerge.width_tile(1113, 693)
        info = imginfo.read_tile(data)
        self.assertEqual(info.kind, "ski")
        self.assertEqual([bits for _z, _i, bits, _n in sorted(info.levels)], ctlmerge.LEVELS)
        lines = [e for e in imginfo.iter_elements(data, with_points=True) if e.kind == "line"]
        for level in range(len(ctlmerge.WIDTH_STEPS)):
            rows = {}
            for element in lines:
                if element.level == level:
                    lats = {round(lat, 7) for lat, _lon in element.points}
                    self.assertEqual(len(lats), 1)                      # the line is horizontal
                    rows[lats.pop()] = (element.type_byte & 0x7F, element.label)
            ordered = [rows[lat] for lat in sorted(rows)]
            # every type is labelled with its code and the types always alternate in the same order
            self.assertTrue(all(label == "%02X" % code for code, label in ordered))
            self.assertEqual({code for code, _l in ordered}, set(ctlmerge.WIDTH_TYPES))
            start = ctlmerge.WIDTH_TYPES.index(ordered[0][0])
            self.assertEqual([code for code, _l in ordered[:8]], (ctlmerge.WIDTH_TYPES * 2)[start:start + 8])
            # the lines lie on the level grid, so they are exactly as far apart as the legend says
            for a, b in zip(sorted(rows), sorted(rows)[1:]):
                self.assertAlmostEqual(b - a, ctlmerge.width_spacing(level), delta=3e-7)   # asi 3 cm
        self.assertEqual(len(ctlmerge.describe_widths()), len(ctlmerge.WIDTH_TYPES) + len(ctlmerge.WIDTH_STEPS))


class RoutesTest(unittest.TestCase):
    # Tile 1113/694 spans roughly lat 49.951 to 50.064 and lon 15.645 to 15.820.
    A, B, C = (50.0000, 15.7000), (50.0000, 15.7100), (50.0100, 15.7100)

    def _data(self):
        shared = _way(10, self.A, self.B)
        return {"elements": [
            _relation({"route": "hiking", "kct_red": "major", "osmc:symbol": "red:white:red_bar"}, shared,
                      _way(11, self.B, self.C)),
            _relation({"route": "foot", "osmc:symbol": "blue:white:blue_bar"}, shared),
            _relation({"route": "bicycle", "ref": "4123", "network": "rcn"}, shared),
            _relation({"route": "bicycle", "ref": "2", "network": "ncn"}, shared),
            _relation({"route": "hiking", "osmc:symbol": "white:white:green_backslash"}, _way(12, self.A, self.C)),
            _relation({"route": "hiking", "osmc:symbol": "yellow:white:yellow_bar"}, _way(13, self.C, None, self.A, self.B)),
            _relation({"route": "hiking", "osmc:symbol": "blue:blue:shell_modern", "name": "Svatojakubská cesta"},
                      _way(15, self.A, self.C)),
            _relation({"route": "horse"}, _way(14, self.A, self.C)),
            {"type": "node", "id": 5},
        ]}

    def test_marks_from_tags(self):
        self.assertEqual(routes.mark_of({"route": "hiking", "kct_green": "local"}), "green")
        self.assertEqual(routes.mark_of({"route": "hiking", "osmc:symbol": "yellow:white:yellow_bar"}), "yellow")
        self.assertEqual(routes.mark_of({"route": "mtb"}), "cycle")
        self.assertEqual(routes.mark_of({"route": "hiking", "osmc:symbol": "white:white:green_backslash"}), None)
        self.assertEqual(routes.mark_of({"route": "hiking"}), None)
        self.assertEqual(routes.mark_of({"route": "bus", "colour": "red"}), None)
        # side trails are marked in the colour of their route
        self.assertEqual(routes.mark_of({"route": "hiking", "kct_red": "interesting_object"}), "red")
        self.assertEqual(routes.mark_of({"route": "hiking", "osmc:symbol": "yellow:white:yellow_bowl"}), "yellow")
        # an educational trail is not a green KČT mark
        self.assertEqual(routes.mark_of({"route": "hiking", "kct_green": "learning"}), "learning")
        self.assertEqual(routes.mark_of({"route": "hiking", "osmc:symbol": "green:white:green_backslash"}), "learning")
        # Polish marking looks the same and has black in addition
        self.assertEqual(routes.mark_of({"route": "hiking", "osmc:symbol": "black:white:black_bar"}), "black")
        self.assertEqual(routes.skitile.compile_type(routes.MARK_TYPES["black"]), (0x10, True))
        # a pilgrim way with a shell, a colour without a KČT mark and non-hiking KČT marks are not drawn
        self.assertEqual(routes.mark_of({"route": "hiking", "osmc:symbol": "blue:blue:shell_modern"}), None)
        self.assertEqual(routes.mark_of({"route": "hiking", "colour": "blue"}), None)
        self.assertEqual(routes.mark_of({"route": "hiking", "osmc:symbol": "red:white:blue_bar"}), None)
        self.assertEqual(routes.mark_of({"route": "hiking", "kct_blue": "ski"}), None)

    def test_alpine_marks_carry_trail_numbers(self):
        alpine = lambda symbol, **more: dict({"route": "hiking", "osmc:symbol": symbol}, **more)
        # a red-white-red mark, red with a white stripe for CAI; the first field can be anything
        for symbol in ("red:red:white_bar:801:black", "red:red:white_stripe:117:black", "red:red:white_bar",
                       "blue:red:white_bar", "yellow:red:white_bar:E5:black"):
            self.assertEqual(routes.mark_of(alpine(symbol)), "red", symbol)
        for symbol in ("red:red:white_triangle", "yellow:yellow:red_lower", "red::blue_triangle_line:AV4", "red:red:"):
            self.assertIsNone(routes.mark_of(alpine(symbol)), symbol)
        self.assertEqual(routes.trail_ref(alpine("red:red:white_bar:801:black", ref="801")), "801")
        self.assertEqual(routes.trail_ref(alpine("red:red:white_bar")), "")                      # a trail without a number
        self.assertEqual(routes.trail_ref(alpine("red:red:white_bar", ref="Dachsteinrundweg")), "")   # an abbreviated name
        # a KČT mark shows no number even if the relation has one
        self.assertEqual(routes.trail_ref(alpine("red:white:red_bar", ref="0401")), "")
        self.assertEqual(routes.trail_ref(alpine("red:red:white_bar", ref="7", kct_red="major")), "")
        self.assertEqual(routes.trail_ref({"route": "bicycle", "ref": "2", "osmc:symbol": "red:red:white_bar"}), "")
        self.assertEqual(routes._refs_label({"105", "9", "101", "40A"}), "9, 101, 105, 40A")
        self.assertEqual(routes._refs_label({"1", "2", "3", "4", "5"}), "1, 2, 3, 4 +")

        shared = _way(10, self.A, self.B)
        data = {"elements": [
            _relation(alpine("red:red:white_stripe:105:black", ref="105"), shared, _way(11, self.B, self.C)),
            _relation(alpine("red:red:white_bar:101:black", ref="101"), shared),
            _relation({"route": "bicycle", "ref": "7", "network": "lcn"}, shared),
        ]}
        ways = routes.merge_ways(routes.parse(data))
        self.assertEqual(sorted(sorted(w.trail_refs) for w in ways), [["101", "105"], ["105"]])   # the number changes, the route splits
        lines = [(code, label) for code, label, _p, level in routes.spread(ways) if level == 0]
        # two trails along one way make one red line with both numbers; the cycle route runs beside it
        self.assertEqual(sorted(lines), [(0x43, "101, 105"), (0x43, "105"), (0x54, "7")])
        source = routes.routes_mp(1113, 694, routes.tile_lines(ways, 1113, 694))
        self.assertIn("Type=0x03\nLabel=101, 105\nDirIndicator=1\n", source)

    def test_parse_collects_marks_per_way(self):
        ways = routes.parse(self._data())
        # an unknown mark and another kind of route are not taken; a way with a gap gives only a contiguous run
        self.assertEqual(len(ways), 3)
        shared = next(w for w in ways if "cycle" in w.marks)
        self.assertEqual(shared.geometry, [self.A, self.B])
        self.assertEqual(shared.marks, {"red", "blue", "cycle"})      # three routes along one way
        self.assertEqual(shared.cycle_refs, {"4123", "2"})
        self.assertEqual(next(w for w in ways if w.geometry == [self.B, self.C]).marks, {"red"})
        yellow = next(w for w in ways if w.marks == {"yellow"})
        self.assertEqual(yellow.geometry, [self.A, self.B])            # the point before the gap was dropped
        totals = routes.summary(ways)
        self.assertAlmostEqual(totals["red"], (716 + 1113) / 1000, delta=0.02)
        self.assertEqual(totals["green"], 0.0)

    def test_offset_and_orientation(self):
        east = [(50.0, 15.70), (50.0, 15.71)]
        moved = routes.offset_line(east, 7.0)
        for before, after in zip(east, moved):                      # right of the eastward direction is south
            self.assertAlmostEqual((before[0] - after[0]) * 111320, 7.0, places=2)
            self.assertAlmostEqual(before[1], after[1], places=9)
        self.assertEqual(routes.offset_line(east, 0.0), east)
        corner = routes.offset_line([self.A, self.B, self.C], 7.0)
        self.assertAlmostEqual((corner[1][1] - self.B[1]) * 111320 * 0.6428, 7.0, delta=0.05)   # roh jde ven
        self.assertAlmostEqual((self.B[0] - corner[1][0]) * 111320, 7.0, delta=0.05)
        # a sharp bend is rounded on the outer side: the corner stays exactly one offset from the route
        hairpin = [(50.0, 15.70), (50.0, 15.71), (50.00001, 15.70)]
        spike = routes.offset_line(hairpin, 7.0)
        self.assertEqual(len(spike), 5)
        for point in spike[1:4]:
            self.assertAlmostEqual(routes._distance(point, hairpin[1]), 7.0, delta=0.1)
        # on the inner side there is no corner, only the ends remain
        self.assertEqual(len(routes.offset_line(hairpin, -7.0)), 2)
        # a bump smaller than the offset makes neither a loop nor a tooth in the offset line
        bump = [(50.0, 15.70), (50.0, 15.7049), (50.0001, 15.705), (50.0, 15.7051), (50.0, 15.71)]
        for point in routes.offset_line(bump, 30.0):
            self.assertAlmostEqual((50.0 - point[0]) * 111320, 30.0, delta=0.5)
        around = routes.offset_line(bump, -30.0)                    # from the other side it goes round the bump
        self.assertAlmostEqual(max((p[0] - 50.0001) * 111320 for p in around), 30.0, delta=0.5)
        self.assertTrue(all(routes._distance(p, bump[2]) > 29.5 for p in around))
        self.assertEqual(routes.oriented([self.B, self.A]), [self.A, self.B])
        self.assertEqual(routes.oriented([self.A, self.B]), [self.A, self.B])

    def test_clip_to_rectangle(self):
        box = dict(north=50.05, east=15.75, south=49.95, west=15.65)
        inside = [(50.0, 15.70), (50.01, 15.71)]
        self.assertEqual(routes.clip_line(inside, **box), [inside])
        self.assertEqual(routes.clip_line([(50.10, 15.70), (50.20, 15.71)], **box), [])
        crossing = routes.clip_line([(50.0, 15.60), (50.0, 15.70), (50.0, 15.80)], **box)
        self.assertEqual(crossing, [[(50.0, 15.65), (50.0, 15.70), (50.0, 15.75)]])
        # a line that leaves the rectangle and comes back gives two pieces
        back = routes.clip_line([(50.0, 15.70), (50.10, 15.70), (50.10, 15.72), (50.0, 15.72)], **box)
        self.assertEqual(len(back), 2)
        self.assertEqual(back[0][-1], (50.05, 15.70))
        self.assertEqual(back[1][0], (50.05, 15.72))

    def test_tile_lines_spread_concurrent_marks(self):
        ways = routes.merge_ways(routes.parse(self._data()))
        lines = routes.tile_lines(ways, 1113, 694)
        self.assertEqual({level for _c, _l, _p, level in lines}, {0, 1, 2, 3})   # every level has its own lines
        self.assertEqual(len(routes.LEVEL_SPACING), 3)                           # level 3 is the overview

        def trio(level):
            """Red, blue and a cycle route along a shared way: (latitude, type) from the south."""
            shared = [(pts[0][0], code) for code, _l, pts, lvl in lines
                      if lvl == level and code in (0x43, 0x46, 0x54) and abs(pts[0][1] - 15.70) < 2e-3
                      and abs(pts[1][0] - pts[0][0]) < 1e-6]
            return sorted(shared)

        for level, spacing in enumerate(routes.LEVEL_SPACING):
            found = trio(level)
            self.assertEqual(len(found), 3, level)
            self.assertLess(2 * spacing, 716)
            # order from the north: red, blue, cycle route; spacing by level
            self.assertEqual([code for _lat, code in found][::-1], [0x43, 0x46, 0x54], level)
            self.assertAlmostEqual((found[2][0] - found[1][0]) * 111320, spacing, delta=spacing * 0.02)
            self.assertAlmostEqual((found[1][0] - found[0][0]) * 111320, spacing, delta=spacing * 0.02)
        self.assertGreater(routes.LEVEL_SPACING[1], 2 * routes.LEVEL_SPACING[0])    # farther apart when zoomed out
        # in the overview only red stands for the shared way, exactly along it
        self.assertEqual(trio(3), [(50.0, 0x43)])
        self.assertEqual(sorted({code for code, _l, _p, lvl in lines if lvl == 3}), [0x43, 0x5C])

        # red continues alone after the shared way; the ends of both lines meet halfway between them
        reds = sorted((pts for code, _l, pts, lvl in lines if lvl == 0 and code == 0x43), key=lambda pts: pts[0][1])
        self.assertEqual(len(reds), 2)
        self.assertEqual(reds[0][-1], reds[1][0])
        self.assertAlmostEqual((reds[0][-1][0] - 50.0) * 111320, routes.LEVEL_SPACING[0] / 2, delta=0.1)
        self.assertEqual(reds[1][1:], [self.B, self.C])

        label = next(label for code, label, _p, _lvl in lines if code == routes.MARK_TYPES["cycle"])
        self.assertEqual(label, "2, 4123")                    # route numbers sorted
        self.assertEqual(routes.tile_lines(ways, 1114, 692), [])   # nothing elsewhere

        source = routes.routes_mp(1113, 694, lines)
        source.encode("ascii")
        self.assertIn("Type=0x03\nDirIndicator=1\nEndLevel=0\nData0=", source)       # red 0x43, level 0
        self.assertIn("Type=0x03\nDirIndicator=1\nEndLevel=2\nData2=", source)       # and level 2 separately
        self.assertIn("Type=0x14\nLabel=2, 4123\nDirIndicator=1\n", source)          # cyklotrasa 0x54
        self.assertIn("ID=11130694", source)

    def test_overview_and_short_ways(self):
        a, b, c = (50.0, 15.70), (50.0, 15.71), (50.0, 15.7001)
        local = routes.RouteWay([a, b], {"cycle"}, {"4123"})
        major = routes.RouteWay([a, b], {"cycle"}, {"2"}, True)
        self.assertEqual(routes.overview_marks(local), [])            # a local cycle route is not in the overview
        self.assertEqual(routes.overview_marks(major), ["cycle"])
        self.assertEqual(routes.overview_marks(routes.RouteWay([a, b], {"cycle", "blue", "yellow"}, {"2"}, True)), ["blue"])
        self.assertEqual(routes.overview_marks(routes.RouteWay([a, b], {"learning"})), [])
        self.assertEqual({level for _c, _l, _p, level in routes.spread([local])}, {0, 1, 2})
        self.assertEqual({level for _c, _l, _p, level in routes.spread([major])}, {0, 1, 2, 3})
        self.assertTrue(routes.major_cycle({"network": "ncn", "ref": "2"}))
        self.assertTrue(routes.major_cycle({"network": "rcn", "ref": "312"}))
        self.assertFalse(routes.major_cycle({"network": "rcn", "ref": "4123"}))    # a four-digit one is local
        self.assertFalse(routes.major_cycle({"network": "lcn", "ref": "K1A"}))
        self.assertFalse(routes.major_cycle({"route": "mtb"}))
        # the cycle route network comes from the relation and survives joining ways
        parsed = routes.merge_ways(routes.parse(self._data()))
        self.assertTrue(next(w for w in parsed if "cycle" in w.marks).cycle_major)
        # a piece shorter than two spacings is not spread: marks stay on top of each other
        short = routes.spread([routes.RouteWay([a, c], {"red", "blue"})])   # 7 m
        self.assertEqual({tuple(points) for _c, _l, points, level in short if level == 0}, {(a, c)})

    def test_merge_and_simplify(self):
        a, b, c, d, e = (50.0, 15.70), (50.0, 15.71), (50.0, 15.72), (50.0, 15.73), (50.01, 15.72)
        red = lambda *pts: routes.RouteWay(list(pts), {"red"})
        # three connecting ways, the middle one drawn the other way round, and a branch at point c
        merged = routes.merge_ways([red(a, b), red(c, b), red(c, d), red(c, e),
                                    routes.RouteWay([d, e], {"blue"})])
        reds = sorted((w.geometry for w in merged if w.marks == {"red"}), key=len, reverse=True)
        self.assertEqual(reds[0] if reds[0][0] == a else reds[0][::-1], [a, b, c])   # joined up to the junction
        self.assertEqual(len(reds), 3)                                               # separately after the junction
        self.assertEqual([w.geometry for w in merged if w.marks == {"blue"}], [[d, e]])
        # a loop is joined whole and does not cycle forever
        loop = routes.merge_ways([red(a, b), red(b, e), red(e, a)])
        self.assertEqual(len(loop), 1)
        self.assertEqual(len(loop[0].geometry), 4)

        wobble = [(50.0, 15.70), (50.00002, 15.705), (50.0, 15.71), (50.01, 15.71)]
        self.assertEqual(routes.simplify(wobble, 5.0), [wobble[0], wobble[2], wobble[3]])   # a 2 m wobble disappears
        self.assertEqual(routes.simplify(wobble, 1.0), wobble)
        self.assertEqual(routes.simplify(wobble[:2], 5.0), wobble[:2])

    def test_fetch_retries_other_servers(self):
        calls, notes = [], []

        class Reply:
            def __init__(self, text): self.text = text
            def read(self): return self.text.encode()
            def __enter__(self): return self
            def __exit__(self, *_): return False

        def opener(request, timeout):
            calls.append(request.full_url)
            self.assertTrue(request.get_header("User-agent").startswith("zeppmap/"))
            self.assertIn(b"route", request.data)
            if "prvni" in request.full_url:
                raise OSError("the server is overloaded")
            return Reply('{"elements": [{"type": "relation"}]}')

        data = routes.fetch((49.95, 15.64, 50.29, 16.0), notes.append, ("https://prvni.example/api", "https://druhy.example/api"),
                            opener=opener, sleep=lambda _s: None)
        self.assertEqual(len(data["elements"]), 1)
        self.assertEqual(calls, ["https://prvni.example/api", "https://druhy.example/api"])
        self.assertIn("49.95000,15.64000,50.29000,16.00000", routes.overpass_query((49.95, 15.64, 50.29, 16.0)))

        waits = []
        with self.assertRaises(routes.RoutesError):
            routes.fetch((49.95, 15.64, 50.29, 16.0), notes.append, ("https://prvni.example/api",),
                         rounds=3, opener=opener, sleep=waits.append)
        self.assertEqual(waits, [10.0, 20.0])                 # the wait between rounds grows
        self.assertEqual(routes.area_of([(1113, 694), (1114, 692)])[1], swatch.tile_bounds(1113, 694)[3])

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_build_makes_ski_tiles(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        notes = []
        ways = routes.parse(self._data())
        self.assertEqual(routes.build(ways, [(1113, 694), (1114, 692)], out, notes.append), 1)
        self.assertIn("no marked routes", "".join(notes))
        with open(os.path.join(out, "11", "1113", "694.img"), "rb") as handle:
            data = handle.read()
        info = imginfo.read_tile(data)
        self.assertEqual(info.kind, "ski")
        self.assertEqual(sorted(info.lines), sorted(routes.MARK_TYPES[m] for m in ("red", "blue", "yellow", "cycle")))
        units = (1 << 24) / 360.0
        self.assertEqual([round(v * units) for v in info.bounds], [round(v * units) for v in swatch.tile_bounds(1113, 694)])
        labels = {e.label for e in imginfo.iter_elements(data) if e.kind == "line" and e.type_byte & 0x7F == 0x54}
        self.assertEqual(labels, {"2, 4123"})
        self.assertEqual({e.level for e in imginfo.iter_elements(data) if e.kind == "line"}, {0, 1, 2, 3})
        with self.assertRaises(routes.RoutesError):
            routes.build(ways, [(1114, 692)], out, notes.append)


class LocalFtp:
    """A local FTP server over a temporary folder."""

    def __init__(self, root: str, username: str = "watch") -> None:
        authorizer = DummyAuthorizer()
        authorizer.add_user(username, "", root, perm="elradfmw")
        handler = type("Handler", (FTPHandler,), {"authorizer": authorizer})
        # Our own handler, otherwise pyftpdlib logs every FTP command.
        ftp_log = logging.getLogger("pyftpdlib")
        if not ftp_log.handlers:
            ftp_log.addHandler(logging.NullHandler())
        ftp_log.setLevel(logging.CRITICAL)
        self.server = FTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.socket.getsockname()[1]
        self._stopping = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        # The server may be closed only from the thread serving it.
        while not self._stopping.is_set():
            self.server.ioloop.loop(timeout=0.05, blocking=False)
        self.server.close_all()

    def stop(self) -> None:
        self._stopping.set()
        self.thread.join(timeout=5)


def make_tiles_zip(path: str, tiles: dict) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in tiles.items():
            archive.writestr(name, data)


@unittest.skipIf(FTPServer is None, "pyftpdlib is not installed")
class FtpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tiles = {"11/1105/694.img": os.urandom(5000), "11/1105/695.img": os.urandom(7000), "11/1106/694.img": b"x" * 10}
        self.zip = os.path.join(self.tmp, "map.zip")
        make_tiles_zip(self.zip, dict(self.tiles, **{"README.txt": b"ignorovat", "11/9999/1.img": b"mimo rozsah"}))

    def _serve(self, root):
        server = LocalFtp(root)
        self.addCleanup(server.stop)
        client = ftpops.WatchFtp("127.0.0.1", server.port, "watch")
        self.addCleanup(client.close)
        return client

    def test_push_backup_pull_with_absolute_paths(self):
        # A server that sees the whole file system: paths start with /mnt/data.
        root = os.path.join(self.tmp, "fs")
        data = os.path.join(root, "mnt", "data")
        os.makedirs(os.path.join(data, "map", "res", "scl", "11", "1105"))
        original = os.urandom(3000)
        with open(os.path.join(data, "map", "res", "scl", "11", "1105", "694.img"), "wb") as handle:
            handle.write(original)

        client = self._serve(root)
        self.assertEqual(client.base, "/mnt/data")
        backup = os.path.join(self.tmp, "backup")
        lines = []
        self.assertEqual(client.push_tiles(self.zip, "full", backup, lines.append), (3, 12010))
        for name, content in self.tiles.items():
            with open(os.path.join(data, "map", "res", "scl", name), "rb") as handle:
                self.assertEqual(handle.read(), content)
        with open(os.path.join(backup, "11", "1105", "694.img"), "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(sorted(os.listdir(os.path.join(backup, "11"))), ["1105"])  # backs up only what was overwritten
        self.assertIn("backed up", lines[0])

        self.assertEqual([(e.name, e.is_dir) for e in client.listdir("map/res/scl/11")], [("1105", True), ("1106", True)])
        self.assertEqual(
            [(e.name, e.size) for e in client.listdir("map/res/scl/11/1105")],
            [("694.img", 5000), ("695.img", 7000)],
        )
        os.makedirs(os.path.join(data, "map", "config"))
        with open(os.path.join(data, "map", "config", "index.json"), "wb") as handle:
            handle.write(b"{}")
        tree = []
        self.assertEqual(client.describe_tree("map", tree.append, list_limit=1), (4, 12012))
        self.assertEqual(tree, [
            "map/  (files: 0, 0 B)",
            "map/config/  (files: 1, 2 B)",
            "    index.json                           2 B",
            "map/res/  (files: 0, 0 B)",
            "map/res/scl/  (files: 0, 0 B)",
            "map/res/scl/11/  (files: 0, 0 B)",
            "map/res/scl/11/1105/  (files: 2, 12.0 kB)",
            "    e.g. 694.img, 695.img",
            "map/res/scl/11/1106/  (files: 1, 10 B)",
            "    694.img                              10 B",
        ])

        target = os.path.join(self.tmp, "pulled")
        self.assertEqual(client.pull("map/res/scl/11", target, lambda _line: None), (3, 12010))
        with open(os.path.join(target, "1106", "694.img"), "rb") as handle:
            self.assertEqual(handle.read(), b"x" * 10)
        single = os.path.join(self.tmp, "one.img")
        self.assertEqual(client.pull("map/res/scl/11/1105/695.img", single, lambda _line: None), (1, 7000))

    def test_push_with_root_relative_paths_creates_folders(self):
        # A server rooted directly in the data partition; the map folder does not exist yet.
        root = os.path.join(self.tmp, "data")
        os.makedirs(root)
        client = self._serve(root)
        self.assertEqual(client.base, "")
        source = os.path.join(self.tmp, "unzipped")
        with zipfile.ZipFile(self.zip) as archive:
            archive.extractall(source)
        self.assertEqual(client.push_tiles(source, "size", None, lambda _line: None), (3, 12010))
        self.assertTrue(os.path.isfile(os.path.join(root, "map", "res", "scl", "11", "1106", "694.img")))
        with self.assertRaises(ftpops.FtpError):
            client.listdir("neexistuje")

    def test_corrupted_upload_is_detected(self):
        root = os.path.join(self.tmp, "data")
        os.makedirs(root)
        client = self._serve(root)
        real_write = client.write
        calls = []

        def flaky(path, data):
            calls.append(path)
            real_write(path, data[:-1] if len(calls) == 1 else data)

        client.write = flaky
        client.push_tiles(self.zip, "full", None, lambda _line: None)
        self.assertEqual(len(calls), 4)  # the first tile at the second attempt

        client.write = lambda path, data: real_write(path, data[:-1])
        for mode in ("full", "size"):
            with self.assertRaises(ftpops.FtpError):
                client.push_tiles(self.zip, mode, None, lambda _line: None)

    def test_interrupted_transfer_is_retried_and_resumed(self):
        root = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(root, "map", "res"))
        contents = {"a.img": os.urandom(3000), "b.img": os.urandom(5000)}
        for name, data in contents.items():
            with open(os.path.join(root, "map", "res", name), "wb") as handle:
                handle.write(data)
        client = self._serve(root)
        client.retry_delay = 0
        real = client._ftp.retrbinary
        failures = {"left": 2}

        def flaky(command, callback, *args, **kwargs):
            if command.startswith("RETR") and command.endswith("b.img") and failures["left"]:
                failures["left"] -= 1
                if failures["left"]:
                    # the connection drops in the middle, as with the watch
                    raise ftplib.error_temp("451 Requested action aborted after 821248 bytes")
                return real(command, lambda chunk: callback(chunk[:10]), *args, **kwargs)  # incomplete data
            return real(command, callback, *args, **kwargs)

        client._ftp.retrbinary = flaky
        target = os.path.join(self.tmp, "pulled")
        lines = []
        self.assertEqual(client.pull("map/res", target, lines.append), (2, 8000))
        for name, data in contents.items():
            with open(os.path.join(target, name), "rb") as handle:
                self.assertEqual(handle.read(), data)

        # the second run downloads nothing
        lines = []
        self.assertEqual(client.pull("map/res", target, lines.append), (0, 0))
        self.assertTrue(all("already downloaded" in line for line in lines), lines)

        # a lasting outage ends with a clear error and saves nothing incomplete
        os.remove(os.path.join(target, "b.img"))
        failures["left"] = 99
        with self.assertRaises(ftpops.FtpError) as caught:
            client.pull("map/res", target, lambda _line: None)
        self.assertIn("even after 3 attempts", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(target, "b.img")))

        # a missing file is not retried and is reported differently
        with self.assertRaises(ftpops.FtpNotFound):
            client.read("map/res/neni.img")

    def test_backup_failure_stops_before_overwriting(self):
        """When the original tile cannot be backed up because of an outage, it must not be overwritten."""
        root = os.path.join(self.tmp, "data")
        tile_dir = os.path.join(root, "map", "res", "scl", "11", "1105")
        os.makedirs(tile_dir)
        original = os.urandom(2000)
        with open(os.path.join(tile_dir, "694.img"), "wb") as handle:
            handle.write(original)
        client = self._serve(root)
        client.retry_delay = 0

        def broken(command, callback, *args, **kwargs):
            raise ftplib.error_temp("451 Requested action aborted")

        client._ftp.retrbinary = broken
        with self.assertRaises(ftpops.FtpError):
            client.push_tiles(self.zip, "none", os.path.join(self.tmp, "backup"), lambda _line: None)
        with open(os.path.join(tile_dir, "694.img"), "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_server_that_does_not_truncate(self):
        """The watch server does not truncate a file it overwrites; a smaller tile over a larger one."""
        root = os.path.join(self.tmp, "data")
        tile_dir = os.path.join(root, "map", "res", "scl", "11", "1105")
        os.makedirs(tile_dir)
        tile = os.path.join(tile_dir, "694.img")
        original = os.urandom(9000)  # larger than the new tile (5000 B)
        with open(tile, "wb") as handle:
            handle.write(original)
        client = self._serve(root)
        client.retry_delay = 0
        real_write = client.write

        def write_without_truncating(path, data):
            local = os.path.join(root, path)
            tail = b""
            if os.path.exists(local):
                with open(local, "rb") as handle:
                    tail = handle.read()[len(data):]
            real_write(path, data)
            with open(local, "ab") as handle:
                handle.write(tail)

        client.write = write_without_truncating

        # with a delete before writing the tile uploads cleanly
        backup = os.path.join(self.tmp, "backup")
        client.push_tiles(self.zip, "full", backup, lambda _line: None)
        with open(tile, "rb") as handle:
            self.assertEqual(handle.read(), self.tiles["11/1105/694.img"])

        # when the server cannot delete, the error says what is wrong and the original tile is put back
        with open(tile, "wb") as handle:
            handle.write(original)

        def no_delete(_path):
            raise ftplib.error_perm("500 Syntax error, command unrecognized.")

        client._ftp.delete = no_delete
        with self.assertRaises(ftpops.FtpError) as caught:
            client.push_tiles(self.zip, "full", os.path.join(self.tmp, "backup2"), lambda _line: None)
        message = str(caught.exception)
        self.assertIn("the watch has 9000 B instead of 5000 B, the common part matches", message)
        self.assertIn("original tile was put back", message)
        with open(tile, "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_failed_new_tile_is_removed(self):
        """A new tile that does not upload correctly does not stay in the watch."""
        root = os.path.join(self.tmp, "data")
        os.makedirs(root)
        client = self._serve(root)
        client.retry_delay = 0
        real_write = client.write
        client.write = lambda path, data: real_write(path, data[:-3])
        with self.assertRaises(ftpops.FtpError) as caught:
            client.push_tiles(self.zip, "full", os.path.join(self.tmp, "backup"), lambda _line: None)
        self.assertIn("was removed", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(root, "map", "res", "scl", "11", "1105", "694.img")))

    def test_new_tile_when_server_reports_missing_file_as_outage(self):
        """The watch server answers 451 for a missing file, as for a broken transfer."""
        root = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(root, "map", "res", "ski"))
        client = self._serve(root)
        client.retry_delay = 0
        real_retr, real_delete = client._ftp.retrbinary, client._ftp.delete

        def local(path):
            return os.path.join(root, path.lstrip("/"))

        def retr(command, callback, *args, **kwargs):
            if not os.path.exists(local(command[len("RETR "):])):
                raise ftplib.error_temp("451 Requested action aborted after 0 bytes")
            return real_retr(command, callback, *args, **kwargs)

        def delete(path):
            if not os.path.exists(local(path)):
                raise ftplib.error_temp("451 Requested action aborted")
            return real_delete(path)

        client._ftp.retrbinary, client._ftp.delete = retr, delete
        backup = os.path.join(self.tmp, "backup")
        lines = []
        self.assertEqual(client.push_tiles(self.zip, "full", backup, lines.append, "ski"), (3, 12010))
        self.assertFalse(os.path.exists(backup))              # there was nothing to back up
        self.assertNotIn("backed up", "".join(lines))
        with open(os.path.join(root, "map", "res", "ski", "11", "1105", "695.img"), "rb") as handle:
            self.assertEqual(handle.read(), self.tiles["11/1105/695.img"])
        self.assertTrue(client.exists("map/res/ski/11/1105/695.img"))
        self.assertFalse(client.exists("map/res/ski/11/1105/000.img"))
        self.assertFalse(client.exists("map/res/ski/11/9999/000.img"))
        self.assertFalse(client.exists("map/res/ski/11/1105"))  # a folder is not a file

        # an empty file left in the watch by a failed read is not backed up and is overwritten
        leftover = os.path.join(root, "map", "res", "ski", "11", "1105", "694.img")
        open(leftover, "wb").close()
        os.remove(os.path.join(root, "map", "res", "ski", "11", "1105", "695.img"))
        os.remove(os.path.join(root, "map", "res", "ski", "11", "1106", "694.img"))
        real_checked = client._ftp.retrbinary

        def retr_empty(command, callback, *args, **kwargs):
            path = local(command[len("RETR "):])
            if os.path.exists(path) and os.path.getsize(path) == 0:
                raise ftplib.error_temp("451 Requested action aborted after 0 bytes")
            return real_checked(command, callback, *args, **kwargs)

        client._ftp.retrbinary = retr_empty
        self.assertEqual(client.stat("map/res/ski/11/1105/694.img").size, 0)
        lines = []
        self.assertEqual(client.push_tiles(self.zip, "full", backup, lines.append, "ski"), (3, 12010))
        self.assertFalse(os.path.exists(backup))
        with open(leftover, "rb") as handle:
            self.assertEqual(handle.read(), self.tiles["11/1105/694.img"])

        # the next upload backs up what is there
        lines = []
        client.push_tiles(self.zip, "full", backup, lines.append, "ski")
        self.assertIn("backed up", lines[0])
        with open(os.path.join(backup, "11", "1105", "695.img"), "rb") as handle:
            self.assertEqual(handle.read(), self.tiles["11/1105/695.img"])

    def test_difference_description(self):
        self.assertEqual(ftpops.describe_difference(b"abcd", b"abcdEF"),
                         "the watch has 6 B instead of 4 B, the common part matches")
        self.assertEqual(ftpops.describe_difference(b"abcd", b"abXdEF"),
                         "the watch has 6 B instead of 4 B, content differs from byte 2")
        self.assertEqual(ftpops.describe_difference(b"abcd", b"ab"),
                         "the watch has 2 B instead of 4 B, the common part matches")
        self.assertEqual(ftpops.describe_difference(b"abcd", b"abcX"), "size matches, content differs from byte 3")

    def test_list_parsing(self):
        listing = [
            "total 8",
            "drwxr-xr-x    2 root     root          4096 Jan  1  1970 config",
            "drwxr-xr-x    2 root     root          4096 Jan  1  1970 .",
            "-rw-r--r--    1 root     root           145 Nov 17 10:02 map_config.json",
            "",
        ]
        self.assertEqual(
            [(e.name, e.is_dir, e.size) for e in ftpops.parse_list(listing)],
            [("config", True, 4096), ("map_config.json", False, 145)],
        )
        # an empty folder: nothing, or just a "total 0" line
        self.assertEqual(ftpops.parse_list([]), [])
        self.assertEqual(ftpops.parse_list(["total 0"]), [])
        self.assertEqual(ftpops.parse_list(["Total 0", "  "]), [])
        # the real answer of a Balance 3 Ti for the empty map/download folder
        self.assertEqual(
            ftpops.parse_list(["total 0", "-rw-rw-r--   1 freertos plusfat        0  Jan 01 1970 "]), []
        )
        # and for a folder with tiles (format from the same server)
        self.assertEqual(
            [(e.name, e.is_dir, e.size) for e in ftpops.parse_list([
                "total 2",
                "drwxrwxr-x   1 freertos plusfat        0  Nov 17 2026 1113",
                "-rw-rw-r--   1 freertos plusfat   546304  Nov 17 2026 692.img",
            ])],
            [("1113", True, 0), ("692.img", False, 546304)],
        )
        # a format other than Unix is not recognised
        self.assertIsNone(ftpops.parse_list(["11-17-26  10:02AM       <DIR>          config"]))

    def test_server_without_nlst_and_with_total_line(self):
        """Behaviour of the watch FTP server: LIST starting with "total N", no MLSD and NLST."""
        root = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(root, "map", "download"))
        os.makedirs(os.path.join(root, "map", "config"))
        with open(os.path.join(root, "map", "config", "a.txt"), "wb") as handle:
            handle.write(b"1234")
        client = self._serve(root)
        real_retrlines = client._ftp.retrlines

        def mlsd(*_args, **_kwargs):
            raise ftplib.error_perm("500 Syntax error, command unrecognized.")

        def nlst(*_args, **_kwargs):
            raise ftplib.error_perm("500 Syntax error, command unrecognized.")

        def retrlines(command, callback):
            callback("total 0")
            return real_retrlines(command, callback)

        client._ftp.mlsd = mlsd
        client._ftp.nlst = nlst
        client._ftp.retrlines = retrlines

        self.assertEqual(client.listdir("map/download"), [])
        tree = []
        self.assertEqual(client.describe_tree("map", tree.append), (1, 4))
        self.assertEqual(tree, [
            "map/  (files: 0, 0 B)",
            "map/config/  (files: 1, 4 B)",
            "    a.txt                                4 B",
            "map/download/  (files: 0, 0 B)",
        ])

        # an unreadable listing of one folder does not stop the walk and the error says what the server returned
        def odd(command, callback):
            callback("???")

        client._ftp.retrlines = odd
        tree = []
        self.assertEqual(client.describe_tree("map", tree.append), (0, 0))
        self.assertEqual(len(tree), 1)
        self.assertIn("unknown format", tree[0])
        self.assertIn("???", tree[0])

    def test_push_to_contour_layer(self):
        root = os.path.join(self.tmp, "data")
        os.makedirs(root)
        client = self._serve(root)
        client.push_tiles(self.zip, "full", None, lambda _line: None, "ctl")
        self.assertTrue(os.path.isfile(os.path.join(root, "map", "res", "ctl", "11", "1105", "694.img")))
        self.assertFalse(os.path.exists(os.path.join(root, "map", "res", "scl")))
        # another layer is uploaded to the folder of its name; a nonsense name does not pass
        client.push_tiles(self.zip, "full", None, lambda _line: None, "ski")
        self.assertTrue(os.path.isfile(os.path.join(root, "map", "res", "ski", "11", "1106", "694.img")))
        for bad in ("../x", "Ski", "a/b", ""):
            with self.assertRaises(ValueError):
                client.push_tiles(self.zip, "full", None, lambda _line: None, bad)

        # a test layer can be removed whole again
        lines = []
        self.assertEqual(client.remove_tree("map/res/ski", lines.append), 3)
        self.assertFalse(os.path.exists(os.path.join(root, "map", "res", "ski")))
        self.assertTrue(os.path.isdir(os.path.join(root, "map", "res", "ctl")))
        with self.assertRaises(ftpops.FtpNotFound):
            client.remove_tree("map/res/ski/11/1105/694.img", lines.append)

    def test_source_without_tiles(self):
        empty = os.path.join(self.tmp, "empty.zip")
        make_tiles_zip(empty, {"notes.txt": b"nic"})
        self.assertEqual(list(ftpops.iter_tiles(empty)), [])
        with self.assertRaises(ftpops.FtpError):
            list(ftpops.iter_tiles(os.path.join(self.tmp, "missing.zip")))


def _route_data(*extra_ways):
    """An Overpass reply in the new shape: relations referencing ways, geometry with the ways."""
    a, b, c = RoutesTest.A, RoutesTest.B, RoutesTest.C
    shape = lambda ref, *points: {"type": "way", "id": ref, "geometry": [{"lat": p[0], "lon": p[1]} for p in points]}
    member = lambda ref: {"type": "way", "ref": ref, "role": ""}
    return {"elements": [
        {"type": "relation", "id": 1, "tags": {"route": "hiking", "osmc:symbol": "red:white:red_bar"},
         "members": [member(10), member(11), {"type": "node", "ref": 5, "role": "guidepost"}, member(99)]},
        {"type": "relation", "id": 2, "tags": {"route": "bicycle", "ref": "2", "network": "ncn"},
         "members": [member(10)] + [member(way["id"]) for way in extra_ways]},
        shape(10, a, b), shape(11, b, c),
    ] + list(extra_ways)}


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_parse_reads_geometry_from_ways(self):
        ways = routes.parse(_route_data())
        self.assertEqual(len(ways), 2)                                # way 99 has no geometry and drops out
        shared = next(w for w in ways if "cycle" in w.marks)
        self.assertEqual(shared.geometry, [RoutesTest.A, RoutesTest.B])
        self.assertEqual(shared.marks, {"red", "cycle"})
        self.assertTrue(shared.cycle_major)
        query = routes.overpass_query((49.95, 15.64, 50.29, 16.0))
        self.assertIn("way(r.r)(49.95000,15.64000,50.29000,16.00000);", query)
        self.assertIn("out ids geom;", query)

    def test_ref_labels_do_not_depend_on_order(self):
        # alpine trails 6 and 06 are two different trails with equal numbers
        for refs in (["6", "06", "277"], ["06", "277", "6"], {"277", "6", "06"}):
            self.assertEqual(routes._refs_label(refs), "06, 6, 277")
        self.assertEqual(routes._refs_label(["K27", "4301", "19A", "2"]), "2, 4301, 19A, K27")

    def test_lifts_are_drawn_with_plain_names(self):
        a, b, c = RoutesTest.A, RoutesTest.B, RoutesTest.C
        self.assertEqual(routes.plain_label("Pec pod Sněžkou ⇔ Růžová hora"), "Pec pod Snezkou - Ruzova hora")
        self.assertEqual(routes.plain_label("Wyciąg „Zbyszek” Łomnica"), 'Wyciag "Zbyszek" Lomnica')
        self.assertEqual(len(routes.plain_label("x" * 100)), 40)
        self.assertEqual(routes.plain_label(""), "")

        def lift(ref, kind, name, *points):
            return {"type": "way", "id": ref, "tags": {"aerialway": kind, "name": name},
                    "geometry": [{"lat": p[0], "lon": p[1]} for p in points]}

        data = {"elements": [
            lift(20, "gondola", "Růžová hora ⇔ Sněžka", a, c),
            lift(21, "chair_lift", "Černá hora", b, c),
            lift(22, "platter", "Kotva", a, b),                      # a drag lift is not drawn
            lift(23, "station", "Horní stanice", a, b),
            {"type": "way", "id": 24, "tags": {"aerialway": "gondola"}},   # without geometry
            {"type": "way", "id": 25, "geometry": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}]},
        ]}
        lifts = routes.parse_lifts(data)
        self.assertEqual(sorted(l.name for l in lifts), ["Cerna hora", "Ruzova hora - Snezka"])
        self.assertEqual(routes.parse_lifts({"elements": []}), [])    # older data has no cable cars
        self.assertEqual(routes.parse(data), [])                      # a cable car is not a marked route
        lines = routes.lift_lines(lifts)
        self.assertEqual({code for code, _l, _p, _lvl in lines}, {0x64})
        self.assertEqual(sorted({level for _c, _l, _p, level in lines}), [0, 1, 2, 3])
        source = routes.routes_mp(1113, 694, routes.clip_lines(lines, 1113, 694))
        self.assertIn("Type=0x24\nLabel=Ruzova hora - Snezka\nDirIndicator=1\nEndLevel=0\n", source)
        self.assertIn('way["aerialway"~"^(cable_car|gondola|chair_lift|mixed_lift)$"](49.95000,',
                      routes.overpass_query((49.95, 15.64, 50.29, 16.0)))
        self.assertIn("0x64", routes.legend())

        # a tile with only a cable car is built too
        made = []
        present, rebuilt = sync.build([], [(1113, 694), (1114, 692)], os.path.join(self.tmp, "z"), lambda _t: None,
                                      lambda source, name: made.append(source) or b"tile", lifts)
        self.assertEqual((present, rebuilt), ([(1113, 694)], 1))
        self.assertIn("Label=Cerna hora", made[0])

    def test_cache_from_older_query_and_tagged_ways(self):
        cache = os.path.join(self.tmp, "cache")
        tiles = [(1113, 694), (1117, 694)]
        plain = {"type": "way", "id": 30, "geometry": [{"lat": 50.0, "lon": 15.70}, {"lat": 50.0, "lon": 15.71}]}
        tagged = dict(plain, tags={"aerialway": "gondola", "name": "Lanovka"})
        replies = [{"elements": [plain]}, {"elements": [tagged]}]
        data = sync.load_routes(tiles, cache, report=lambda _t: None, fetch=lambda area, report: replies.pop(0),
                                pause=lambda _s: None)
        # the same way came once bare and once with tags: the one with tags stays
        self.assertEqual([el.get("tags", {}).get("name") for el in data["elements"]], ["Lanovka"])
        self.assertEqual(sync.stale_blocks(tiles, cache), [])
        # data downloaded by an older query counts as incomplete
        path = os.path.join(cache, sync.cache_name([(1113, 694)]))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"elements": []}, handle)
        self.assertEqual(sync.stale_blocks(tiles, cache), [[(1113, 694)]])

    def test_builds_block_by_block_with_neighbours(self):
        # The route crosses from block to block (x 1115 | 1116). Way 51 lies entirely in the
        # eastern block but belongs to the same route as way 50, which crosses the border.
        tiles = [(1115, 694), (1116, 694)]
        north, edge, south, _west = swatch.tile_bounds(1115, 694)
        lat = (north + south) / 2

        def shape(ref, *points):
            return {"type": "way", "id": ref, "geometry": [{"lat": p[0], "lon": p[1]} for p in points]}

        relations = [
            {"type": "relation", "id": 1, "tags": {"route": "hiking", "osmc:symbol": "red:white:red_bar"},
             "members": [{"type": "way", "ref": 50, "role": ""}, {"type": "way", "ref": 51, "role": ""}]},
            {"type": "relation", "id": 2, "tags": {"route": "bicycle", "ref": "2", "network": "ncn"},
             "members": [{"type": "way", "ref": 50, "role": ""}, {"type": "way", "ref": 51, "role": ""}]},
        ]
        crossing = shape(50, (lat, edge - 0.02), (lat, edge + 0.01))
        beyond = shape(51, (lat, edge + 0.01), (lat + 0.02, edge + 0.05))
        gondola = dict(shape(60, (lat - 0.01, edge - 0.03), (lat - 0.01, edge + 0.03)),
                       tags={"aerialway": "gondola", "name": "Přes hranici"})

        def fetch(area, report):
            west_block = area[3] <= edge + 1e-9
            return {"elements": relations + [crossing, gondola] + ([] if west_block else [beyond])}

        cache, out = os.path.join(self.tmp, "cache"), os.path.join(self.tmp, "build-ski")
        notes, sources = [], {}
        self.assertEqual(sync.download(tiles, cache, report=notes.append, fetch=fetch, pause=lambda _s: None), 2)
        self.assertIn("block 2 of 2", notes[-1])
        self.assertEqual(sync.download(tiles, cache, report=notes.append, fetch=fetch), 0)   # nothing the second time

        def make(source, name):
            sources[name] = source
            return ("tile " + name).encode()

        present, rebuilt = sync.build_from_cache(tiles, cache, out, notes.append, make, workers=2)
        self.assertEqual((present, rebuilt), (tiles, 2))
        self.assertIn("Block 2 of 2", notes[-1])
        self.assertEqual(sync.build_from_cache(tiles, cache, out, notes.append, make)[1], 0)   # unchanged

        def red_ends(name):
            """End points of the red line in level 0: (westernmost, easternmost)."""
            found = re.findall(r"Type=0x03\nDirIndicator=1\nEndLevel=0\nData0=(.*)", sources[name])
            points = [tuple(float(v) for v in pair.split(",")) for line in found
                      for pair in re.findall(r"\(([^)]*)\)", line)]
            return min(points, key=lambda p: p[1]), max(points, key=lambda p: p[1])

        west_end = red_ends("11150694")[1]
        east_start = red_ends("11160694")[0]
        # red is offset sideways because of the cycle route; at the block border it must continue at the same latitude
        self.assertNotAlmostEqual(west_end[0], lat, places=6)
        self.assertAlmostEqual(west_end[0], east_start[0], places=6)
        self.assertAlmostEqual(west_end[1], east_start[1], places=3)
        self.assertIn("Label=Pres hranici", sources["11150694"])
        self.assertIn("Label=Pres hranici", sources["11160694"])

        # upload record: knowing the tile sizes in the watch is enough
        tile = (1115, 694)
        size = len(b"tile 11150694")
        record = sync.UploadRecord(out)
        self.assertEqual(record.changed(out, [tile], {tile: size}), [tile])      # nothing was uploaded
        record.note(sync.tile_path(tile), b"tile 11150694")
        record.save()
        again = sync.UploadRecord(out)
        self.assertEqual(again.changed(out, [tile], {tile: size}), [])
        self.assertEqual(again.changed(out, [tile], {tile: size + 512}), [tile])  # the watch has something else
        self.assertEqual(again.changed(out, [tile], {}), [tile])                  # missing in the watch
        with open(os.path.join(out, "11", "1115", "694.img"), "wb") as handle:
            handle.write(b"tile 1115069X")                                    # the local tile changed
        self.assertEqual(again.changed(out, [tile], {tile: size}), [tile])

        # the tile list can be saved and loaded, from a folder and from a file
        self.assertEqual(sync.tiles_from(sync.save_tiles(cache, tiles + tiles)), tiles)
        self.assertEqual(sync.tiles_from(out), tiles)
        with self.assertRaises(sync.SyncError):
            sync.tiles_from(os.path.join(self.tmp, "neni.json"))
        # a missing downloaded block is a clear error
        os.remove(os.path.join(cache, sync.cache_name([(1116, 694)])))
        with self.assertRaises(sync.SyncError):
            sync.build_from_cache(tiles, cache, out, notes.append, make)

    def test_blocks_and_cache(self):
        tiles = [(1113, 694), (1113, 695), (1117, 694), (1113, 694)]
        self.assertEqual(sync.blocks(tiles), [[(1113, 694), (1113, 695)], [(1117, 694)]])
        self.assertEqual(sync.cache_name([(1113, 694), (1113, 695)]), "routes-x1113-1113-y694-695.json")
        self.assertEqual(sync.tile_path((1113, 694)), "11/1113/694.img")

        cache = os.path.join(self.tmp, "cache")
        calls, notes, pauses = [], [], []

        def fetch(area, report):
            calls.append(area)
            return _route_data()

        load = lambda **kw: sync.load_routes(tiles, cache, report=notes.append, fetch=fetch, pause=pauses.append, **kw)
        self.assertEqual(len(sync.stale_blocks(tiles, cache)), 2)
        data = load()
        self.assertEqual(len(calls), 2)                               # each block once
        self.assertEqual(calls[0], routes.area_of([(1113, 694), (1113, 695)]))
        self.assertEqual(pauses, [2.0])                               # mezi dotazy pauza
        self.assertEqual(len(data["elements"]), 4)                    # the same routes from both blocks only once
        self.assertEqual(sorted(os.listdir(cache)), ["routes-x1113-1113-y694-695.json", "routes-x1117-1117-y694-694.json"])
        self.assertEqual(sync.stale_blocks(tiles, cache), [])

        load()
        self.assertEqual(len(calls), 2)                               # from the cache the second time
        self.assertIn("from the cache", notes[-1])
        later = __import__("time").time() + 31 * 86400
        self.assertEqual(len(sync.stale_blocks(tiles, cache, now=later)), 2)
        self.assertEqual(len(sync.stale_blocks(tiles, cache, max_age_days=60, now=later)), 0)
        load(now=later)
        self.assertEqual(len(calls), 4)                               # old data is downloaded again
        load(refresh=True)
        self.assertEqual(len(calls), 6)
        # a damaged cache file is not used
        with open(os.path.join(cache, "routes-x1117-1117-y694-694.json"), "w") as handle:
            handle.write("{incomplete")
        self.assertEqual(sync.stale_blocks(tiles, cache), [[(1117, 694)]])

    def test_build_reuses_unchanged_tiles(self):
        out = os.path.join(self.tmp, "build-ski")
        made, notes = [], []

        def make(source, name):
            made.append(name)
            return ("tile %s %d" % (name, len(source))).encode()

        tiles = [(1113, 694), (1114, 692)]
        build = lambda data: sync.build(routes.parse(data), tiles, out, notes.append, make)
        self.assertEqual(build(_route_data()), ([(1113, 694)], 1))    # no route in 1114/692
        target = os.path.join(out, "11", "1113", "694.img")
        with open(target, "rb") as handle:
            first = handle.read()
        self.assertEqual(build(_route_data()), ([(1113, 694)], 0))    # not rebuilt when unchanged
        self.assertEqual(made, ["11130694"])

        extra = {"type": "way", "id": 12, "geometry": [{"lat": 50.0, "lon": 15.70}, {"lat": 49.99, "lon": 15.70}]}
        self.assertEqual(build(_route_data(extra)), ([(1113, 694)], 1))   # a new cycle route changes the tile
        with open(target, "rb") as handle:
            second = handle.read()
        self.assertNotEqual(first, second)
        os.remove(target)
        self.assertEqual(build(_route_data(extra))[1], 1)             # a missing file is filled in
        os.remove(os.path.join(out, sync.MANIFEST))
        self.assertEqual(build(_route_data(extra))[1], 1)             # built again without the manifest

        self.assertEqual(sync.changed_tiles(out, [(1113, 694)], {}), [(1113, 694)])
        self.assertEqual(sync.changed_tiles(out, [(1113, 694)], {(1113, 694): b"other"}), [(1113, 694)])
        with open(target, "rb") as handle:
            self.assertEqual(sync.changed_tiles(out, [(1113, 694)], {(1113, 694): handle.read()}), [])
        backup = os.path.join(self.tmp, "backup")
        self.assertEqual(sync.save_backups(backup, [(1113, 694), (1114, 692)], {(1113, 694): b"original"}), 1)
        with open(os.path.join(backup, "11", "1113", "694.img"), "rb") as handle:
            self.assertEqual(handle.read(), b"original")

    def test_reads_tiles_from_watch(self):
        root = os.path.join(self.tmp, "ftp")
        for layer, name, content in (("scl", "11/1113/694.img", b"m" * 30), ("scl", "11/1113/693.img", b"m" * 20),
                                     ("scl", "11/1114/692.img", b"m" * 10), ("scl", "11/1114/prazdna.txt", b"x"),
                                     ("scl", "11/1114/700.img", b""), ("ski", "11/1113/694.img", b"ski")):
            path = os.path.join(root, "map", "res", layer, *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(content)
        server = LocalFtp(root)
        self.addCleanup(server.stop)
        client = ftpops.WatchFtp("127.0.0.1", server.port, "watch")
        self.addCleanup(client.close)
        tiles = sync.watch_tiles(client)
        self.assertEqual(tiles, [(1113, 693), (1113, 694), (1114, 692)])     # not an empty file or a foreign name
        self.assertEqual(sync.read_tiles(client, tiles, "ski"), {(1113, 694): b"ski"})
        self.assertEqual(sync.watch_sizes(client, "ski"), {(1113, 694): 3})
        self.assertEqual(sync.watch_tiles(client, "ctl"), [])                 # a layer the watch does not have
        # only selected tiles from a folder can be uploaded
        source = os.path.join(root, "map", "res", "scl")
        count, size = client.push_tiles(source, "full", None, lambda _t: None, "pokus", only=["11/1113/693.img"])
        self.assertEqual((count, size), (1, 20))
        self.assertEqual(sync.watch_tiles(client, "pokus"), [(1113, 693)])


    @unittest.skipIf(FTPServer is None, "pyftpdlib is not installed")
    def test_fetches_original_contours(self):
        def img(kind, fill, size=600):
            first, second = imginfo.SIGNATURES[kind]
            data = bytearray(fill * size)
            data[0x10:0x16], data[0x41:0x47] = first, second
            return bytes(data)

        def put(folder, tile, content):
            target = os.path.join(folder, *sync.tile_path(tile).split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(content)

        a, b, c, d, e, f = [(1113, y) for y in range(690, 696)]
        root, backups = os.path.join(self.tmp, "ftp"), os.path.join(self.tmp, "backups")
        ctl = os.path.join(root, "map", "res", "ctl")
        put(ctl, a, img("garmin", b"a"))            # Zepp's original tile
        put(ctl, b, img("ski", b"b"))               # our tile, the original is in a backup
        put(ctl, c, img("ski", b"c"))               # our tile, the original is nowhere
        put(ctl, e, img("garmin", b"E"))            # the original is already saved, not read from the watch
        put(ctl, f, img("ski", b"f", 700))          # our tile according to the upload record
        put(os.path.join(backups, "20260101-000000-ctl"), b, img("ski", b"x"))     # a backup of our earlier tile
        put(os.path.join(backups, "20260102-000000-ctl"), b, img("garmin", b"B"))
        put(os.path.join(backups, "20260103-000000-ctl"), b, img("garmin", b"pozdejsi"))
        put(os.path.join(backups, "20260101-000000-ski"), c, img("garmin", b"ski"))   # a backup of another layer
        store = sync.ContourStore(os.path.join(self.tmp, "contours"))
        put(store.folder, e, img("garmin", b"old"))

        server = LocalFtp(root)
        self.addCleanup(server.stop)
        client = ftpops.WatchFtp("127.0.0.1", server.port, "watch")
        self.addCleanup(client.close)
        read = []

        class Counting:
            def read(self, remote, size=None):
                read.append(remote.rsplit("/", 1)[-1])
                return client.read(remote, size)

        sizes = sync.watch_sizes(client, "ctl")
        self.assertEqual(sorted(sizes), [a, b, c, e, f])
        notes = []
        result = sync.fetch_contours(Counting(), [a, b, c, d, e, f], store, sizes, {sync.tile_path(f): [700, "otisk"]},
                                     notes.append, None, backups)
        self.assertEqual(result, (1, 1, [c, f]))
        self.assertEqual(read, ["690.img", "691.img", "692.img"])           # d is not in the watch, e is saved, f is ours
        self.assertIn("for 4 tiles", notes[0])                              # a, b, c and f are missing on disk
        self.assertEqual(store.read(a), img("garmin", b"a"))
        self.assertEqual(store.read(b), img("garmin", b"B"))                # the oldest backup in the original format
        self.assertEqual(store.read(e), img("garmin", b"old"))            # a saved tile is not overwritten
        self.assertEqual(store.read_relative("11/1113/690.img"), store.read(a))
        self.assertFalse(store.has(c) or store.has(d) or store.has(f))
        # only the original format gets into the store
        self.assertFalse(store.add(c, img("ski", b"c")))
        self.assertFalse(store.add(c, b"not a tile"))
        self.assertFalse(store.has(c))
        # content the caller has already read is not read from the watch again
        del read[:]
        self.assertEqual(sync.fetch_contours(Counting(), [c], store, sizes, {}, notes.append,
                                             {c: img("garmin", b"C")}, backups), (1, 0, []))
        self.assertEqual(read, [])
        self.assertEqual(sync.fetch_contours(Counting(), [a, b, c, e], store, sizes, {}, notes.append, None, backups),
                         (0, 0, []))                                        # nothing left to add the second time

    @unittest.skipIf(FTPServer is None, "pyftpdlib is not installed")
    def test_push_with_known_originals(self):
        root, source = os.path.join(self.tmp, "ftp"), os.path.join(self.tmp, "nove")
        backup = os.path.join(self.tmp, "backup")
        files = {os.path.join(root, "map", "res", "ctl", "11", "1113", "694.img"): b"in watch 694",
                 os.path.join(root, "map", "res", "ctl", "11", "1113", "695.img"): b"in watch 695",
                 os.path.join(source, "11", "1113", "694.img"): b"new 694",
                 os.path.join(source, "11", "1113", "695.img"): b"new 695"}
        for target, content in files.items():
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(content)
        server = LocalFtp(root)
        self.addCleanup(server.stop)
        known = {"11/1113/694.img": b"saved original"}.get

        class Failing(ftpops.WatchFtp):
            fail = b""

            def _upload_verified(self, remote, data, verify):
                if data == self.fail:
                    raise ftpops.FtpError("writing failed")
                super()._upload_verified(remote, data, verify)

        client = Failing("127.0.0.1", server.port, "watch")
        self.addCleanup(client.close)
        lines = []
        self.assertEqual(client.push_tiles(source, "full", backup, lines.append, "ctl", None, None, known), (2, 14))
        self.assertEqual(client.read("map/res/ctl/11/1113/694.img"), b"new 694")
        # a tile with a known original is not backed up, the others are
        self.assertFalse(os.path.exists(os.path.join(backup, "11", "1113", "694.img")))
        with open(os.path.join(backup, "11", "1113", "695.img"), "rb") as handle:
            self.assertEqual(handle.read(), b"in watch 695")
        self.assertNotIn("backed up", lines[0])
        self.assertIn("backed up", lines[1])
        # a failed write puts the known original back into the watch
        client.fail = b"new 694"
        with self.assertRaises(ftpops.FtpError) as failed:
            client.push_tiles(source, "full", None, lines.append, "ctl", ["11/1113/694.img"], None, known)
        self.assertIn("original tile was put back", str(failed.exception))
        self.assertEqual(client.read("map/res/ctl/11/1113/694.img"), b"saved original")

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    def test_builds_contour_layer_tiles(self):
        def middle(tile):
            north, east, south, west = swatch.map_bounds(*tile)
            return (north + south) / 2, (east + west) / 2

        def contour_tile(tile, levels=swatch.CONTOUR_LEVELS):
            lat, lon = middle(tile)
            source = ["[IMG ID]", "ID=%s" % swatch.tile_name(*tile), "Name=OSM street map", "Elevation=F",
                      "Levels=%d" % len(levels)]
            source += ["Level%d=%d" % (index, bits) for index, bits in enumerate(levels)] + ["[END-IMG ID]"]
            source += ["[POLYLINE]", "Type=0x21", "Label=820", "EndLevel=0",
                       "Data0=(%.6f,%.6f),(%.6f,%.6f)" % (lat + 0.01, lon, lat + 0.01, lon + 0.02), "[END]"]
            return swatch.compile_mp("\n".join(source) + "\n", swatch.tile_name(*tile))

        def way(ref, tile):
            lat, lon = middle(tile)
            return {"type": "way", "id": ref, "geometry": [{"lat": lat, "lon": lon - 0.01}, {"lat": lat, "lon": lon + 0.01}]}

        with_contours, plain, odd = (1113, 694), (1113, 693), (1114, 694)    # all in one block
        tiles = sorted([with_contours, plain, odd, (1114, 693)])            # no route in the last one
        data = {"elements": [
            {"type": "relation", "id": 1, "tags": {"route": "hiking", "osmc:symbol": "red:white:red_bar"},
             "members": [{"type": "way", "ref": ref, "role": ""} for ref in (70, 71, 72)]},
            way(70, with_contours), way(71, plain), way(72, odd)]}
        cache, out = os.path.join(self.tmp, "cache"), os.path.join(self.tmp, "build-contours")
        sync.download(tiles, cache, report=lambda _t: None, fetch=lambda area, report: dict(data), pause=lambda _s: None)
        store = sync.ContourStore(os.path.join(self.tmp, "contours"))
        self.assertTrue(store.add(with_contours, contour_tile(with_contours)))
        self.assertTrue(store.add(odd, contour_tile(odd, swatch.BASE_LEVELS)))   # a different structure than Zepp's
        self.assertTrue(ctlmerge.supported(store.read(with_contours)))
        self.assertFalse(ctlmerge.supported(store.read(odd)))
        # a tile with other lines besides contours is not merged: they would be lost
        mixed_source = routes.routes_mp(odd[0], odd[1], [], levels=swatch.CONTOUR_LEVELS,
                                        plain=[(0x06, "", [middle(odd), (middle(odd)[0], middle(odd)[1] + 0.01)], 0)])
        self.assertFalse(ctlmerge.supported(swatch.compile_mp(mixed_source, swatch.tile_name(*odd))))

        skipped, notes = [], []
        build = lambda **more: sync.build_from_cache(tiles, cache, out, notes.append, None, 2, store, skipped, **more)
        self.assertEqual(build(), ([plain, with_contours], 2))
        self.assertEqual(skipped, [odd])                                    # stays in the watch as it is

        def lines_of(tile):
            with open(os.path.join(out, *sync.tile_path(tile).split("/")), "rb") as handle:
                data = handle.read()
            info = imginfo.read_tile(data)
            self.assertEqual(info.kind, "ski")
            self.assertEqual([bits for _z, _i, bits, _n in sorted(info.levels)], ctlmerge.LEVELS)
            return sorted({e.type_byte & 0x7F for e in imginfo.iter_elements(data) if e.kind == "line"})

        self.assertEqual(lines_of(with_contours), [0x21, 0x43])            # contours and a mark
        self.assertEqual(lines_of(plain), [0x43])                           # the watch has no contours: a mark only
        self.assertFalse(os.path.exists(os.path.join(out, *sync.tile_path(odd).split("/"))))
        del skipped[:]
        self.assertEqual(build()[1], 0)                                     # not rebuilt when unchanged
        # newly saved original contours change the tile
        self.assertTrue(store.add(plain, contour_tile(plain)))
        self.assertEqual(build()[1], 1)
        self.assertEqual(lines_of(plain), [0x21, 0x43])
        # left-out tiles are neither built nor reported as present
        self.assertEqual(build(leave=[with_contours]), ([plain], 0))
        # ski layer tiles have their own structure and do not belong in the same folder
        ski_out = os.path.join(self.tmp, "build-ski")
        self.assertEqual(sync.build_from_cache(tiles, cache, ski_out, notes.append)[0], [plain, with_contours, odd])


class FakeRun:
    """A stand-in for subprocess.run: records the commands and answers from a list.

    An answer is (exit code, output), an exception to raise, or a function of
    (command, keyword arguments) returning one of those. Without answers left,
    every command succeeds.
    """

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = []

    @property
    def commands(self):
        return [command for command, _kwargs in self.calls]

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        answer = self.answers.pop(0) if self.answers else (0, "")
        if callable(answer):
            answer = answer(command, kwargs)
        if isinstance(answer, BaseException):
            raise answer
        code, output = answer
        return subprocess.CompletedProcess(command, code, output, "")


class WifiJoinTest(unittest.TestCase):
    """Building the commands that join the hotspot; the system tools are replaced, no Wi-Fi changes."""

    SSID = "ZM-1234"
    PASSWORD = "Secret23abcd"

    def _fake(self, system, answers=()):
        from zeppmap import wifi

        fake = FakeRun(answers)
        for patch in (mock.patch.object(wifi, "system", return_value=system),
                      mock.patch.object(wifi.subprocess, "run", fake),
                      mock.patch.object(wifi.time, "sleep", lambda _seconds: None)):
            patch.start()
            self.addCleanup(patch.stop)
        return wifi, fake

    def _assert_secret_kept(self, error):
        self.assertNotIn(self.PASSWORD, str(error))
        self.assertIn("--manual-wifi", str(error))

    def test_tool_per_system(self):
        for system, tool in (("macos", "networksetup"), ("linux", "nmcli"), ("windows", "netsh"), (None, None)):
            wifi, _fake = self._fake(system)
            self.assertEqual(wifi.join_tool(), tool)

    def test_macos_uses_networksetup(self):
        wifi, fake = self._fake("macos", [
            (0, "Hardware Port: Ethernet\nDevice: en1\n\nHardware Port: Wi-Fi\nDevice: en0\n"),
            (0, "Could not find network %s." % self.SSID),
            (0, ""),
        ])
        self.assertEqual(wifi.join(self.SSID, self.PASSWORD), "en0")
        join = ["networksetup", "-setairportnetwork", "en0", self.SSID, self.PASSWORD]
        self.assertEqual(fake.commands[1:], [join, join])

    def test_linux_retries_until_the_network_appears(self):
        wifi, fake = self._fake("linux", [
            (10, "Error: No network with SSID '%s' found." % self.SSID),
            (1, "Error: Scanning not allowed immediately following previous scan."),
            (0, "Device 'wlan0' successfully activated with 'f00d'."),
        ])
        self.assertEqual(wifi.join(self.SSID, self.PASSWORD), "")
        connect = ["nmcli", "device", "wifi", "connect", self.SSID, "password", self.PASSWORD]
        self.assertEqual(fake.commands, [connect, ["nmcli", "device", "wifi", "rescan"], connect])
        self.assertTrue(fake.calls[0][1]["capture_output"])

    def test_linux_gives_up_without_showing_the_password(self):
        wifi, fake = self._fake("linux", [(4, "Error: Connection activation failed (%s)." % self.PASSWORD)])
        with self.assertRaises(wifi.WifiError) as caught:
            wifi.join(self.SSID, self.PASSWORD, timeout=0.0)
        self._assert_secret_kept(caught.exception)
        self.assertIn("Connection activation failed (***)", str(caught.exception))
        self.assertEqual(len(fake.calls), 1)

    def test_missing_tool_points_to_manual_mode(self):
        wifi, _fake = self._fake("linux", [FileNotFoundError(2, "No such file", "nmcli")])
        with self.assertRaises(wifi.WifiError) as caught:
            wifi.join(self.SSID, self.PASSWORD)
        self.assertIn("nmcli was not found", str(caught.exception))
        self._assert_secret_kept(caught.exception)

    def test_unsupported_system_points_to_manual_mode(self):
        wifi, fake = self._fake(None)
        with self.assertRaises(wifi.WifiError) as caught:
            wifi.join(self.SSID, self.PASSWORD)
        self._assert_secret_kept(caught.exception)
        self.assertEqual(fake.calls, [])

    def test_windows_imports_a_temporary_profile(self):
        from xml.etree import ElementTree

        seen = {}

        def add_profile(command, kwargs):
            # The file exists only while netsh imports it.
            path = os.path.join(kwargs["cwd"], command[-1].split("=", 1)[1])
            seen["folder"] = kwargs["cwd"]
            seen["profile"] = ElementTree.parse(path).getroot()
            return 0, 'Profile %s is added on interface Wi-Fi.' % self.SSID

        wifi, fake = self._fake("windows", [
            add_profile,
            (1, 'The network specified by profile "%s" is not available to connect.' % self.SSID),
            (0, ""),
            (0, "Connection request was completed successfully."),
        ])
        self.assertEqual(wifi.join(self.SSID, self.PASSWORD), "")
        connect = ["netsh", "wlan", "connect", "name=" + self.SSID]
        self.assertEqual(fake.commands, [
            ["netsh", "wlan", "add", "profile", "filename=" + wifi.WLAN_PROFILE_FILE],
            connect, ["netsh", "wlan", "show", "networks"], connect,
        ])
        self.assertFalse(os.path.exists(seen["folder"]))   # the password does not stay on disk
        self.assertTrue(all(kwargs["encoding"] == "oem" for _command, kwargs in fake.calls))

        ns = {"w": "http://www.microsoft.com/networking/WLAN/profile/v1"}
        profile = seen["profile"]
        self.assertEqual(profile.find("w:name", ns).text, self.SSID)
        self.assertEqual(profile.find("w:SSIDConfig/w:SSID/w:name", ns).text, self.SSID)
        self.assertEqual(profile.find("w:connectionMode", ns).text, "manual")
        security = profile.find("w:MSM/w:security", ns)
        self.assertEqual(security.find("w:authEncryption/w:authentication", ns).text, "WPA2PSK")
        self.assertEqual(security.find("w:sharedKey/w:keyType", ns).text, "passPhrase")
        self.assertEqual(security.find("w:sharedKey/w:keyMaterial", ns).text, self.PASSWORD)

    def test_windows_failed_import(self):
        folders = []

        def add_profile(command, kwargs):
            folders.append(kwargs["cwd"])
            return 1, "The profile %s is not valid (%s)." % (self.SSID, self.PASSWORD)

        wifi, fake = self._fake("windows", [add_profile])
        with self.assertRaises(wifi.WifiError) as caught:
            wifi.join(self.SSID, self.PASSWORD)
        self._assert_secret_kept(caught.exception)
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(os.path.exists(folders[0]))

    def test_windows_profile_escapes_xml(self):
        from xml.etree import ElementTree

        from zeppmap import wifi

        ns = {"w": "http://www.microsoft.com/networking/WLAN/profile/v1"}
        profile = ElementTree.fromstring(wifi.wlan_profile("a&b <c>", "p\"w'd<&>x"))
        self.assertEqual(profile.find("w:name", ns).text, "a&b <c>")
        self.assertEqual(profile.find("w:MSM/w:security/w:sharedKey/w:keyMaterial", ns).text, "p\"w'd<&>x")

    def test_forget_removes_the_profile(self):
        for system, command in (
            ("linux", ["nmcli", "connection", "delete", "id", self.SSID]),
            ("windows", ["netsh", "wlan", "delete", "profile", "name=" + self.SSID]),
        ):
            wifi, fake = self._fake(system, [(10, "Error: unknown connection")])
            wifi.forget(self.SSID)   # a failure is not an error
            self.assertEqual(fake.commands, [command])
            wifi, fake = self._fake(system, [FileNotFoundError(2, "No such file")])
            wifi.forget(self.SSID)
        wifi, fake = self._fake("macos")
        wifi.forget(self.SSID)
        self.assertEqual(fake.calls, [])


class FakeLink:
    """A stand-in for the Bluetooth link: packets go straight to the simulated watch."""

    watch_options = {}
    last_watch = None

    def __init__(self, _device, on_packet):
        loop = asyncio.get_running_loop()
        self.watch = FakeWatch(KEY, lambda packet: loop.call_soon(on_packet, packet), **self.watch_options)
        FakeLink.last_watch = self.watch
        self.closed = False

    async def open(self):
        pass

    async def write_packet(self, packet):
        self.watch.on_data_packet(packet)

    async def write_ack(self, packet):
        self.watch.on_ack_packet(packet)

    async def close(self):
        self.closed = True


@unittest.skipIf(FTPServer is None, "pyftpdlib is not installed")
class CliFlowTest(unittest.IsolatedAsyncioTestCase):
    """The whole flow of the commands; Bluetooth and Wi-Fi are replaced."""

    async def asyncSetUp(self):
        from zeppmap import ble, wifi

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        server = LocalFtp(self.data)
        self.addCleanup(server.stop)

        key_file = os.path.join(self.tmp, "key")
        with open(key_file, "w") as handle:
            handle.write(KEY.hex())
        self.zip = os.path.join(self.tmp, "map.zip")
        make_tiles_zip(self.zip, {"11/1105/694.img": b"a" * 100, "11/1105/695.img": b"b" * 200})

        self.joined = []

        async def find_watch(name, address, timeout):
            return argparse.Namespace(name="Balance 3", address="TEST")

        async def ensure_bluetooth(on_wait=None):
            return None

        saved = (ble.find_watch, ble.BleLink, wifi.join, ble.ensure_bluetooth, ble.connected_watches,
                 os.environ.pop(cli.KEY_ENV, None))

        def restore():
            ble.find_watch, ble.BleLink, wifi.join, ble.ensure_bluetooth, ble.connected_watches = saved[:5]
            if saved[5] is not None:
                os.environ[cli.KEY_ENV] = saved[5]

        self.addCleanup(restore)
        ble.find_watch = find_watch
        ble.BleLink = FakeLink
        ble.ensure_bluetooth = ensure_bluetooth
        self.still_connected = []
        ble.connected_watches = lambda name=None: list(self.still_connected)
        wifi.join = lambda ssid, password, timeout=90.0: self.joined.append((ssid, password)) or "en0"
        self.forgotten = []
        self.addCleanup(setattr, wifi, "forget", wifi.forget)
        wifi.forget = self.forgotten.append
        FakeLink.watch_options = {"ftp_address": "ftp://127.0.0.1:%d/" % server.port}
        self.base_args = ["--key-file", key_file]

    def _args(self, *command):
        return cli.build_parser().parse_args(self.base_args + list(command))

    async def _run(self, *command):
        args = self._args(*command)
        output, real = io.StringIO(), sys.stdout
        sys.stdout = output
        try:
            code = await args.run(args)
        finally:
            sys.stdout = real
        return code, output.getvalue()

    async def test_check_lists_what_is_missing(self):
        code, output = await self._run("check", "--no-bluetooth")
        self.assertIn("OK    Python", output)
        self.assertIn("OK    Auth key", output)
        self.assertEqual(code, 0 if "FAIL" not in output else 1, output)
        # a missing key is a problem, and the check says where it should be
        saved = os.environ.pop("ZEPPMAP_KEY", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "ZEPPMAP_KEY", saved)
        missing = os.path.join(self.tmp, "no-key")
        args = cli.build_parser().parse_args(["--key-file", missing, "check", "--no-bluetooth"])
        output, real = io.StringIO(), sys.stdout
        sys.stdout = output
        try:
            code = await args.run(args)
        finally:
            sys.stdout = real
        self.assertEqual(code, 1)
        self.assertIn("FAIL  Auth key", output.getvalue())
        self.assertIn(missing, output.getvalue())
        self.assertIn("problem", output.getvalue())

    async def test_push_then_ls(self):
        backup = os.path.join(self.tmp, "backup")
        code, output = await self._run("push", self.zip, "--backup-dir", backup)
        self.assertEqual(code, 0, output)
        with open(os.path.join(self.data, "map", "res", "scl", "11", "1105", "695.img"), "rb") as handle:
            self.assertEqual(handle.read(), b"b" * 200)
        watch = FakeLink.last_watch
        self.assertEqual(len(self.joined), 1)
        self.assertRegex(self.joined[0][0], r"^ZM-\d{4}$")
        self.assertEqual(len(self.joined[0][1]), 12)
        # afterwards both FTP and the hotspot are off
        self.assertIsNone(watch.ftp_root)
        self.assertIsNone(watch.hotspot)
        self.assertIn("Uploaded 2 tiles", output)
        self.assertEqual(self.forgotten, [self.joined[0][0]])   # the temporary network profile is removed
        self.assertNotIn(self.joined[0][1], output)             # the password is not printed

        code, output = await self._run("ls", "-r", "map", "--no-hotspot")
        self.assertEqual(code, 0, output)
        self.assertIn("map/res/scl/11/1105/  (files: 2, 300 B)", output)
        self.assertIn("Files in total: 2, 300 B.", output)

        code, output = await self._run("ls", "map/res/scl/11/1105", "--no-hotspot")
        self.assertEqual(code, 0, output)
        self.assertIn("694.img", output)
        self.assertIn("200 B", output)
        self.assertEqual(len(self.joined), 1)  # --no-hotspot joins no network
        self.assertEqual(ftpops.human_size(999), "999 B")
        self.assertEqual(ftpops.human_size(1_500_000), "1.5 MB")
        self.assertEqual([e for e, _p, _c in FakeLink.last_watch.log if e == 0x0003], [])

    async def test_warns_when_mac_keeps_the_watch(self):
        self.still_connected = [argparse.Namespace(name="Balance 3", address="test")]
        code, output = await self._run("services")
        self.assertEqual(code, 0, output)
        if ON_MACOS:
            self.assertIn("stays connected to this Mac", output)
        else:
            self.assertNotIn("stays connected", output)   # only macOS holds the watch

        self.still_connected = []
        code, output = await self._run("services")
        self.assertNotIn("stays connected", output)

    async def test_cleanup_after_failure(self):
        code_error = None
        try:
            await self._run("pull", "neexistuje/soubor.img", os.path.join(self.tmp, "x"))
        except ftpops.FtpError as exc:
            code_error = exc
        self.assertIsNotNone(code_error)
        watch = FakeLink.last_watch
        self.assertIsNone(watch.ftp_root)
        self.assertIsNone(watch.hotspot)

    async def test_rm_is_limited_to_test_layers(self):
        for layer in ("trial", "ski"):
            code, _output = await self._run("push", self.zip, "--layer", layer, "--no-backup")
            self.assertEqual(code, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.data, "map", "res", "trial")))
        for forbidden in ("map", "map/config", "map/res", "map/res/scl", "map/res/ctl/11", "map/res/ski",
                          "map/res/ski/11/1105", "map/res/trial/../scl"):
            with self.assertRaises(cli.UsageError):
                await self._run("rm", forbidden)
        # a test layer can be deleted whole, the official layers only one tile at a time
        code, output = await self._run("rm", "map/res/trial")
        self.assertEqual(code, 0, output)
        self.assertIn("Files deleted: 2.", output)
        self.assertFalse(os.path.exists(os.path.join(self.data, "map", "res", "trial")))
        code, output = await self._run("rm", "map/res/ski/11/1105/694.img")
        self.assertEqual(code, 0, output)
        self.assertEqual(os.listdir(os.path.join(self.data, "map", "res", "ski", "11", "1105")), ["695.img"])

    async def test_dry_run_and_missing_key(self):
        code, output = await self._run("push", self.zip, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("11/1105/694.img (100 B)", output)
        self.assertEqual(self.joined, [])

        self.base_args = ["--key-file", os.path.join(self.tmp, "nokey")]
        with self.assertRaises(cli.UsageError):
            await self._run("services")

    def _hotspots_started(self, watch):
        """(network, password) of every hotspot the watch was asked to start."""
        started = []
        for endpoint, payload, _encrypted in watch.log:
            if endpoint == 0x0003 and payload[:1] == b"\x11":
                ssid, password = payload[1:].split(b"\x00")[:2]
                started.append((ssid.decode(), password.decode()))
        return started

    async def test_manual_wifi_waits_for_the_user(self):
        from zeppmap import wifi

        waits = []
        real_wait = wifi.wait_for_port
        self.addCleanup(setattr, wifi, "wait_for_port", real_wait)

        def wait_for_port(host, port, timeout=30.0):
            waits.append(timeout)
            real_wait(host, port, timeout)

        wifi.wait_for_port = wait_for_port
        # the option works before the command and after it
        for command in (("--manual-wifi", "push", self.zip, "--no-backup"),
                        ("push", self.zip, "--no-backup", "--manual-wifi")):
            code, output = await self._run(*command)
            self.assertEqual(code, 0, output)
            self.assertIn("Uploaded 2 tiles", output)
            [(ssid, password)] = self._hotspots_started(FakeLink.last_watch)
            self.assertIn("  network:  %s\n" % ssid, output)
            self.assertIn("  password: %s\n" % password, output)
            self.assertIn("join it again", output)
            self.assertIsNone(FakeLink.last_watch.hotspot)
        self.assertEqual(self.joined, [])      # nothing joined automatically
        self.assertEqual(self.forgotten, [])   # and no profile to remove
        self.assertEqual(waits, [cli.MANUAL_WIFI_TIMEOUT] * 2)

        # without the option the automatic join stays
        code, output = await self._run("push", self.zip, "--no-backup")
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.joined), 1)
        self.assertNotIn("password:", output)
        self.assertEqual(waits[-1], cli.PORT_TIMEOUT)

    async def test_manual_wifi_keeps_the_network_within_one_run(self):
        # sync starts the hotspot twice; the user should not have to join a second, different network
        os.makedirs(os.path.join(self.data, "map"))
        args = self._args("--manual-wifi", "ls", "map")
        started = []
        for _ in range(2):
            output, real = io.StringIO(), sys.stdout
            sys.stdout = output
            try:
                self.assertEqual(await args.run(args), 0)
            finally:
                sys.stdout = real
            started += self._hotspots_started(FakeLink.last_watch)
        self.assertEqual(len(started), 2)
        self.assertEqual(started[0], started[1])
        # a new run picks a new network
        await self._run("--manual-wifi", "ls", "map")
        self.assertNotEqual(self._hotspots_started(FakeLink.last_watch)[0], started[0])

    async def test_unreachable_watch_after_join_suggests_manual_wifi(self):
        from zeppmap import wifi

        def unreachable(host, port, timeout=30.0):
            raise wifi.WifiError("%s:%d is not reachable: timed out" % (host, port))

        self.addCleanup(setattr, wifi, "wait_for_port", wifi.wait_for_port)
        wifi.wait_for_port = unreachable
        with self.assertRaises(wifi.WifiError) as caught:
            await self._run("ls", "map")
        [(ssid, password)] = self.joined
        self.assertIn("--manual-wifi", str(caught.exception))
        self.assertIn(ssid, str(caught.exception))
        self.assertNotIn(password, str(caught.exception))
        self.assertEqual(self.forgotten, [ssid])
        self.assertIsNone(FakeLink.last_watch.hotspot)

    async def test_check_on_linux_and_windows(self):
        from zeppmap import wifi

        real_which = shutil.which
        cases = (
            ("linux", True, "NOTE  System       Linux", "OK    Wi-Fi        joins the watch hotspot with nmcli"),
            ("linux", False, "NOTE  System       Linux", "NOTE  Wi-Fi        nmcli not found; add --manual-wifi"),
            ("windows", True, "NOTE  System       Windows", "OK    Wi-Fi        joins the watch hotspot with netsh"),
        )
        for system, tool_found, system_line, wifi_line in cases:
            def which(name, *rest, **kwargs):
                if name in ("nmcli", "netsh"):
                    return "/usr/bin/" + name if tool_found else None
                return real_which(name, *rest, **kwargs)

            with mock.patch.object(wifi, "system", return_value=system), mock.patch("shutil.which", which):
                code, output = await self._run("check")
            self.assertIn(system_line, output)
            self.assertIn("not yet tested with a real watch", output)
            self.assertIn("--manual-wifi", output)
            self.assertIn(wifi_line, output)
            self.assertNotIn("FAIL  System", output)
            self.assertIn("NOTE  Bluetooth    not checked", output)
            # notes are not problems
            self.assertEqual(code, 0 if "FAIL" not in output else 1, output)

        with mock.patch.object(wifi, "system", return_value=None):
            code, output = await self._run("check", "--no-bluetooth")
        self.assertIn("FAIL  System", output)
        self.assertNotIn("Wi-Fi", output)
        self.assertEqual(code, 1)

    async def test_sync_uploads_only_what_changed(self):
        from zeppmap import wifi

        for name in ("11/1113/694.img", "11/1114/692.img"):
            path = os.path.join(self.data, "map", "res", "scl", *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"base map")
        official = os.path.join(self.data, "map", "res", "ski", "11", "1113", "694.img")
        os.makedirs(os.path.dirname(official))
        with open(official, "wb") as handle:
            handle.write(b"official ski tile")

        fetched, waited = [], []
        saved = (routes.fetch, sync.make_tile, wifi.wait_for_internet)

        def restore():
            routes.fetch, sync.make_tile, wifi.wait_for_internet = saved

        self.addCleanup(restore)
        routes.fetch = lambda area, report: fetched.append(area) or _route_data()
        sync.make_tile = lambda source, name: ("marks %s %d" % (name, len(source))).encode()
        wifi.wait_for_internet = lambda hosts, timeout=90.0: waited.append(list(hosts))

        out, cache, backup = (os.path.join(self.tmp, name) for name in ("build-ski", "cache", "backup"))
        command = ("sync", "--layer", "ski", "--out", out, "--cache", cache, "--backup-dir", backup)

        code, output = await self._run(*command, "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("base map for 2 tiles", output)
        self.assertIn("in the ski layer", output)
        self.assertIn("To upload: 1 tiles, 0.0 MB (11/1113/694.img)", output)
        self.assertIn("Dry run", output)
        with open(official, "rb") as handle:
            self.assertEqual(handle.read(), b"official ski tile")
        self.assertEqual(len(self.joined), 1)                    # a dry run needs only one connection
        self.assertEqual(len(fetched), 1)                        # both tiles lie in one block
        self.assertEqual(waited[0][0], "overpass.openstreetmap.fr")

        code, output = await self._run(*command)
        self.assertEqual(code, 0, output)
        self.assertIn("Uploaded 1 tiles", output)
        self.assertIn("in the cache", output)
        self.assertIn("newly built: 0", output)
        self.assertEqual(len(fetched), 1)
        self.assertEqual(len(self.joined), 3)                    # reading and uploading separately
        with open(os.path.join(out, "11", "1113", "694.img"), "rb") as handle:
            built = handle.read()
        with open(official, "rb") as handle:
            self.assertEqual(handle.read(), built)
        with open(os.path.join(backup, "11", "1113", "694.img"), "rb") as handle:
            self.assertEqual(handle.read(), b"official ski tile")
        self.assertFalse(os.path.exists(os.path.join(self.data, "map", "res", "ski", "11", "1114")))
        self.assertTrue(os.path.exists(os.path.join(out, "legend.txt")))

        code, output = await self._run(*command)
        self.assertEqual(code, 0, output)
        self.assertIn("up to date", output)
        self.assertEqual(len(self.joined), 4)                    # nothing to upload, the second connection is skipped

        # the Zepp app overwrote the tile: sync puts it back
        with open(official, "wb") as handle:
            handle.write(b"overwritten by a map update")
        code, output = await self._run(*command)
        self.assertEqual(code, 0, output)
        self.assertIn("Uploaded 1 tiles", output)
        with open(official, "rb") as handle:
            self.assertEqual(handle.read(), built)

        # the thorough check reads tile content and finds nothing either
        code, output = await self._run(*command, "--full-check")
        self.assertEqual(code, 0, output)
        self.assertIn("up to date", output)

        # the tile list from the last run is enough to prepare without the watch
        joined = len(self.joined)
        code, output = await self._run(*command, "--dry-run", "--like", os.path.join(cache, sync.TILE_LIST))
        self.assertEqual(code, 0, output)
        self.assertIn("base map for 2 tiles", output)
        self.assertIn("To upload: 1 tiles", output)              # without the watch it is unknown what it has
        self.assertEqual(len(self.joined), joined)

    @unittest.skipUnless(_mkgmap_available(), "Java nebo mkgmap nejsou k dispozici")
    async def test_sync_merges_marks_into_contour_layer(self):
        from zeppmap import wifi

        def write(path, content):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(content)

        def read(path):
            with open(path, "rb") as handle:
                return handle.read()

        for name in ("11/1113/694.img", "11/1114/692.img"):
            write(os.path.join(self.data, "map", "res", "scl", *name.split("/")), b"base map")
        north, east, south, west = swatch.map_bounds(1113, 694)
        lat, lon = (north + south) / 2, (east + west) / 2
        source = ["[IMG ID]", "ID=11130694", "Name=OSM street map", "Elevation=F", "Levels=6"]
        source += ["Level%d=%d" % (index, bits) for index, bits in enumerate(swatch.CONTOUR_LEVELS)] + ["[END-IMG ID]"]
        source += ["[POLYLINE]", "Type=0x21", "Label=820", "EndLevel=0",
                   "Data0=(%.6f,%.6f),(%.6f,%.6f)" % (lat + 0.01, lon, lat + 0.01, lon + 0.02), "[END]"]
        original = swatch.compile_mp("\n".join(source) + "\n", "11130694")
        in_watch = os.path.join(self.data, "map", "res", "ctl", "11", "1113", "694.img")
        write(in_watch, original)

        saved = (routes.fetch, wifi.wait_for_internet)

        def restore():
            routes.fetch, wifi.wait_for_internet = saved

        self.addCleanup(restore)
        routes.fetch = lambda area, report: _route_data()
        wifi.wait_for_internet = lambda hosts, timeout=90.0: None

        out, cache, store, backups = (os.path.join(self.tmp, name)
                                      for name in ("build-contours", "cache", "contours", "backups"))
        stored = os.path.join(store, "11", "1113", "694.img")
        command = ("sync", "--out", out, "--cache", cache, "--contours", store,
                   "--backup-dir", os.path.join(backups, "today-ctl"))

        # dry run: the original contours are saved to disk, nothing changes in the watch
        code, output = await self._run(*command, "--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("in the contour layer", output)
        self.assertIn("1 from the watch, 0 from backups", output)
        self.assertIn("To upload: 1 tiles", output)
        self.assertEqual(read(stored), original)
        self.assertEqual(read(in_watch), original)

        # for real: the watch has a tile with marks and contours, the original stays on disk
        code, output = await self._run(*command)
        self.assertEqual(code, 0, output)
        self.assertIn("Uploaded 1 tiles", output)
        self.assertIn("Do not delete it", output)
        merged = read(in_watch)
        self.assertEqual(merged, read(os.path.join(out, "11", "1113", "694.img")))
        self.assertEqual(imginfo.read_tile(merged).kind, "ski")
        types = {e.type_byte & 0x7F for e in imginfo.iter_elements(merged) if e.kind == "line"}
        self.assertEqual(types, {0x21, 0x43, 0x54})                # contours, red mark, cycle route
        self.assertEqual(read(stored), original)
        self.assertFalse(os.path.exists(os.path.join(backups, "today-ctl")))   # the original is already in the store
        self.assertIn("contour layer", read(os.path.join(out, "legend.txt")).decode("utf-8"))

        code, output = await self._run(*command)
        self.assertEqual(code, 0, output)
        self.assertIn("up to date", output)
        self.assertEqual(read(stored), original)                  # our tile does not get into the store

        # the store is gone and the watch has our tile: without the original contours it is not overwritten
        shutil.rmtree(store)
        code, output = await self._run(*command, "--refresh")
        self.assertEqual(code, 0, output)
        self.assertIn("original contours are not on disk", output)
        self.assertIn("11/1113/694.img", output)
        self.assertIn("up to date", output)
        self.assertEqual(read(in_watch), merged)
        self.assertFalse(os.path.exists(stored))

        # the original tile is in an earlier backup: the store is filled in from it
        write(os.path.join(backups, "20260101-000000-ctl", "11", "1113", "694.img"), original)
        code, output = await self._run(*command)
        self.assertEqual(code, 0, output)
        self.assertIn("0 from the watch, 1 from backups", output)
        self.assertIn("up to date", output)
        self.assertEqual(read(stored), original)

        # without reading the watch nothing is uploaded to the contour layer
        joined = len(self.joined)
        code, output = await self._run(*command, "--like", os.path.join(cache, sync.TILE_LIST))
        self.assertEqual(code, 0, output)
        self.assertIn("To upload: 1 tiles", output)
        self.assertIn("nothing is uploaded to the contour layer", output)
        self.assertEqual(len(self.joined), joined)
        self.assertEqual(read(in_watch), merged)

    async def test_sync_without_base_map(self):
        code, output = await self._run("sync", "--out", os.path.join(self.tmp, "z"), "--cache", os.path.join(self.tmp, "c"))
        self.assertEqual(code, 1, output)
        self.assertIn("Download a map", output)

    async def test_sync_reports_held_watch_once(self):
        path = os.path.join(self.data, "map", "res", "scl", "11", "1114", "692.img")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as handle:
            handle.write(b"base map")
        saved = routes.fetch
        self.addCleanup(lambda: setattr(routes, "fetch", saved))
        routes.fetch = lambda area, report: _route_data()
        from zeppmap import wifi
        saved_wait = wifi.wait_for_internet
        self.addCleanup(lambda: setattr(wifi, "wait_for_internet", saved_wait))
        wifi.wait_for_internet = lambda hosts, timeout=90.0: None
        self.still_connected = [argparse.Namespace(name="Balance 3", address="test")]
        code, output = await self._run("sync", "--out", os.path.join(self.tmp, "z"), "--cache", os.path.join(self.tmp, "c"))
        self.assertEqual(code, 0, output)
        self.assertIn("up to date", output)                    # no route in the tile
        self.assertEqual(output.count("stays connected"), 1 if ON_MACOS else 0)
        if ON_MACOS:
            self.assertTrue(output.rstrip().endswith("on the Mac again."))   # the notice comes last


if __name__ == "__main__":
    unittest.main()
