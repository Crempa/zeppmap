# Instructions for AI assistants

This file is for AI coding assistants (Claude Code, Codex and similar) helping someone install and use zeppmap, or working on its code. The user-facing documentation is in Czech ([README.md](README.md), [docs/](docs/)); the code, comments and program output are in English.

## What the tool does

zeppmap adds hiking marks, cycle routes and cable cars from OpenStreetMap to the offline map of an Amazfit watch running Zepp OS. It connects to the watch over Bluetooth LE, makes the watch start its own Wi-Fi hotspot and FTP server, reads which map tiles the watch has, builds tiles with the marks merged into Zepp's contour layer, and uploads them. Tested on an Amazfit Balance 3 Ti with macOS on Apple Silicon. Linux and Windows are supported in the code but have never been tried with a real watch; see [Linux and Windows](#linux-and-windows-untested).

## Hard rules

- **Never handle the auth key in chat.** The user saves it to `~/.zeppmap-key` themselves (32 hex digits, `chmod 600`). Do not ask them to paste it to you, do not print it, do not put it in the repository, in commit messages, in issues or anywhere else. If it must be checked, run `check`, which only reports whether it is well-formed.
- **Never handle the user's Zepp account password.** The key is obtained with the third-party script huami-token, which logs into the Zepp account. The user runs it themselves, following its own instructions.
- **Do not delete `zepp-contours/` or `backups/`.** `zepp-contours/` holds the only copy of the watch's original contour tiles once they have been overwritten; every later merged tile is built from it, and it is how the original state is restored.
- **Dry run first.** Before the first real `sync`, run `sync --dry-run` and show the user what it would upload.
- **Do not delete map layers on the watch.** `rm` is only meant for test tiles; in the `scl`, `ctl` and `ski` layers it removes a single tile at most, and even that only when the user asks for it.
- **Mind the public Overpass servers.** A whole country is close to 1 GB of downloads. Do not run `sync --refresh` repeatedly; the cache keeps routes for 30 days.

## Setup (macOS)

Run in the repository root:

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
```

```bash
brew install openjdk
```

```bash
mkdir -p tools && curl -L -o tools/mkgmap.zip https://www.mkgmap.org.uk/download/mkgmap-r4924.zip && unzip -q tools/mkgmap.zip -d tools && rm tools/mkgmap.zip
```

If `git` or `python3` is missing, macOS offers the developer tools when you run them; the user has to confirm that dialog. Homebrew installation also needs the user (it asks for their password).

Then ask the user to get the auth key with huami-token and save it to `~/.zeppmap-key`, and verify:

```bash
.venv/bin/python -m zeppmap check
```

`check` does not touch the watch. On the first run macOS asks whether the terminal app may use Bluetooth; the user has to allow it on screen. If you run inside an app without Bluetooth access, `check` says so; ask the user to run the command in the Terminal app instead.

## Using it

Before any command that talks to the watch, the user must turn Bluetooth off on their phone (the watch keeps only one connection) and wake up the watch display.

```bash
.venv/bin/python -m zeppmap services
```

```bash
caffeinate -i .venv/bin/python -m zeppmap sync --dry-run
```

```bash
caffeinate -i .venv/bin/python -m zeppmap sync
```

- Commands that transfer files switch the computer's Wi-Fi to the watch hotspot for a while and back again afterwards. Warn the user that their internet connection drops during that time.
- The first `sync` for a large area takes over an hour. Run it where the user can see the output (their terminal), not in a background job that may be cut off.
- After uploading, the user closes and reopens the map on the watch and turns on the contours layer. The ski layer should stay off.
- macOS keeps the watch connected afterwards. To return it to the phone: Bluetooth off on the Mac, on on the phone, then on again on the Mac once the watch has connected.
- To restore the original contours: `push zepp-contours --layer ctl`.

## Linux and Windows (untested)

Nobody has run zeppmap on Linux or Windows with a real watch yet. Tell the user so before they start, and ask them to report the result in the project's GitHub issues. Bluetooth (bleak), FTP and tile building are the same on every system; only joining the watch hotspot differs:

- Linux joins it with NetworkManager (`nmcli device wifi connect`) and deletes the connection profile afterwards. Systems without NetworkManager need `--manual-wifi`.
- Windows imports a temporary WLAN profile (`netsh wlan add profile`, WPA2 with a passphrase, deleted after the transfer) and connects with `netsh wlan connect`.
- `--manual-wifi` (before or after the command) skips the automatic join: zeppmap prints the hotspot name and password, the user joins that network themselves, and zeppmap continues once the watch FTP port answers (up to 5 minutes). Use it whenever the automatic join fails. The password printed there is a random one-off for that hotspot; it is fine for the user to read it, but do not copy it anywhere else.

Setup differences from the macOS steps above: install Java from the system's packages (on Windows, for example Eclipse Temurin from adoptium.net); on Windows the interpreter is `.venv\Scripts\python` instead of `.venv/bin/python`, and `tar -xf` unpacks the mkgmap zip if `unzip` is missing. `caffeinate` exists only on macOS; on other systems keep the computer from sleeping during a long `sync` some other way. `check` reports Linux and Windows as a NOTE (supported but untested) and does not check Bluetooth permissions there; `scan` shows whether the watch is found. The macOS-only parts are the Bluetooth permission check (`zeppmap/btstate.py`) and finding a watch the system already keeps connected (`ble.connected_watches`).

## Working on the code

```bash
.venv/bin/python -m pip install pyftpdlib
```

```bash
.venv/bin/python -m unittest discover -s tests
```

- Tests run without a watch: a simulated watch, a local FTP server, and real mkgmap compilation where Java and mkgmap are available. Keep them passing.
- `zeppmap/wifi.py` joins the hotspot per system (`networksetup`, `nmcli`, `netsh`). The Linux and Windows commands are tested only with `subprocess.run` replaced (`WifiJoinTest`). Never log or show the hotspot password in errors; only `--manual-wifi` prints it, on purpose.
- Comments, docstrings and program output are in English; identifiers too.
- The tile format and watch behaviour are documented in Czech in [docs/poznatky-o-hodinkach.md](docs/poznatky-o-hodinkach.md); the key constants carry English comments in `zeppmap/ctlmerge.py` and `zeppmap/routes.py`.
- `zeppmap/sync.py` hashes a description of each tile into `build/*/sync.json`. Changing that description (including its literal text) makes every tile rebuild and upload again; bump `BUILD_VERSION` only when that is intended.
- The project is AGPL-3.0-or-later because the Zepp OS protocol code is ported from Gadgetbridge.
