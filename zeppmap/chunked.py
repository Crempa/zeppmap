# SPDX-License-Identifier: AGPL-3.0-or-later
"""Huami "chunked 2021" message framing.

Port of the Huami2021ChunkedEncoder / Huami2021ChunkedDecoder classes from
Gadgetbridge (https://codeberg.org/Freeyourgadget/Gadgetbridge, copyright the
Gadgetbridge contributors, AGPL-3.0-or-later), always with the extended
header used by Zepp OS devices.

A message for an endpoint is split into packets:

    first packet:  03 flags 00 handle count  length(4, LE) endpoint(2, LE)  data...
    next packets:  03 flags 00 handle count  data...

flags: 0x01 first, 0x02 last, 0x04 wants an acknowledgement, 0x08 encrypted.
The encrypted body is AES-ECB(payload + seq(4) + crc32(4) + zero padding)
with the session key XORed with the message number (handle); the length
field carries the length of the unencrypted payload.
"""
from __future__ import annotations

import zlib
from typing import List, Optional, Tuple

from .crypto import aes_decrypt, aes_encrypt

FLAG_FIRST = 0x01
FLAG_LAST = 0x02
FLAG_NEEDS_ACK = 0x04
FLAG_ENCRYPTED = 0x08

_FIRST_HEADER = 11
_NEXT_HEADER = 5


def _message_key(session_key: bytes, handle: int) -> bytes:
    return bytes(b ^ handle for b in session_key)


def _padded_length(length: int) -> int:
    total = length + 8
    if total % 16:
        total += 16 - total % 16
    return total


class ChunkedEncoder:
    def __init__(self, max_packet: int = 20):
        if max_packet <= _FIRST_HEADER:
            raise ValueError("max_packet is too small")
        self.max_packet = max_packet
        self._handle = 0
        self._sequence = 0
        self._session_key: Optional[bytes] = None

    def reset(self) -> None:
        self._handle = 0
        self._sequence = 0

    def set_encryption(self, sequence: int, session_key: bytes) -> None:
        self._sequence = sequence & 0xFFFFFFFF
        self._session_key = bytes(session_key)

    def encode(self, endpoint: int, payload: bytes, encrypt: bool) -> List[bytes]:
        if encrypt and self._session_key is None:
            raise RuntimeError("encryption is only possible after authentication")

        self._handle = (self._handle + 1) & 0xFF
        handle = self._handle
        length = len(payload)

        if encrypt:
            plain = bytearray(_padded_length(length))
            plain[:length] = payload
            plain[length:length + 4] = self._sequence.to_bytes(4, "little")
            self._sequence = (self._sequence + 1) & 0xFFFFFFFF
            checksum = zlib.crc32(bytes(plain[:length + 4])) & 0xFFFFFFFF
            plain[length + 4:length + 8] = checksum.to_bytes(4, "little")
            body = aes_encrypt(_message_key(self._session_key, handle), bytes(plain))
        else:
            body = bytes(payload)

        packets: List[bytes] = []
        offset = 0
        count = 0
        while offset < len(body):
            header_size = _FIRST_HEADER if count == 0 else _NEXT_HEADER
            take = min(len(body) - offset, self.max_packet - header_size)
            flags = FLAG_ENCRYPTED if encrypt else 0
            if count == 0:
                flags |= FLAG_FIRST
            if offset + take >= len(body):
                flags |= FLAG_LAST | FLAG_NEEDS_ACK
            packet = bytearray([0x03, flags, 0x00, handle, count & 0xFF])
            if count == 0:
                packet += length.to_bytes(4, "little")
                packet += endpoint.to_bytes(2, "little")
            packet += body[offset:offset + take]
            packets.append(bytes(packet))
            offset += take
            count += 1
        return packets


class ChunkedDecoder:
    def __init__(self) -> None:
        self._session_key: Optional[bytes] = None
        self._handle: Optional[int] = None
        self._endpoint = 0
        self._length = 0
        self._buffer = bytearray()

    def reset(self) -> None:
        self._handle = None
        self._endpoint = 0
        self._buffer = bytearray()

    def set_session_key(self, session_key: bytes) -> None:
        self._session_key = bytes(session_key)

    def feed(self, packet: bytes) -> Tuple[Optional[Tuple[int, bytes]], Optional[bytes]]:
        """Process one packet.

        Returns (message, ack): message is (endpoint, payload) once it is
        complete; ack is a packet to send back if the sender asked for one.
        """
        if len(packet) < _NEXT_HEADER or packet[0] != 0x03:
            return None, None

        flags = packet[1]
        handle = packet[3]
        count = packet[4]
        index = _NEXT_HEADER

        if self._handle is not None and self._handle != handle:
            return None, None

        if flags & FLAG_FIRST:
            if len(packet) < _FIRST_HEADER:
                return None, None
            self._length = int.from_bytes(packet[5:9], "little")
            self._endpoint = int.from_bytes(packet[9:11], "little")
            self._handle = handle
            self._buffer = bytearray()
            index = _FIRST_HEADER
        elif self._handle is None:
            return None, None

        self._buffer += packet[index:]

        ack = bytes([0x04, 0x00, handle, 0x01, count]) if flags & FLAG_NEEDS_ACK else None

        if not flags & FLAG_LAST:
            return None, ack

        body = bytes(self._buffer)
        endpoint, length = self._endpoint, self._length
        self.reset()

        if flags & FLAG_ENCRYPTED:
            if self._session_key is None or len(body) != _padded_length(length):
                return None, ack
            body = aes_decrypt(_message_key(self._session_key, handle), body)
        return (endpoint, body[:length]), ack
