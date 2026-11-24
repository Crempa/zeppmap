# SPDX-License-Identifier: AGPL-3.0-or-later
"""ECDH on the NIST B-163 curve in the format the watch expects.

Port of ECDH_B163.java from Gadgetbridge (copyright the Gadgetbridge
contributors, AGPL-3.0-or-later), which itself is a port of kokke/tiny-ECDH-c
(public domain). The original works with arrays of 32-bit words; here a field
element is a single Python integer whose bits are the polynomial
coefficients. The results are the same and the code is a fraction.

Wire format: a field element is 24 bytes, little-endian. A public key (and the
shared secret) is x followed by y, 48 bytes in total.
"""
from __future__ import annotations

import os

PRIVATE_KEY_SIZE = 24
PUBLIC_KEY_SIZE = 48

_DEGREE = 163
_POLY = (1 << 163) | 0xC9  # x^163 + x^7 + x^6 + x^3 + 1
_COEFF_B = 0x020A601907B8C953CA1481EB10512F78744A3205FD
_BASE_X = 0x03F0EBA16286A2D57EA0991168D4994637E8343E36
_BASE_Y = 0x00D51FBC6C71A0094FA2CDD545B11C5C0C797324F1
_ORDER = 0x040000000000000000000292FE77E70C12A4234C33

# tiny-ECDH clears all bits from (bit length of the order - 1) up, so the
# scalar actually used is always smaller than 2^162.
_SCALAR_MASK = (1 << (_ORDER.bit_length() - 1)) - 1


def _mul(a: int, b: int) -> int:
    result = 0
    while b:
        if b & 1:
            result ^= a
        b >>= 1
        a <<= 1
        if (a >> _DEGREE) & 1:
            a ^= _POLY
    return result


def _inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("inverse of zero in GF(2^163)")
    u, v = a, _POLY
    g1, g2 = 1, 0
    while u != 1:
        shift = u.bit_length() - v.bit_length()
        if shift < 0:
            u, v = v, u
            g1, g2 = g2, g1
            shift = -shift
        u ^= v << shift
        g1 ^= g2 << shift
    return g1


def _double(x: int, y: int):
    if x == 0:
        return 0, 0
    lam = _mul(_inv(x), y) ^ x
    new_y = _mul(x, x)
    new_x = _mul(lam, lam)
    lam ^= 1
    new_x ^= lam
    new_y ^= _mul(lam, new_x)
    return new_x, new_y


def _add(x1: int, y1: int, x2: int, y2: int):
    if x2 == 0 and y2 == 0:
        return x1, y1
    if x1 == 0 and y1 == 0:
        return x2, y2
    if x1 == x2:
        if y1 == y2:
            return _double(x1, y1)
        return 0, 0
    a = y1 ^ y2
    b = x1 ^ x2
    c = _mul(_inv(b), a)
    d = _mul(c, c) ^ c ^ b ^ 1
    x1 ^= d
    a = _mul(x1, c) ^ d
    return d, y1 ^ a


def _point_mul(x: int, y: int, scalar: int):
    rx, ry = 0, 0
    for i in range(scalar.bit_length() - 1, -1, -1):
        rx, ry = _double(rx, ry)
        if (scalar >> i) & 1:
            rx, ry = _add(rx, ry, x, y)
    return rx, ry


def _on_curve(x: int, y: int) -> bool:
    if x == 0 and y == 0:
        return False
    x2 = _mul(x, x)
    left = x2 ^ _mul(x2, x) ^ _COEFF_B ^ _mul(y, y)
    return left == _mul(x, y)


def _scalar(private_key: bytes) -> int:
    if len(private_key) != PRIVATE_KEY_SIZE:
        raise ValueError("the private key must be 24 bytes")
    return int.from_bytes(private_key, "little") & _SCALAR_MASK


def _encode_point(x: int, y: int) -> bytes:
    return x.to_bytes(24, "little") + y.to_bytes(24, "little")


def generate_private() -> bytes:
    """A random private key that passes the "not too small" check."""
    while True:
        key = os.urandom(PRIVATE_KEY_SIZE)
        if int.from_bytes(key, "little").bit_length() >= _DEGREE // 2:
            return key


def generate_public(private_key: bytes) -> bytes:
    if int.from_bytes(private_key, "little").bit_length() < _DEGREE // 2:
        raise ValueError("the private key is too small")
    return _encode_point(*_point_mul(_BASE_X, _BASE_Y, _scalar(private_key)))


def shared_secret(private_key: bytes, remote_public: bytes) -> bytes:
    if len(remote_public) != PUBLIC_KEY_SIZE:
        raise ValueError("must be 48 bytes")
    x = int.from_bytes(remote_public[:24], "little")
    y = int.from_bytes(remote_public[24:], "little")
    if x >> _DEGREE or y >> _DEGREE or not _on_curve(x, y):
        raise ValueError("not a point on the B-163 curve")
    return _encode_point(*_point_mul(x, y, _scalar(private_key)))
