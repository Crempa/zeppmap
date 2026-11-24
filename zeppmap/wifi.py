# SPDX-License-Identifier: AGPL-3.0-or-later
"""Joining the watch Wi-Fi hotspot.

macOS uses networksetup and is tested with a real watch. Linux uses
NetworkManager (nmcli) and Windows netsh with a temporary WLAN profile; those
two are covered by unit tests only and have not been tried with a watch yet.
Where joining fails, the --manual-wifi option lets the user join the hotspot
themselves.

The hotspot password is passed to the system tools but never logged or shown
in error messages.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Iterable, List, Optional
from xml.sax.saxutils import escape

from .errors import ZeppmapError

MANUAL_HINT = "run the command with --manual-wifi and join the network yourself"


class WifiError(ZeppmapError):
    pass


def system() -> Optional[str]:
    """macos, linux or windows; None where joining the hotspot is not automated."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    return None


_JOIN_TOOLS = {"macos": "networksetup", "linux": "nmcli", "windows": "netsh"}


def join_tool() -> Optional[str]:
    """The system tool join() uses on this computer, or None where joining is not automated."""
    return _JOIN_TOOLS.get(system() or "")


def join(ssid: str, password: str, timeout: float = 90.0) -> str:
    """Join the network, retrying until it shows up.

    Returns the Wi-Fi interface name where it is known (macOS), otherwise an empty string.
    """
    current = system()
    if current == "macos":
        return _join_macos(ssid, password, timeout)
    if current == "linux":
        return _join_linux(ssid, password, timeout)
    if current == "windows":
        return _join_windows(ssid, password, timeout)
    raise WifiError("joining the watch hotspot is not automated on %s; %s" % (sys.platform, MANUAL_HINT))


def forget(ssid: str) -> None:
    """Remove the network profile join() created on Linux or Windows. Best effort, never raises.

    Each run uses a new random network name, so without this the profiles would pile up.
    On macOS it does nothing.
    """
    current = system()
    if current == "linux":
        command = ["nmcli", "connection", "delete", "id", ssid]
    elif current == "windows":
        command = ["netsh", "wlan", "delete", "profile", "name=" + ssid]
    else:
        return
    try:
        _run(command)
    except Exception:
        pass   # a profile left behind does no harm


# ---------------------------------------------------------------- macOS

def wifi_device() -> str:
    """Name of the Wi-Fi interface, usually en0."""
    out = subprocess.run(
        ["networksetup", "-listallhardwareports"], capture_output=True, text=True, check=True
    ).stdout
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("Hardware Port:") and ("Wi-Fi" in line or "AirPort" in line):
            for follow in lines[index + 1:index + 3]:
                if follow.startswith("Device:"):
                    return follow.split(":", 1)[1].strip()
    raise WifiError("this Mac has no Wi-Fi interface")


def _join_macos(ssid: str, password: str, timeout: float) -> str:
    device = wifi_device()
    deadline = time.monotonic() + timeout
    while True:
        result = subprocess.run(
            ["networksetup", "-setairportnetwork", device, ssid, password],
            capture_output=True, text=True,
        )
        # networksetup returns 0 even on failure and only prints the reason.
        message = _message(result, password)
        if result.returncode == 0 and not message:
            return device
        if time.monotonic() >= deadline:
            raise WifiError("could not join the %s network: %s" % (ssid, message or "unknown error"))
        time.sleep(3)


# ---------------------------------------------------------------- Linux and Windows (not tried with a watch)

def _run(command: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a system tool without raising on a non-zero exit; a missing tool becomes a WifiError."""
    if system() == "windows":
        kwargs.setdefault("encoding", "oem")   # netsh prints in the console code page
    try:
        return subprocess.run(command, capture_output=True, text=True, errors="replace", **kwargs)
    except FileNotFoundError:
        raise WifiError("%s was not found on this computer; %s" % (command[0], MANUAL_HINT)) from None


def _message(result: subprocess.CompletedProcess, password: str) -> str:
    """What the tool printed, with the password masked in case it ever echoes it."""
    text = ((result.stdout or "") + (result.stderr or "")).strip()
    return text.replace(password, "***") if password else text


def _join_linux(ssid: str, password: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while True:
        # nmcli waits until the connection is up, including the address from the watch.
        result = _run(["nmcli", "device", "wifi", "connect", ssid, "password", password])
        if result.returncode == 0:
            return ""
        if time.monotonic() >= deadline:
            raise WifiError("could not join the %s network with nmcli: %s. Or %s." % (
                ssid, _message(result, password) or "unknown error", MANUAL_HINT))
        # The hotspot may not be in the list of networks yet. A refused rescan (too soon after
        # the previous one) does not matter; nmcli also rescans by itself.
        _run(["nmcli", "device", "wifi", "rescan"])
        time.sleep(3)


# A profile for a WPA2 network with a pre-shared key. connectionMode manual keeps Windows from
# joining the network on its own later; forget() removes the profile after the transfer anyway.
_WLAN_PROFILE = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{name}</name>
  <SSIDConfig>
    <SSID>
      <name>{name}</name>
    </SSID>
  </SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM>
    <security>
      <authEncryption>
        <authentication>WPA2PSK</authentication>
        <encryption>AES</encryption>
        <useOneX>false</useOneX>
      </authEncryption>
      <sharedKey>
        <keyType>passPhrase</keyType>
        <protected>false</protected>
        <keyMaterial>{key}</keyMaterial>
      </sharedKey>
    </security>
  </MSM>
</WLANProfile>
"""

WLAN_PROFILE_FILE = "zeppmap-wifi.xml"


def wlan_profile(ssid: str, password: str) -> str:
    """The WLAN profile XML that netsh imports for the watch hotspot."""
    return _WLAN_PROFILE.format(name=escape(ssid), key=escape(password))


def _join_windows(ssid: str, password: str, timeout: float) -> str:
    # The profile holds the password in plain text: it is written to a private temporary
    # folder and deleted right after the import. netsh gets only the file name and runs in
    # that folder, so spaces in the folder path need no quoting.
    work = tempfile.mkdtemp(prefix="zeppmap-")
    try:
        with open(os.path.join(work, WLAN_PROFILE_FILE), "w", encoding="utf-8") as handle:
            handle.write(wlan_profile(ssid, password))
        result = _run(["netsh", "wlan", "add", "profile", "filename=" + WLAN_PROFILE_FILE], cwd=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if result.returncode != 0:
        raise WifiError("could not add a Wi-Fi profile for the %s network with netsh: %s. Or %s." % (
            ssid, _message(result, password) or "unknown error", MANUAL_HINT))

    deadline = time.monotonic() + timeout
    while True:
        # netsh only asks Windows to connect and returns; the caller then waits for the FTP port.
        result = _run(["netsh", "wlan", "connect", "name=" + ssid])
        if result.returncode == 0:
            return ""
        if time.monotonic() >= deadline:
            raise WifiError("could not join the %s network with netsh: %s. Or %s." % (
                ssid, _message(result, password) or "unknown error", MANUAL_HINT))
        # Until Windows scans again it does not know the hotspot. Listing the networks may make
        # it scan; if it does not, the next attempt simply waits for the periodic scan.
        _run(["netsh", "wlan", "show", "networks"])
        time.sleep(3)


# ---------------------------------------------------------------- waiting

def wait_for_internet(hosts: Iterable[str], timeout: float = 90.0) -> None:
    """Wait until one of the servers is reachable (after the hotspot stops, the computer returns to its Wi-Fi)."""
    hosts = list(hosts)
    deadline = time.monotonic() + timeout
    while True:
        for host in hosts:
            try:
                with socket.create_connection((host, 443), timeout=3):
                    return
            except OSError:
                continue
        if time.monotonic() >= deadline:
            raise WifiError(
                "the computer is not online (unreachable: %s). Connect it to Wi-Fi and run the command again."
                % ", ".join(hosts))
        time.sleep(2)


def wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection((host, port), timeout=3):
                return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise WifiError("%s:%d is not reachable: %s" % (host, port, exc)) from None
            time.sleep(1)
