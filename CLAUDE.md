# Oreloj — Claude Code Context

Oreloj is a hacked Oregon Scientific Horizon globe that plays live web radio from wherever you point the stylus. Tap a country, hear radio from that region. Built by Adrien. The globe is a product — it will be shipped to end users.

## Hardware

- **Globe:** Oregon Scientific Horizon Globe. USB stylus (SONiX HID, VID:0x0c45 PID:0x7700) acts as a barcode scanner — each tap sends a 6-char hex code (e.g. `a0387f` = UK).
- **Pi:** Raspberry Pi 3 A+, headless inside/beside the globe.
- **Audio:** Bluetooth speaker primary, 3.5mm jack fallback.

## Repo & live site

- **Repo:** https://github.com/oreloj/oreloj.github.io
- **Live site:** https://oreloj.github.io/
- **Pi interface:** http://oreloj.local:5000 — unified website + admin UI

## Editing & deploy model — READ BEFORE CHANGING ANYTHING

Two kinds of file, two different ownership rules. Mixing them up causes data loss.

- **Code & docs** (`index.html`, `globe.py`, `admin.py`, `*.service`, `CLAUDE.md`, `CHANGELOG.md`) — **the repo is the source of truth.** Edit in the repo, deploy repo→Pi + GitHub. Clean one-way push.
- **Runtime data** (`stations.csv`, `actions.json`, `calibration.csv`, `favourites.json`) — **the Pi is the source of truth.** These are edited *live on the device* via the admin UI at `oreloj.local:5000` (add stations, calibrate zones, set the 6 presets, assign pen codes). The repo's copies are usually STALE.
  - **Preferred way to change data:** do it in the admin UI, then use its **"Push to GitHub"** to sync (these are the "update stations via website" commits).
  - **If you must change data programmatically:** NEVER `scp` the repo copy onto the Pi — it silently wipes on-device stations/zones/presets/codes. **Reconcile instead:** pull the Pi's copy, apply only your specific change, verify on-device data survives, push the result to both Pi and GitHub. See the `oreloj-deploy` skill.
- **Deploy/restart:** both `globe.service` and `admin.service` are user systemd units (`systemctl --user restart <unit>`). Never `pkill` admin — it kills the managed service and (with `Restart=on-failure`) systemd won't bring it back.
- **Avoid drift at the root:** `index.html` also hardcodes a `BUILT_IN` station array that duplicates `stations.csv` — a known smell that spawns phantom stations. If touching station data heavily, prefer consolidating to one canonical source.

## Key files

| File | What it does |
|---|---|
| `index.html` | Single-file app (~3200 lines): public website AND Pi admin UI in one |
| `globe.py` | Pi main process: reads pen HID events, resolves country codes, plays via mpv, TTS via espeak-ng |
| `admin.py` | Flask on port 5000: serves index.html + REST API (stations, BT, Wi-Fi, zones, presets, volume, update) |
| `stations.csv` | Canonical station list on Pi |
| `actions.json` | Zone registry: pen code → action, with label/description/position + 6 preset slots |
| `favourites.json` | Favourite station names, shared between globe.py and admin.py |
| `globe.service` / `admin.service` | systemd services |
| `99-globe-hid.rules` | udev rules for HID access without root |
| `oreloj-hotspot.sh` | iptables + dnsmasq captive portal for Wi-Fi setup |

## Architecture

### Two Pi services, loosely coupled via shared files

**globe.py** reads pen codes via evdev (Pi) or pynput (Mac, for dev). Resolves against CODE_MAP (250+ entries). Station priority: favourites.json → curated → Radio Browser API. Plays via mpv + Unix socket. Actions dispatched from ACTION_MAP (volume up/down, discover, random love, stop, 6 presets, favourite_toggle).

**admin.py** serves `index.html` directly and exposes a REST API. Key endpoints: `/api/stations`, `/api/stations/json`, `/api/favourites`, `/api/zones`, `/api/presets`, `/api/bt/*`, `/api/wifi/*`, `/api/status`, `/api/volume`, `/api/sync`, `/api/update`. Calls `_notify_globe()` after data writes to hot-reload globe.py.

### index.html — unified interface

One file serves as both the public website (GitHub Pages) and the Pi admin UI (`oreloj.local:5000`). `PI_BASE` is `''` on `oreloj.local`/`localhost`, otherwise `http://oreloj.local:5000`. All Pi calls are fire-and-forget (2s timeout).

**3-tier access:**
- **Public** (default) — browse globe, play stations, localStorage favourites
- **Globe owner** (auto, Pi detected) — zones, presets, Bluetooth, WiFi, Update
- **Developer** (5-tap logo + PIN) — GitHub PAT config, data export, calibration tools

### Client-side data model (localStorage)

| Key | Contents |
|---|---|
| `rw_custom` | User-added stations |
| `rw_overrides` | Edits to BUILT_IN stations |
| `rw_hidden_builtin` | Deleted BUILT_IN names |
| `rw_favs` | Favourite names (cache; Pi is source of truth when connected) |
| `oreloj_dev_pin` | SHA-256 hash of developer PIN |
| `oreloj_gh_token` | GitHub PAT |
| `oreloj_gh_repo` | GitHub repo (default: `oreloj/oreloj.github.io`) |

### actions.json schema

```json
{
  "zones": [{"code": "a0337f", "action": "favourite_toggle", "label": "Zone A", "description": "", "position": null}],
  "presets": [{"slot": 1, "name": "", "url": "", "label": "Preset 1"}]
}
```
Codes starting with `TBD_` are skipped at runtime — assign real codes via calibration.

## Adding action zones (how-to)

1. Plug the globe pen into your Mac
2. In Terminal: `cd ~/Documents/Claude/Projects/Web\ Radio\ Interactive\ Globe && python3 globe.py --calibrate`
3. Tap any blank spot on the globe — a hex code prints in the terminal
4. Open http://oreloj.local:5000 → sidebar → **Zones** panel
5. Find the TBD zone you want (e.g. "Volume Up") → click **Edit** → paste the code
6. Write a label on the globe surface with a marker (safe — won't affect the infrared dot pattern)
7. Repeat for each zone

## Active development priorities

1. **Calibrate action zones** — assign real codes to the 10 TBD zones (volume up/down, discover, random love, presets 1–6)
2. **Set 6 presets** — open admin UI → Presets panel, assign stations to slots 1–6
3. **Push to GitHub** — delete `.git/index.lock` then `git add . && git commit -m "..." && git push`
4. **Add radio stations** — build up stations.csv before shipping

## Planned features

- **Onboarding flow** — first-time modal guiding Wi-Fi → Bluetooth setup
- **Zone positions on map** — show action icons on the globe SVG once zones are calibrated
- **Presets GitHub sync** — persist preset slots to GitHub

## Decisions made

- **Oregon Scientific Horizon** — not VTech, not VTech XL
- **No streaming calibration** — plug pen into laptop, copy-paste codes from terminal
- **localStorage for anonymous favs** — no backend needed for public users
- **Atomic CSV/JSON writes** — write to `.tmp` then `os.replace()` to prevent data loss
- **Debounce fix** — same-country re-tap uses `and` not `or`
- **admin.py serves index.html directly** — one URL for everything
- **3-tier access** — Public / Globe owner (auto) / Developer (PIN) — no accounts needed
- **favourites.json** — shared file between globe.py and admin.py, bootstrapped from CSV on first run
