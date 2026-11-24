# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zepp OS session: authentication and the few services needed to upload maps.

Independent of the transport. The owner supplies two coroutines (write a
data packet, write an acknowledgement) and calls on_packet() for every
incoming notification.

The protocol is ported from Gadgetbridge (https://codeberg.org/Freeyourgadget/Gadgetbridge,
copyright the Gadgetbridge contributors, AGPL-3.0-or-later), classes
ZeppOsAuthenticationService, ZeppOsServicesService, ZeppOsWifiService and
ZeppOsFtpServerService.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Awaitable, Callable, Dict, Optional, Tuple

from . import ecdh_b163
from .chunked import ChunkedDecoder, ChunkedEncoder
from .crypto import aes_encrypt
from .errors import ZeppmapError

log = logging.getLogger("zeppmap.session")

ENDPOINT_SERVICES = 0x0000
ENDPOINT_WIFI = 0x0003
ENDPOINT_FTP = 0x0006
ENDPOINT_MAPS = 0x0046
ENDPOINT_AUTH = 0x0082

ENDPOINT_NAMES = {
    0x0000: "services",
    0x0003: "wifi",
    0x0006: "ftp-server",
    0x0007: "calendar",
    0x0008: "world-clocks",
    0x0009: "shortcut-cards",
    0x000B: "phone",
    0x000E: "weather",
    0x000F: "alarms",
    0x0015: "connection",
    0x0017: "user-info",
    0x0018: "vibration-patterns",
    0x0019: "workout",
    0x0023: "watchface",
    0x0029: "battery",
    0x003B: "silent-mode",
    0x0042: "agps",
    0x0046: "maps",
    0x004B: "activity-fetch",
    0x0082: "authentication",
    0x00A0: "apps",
}

# Services the tool works with. Only their messages are processed and logged.
_LOGGED_ENDPOINTS = frozenset((ENDPOINT_SERVICES, ENDPOINT_WIFI, ENDPOINT_FTP, ENDPOINT_AUTH))

_RESPONSE = 0x10
_SUCCESS = 0x01
_AUTH_CMD_PUB_KEY = 0x04
_AUTH_CMD_SESSION_KEY = 0x05
_AUTH_WRONG_KEY = 0x25

_SERVICES_GET = 0x03
_SERVICES_RET = 0x04

_WIFI_HOTSPOT_START = 0x11
_WIFI_HOTSPOT_STOP = 0x12
_WIFI_HOTSPOT_STATE = 0x13
_WIFI_STATE_STOPPED = 0x00
_WIFI_STATE_STARTED = 0x02

_FTP_START = 0x01
_FTP_STOP = 0x02
_FTP_INFO = 0x03
_FTP_INFO_STOPPED = 0x00
_FTP_INFO_STARTED = 0x02

_PASSWORD_CHARS = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"

PacketWriter = Callable[[bytes], Awaitable[None]]


class ZeppError(ZeppmapError):
    """The watch did not do what was asked."""


class WrongAuthKey(ZeppError):
    """The watch rejected the auth key."""


def parse_auth_key(text: str) -> bytes:
    key = text.strip()
    if key.lower().startswith("0x"):
        key = key[2:]
    try:
        raw = bytes.fromhex(key)
    except ValueError:
        raise ValueError("must be 32 hex digits") from None
    if len(raw) != 16:
        raise ValueError("must be 32 hex digits (16 bytes)")
    return raw


def random_hotspot_credentials() -> Tuple[str, str]:
    ssid = "ZM-%04d" % secrets.randbelow(10000)
    password = "".join(secrets.choice(_PASSWORD_CHARS) for _ in range(12))
    return ssid, password


def _cstring(data: bytes, offset: int) -> Tuple[str, int]:
    end = data.index(0, offset)
    return data[offset:end].decode("utf-8", "replace"), end + 1


class ZeppSession:
    def __init__(
        self,
        write_packet: PacketWriter,
        write_ack: PacketWriter,
        auth_key: bytes,
        max_packet: int = 20,
    ) -> None:
        if len(auth_key) != 16:
            raise ValueError("the auth key must be 16 bytes")
        self._write_packet = write_packet
        self._write_ack = write_ack
        self._auth_key = auth_key
        self._encoder = ChunkedEncoder(max_packet)
        self._decoder = ChunkedDecoder()
        self._write_lock = asyncio.Lock()
        self._queues: Dict[int, asyncio.Queue] = {}
        self._pending_acks: set = set()
        self.services: Dict[int, bool] = {}
        self.authenticated = False

    # ----------------------------------------------------------- transport

    def on_packet(self, data: bytes) -> None:
        """Process one incoming notification. Call from the event loop thread."""
        message, ack = self._decoder.feed(bytes(data))
        if ack is not None:
            task = asyncio.ensure_future(self._send_ack(ack))
            self._pending_acks.add(task)
            task.add_done_callback(self._pending_acks.discard)
        if message is None:
            return
        endpoint, payload = message
        if endpoint in _LOGGED_ENDPOINTS:
            log.debug("<- %04x %s", endpoint, payload.hex())
        else:
            # The watch also sends messages meant for the phone app, such as a weather
            # request with the account token and location. Their content stays out of the log.
            log.debug("<- %04x (%d B, message for another service, content hidden)", endpoint, len(payload))
        if payload and endpoint in _LOGGED_ENDPOINTS:
            self._queue(endpoint).put_nowait(payload)

    async def _send_ack(self, ack: bytes) -> None:
        try:
            await self._write_ack(ack)
        except Exception as exc:  # the connection may be closing right now
            log.debug("could not send acknowledgement: %s", exc)

    def _queue(self, endpoint: int) -> asyncio.Queue:
        if endpoint not in self._queues:
            self._queues[endpoint] = asyncio.Queue()
        return self._queues[endpoint]

    async def send(self, endpoint: int, payload: bytes, encrypt: Optional[bool] = None) -> None:
        if encrypt is None:
            encrypt = self.services.get(endpoint, True)
        if endpoint == ENDPOINT_WIFI and payload[:1] == bytes([_WIFI_HOTSPOT_START]):
            log.debug(
                "-> %04x (%d B, hotspot credentials hidden)%s",
                endpoint, len(payload), " (encrypted)" if encrypt else "",
            )
        else:
            log.debug("-> %04x %s%s", endpoint, payload.hex(), " (encrypted)" if encrypt else "")
        async with self._write_lock:
            for packet in self._encoder.encode(endpoint, payload, encrypt):
                await self._write_packet(packet)

    async def expect(
        self,
        endpoint: int,
        match: Callable[[bytes], bool],
        timeout: float,
        what: str,
    ) -> bytes:
        queue = self._queue(endpoint)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        failure = "the watch did not send %s within %.0f s" % (what, timeout)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ZeppError(failure)
            try:
                payload = await asyncio.wait_for(queue.get(), remaining)
            except asyncio.TimeoutError:
                raise ZeppError(failure) from None
            if match(payload):
                return payload
            log.debug("skipping %04x %s (waiting for: %s)", endpoint, payload.hex(), what)

    # ------------------------------------------------------ authentication

    async def authenticate(self, timeout: float = 15.0) -> None:
        self._encoder.reset()
        self._decoder.reset()
        self.authenticated = False

        private = ecdh_b163.generate_private()
        public = ecdh_b163.generate_public(private)
        await self.send(ENDPOINT_AUTH, bytes([0x04, 0x02, 0x00, 0x02]) + public, encrypt=False)

        reply = await self.expect(
            ENDPOINT_AUTH,
            lambda p: len(p) >= 3 and p[0] == _RESPONSE and p[1] == _AUTH_CMD_PUB_KEY,
            timeout,
            "its public key",
        )
        if reply[2] != _SUCCESS or len(reply) < 67:
            raise ZeppError("the watch rejected the key exchange (status 0x%02x)" % reply[2])
        remote_random = reply[3:19]
        remote_public = reply[19:67]

        try:
            shared = ecdh_b163.shared_secret(private, remote_public)
        except ValueError as exc:
            raise ZeppError("the watch sent an invalid public key (%s)" % exc) from None
        sequence = int.from_bytes(shared[0:4], "little")
        session_key = bytes(shared[i + 8] ^ self._auth_key[i] for i in range(16))
        self._encoder.set_encryption(sequence, session_key)
        self._decoder.set_session_key(session_key)

        proof = aes_encrypt(self._auth_key, remote_random) + aes_encrypt(session_key, remote_random)
        await self.send(ENDPOINT_AUTH, bytes([_AUTH_CMD_SESSION_KEY]) + proof, encrypt=False)

        reply = await self.expect(
            ENDPOINT_AUTH,
            lambda p: len(p) >= 3 and p[0] == _RESPONSE and p[1] == _AUTH_CMD_SESSION_KEY,
            timeout,
            "the authentication result",
        )
        if reply[2] == _AUTH_WRONG_KEY:
            raise WrongAuthKey(
                "the watch rejected the auth key. Only the key from the current pairing in Zepp is valid."
            )
        if reply[2] != _SUCCESS:
            raise ZeppError("authentication failed (status 0x%02x)" % reply[2])
        self.authenticated = True

    # ------------------------------------------------------------ services

    async def request_services(self, timeout: float = 15.0) -> Dict[int, bool]:
        """Find out the watch services and, for each, whether it is encrypted."""
        await self.send(ENDPOINT_SERVICES, bytes([_SERVICES_GET]), encrypt=False)
        reply = await self.expect(
            ENDPOINT_SERVICES,
            lambda p: p[0] == _SERVICES_RET and len(p) >= 3,
            timeout,
            "the list of services",
        )
        count = int.from_bytes(reply[1:3], "little")
        services: Dict[int, bool] = {}
        offset = 3
        for _ in range(count):
            if offset + 3 > len(reply):
                break
            endpoint = int.from_bytes(reply[offset:offset + 2], "little")
            services[endpoint] = reply[offset + 2] == 0x01
            offset += 3
        self.services = services
        return services

    def require_service(self, endpoint: int) -> None:
        if self.services and endpoint not in self.services:
            raise ZeppError(
                "the watch does not offer the %s service (0x%04x)"
                % (ENDPOINT_NAMES.get(endpoint, "?"), endpoint)
            )

    # --------------------------------------------------------------- wifi

    async def start_hotspot(self, ssid: str, password: str, timeout: float = 30.0) -> None:
        if len(password) < 8:
            raise ValueError("the hotspot password must have at least 8 characters")
        self.require_service(ENDPOINT_WIFI)
        # Gadgetbridge sends the third string; its meaning is unknown, so it stays.
        payload = (
            bytes([_WIFI_HOTSPOT_START])
            + ssid.encode("utf-8") + b"\x00"
            + password.encode("utf-8") + b"\x00"
            + b"gadgetbridge\x00"
        )
        await self.send(ENDPOINT_WIFI, payload)
        states = []

        def started(p: bytes) -> bool:
            if len(p) >= 2 and p[0] == _WIFI_HOTSPOT_STATE:
                states.append(p[1])
                return p[1] == _WIFI_STATE_STARTED
            return False

        try:
            await self.expect(ENDPOINT_WIFI, started, timeout, "confirmation that the hotspot is running")
        except ZeppError as exc:
            seen = ", ".join("0x%02x" % s for s in states) or "none"
            raise ZeppError("%s (reported hotspot states: %s)" % (exc, seen)) from None

    async def stop_hotspot(self, timeout: float = 10.0) -> None:
        await self.send(ENDPOINT_WIFI, bytes([_WIFI_HOTSPOT_STOP]))
        try:
            await self.expect(
                ENDPOINT_WIFI,
                lambda p: len(p) >= 2 and p[0] == _WIFI_HOTSPOT_STATE and p[1] == _WIFI_STATE_STOPPED,
                timeout,
                "confirmation that the hotspot stopped",
            )
        except ZeppError as exc:
            log.debug("%s", exc)

    # ---------------------------------------------------------------- ftp

    async def start_ftp(self, root_dir: str = "/mnt/data", timeout: float = 30.0) -> Tuple[str, str]:
        """Start the watch FTP server. Returns (address, user name)."""
        self.require_service(ENDPOINT_FTP)
        payload = bytes([_FTP_START])
        if root_dir:
            payload += root_dir.encode("utf-8") + b"\x00"
        await self.send(ENDPOINT_FTP, payload)
        reply = await self.expect(
            ENDPOINT_FTP,
            lambda p: len(p) >= 2 and p[0] == _FTP_INFO and p[1] == _FTP_INFO_STARTED,
            timeout,
            "confirmation that the FTP server is running",
        )
        try:
            address, offset = _cstring(reply, 2)
            username, _ = _cstring(reply, offset)
        except ValueError:
            raise ZeppError("unreadable FTP server reply: %s" % reply.hex()) from None
        return address, username

    async def stop_ftp(self, timeout: float = 10.0) -> None:
        await self.send(ENDPOINT_FTP, bytes([_FTP_STOP]))
        try:
            await self.expect(
                ENDPOINT_FTP,
                lambda p: len(p) >= 2 and p[0] == _FTP_INFO and p[1] == _FTP_INFO_STOPPED,
                timeout,
                "confirmation that the FTP server stopped",
            )
        except ZeppError as exc:
            log.debug("%s", exc)
