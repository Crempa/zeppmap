# SPDX-License-Identifier: AGPL-3.0-or-later
"""AES-128-ECB without padding, as used by the Huami 2021 protocol."""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def aes_encrypt(key: bytes, data: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def aes_decrypt(key: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()
