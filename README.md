# Oreloj

*Esperanto for "clock" — a device that tells you where in the world you are.*

Oreloj is a hacked children's educational globe that plays live web radio from wherever you point the stylus. Tap France, hear FIP. Tap Brazil, hear Rádio Batuta. Tap somewhere you've never been and find out who's broadcasting.

It runs headless on a Raspberry Pi 3 A+ tucked inside or beside the globe, plays audio through a Bluetooth speaker or the Pi's 3.5mm jack, and has a web admin UI for managing stations. No screen. No keyboard. Just the globe and a pen.

---

## Hardware

- **Oregon Scientific Horizon Globe** — the globe itself (SONiX HID device, VID `0x0c45` / PID `0x7700`)
- **Raspberry Pi 3 A+** — runs everything; fits neatly alongside the globe's existing electronics
- **USB stylus** — the globe's original pen, connected to the Pi over USB; reports as a HID keyboard device
- **Bluetooth speaker** — for wireless audio; 3.5mm jack also works as a fallback

---

## How it works

The stylus works like a barcode scanner for geography. When you tap a country, the pen sends a sequence of six keystrokes — a code like `a0387f` for the United Kingdom or `a0499f` for Brazil. `globe.py` reads these codes via evdev (on the Pi) or pynput (on a Mac for development), looks up the country in its code map, and plays a stream.

Station selection follows a simple priority:

1. **Curated favourites** — stations marked as favourites in `stations.csv` for that country
2. **Any curated station** — a random pick from whatever's in `stations.csv` for that country
3. **Radio Browser API** — a fallback that queries [radio-browser.info](https://www.radio-browser.info/) for popular stations in that country, so even uncurated countries get something

Playback is handled by `mpv` running as a child process. When you tap a new country, the previous stream stops and the new one starts. A short TTS announcement (via `espeak-ng`) names the country before the music begins.

Tapping the same country twice within two seconds is ignored — the debounce prevents accidental double-taps.

---

## Features

**Curated station list** — `stations.csv` holds hand-picked stations from around the world: NTS, FIP, Rinse FM, Kiosk Radio, Radio Alhara, Rádio Batuta and ~100 more. Favourites get priority over other curated stations.

**Action zones** — certain pen codes can be mapped to actions instead of countries. Currently supported: `favourite_toggle` (marks or unmarks the playing station as a favourite, written back to `stations.csv`) and `stop`. Configured in `actions.json`.

**Web admin UI** — a Flask app at `http://oreloj.local:5000` lets you add, edit, and delete stations; toggle favourites; manage Bluetooth speakers; and connect to Wi-Fi. Changes to `stations.csv` automatically restart `globe.py` so they take effect immediately — no manual restart needed.

**Setup mode** — `oreloj-netwatch.py` (root service) watches the Wi-Fi. If the globe can't find a known network at boot, or loses it for 2 minutes, or the pen taps the "setup" spot, it opens a password-protected Wi-Fi (`Oreloj-1`, `Oreloj-2`…). Phones that join get the `/setup` page automatically (captive portal). Picking a network there is safe: if the password is wrong the globe rolls back and reopens the setup Wi-Fi, and it says every step out loud. A Wi-Fi can also be added from any computer by editing `oreloj-wifi.txt` on the SD card. Install: `sudo bash ~/globe/install-setup-mode.sh`.

**Sync from remote CSV** — the admin UI has a Sync button that fetches a remote `stations.csv` (configure `SYNC_URL` in `admin.py`) and merges any new stations into the local file, skipping duplicates by URL.

**Calibration mode** — run `python3 globe.py --calibrate` to discover pen codes for regions not yet in the map. Tap countries, get a summary on exit with ready-to-paste `CODE_MAP` snippets.

---

## Setup

Full step-by-step instructions are in [SETUP.md](SETUP.md). The short version:

```bash
# On the Pi (after flashing Raspberry Pi OS Lite 64-bit):
sudo apt install -y python3-pip python3-evdev mpv
sudo usermod -aG input oreloj

# Copy files to the Pi:
scp globe.py admin.py stations.csv actions.json 99-globe-hid.rules oreloj@oreloj.local:~/

# On the Pi — move files into place:
mkdir -p ~/globe
mv ~/globe.py ~/admin.py ~/stations.csv ~/actions.json ~/globe/
sudo mv ~/99-globe-hid.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# Install as systemd user services:
cp globe.service admin.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now globe admin
sudo loginctl enable-linger oreloj
```

For Bluetooth audio setup (phase 2), see the audio section in SETUP.md.

---

## Admin UI

Access at `http://oreloj.local:5000` from any device on the same network — or automatically via the captive portal when connected to the globe's setup Wi-Fi.

**Stations tab** — your full station list, searchable. Add a station with country, city, name, stream URL and an optional favourite flag. Edit or delete any entry inline. Heart icon toggles favourites. The Sync button pulls from a configured remote CSV.

**Bluetooth tab** — scan for nearby Bluetooth devices, pair them, connect or disconnect. Shows currently connected speakers in the status bar.

**Wi-Fi tab** — scan for available networks, connect with a password. When in hotspot mode, successfully connecting will close the hotspot and show reconnection instructions.

The status bar at the top shows whether a stream is currently playing, which Bluetooth speaker is connected, and which Wi-Fi network is active.

---

## File structure

| File | Purpose |
|------|---------|
| `globe.py` | Main script — reads pen codes, looks up countries, plays streams |
| `admin.py` | Flask web UI — station management, Bluetooth, Wi-Fi |
| `stations.csv` | Curated station list with country, city, URL and favourite flag |
| `actions.json` | Maps pen codes to actions (stop, favourite_toggle) |
| `globe.service` | systemd user service for globe.py |
| `admin.service` | systemd user service for admin.py |
| `99-globe-hid.rules` | udev rule to expose the pen as an evdev device |
| `oreloj-netwatch.py` | Wi-Fi watchdog: home Wi-Fi, or the password-protected setup Wi-Fi when lost |
| `oreloj-netwatch.service` | systemd unit for the watchdog (installed by `install-setup-mode.sh`) |
| `install-setup-mode.sh` | One-time installer: watchdog, captive portal, port 80, SD-card files |
| `setup.html` | Small phone page at `/setup`: pick Wi-Fi, pair a speaker |
| `oreloj-hotspot.sh` / `.service` | Legacy open hotspot (pre-setup-mode); stands down when netwatch is installed |
| `audio_setup.sh` | Configures PipeWire + Bluetooth audio sink |
| `SETUP.md` | Full setup instructions for a fresh Pi |

---

## Useful commands

```bash
ssh oreloj@oreloj.local

# Status
systemctl --user status globe
journalctl --user -u globe -f

# Restart after editing
systemctl --user restart globe

# Check pen is detected
python3 -c "import evdev; print([d.name for d in map(evdev.InputDevice, evdev.list_devices())])"

# Calibrate new codes
python3 ~/globe/globe.py --calibrate
```
