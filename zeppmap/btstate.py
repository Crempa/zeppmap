# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bluetooth state on a Mac, before the tool starts looking for the watch.

The bleak library waits for the system only a second and reports anything
other than "powered on" as Bluetooth being off. Here it waits longer (for
example for the user to answer the permission prompt) and says exactly why.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .errors import ZeppmapError

# CBManagerState values from CoreBluetooth.
STATE_UNKNOWN = 0
STATE_RESETTING = 1
STATE_UNSUPPORTED = 2
STATE_UNAUTHORIZED = 3
STATE_OFF = 4
STATE_ON = 5

_USE_TERMINAL = "run the command in the Terminal app"


class BluetoothUnavailable(ZeppmapError):
    pass


def explain(state: int, waited: float) -> str:
    if state == STATE_OFF:
        return "Bluetooth is off on this Mac. Turn it on and try again."
    if state == STATE_UNAUTHORIZED:
        return (
            "the application running the command is not allowed to use Bluetooth. Allow it in System "
            "Settings > Privacy & Security > Bluetooth, or %s." % _USE_TERMINAL
        )
    if state == STATE_UNSUPPORTED:
        return "the environment running the command does not offer Bluetooth LE; %s." % _USE_TERMINAL
    return (
        "macOS did not tell within %.0f s whether Bluetooth is available (it may well be on). "
        "Either a Bluetooth permission prompt is waiting on the screen: allow it and run the "
        "command again. Or the environment running the command has no access to Bluetooth: then %s."
        % (waited, _USE_TERMINAL)
    )


def wait_for_state(
    read_state: Callable[[], int],
    wait_for_change: Callable[[float], None],
    timeout: float,
    on_wait: Optional[Callable[[], None]] = None,
    notify_after: float = 2.0,
) -> None:
    """Wait until the system reports a final state. Returns only on "powered on"."""
    start = time.monotonic()
    notified = False
    while True:
        state = read_state()
        if state == STATE_ON:
            return
        elapsed = time.monotonic() - start
        if state not in (STATE_UNKNOWN, STATE_RESETTING) or elapsed >= timeout:
            raise BluetoothUnavailable(explain(state, elapsed))
        if on_wait is not None and not notified and elapsed >= notify_after:
            notified = True
            on_wait()
        wait_for_change(0.5)


_delegate_class = None


def make_delegate():
    """The object CoreBluetooth reports state changes to; it has a changed attribute (Event)."""
    global _delegate_class
    if _delegate_class is None:
        import objc
        from Foundation import NSObject

        # An Objective-C class may be created only once per process.
        class ZeppmapBluetoothStateDelegate(NSObject):
            ___pyobjc_protocols__ = [objc.protocolNamed("CBCentralManagerDelegate")]

            def centralManagerDidUpdateState_(self, _manager) -> None:
                self.changed.set()

        _delegate_class = ZeppmapBluetoothStateDelegate

    delegate = _delegate_class.alloc().init()
    delegate.changed = threading.Event()
    return delegate


def wait_until_ready(timeout: float = 30.0, on_wait: Optional[Callable[[], None]] = None) -> None:
    """Wait until macOS makes Bluetooth available, otherwise raise BluetoothUnavailable."""
    from CoreBluetooth import CBCentralManager
    from libdispatch import DISPATCH_QUEUE_SERIAL, dispatch_queue_create

    # 1 = restricted by device management, 2 = the user denied access.
    if CBCentralManager.authorization() in (1, 2):
        raise BluetoothUnavailable(explain(STATE_UNAUTHORIZED, 0.0))

    delegate = make_delegate()
    manager = CBCentralManager.alloc().initWithDelegate_queue_(
        delegate, dispatch_queue_create(b"zeppmap.btstate", DISPATCH_QUEUE_SERIAL)
    )

    def wait_for_change(seconds: float) -> None:
        delegate.changed.wait(seconds)
        delegate.changed.clear()

    try:
        wait_for_state(lambda: int(manager.state()), wait_for_change, timeout, on_wait)
    finally:
        manager.setDelegate_(None)
