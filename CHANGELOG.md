# Changelog

All notable changes to Oreloj are documented here.
Format: [date] — what changed and why.

---

## 2026-10-25 — control page works offline, new repo address

### Fixed
- **Globe now draws with no internet** (`index.html`, `admin.py`, new `vendor/`) — the map library (d3), world outline, topojson helper, hls.js and the Space Mono / Syne fonts are served by the Pi itself via a new `/vendor/` route, with pinned CDN fallbacks. Before, all of these came from unpkg/jsdelivr/Google Fonts, so in hotspot setup mode (phone joined to the "Oreloj" network, no internet) the globe never appeared. (The 2026-05-25 attempt at this was reverted the same day and never had a Pi route.)
- **Old repo address removed** — the "update available" check and the Push-to-GitHub default still pointed at `n8b2bp67qj-collab/oreloj`; now `oreloj/oreloj.github.io`. A previously saved old name in the browser is upgraded automatically. SETUP.md clone URL, CLAUDE.md and USER_GUIDE.md fixed too.

---

## 2026-07-16 (part 2) — multi-globe support

### Changed
- **`PI_BASE` hostname gate** (`index.html`) — now treats *any* `*.local` hostname as "running on the Pi itself", so the same file works unchanged on `oreloj2.local`, `oreloj3.local`, … Previously only `oreloj.local` qualified; a second globe's UI would have talked to the first globe's API. Public GitHub Pages site still targets the primary globe.
- **`SETUP.md` rewritten** as a complete provisioning guide: admin UI + services, git clone instead of scp (enables the admin UI's `git pull` update path on fresh Pis), hotspot + bt-autoconnect units, unique-hostname rule for multiple globes, per-device vs shared data files.

---

## 2026-07-16 — full Community Radio Index import

### Added
- **65 more CRI stations** (`stations.csv`) — the remaining working, identity-verified streams from community-radio-index.com, mostly deepening already-covered countries: UK +13 (Resonance FM, Clyde Built, Subtle, Kindred, Sister Midnight…), Netherlands +6 (Echobox, Operator, Radio Worm's siblings), USA +8 (BFF.fm, KFAI, KDRT, KUAA, KUCR), New Zealand +4 (95bFM, RDU), France +6 (Tsugi, Radio Panik's cousins, Rinse France), Germany +6 (Radio X Frankfurt, Corax, radio.syg.ma)… Total: **285 stations**.
- Reform Radio, RadioActive.FM 88.6 and Lusophonica rescued with corrected stream URLs (CRI's were stale; confirmed via Radio Browser cross-reference).

### Rejected during verification (~40)
- CRI's own map shows 317 pins, but **105 have no stream in their API** (website-only listings) and **48 more are dead** — parity with their pin count is impossible for *playable* stations.
- Impostor streams caught by ICY identity checks: 5 stations pointing at Slovak "Fun Radio", 4 at Dutch "Ice Radio", plus Planet Wax→German Planet Radio, Hotel Radio Paris→Hotel Costes, Radio X Basel→Radio X Cagliari, "Disco Duro Venezuela"→Italian Euro Disco, and three stations sharing one anonymous msvdn CDN URL.

---

## 2026-07-15 (part 2) — world coverage

### Added
- **84 new countries covered** (`stations.csv`) — one verified station each, curated for the community/university/ad-free brief where available: Radio Študent Ljubljana (Europe's oldest student radio), Kanal 103 Skopje, Radio NABA→XO.FM Riga, USMARADIO San Marino, Yarmouk University FM Jordan, Campus 103.7 Malta, Shih Hsin University Taipei, Radio JKKNIU Bangladesh, GugakFM Seoul, Hà Nội Community Radio, Radio 1 Praha, Radio Ultra Moscow, Rondó Reykjavík, Radio 100,7 Luxembourg, Babel Montevideo, Radio Ndeke Luka Bangui, Femme Pawa Vanuatu, UpBeat Palau, Radio Kiribati… Total: 220 stations, 150 countries.
- Every candidate was **stream-probed AND identity-checked** — Radio Browser is full of impostors (one Quran stream registered under 28 countries; "Offshore Radio" claiming the Marshall Islands; Mexican Exa FM claiming Andorra/Angola). ~40 such fakes rejected.
- `COUNTRY_TO_ISO` (globe.py) + `STATION_COUNTRY_TO_ISO` (index.html) extended accordingly (73/82 new entries).

### Known gaps (no suitable/live stream found)
Samoa, Andorra, Afghanistan, American Samoa, Bahrain, Bhutan, Botswana, Congo-Brazzaville, Cyprus, Djibouti, Egypt, Fiji, Micronesia, Gabon, Gambia, Guinea, Eq. Guinea, India, Kuwait, Kazakhstan, Liberia, Lesotho, Montenegro, Monaco, Marshall Is., Mauritania, Nauru, PNG, Philippines, Qatar, Solomon Is., Singapore, Somalia, São Tomé, Eswatini, Tajikistan, Timor-Leste, Turkmenistan, Tonga, Tuvalu, Comoros — these fall back to the live Radio Browser lookup at tap time.

---

## 2026-07-15

### Added
- **22 new stations across 16 new countries** (`stations.csv`) — Austria, China (Hong Kong), Croatia, Finland, Hungary, Indonesia, Kenya, Lithuania, Norway, Romania, Serbia, South Africa, Switzerland, Turkey, UAE + fixes for Japan, Greece, Ukraine, Thailand. Sourced from community-radio-index.com, every stream verified live AND identity-checked via ICY metadata (8 CRI records pointed at the wrong station's stream and were rejected).
- **HLS playback** (`index.html`) — `.m3u8` streams now play in Chrome/Firefox via lazy-loaded hls.js (The Lot Radio, RNE Radio 3, Hong Kong Community Radio). Safari and Pi mpv already handled them natively.
- **COUNTRY_TO_ISO additions** (`globe.py`) — Mexico, Belize, Bahamas and "United States" were missing, so their CSV stations were silently skipped on the Pi; also added all new countries + spares (Iceland, Latvia, Jordan, Taiwan, Vietnam…). Website `STATION_COUNTRY_TO_ISO` extended to match.

### Fixed
- **10 dead stream URLs replaced** (`stations.csv`) — Rinse FM, The Lake Radio, Movement Radio, CKUT, La Salsera FM, Radio Nopal, Veneno, The Lot Radio, Radio 3 RNE, 20ft Radio. La Salsera + Veneno also had dead streams hardcoded in `BUILT_IN`.
- **Bangkok Community Radio** — country was "Unknown"; now Asia/Thailand/Bangkok with a real description.
- **Duplicate rows removed** — Belize ×3 (radio.garden cache-buster twins), Radio Habana Cuba (website-as-URL copy), KLPM FM, FM La Tribu.

### Removed
- **13 dead stations with no working replacement** — Pound & Yam, Poolsuite FM (expired signed URL), Drama Radio (malformed Vimeo URL), Inter FM 897, Boxout FM, Seoul Community Radio (moved to video-only), Radio JGM, Radio U, Radio Otoro, Radio Guarajambala, La Voz Del Niquel, Radio Punctum, Chwiti Web. Their countries keep coverage via other stations or the Radio Browser fallback. Also dropped from `BUILT_IN` where present.

---

## 2026-06-10

### Added
- **FM tuning static** (`globe.py`) — a soft, synthesized radio-static loop (`sounds/tuning.wav`, generated on first run) starts the instant a country is tapped and is cut by a watchdog the moment the stream actually produces audio. Covers the announce/connect gap like the website's white-noise effect.
- **Off-air announcement** (`globe.py`) — if the player dies or produces no audio within 12 s (webpage URL, 404, dead host), the globe says "This station seems to be off air" instead of failing silently, kills the stuck player and clears state.
- **Station health-check** (`check_stations.py`) — probes every `stations.csv` stream URL concurrently and flags webpages, HTTP errors, timeouts and non-audio responses. Exit code 1 when something is broken (cron-friendly). First run: 110/131 healthy.
- **Bluetooth auto-reconnect** (`bt-autoconnect.service` + `.timer`) — user systemd timer (every 2 min, 30 s after boot) connects any paired-but-disconnected speaker. The Pi no longer waits for the speaker to initiate.

### Changed
- **Re-tap behaviour** (`globe.py`) — tapping the country already playing no longer re-announces its name or restarts the stream: it cycles to another of that country's stations (favourites first); with a single station it just keeps playing.
- **BT pairing is verified** (`admin.py`) — `/api/bt/pair` re-checks `Paired: yes` via `bluetoothctl info` (pair output alone can lie — Bose drops bonds silently when its device list is full), reports honestly, and auto-rebuilds the PipeWire combine sink with all paired speakers so newly paired speakers receive audio without manual config. `/api/bt/connect` does the same for speakers paired outside the UI.

### Fixed
- **Radio Alhara stream URL** (`stations.csv`) — `radioalhara.net/stream` is a webpage; replaced with the real RadioJar stream.

### Device-side (documented for reference, not in repo)
- Wi-Fi: cloud-init's instance-id is pinned in `cmdline.txt` (`ds=nocloud;i=…`) — bump it there, not in `meta-data`, to re-apply `network-config`. The "Freebox Bestron " SSID ends with a real trailing space.
- Bluetooth audio on headless: `~/.config/wireplumber/wireplumber.conf.d/80-bluez-headless.conf` disables seat-monitoring; lightdm disabled (its greeter session's pipewire stole BT endpoints); rogue *system-level* `admin.service` disabled (it squatted port 5000 and made the user unit crash-loop).

---

## 2026-05-25

### Added
- **Volume persistence** (`globe.py`) — `_current_volume` is now saved to `volume.json` on every change and loaded at startup. Volume no longer resets to 80 after a service restart.
- **"Stream lost" detection** (`globe.py`) — the main loop now monitors mpv every second. If mpv exits unexpectedly (station offline, network drop), globe.py speaks "stream lost" via TTS and clears the playback state cleanly.
- **Local D3 + topojson** (`d3.min.js`, `topojson-client.min.js`) — downloaded and bundled with the project (273 KB + 7 KB). Globe map now renders fully offline without depending on unpkg CDN.

### Changed
- **`admin.service` converted to user systemd unit** — was a system service (`User=oreloj`, `WantedBy=multi-user.target`), now matches `globe.service` style (`%h` paths, `WantedBy=default.target`). Installed on Pi at `~/.config/systemd/user/admin.service`, enabled for boot, replaces the previous bare-process approach. Admin UI now auto-restarts on crash.
- **`index.html` script tags** — `<script src="d3.min.js">` and `<script src="topojson-client.min.js">` (local paths, no CDN).

---

## 2026-05-24

### Added
- **Country-tap cycling** (`globe.py`) — tapping a country now steps through stations in a fixed order instead of picking randomly. Order: presets matching that country (slot 1→6) → liked stations → other stations → random API fallback. Tapping a different country or waiting 25 s resets the sequence to the top. Each tap within the sequence plays the next station ("channel button" behaviour).
- **`build_country_playlist()`** (`globe.py`) — new function that builds the ordered playlist for a tap, resolving preset-to-country via stored `iso` field or URL/name matching against `stations.csv`.
- **`DELETE /api/stations/by-key`** (`admin.py`) — new endpoint to remove a station by stable key (stream URL, or name if URL is empty). Used by the website when deleting stations so the Pi stays in sync.
- **`piDelete()` helper** (`index.html`) — mirrors `piPost()` but sends a DELETE with a JSON body. Used by station deletion and future key-based removes.
- **Pi-authoritative sync (`pollPi`)** (`index.html`) — replaces the one-shot `syncFromPi()`. Runs once on load and every 20 seconds. Reconciles local custom stations against Pi using a `_piOrigin` tag: adds Pi-side additions, updates changed fields, removes Pi-side deletions, and pushes locally-added stations up to the Pi on reconnect.

### Changed
- **`DEBOUNCE_SEC` 2.0 → 0.5** (`globe.py`) — allows deliberate "next station" taps (~1 s apart) while still suppressing the pen's accidental double-reads from a single physical touch. Added `CYCLE_RESET_SEC = 25`.
- **`POST /api/stations` is now an upsert** (`admin.py`) — matches by URL Link first, then Radio Station name. Updates the existing row in place if found; appends only if new. Eliminates duplicate rows from repeated syncs. Returns `{ok, added, updated}`.
- **`PUT /api/presets/<slot>`** (`admin.py`) — now accepts optional `iso` field (ISO 3166-1 alpha-2 country code), stored in `actions.json` so `globe.py` can match presets to countries without URL scanning.
- **`saveStation()` pushes all edits to Pi** (`index.html`) — previously only new stations were sent to the Pi. Now edits to custom stations and built-in overrides are also upserted via `POST /api/stations`.
- **`deleteCurrentEdit()` deletes from Pi** (`index.html`) — calls `DELETE /api/stations/by-key` so deletions made on the website propagate to the Pi's `stations.csv`.
- **Preset modal captures country code** (`index.html`) — when a station is selected from the Radio Browser search in the preset modal, `s.countrycode` is stored as `_pmIso` and included in the `PUT /api/presets/<slot>` body as `iso`.

---

## 2026-05-21

### Added
- **"Update available" banner** — when `oreloj.local:5000` is open, the UI silently checks GitHub's latest commit date against the Pi's deployment timestamp (admin.py file mtime). If GitHub is ahead, a `▲ update available` banner appears at the top of the sidebar with an "update now" button. Only visible in Globe Owner tier.
- **`calibration.csv` added to GitHub repo** — this file maps 316 pen hex codes to countries and is required by `globe.py` at startup. It was missing from the repo (and from the Pi). Now tracked in git and must be included in Pi deploys.
- **`build_time` field in `/api/status`** — `admin.py` now returns its own file mtime as an ISO-8601 UTC string. Used by the update banner to detect stale deployments.

### Changed
- **`.gitignore` updated** — added `.claude/` (Claude Cowork session data), `*.skill` (Claude plugin files), and `favourites.json` (runtime data that changes with use).

### Fixed
- **Pi was running 6-day-old `globe.py`** — local May 20 versions of `globe.py`, `admin.py`, and `index.html` deployed to Pi and GitHub.

---

## 2026-05-20

### Changed
- `globe.py` — pen code handling, CODE_MAP updates
- `admin.py` — API and admin UI improvements
- `index.html` — UI updates (3200+ line unified public + admin interface)

---

## 2026-05-19

### Added
- `99-globe-hid.rules` — udev rules for HID access without root
- `README.md` — project documentation

### Changed
- `globe.py` — cross-platform support (macOS pynput + Linux evdev)
- `admin.py` — Flask admin server with REST API

---

## 2026-05-15

### Added
- `audio_setup.sh` — PulseAudio/PipeWire combined sink setup (3.5mm + Bluetooth simultaneously)
- `globe.service` / `admin.service` — systemd service files
- `oreloj-hotspot.sh` / `oreloj-hotspot.service` — captive portal for Wi-Fi setup

---

## 2026-05-14

### Added
- Initial commit — `index.html`, `globe.py`, `admin.py`, `stations.csv`, `actions.json`
- Pi standalone build working: tap pen → country detected → stream plays via mpv
