# SPDX-License-Identifier: AGPL-3.0-or-later
"""Connection to the watch over Bluetooth LE (through the bleak library)."""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Callable, Iterable, List, Optional, Tuple

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import btstate
from .errors import ZeppmapError

log = logging.getLogger("zeppmap.ble")

CHAR_CHUNKED_WRITE = "00000016-0000-3512-2118-0009af100700"
CHAR_CHUNKED_READ = "00000017-0000-3512-2118-0009af100700"
SERVICE_HUAMI = "0000fee0-0000-1000-8000-00805f9b34fb"
COMPANY_HUAMI = 0x0157  # Anhui Huami Information Technology, maker of Amazfit watches
# Services by which the system recognises an already connected watch.
HUAMI_SERVICES = ("FEE0", "FEE1", "00001530-0000-3512-2118-0009AF100700")

_WATCH_NAME_HINTS = ("amazfit", "balance", "zepp", "t-rex", "cheetah", "falcon", "active", "bip", "gtr", "gts")


class LinkError(ZeppmapError):
    pass


def looks_like_watch(name: Optional[str], service_uuids: List[str], company_ids: Iterable[int] = ()) -> bool:
    if SERVICE_HUAMI in [u.lower() for u in service_uuids]:
        return True
    if COMPANY_HUAMI in company_ids:
        return True
    return bool(name) and any(hint in name.lower() for hint in _WATCH_NAME_HINTS)


def describe_advertisement(service_uuids: List[str], company_ids: Iterable[int]) -> str:
    """Short description of what a device advertises, for scan --all."""
    parts = []
    short = []
    for uuid in service_uuids:
        uuid = uuid.lower()
        # Standard 16-bit services are shortened to four hex digits.
        short.append(uuid[4:8] if uuid.endswith("-0000-1000-8000-00805f9b34fb") else uuid)
    if short:
        parts.append("services " + ",".join(short))
    ids = ["0x%04x" % company for company in company_ids]
    if ids:
        parts.append("manufacturer " + ",".join(ids))
    return "; ".join(parts)


async def ensure_bluetooth(on_wait: Optional[Callable[[], None]] = None) -> None:
    """Check that macOS made Bluetooth available to this process; otherwise say why not.

    Only macOS asks for a per-application permission; elsewhere bleak reports
    problems itself.
    """
    if sys.platform != "darwin":
        return
    await asyncio.to_thread(btstate.wait_until_ready, 30.0, on_wait)


async def scan(timeout: float = 8.0) -> List[Tuple[BLEDevice, Optional[str], int, bool, str]]:
    """Return (device, name, rssi, looks like a watch, description) for everything advertising."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    result = []
    for device, adv in found.values():
        name = adv.local_name or device.name
        companies = list(adv.manufacturer_data)
        result.append((
            device, name, adv.rssi,
            looks_like_watch(name, adv.service_uuids, companies),
            describe_advertisement(adv.service_uuids, companies),
        ))
    result.sort(key=lambda item: (not item[3], -item[2]))
    return result


def connected_watches(name: Optional[str] = None) -> List[BLEDevice]:
    """Watches that macOS itself keeps connected.

    After the first connection macOS keeps the watch connected at the system
    level. It then stops advertising, scanning does not find it, and this
    query is the only way to reach it. Call from the event loop thread.
    Only works on macOS.
    """
    from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
    from CoreBluetooth import CBUUID

    manager = CentralManagerDelegate.alloc().init()
    services = [CBUUID.UUIDWithString_(uuid) for uuid in HUAMI_SERVICES]
    devices = []
    for peripheral in manager.central_manager.retrieveConnectedPeripheralsWithServices_(services):
        peripheral_name = peripheral.name()
        if name and not (peripheral_name and name.lower() in peripheral_name.lower()):
            continue
        # The same pair bleak stores for devices found by scanning.
        devices.append(BLEDevice(peripheral.identifier().UUIDString(), peripheral_name, (peripheral, manager)))
    return devices


def held_by_system(name: Optional[str] = None, address: Optional[str] = None) -> List[BLEDevice]:
    """Like connected_watches, but only logs a failed query and returns an empty list."""
    if sys.platform != "darwin":
        return []
    try:
        devices = connected_watches(name)
    except Exception as exc:
        log.debug("query for watches connected to the system failed: %s", exc)
        return []
    if address:
        devices = [device for device in devices if device.address.lower() == address.lower()]
    return devices


async def find_watch(name: Optional[str], address: Optional[str], timeout: float = 30.0) -> BLEDevice:
    held = held_by_system(name, address)
    if held:
        log.debug("watch %s is already held by macOS, no scan needed", held[0].address)
        return held[0]

    if address:
        device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise LinkError("device with address %s is not advertising" % address)
        return device

    def matches(device: BLEDevice, adv) -> bool:
        found_name = adv.local_name or device.name
        if not looks_like_watch(found_name, adv.service_uuids, list(adv.manufacturer_data)):
            return False
        return not name or bool(found_name and name.lower() in found_name.lower())

    # Ends as soon as the watch answers; it advertises only now and then, hence the long wait.
    device = await BleakScanner.find_device_by_filter(matches, timeout=timeout)
    if device is None:
        raise LinkError(
            "the watch did not answer within %.0f s. It keeps only one connection: make sure "
            "Bluetooth is off on your phone (do not unpair it in Zepp), wake up the watch "
            "display and try again." % timeout
        )
    return device


class BleLink:
    """A connected watch. Incoming packets are passed to on_packet(bytes)."""

    def __init__(self, device: BLEDevice, on_packet: Callable[[bytes], None]) -> None:
        self._client = BleakClient(device, disconnected_callback=self._on_disconnect)
        self._on_packet = on_packet
        self._write_with_response = False
        self._ack_with_response = False
        self.connected = False

    def _on_disconnect(self, _client) -> None:
        if self.connected:
            log.warning("the watch disconnected")
        self.connected = False

    async def open(self) -> None:
        await self._client.connect()
        services = self._client.services
        write_char = services.get_characteristic(CHAR_CHUNKED_WRITE)
        read_char = services.get_characteristic(CHAR_CHUNKED_READ)
        if write_char is None or read_char is None:
            await self._client.disconnect()
            raise LinkError(
                "the device has no Zepp OS data channel over Bluetooth LE. This firmware "
                "may require Bluetooth Classic, which this tool does not support."
            )
        self._write_with_response = "write-without-response" not in write_char.properties
        self._ack_with_response = "write-without-response" not in read_char.properties
        log.debug(
            "write: %s, read: %s, mtu %s",
            write_char.properties, read_char.properties, self._client.mtu_size,
        )
        await self._client.start_notify(read_char, self._notified)
        self.connected = True

    def _notified(self, _sender, data: bytearray) -> None:
        self._on_packet(bytes(data))

    async def write_packet(self, packet: bytes) -> None:
        await self._client.write_gatt_char(CHAR_CHUNKED_WRITE, packet, response=self._write_with_response)

    async def write_ack(self, packet: bytes) -> None:
        # Following Gadgetbridge, acknowledgements are written to the read characteristic.
        await self._client.write_gatt_char(CHAR_CHUNKED_READ, packet, response=self._ack_with_response)

    async def close(self) -> None:
        self.connected = False
        try:
            await self._client.disconnect()
        except Exception as exc:
            log.debug("disconnecting failed: %s", exc)
