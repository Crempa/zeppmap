# SPDX-License-Identifier: AGPL-3.0-or-later
"""A simulated watch for tests without a device.

It behaves the way the watch answers according to the Gadgetbridge sources.
So it checks that the tool speaks the protocol as Gadgetbridge describes it;
it does not prove that a real watch answers the same way.
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Tuple

from zeppmap import ecdh_b163
from zeppmap.chunked import FLAG_ENCRYPTED, FLAG_FIRST, ChunkedDecoder, ChunkedEncoder
from zeppmap.crypto import aes_encrypt


class FakeWatch:
    def __init__(
        self,
        auth_key: bytes,
        deliver: Callable[[bytes], None],
        ftp_address: str = "ftp://192.168.4.1:21/",
        ftp_username: str = "watch",
        max_packet: int = 20,
        services: Optional[List[Tuple[int, bool]]] = None,
    ) -> None:
        self._auth_key = auth_key
        self._deliver = deliver
        self._encoder = ChunkedEncoder(max_packet)
        self._decoder = ChunkedDecoder()
        self._random = b""
        self._session_key = b""
        self._first_flags = 0
        self.ftp_address = ftp_address
        self.ftp_username = ftp_username
        self.services = services if services is not None else [
            (0x0000, False), (0x0003, True), (0x0006, True), (0x0046, True), (0x0082, False),
        ]
        self.authenticated = False
        self.hotspot: Optional[Tuple[str, str, str]] = None
        self.ftp_root: Optional[str] = None
        self.acks: List[bytes] = []
        self.log: List[Tuple[int, bytes, bool]] = []  # (endpoint, payload, encrypted)

    # What the computer wrote to the write characteristic.
    def on_data_packet(self, packet: bytes) -> None:
        if packet[1] & FLAG_FIRST:
            self._first_flags = packet[1]
        message, _ack = self._decoder.feed(packet)
        if message is None:
            return
        endpoint, payload = message
        encrypted = bool(self._first_flags & FLAG_ENCRYPTED)
        self.log.append((endpoint, payload, encrypted))
        self._handle(endpoint, payload, encrypted)

    # What the computer wrote to the read characteristic (acknowledgements).
    def on_ack_packet(self, packet: bytes) -> None:
        self.acks.append(packet)

    def _reply(self, endpoint: int, payload: bytes, encrypt: bool) -> None:
        for packet in self._encoder.encode(endpoint, payload, encrypt):
            self._deliver(packet)

    def _handle(self, endpoint: int, payload: bytes, encrypted: bool) -> None:
        if endpoint == 0x0082:
            self._handle_auth(payload)
            return
        if not self.authenticated:
            return
        if endpoint == 0x0000 and payload[:1] == b"\x03":
            body = bytes([0x04]) + len(self.services).to_bytes(2, "little")
            for service, service_encrypted in self.services:
                body += service.to_bytes(2, "little") + bytes([1 if service_encrypted else 0])
            self._reply(0x0000, body, False)
        elif endpoint == 0x0003 and encrypted:
            if payload[:1] == b"\x11":
                ssid, password, client, _rest = payload[1:].split(b"\x00", 3)
                self.hotspot = (ssid.decode(), password.decode(), client.decode())
                self._reply(0x0003, bytes([0x13, 0x01]), True)  # intermediate state
                self._reply(0x0003, bytes([0x13, 0x02]), True)
            elif payload[:1] == b"\x12":
                self.hotspot = None
                self._reply(0x0003, bytes([0x13, 0x00]), True)
        elif endpoint == 0x0006 and encrypted:
            if payload[:1] == b"\x01":
                self.ftp_root = payload[1:].split(b"\x00", 1)[0].decode()
                body = bytes([0x03, 0x02]) + self.ftp_address.encode() + b"\x00" + self.ftp_username.encode() + b"\x00"
                self._reply(0x0006, body, True)
            elif payload[:1] == b"\x02":
                self.ftp_root = None
                self._reply(0x0006, bytes([0x03, 0x00]), True)

    def _handle_auth(self, payload: bytes) -> None:
        if payload[:1] == b"\x04":
            host_public = payload[4:52]
            private = ecdh_b163.generate_private()
            shared = ecdh_b163.shared_secret(private, host_public)
            self._random = os.urandom(16)
            self._session_key = bytes(shared[i + 8] ^ self._auth_key[i] for i in range(16))
            self._sequence = int.from_bytes(shared[0:4], "little")
            self.authenticated = False
            self._reply(0x0082, bytes([0x10, 0x04, 0x01]) + self._random + ecdh_b163.generate_public(private), False)
        elif payload[:1] == b"\x05":
            proof_key, proof_session = payload[1:17], payload[17:33]
            if proof_key != aes_encrypt(self._auth_key, self._random):
                self._reply(0x0082, bytes([0x10, 0x05, 0x25]), False)
            elif proof_session != aes_encrypt(self._session_key, self._random):
                self._reply(0x0082, bytes([0x10, 0x05, 0x02]), False)
            else:
                self.authenticated = True
                self._encoder.set_encryption(self._sequence, self._session_key)
                self._decoder.set_session_key(self._session_key)
                self._reply(0x0082, bytes([0x10, 0x05, 0x01]), False)
