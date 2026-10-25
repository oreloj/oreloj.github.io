# Oreloj — Provisioning a Globe Pi

How to turn a blank Raspberry Pi into a working Oreloj globe.
Works for the first globe **and** any additional ones.

Hardware: Pi 3 A+ (or similar), headless. User: `oreloj` on every globe.

---

## Naming rule (important with several globes)

Every globe on the same network needs a **unique hostname** — think of it as
a street address: two houses with the same address means the mail goes to the
wrong one. The first globe is `oreloj`; name the next ones `oreloj2`,
`oreloj3`, …

Each globe is then reached at its own address:

| Globe | SSH | Admin UI |
|---|---|---|
| oreloj | `ssh oreloj@oreloj.local` | http://oreloj.local:5000 |
| oreloj2 | `ssh oreloj@oreloj2.local` | http://oreloj2.local:5000 |

Everything else in this guide is identical for every globe.

> `index.html` (2026-07-16 or later) auto-detects which globe it is served
> from — no per-globe edits needed. The public GitHub Pages site always
> talks to the primary globe (`oreloj.local`).

---

## 1. Flash the SD card

Use **Raspberry Pi Imager** → **Raspberry Pi OS Lite (64-bit)**.
In the customisation panel (gear icon) set:

- Hostname: `oreloj` (or `oreloj2`, `oreloj3`, … — see naming rule)
- Username: `oreloj`
- WiFi SSID + password
- Enable SSH (password auth is fine)

> **Note:** this image uses cloud-init, not `firstrun.sh`.
> Settings live in `/boot/bootfs/user-data` if you need to edit them after flashing.
> `openssh-server` must be explicitly listed in the packages section — it is **not** installed by default.

---

## 2. First boot — install dependencies

Open a **new Terminal window on your Mac** and connect
(replace `oreloj.local` with the new globe's name, e.g. `oreloj2.local`):

```bash
ssh oreloj@oreloj.local
```

Everything below runs **on the Pi, inside that ssh session**:

```bash
sudo apt update && sudo apt install -y \
    git \
    python3-pip \
    python3-evdev \
    python3-requests \
    python3-flask \
    mpv \
    espeak-ng
```

(`espeak-ng` is the text-to-speech voice that announces the country when you tap.)

(Bluetooth/PipeWire packages come later, in the audio step.)

---

## 3. Add user to the input group

This lets `globe.py` read the pen without root:

```bash
sudo usermod -aG input oreloj
# Log out and back in for the group to take effect:
exit
ssh oreloj@oreloj.local
```

---

## 4. Get the project files

The repo is public — clone it straight onto the Pi (no token needed):

```bash
git clone https://github.com/oreloj/oreloj.github.io.git ~/globe
```

Because `~/globe` is a git clone, the admin UI's **update** feature
(`git pull` + service restart) works out of the box — future updates
don't need scp at all.

Install the pen's udev rule:

```bash
sudo cp ~/globe/99-globe-hid.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

---

## 5. Test playback manually

Plug the pen's USB into the Pi, then:

```bash
python3 ~/globe/globe.py
```

Tap a country — you should hear a stream within a few seconds on the 3.5mm jack.

### Calibration check

`calibration.csv` and `actions.json` from the repo already map the pen codes
for the SmartGlobe Horizon SG0218-12 — an identical globe model should work
as-is. To verify (or map a different sticker set):

```bash
python3 ~/globe/globe.py --calibrate
```

Tap countries and note any `⚠ NOT IN CODE_MAP` codes.
On Ctrl+C, an exit summary prints a ready-to-paste CODE_MAP snippet.

---

## 6. Install the services

Two user services (the radio itself + the admin web UI):

```bash
mkdir -p ~/.config/systemd/user
cp ~/globe/globe.service ~/globe/admin.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now globe admin

# Allow the services to run without a logged-in session:
sudo loginctl enable-linger oreloj
```

Two system units (WiFi hotspot fallback + Bluetooth speaker reconnect):

```bash
sudo cp ~/globe/oreloj-hotspot.service /etc/systemd/system/
sudo cp ~/globe/bt-autoconnect.service ~/globe/bt-autoconnect.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable oreloj-hotspot.service
sudo systemctl enable --now bt-autoconnect.timer
```

`oreloj-hotspot.sh` activates a NetworkManager profile named `hotspot` — it
does **not** create it. Create it once:

```bash
sudo nmcli con add type wifi ifname wlan0 con-name hotspot autoconnect no \
    ssid Oreloj 802-11-wireless.mode ap 802-11-wireless.band bg \
    ipv4.method shared ipv4.addresses 192.168.4.1/24
```

Check both user services are alive:

```bash
systemctl --user is-active globe admin     # expect: active active
```

Then open http://oreloj.local:5000 (or `oreloj2.local:5000`, …) in a browser —
you should see the globe interface.

---

## 7. Audio — combined 3.5mm + Bluetooth sink

```bash
sudo apt install -y pipewire-audio bluez bluetooth pulseaudio-utils alsa-utils

# Fresh Pi OS images ship with Bluetooth rfkill-blocked — unblock it once
# (persists across reboots). Without this, scan/pair fails with
# "PowerState: off-blocked".
sudo rfkill unblock bluetooth
bluetoothctl power on
```

(`pulseaudio-utils` provides `pactl`, needed by admin.py's combine-sink rebuild
and volume control; `alsa-utils` provides `amixer`.)

### Headless Bluetooth fix (required — do this before pairing)

On a headless Pi (no monitor, no logged-in desktop session), WirePlumber
refuses to take over Bluetooth audio because no "seat" is active. Disable
seat-monitoring for the Bluetooth monitor:

```bash
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cat > ~/.config/wireplumber/wireplumber.conf.d/80-bluez-headless.conf <<'EOF'
# Headless Pi: no logind seat is ever active, so BlueZ audio would never
# be picked up. Run the Bluetooth monitor unconditionally.
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF
systemctl --user restart wireplumber pipewire pipewire-pulse
```

Two more things that can silently steal or block Bluetooth audio — check both:

```bash
# 1. A display manager's greeter session runs its own pipewire, which
#    grabs the BT endpoints before the oreloj user can. Pi OS Lite has
#    none, but if lightdm (or another greeter) is installed, disable it:
systemctl is-enabled lightdm 2>/dev/null && sudo systemctl disable --now lightdm

# 2. There must be NO system-level admin.service — it would squat port
#    5000 and crash-loop the user unit. This must print nothing:
systemctl list-unit-files | grep admin.service
```

### Pair the speaker

```bash
# Pair your Bluetooth speaker:
bluetoothctl
  power on
  agent on
  scan on
  # wait for your speaker's MAC, e.g. AA:BB:CC:DD:EE:FF
  pair   AA:BB:CC:DD:EE:FF
  trust  AA:BB:CC:DD:EE:FF
  connect AA:BB:CC:DD:EE:FF
  quit

# Then run audio_setup.sh with the MAC:
bash ~/globe/audio_setup.sh AA:BB:CC:DD:EE:FF
```

Restart the service after audio is configured:
```bash
systemctl --user restart globe
```

---

## 8. How data flows between globes (multi-globe notes)

| File | Behaviour |
|---|---|
| `stations.csv` | Shared via GitHub. Every globe pulls the same list; the website's "Push to GitHub" merges the remote first, so globes don't clobber each other. |
| `actions.json`, `calibration.csv` | Same for identical globe models. On-device edits (admin UI) stay local until pushed. |
| `favourites.json` | **Per-globe, never synced.** Each globe keeps its own favourites. |

### Updating a globe later

Preferred: the update banner in the globe's own web UI (runs `git pull` and
restarts services). Or by hand:

```bash
ssh oreloj@oreloj2.local "cd ~/globe && git pull origin main && systemctl --user restart globe admin"
```

---

## Useful commands

| What | Command |
|------|---------|
| Check status | `systemctl --user status globe admin` |
| Live logs | `journalctl --user -u globe -f` |
| Restart after editing | `systemctl --user restart globe` |
| Check audio sinks | `pactl list short sinks` |
| Check pen is detected | `python3 -c "import evdev; print([d.name for d in map(evdev.InputDevice, evdev.list_devices())])"` |
