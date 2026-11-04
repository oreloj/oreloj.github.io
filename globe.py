#!/usr/bin/env python3
# Globe Radio — Oregon Scientific Horizon Globe (SONiX HID, VID:0x0c45 PID:0x7700)
# Input: pynput keyboard listener using vk codes (layout-independent).
# Tap a country → play a curated or Radio Browser stream.
# Tap an action zone → execute action (e.g. favourite toggle) with TTS feedback.
# Usage: python3 globe.py [--calibrate]

import sys, csv, queue, re, time, signal, random, logging, subprocess, requests, platform, threading, json, tempfile, os, wave, math, struct
from pathlib import Path

OS = platform.system()   # 'Darwin' on Mac, 'Linux' on Pi

if OS == 'Darwin':
    from pynput import keyboard as kb

_VK_MAC: dict[int, str] = {
    0x00: 'a', 0x0B: 'b', 0x08: 'c', 0x02: 'd', 0x0E: 'e', 0x03: 'f',
    0x1D: '0', 0x12: '1', 0x13: '2', 0x14: '3', 0x15: '4',
    0x17: '5', 0x16: '6', 0x1A: '7', 0x1C: '8', 0x19: '9',
}
_VK_LINUX: dict[int, str] = {
    30: 'a', 48: 'b', 46: 'c', 32: 'd', 18: 'e', 33: 'f',
    11: '0',  2: '1',  3: '2',  4: '3',  5: '4',
     6: '5',  7: '6',  8: '7',  9: '8', 10: '9',
}
VK_TO_HEX: dict[int, str] = _VK_MAC if OS == 'Darwin' else _VK_LINUX
SEQ_TIMEOUT  = 0.3
_tap_queue: queue.Queue = queue.Queue()
_seq:   list[str] = []
_seq_t: float     = 0.0
SCRIPT_DIR   = Path(__file__).parent
STATIONS_CSV = SCRIPT_DIR / "stations.csv"
FAVS_PATH    = SCRIPT_DIR / "favourites.json"
ACTIONS_FILE = SCRIPT_DIR / "actions.json"
STATE_PATH   = SCRIPT_DIR / "state.json"
SOUNDS_DIR   = SCRIPT_DIR / "sounds"
CHIME_LIKE   = SOUNDS_DIR / "like.wav"
CHIME_UNLIKE = SOUNDS_DIR / "unlike.wav"
CHIME_VOL_UP   = SOUNDS_DIR / "vol_up.wav"
CHIME_VOL_DOWN = SOUNDS_DIR / "vol_down.wav"
TUNING_WAV     = SOUNDS_DIR / "tuning.wav"   # FM-static played while a stream loads
STREAM_START_TIMEOUT = 12.0  # noise safety stop; also the window to call a stream dead
API_SERVERS  = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]
MPV_SOCKET   = "/tmp/globe-mpv.sock"
MPV_CMD      = ["mpv", "--no-video", "--really-quiet", "--cache=yes", "--no-input-terminal",
                f"--input-ipc-server={MPV_SOCKET}"]
DEBOUNCE_SEC = 2.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("globe")

# ── Pen code → country/region map ────────────────────────────────────────────
# Loaded at startup from calibration.csv (same directory as this script).
# Each entry: [country_name, ISO_code, region_or_None, lat_or_None, lng_or_None]
CODE_MAP: dict[str, list[list]] = {}

CALIBRATION_CSV_PATH = SCRIPT_DIR / "calibration.csv"


def load_calibration() -> None:
    """Load pen code → country map from calibration.csv. Safe to call repeatedly."""
    global CODE_MAP
    if not CALIBRATION_CSV_PATH.exists():
        log.warning("calibration.csv not found — no pen codes loaded")
        return
    new_map: dict[str, list[list]] = {}
    try:
        with open(CALIBRATION_CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                code = row.get("code", "").strip()
                if not code:
                    continue
                lat = float(row["lat"]) if row.get("lat") else None
                lng = float(row["lng"]) if row.get("lng") else None
                entry = [
                    row.get("country_name", ""),
                    row.get("country_iso", ""),
                    row.get("region") or None,
                    lat,
                    lng,
                ]
                new_map.setdefault(code, []).append(entry)
        CODE_MAP = new_map
        log.info(f"Loaded {len(CODE_MAP)} pen codes from calibration.csv")
    except Exception as e:
        log.error(f"Failed to load calibration.csv: {e}")


# ── Action zones ──────────────────────────────────────────────────────────────
# Pen codes mapped to actions instead of stations.
# Loaded from actions.json at startup.
# Format: {"a0xxxf": "favourite_toggle", ...}
ACTION_MAP: dict[str, str] = {}
PRESET_MAP: dict[int, dict] = {}   # slot (1-6) → {name, url, label}
_current_volume: int = 80

def load_actions() -> None:
    if not ACTIONS_FILE.exists():
        return
    try:
        data = json.loads(ACTIONS_FILE.read_text(encoding="utf-8"))
        # New rich schema: {"zones": [...], "presets": [...]}
        if "zones" in data:
            loaded = 0
            for zone in data["zones"]:
                code   = zone.get("code", "")
                action = zone.get("action", "")
                if code and action and not code.startswith("TBD"):
                    ACTION_MAP[code] = action
                    loaded += 1
            log.info(f"Loaded {loaded} action zone(s) from zones list")
            # Load presets
            PRESET_MAP.clear()
            for preset in data.get("presets", []):
                slot = preset.get("slot")
                if isinstance(slot, int):
                    PRESET_MAP[slot] = {
                        "name":  preset.get("name", ""),
                        "url":   preset.get("url", ""),
                        "label": preset.get("label", f"Preset {slot}"),
                    }
            log.info(f"Loaded {len(PRESET_MAP)} preset slot(s)")
        else:
            # Legacy flat format: {"a0xxxf": "action", ...}
            ACTION_MAP.update(data)
            log.info(f"Loaded {len(data)} action zone(s) (legacy format)")
    except Exception as e:
        log.warning(f"Could not load actions.json: {e}")


COUNTRY_TO_ISO: dict[str, str] = {
    "UK": "GB", "United Kingdom": "GB", "Scotland": "GB-SCT",
    "Ireland": "IE", "France": "FR", "Belgium": "BE",
    "Netherlands": "NL", "Germany": "DE", "Portugal": "PT", "Spain": "ES",
    "Italy": "IT", "Poland": "PL", "Czechia": "CZ", "Denmark": "DK",
    "Sweden": "SE", "Estonia": "EE", "Ukraine": "UA", "Greece": "GR",
    "Georgia": "GE", "Palestine": "PS", "Ghana": "GH", "Nigeria": "NG",
    "India": "IN", "South Korea": "KR", "Japan": "JP", "Australia": "AU",
    "New Zealand": "NZ", "USA": "US", "Canada": "CA", "Brazil": "BR",
    "Argentina": "AR", "Chile": "CL", "Ecuador": "EC", "Colombia": "CO",
    "Peru": "PE", "Bolivia": "BO", "Paraguay": "PY", "Venezuela": "VE",
    "Suriname": "SR", "French Guyana": "GF", "French Guiana": "GF",
    "Guyana": "GY", "Syria": "SY", "Bali": "ID", "La Paz": "BO",
    "Thailand": "TH", "Indonesia": "ID", "Nicaragua": "NI", "Nicaragüa": "NI",
    "Panama": "PA", "Costa Rica": "CR", "Honduras": "HN", "Guatemala": "GT",
    "Jamaica": "JM", "Cuba": "CU", "Haiti": "HT",
    "Dominican Republic": "DO", "El Salvador": "SV", "Malaysia": "MY",
    "Mexico": "MX", "Belize": "BZ", "Bahamas": "BS", "United States": "US",
    "Austria": "AT", "Croatia": "HR", "Finland": "FI", "Hungary": "HU",
    "Kenya": "KE", "Lithuania": "LT", "Norway": "NO", "Romania": "RO",
    "Serbia": "RS", "South Africa": "ZA", "Switzerland": "CH", "Turkey": "TR",
    "United Arab Emirates": "AE", "UAE": "AE", "China": "CN", "Hong Kong": "CN",
    "Czech Republic": "CZ", "Philippines": "PH", "Singapore": "SG",
    "Iceland": "IS", "Latvia": "LV", "Belarus": "BY", "Kazakhstan": "KZ",
    "Jordan": "JO", "Lebanon": "LB", "Egypt": "EG", "Iran": "IR",
    "Pakistan": "PK", "Taiwan": "TW", "Vietnam": "VN", "Turtle Island": "US",
    "Albania": "AL", "Algeria": "DZ", "Antigua and Barbuda": "AG",
    "Armenia": "AM", "Azerbaijan": "AZ", "Bangladesh": "BD",
    "Barbados": "BB", "Benin": "BJ", "Bosnia and Herzegovina": "BA",
    "Brunei": "BN", "Bulgaria": "BG", "Burkina Faso": "BF",
    "Burundi": "BI", "Cambodia": "KH", "Cameroon": "CM",
    "Cape Verde": "CV", "Central African Republic": "CF", "Chad": "TD",
    "Dominica": "DM", "Eritrea": "ER", "Ethiopia": "ET",
    "Grenada": "GD", "Guinea-Bissau": "GW", "Iraq": "IQ",
    "Kiribati": "KI", "Kyrgyzstan": "KG", "Laos": "LA",
    "Libya": "LY", "Luxembourg": "LU", "Madagascar": "MG",
    "Malawi": "MW", "Maldives": "MV", "Mali": "ML",
    "Malta": "MT", "Mauritius": "MU", "Moldova": "MD",
    "Mongolia": "MN", "Morocco": "MA", "Mozambique": "MZ",
    "Myanmar": "MM", "Namibia": "NA", "Nepal": "NP",
    "Niger": "NE", "North Korea": "KP", "North Macedonia": "MK",
    "Oman": "OM", "Palau": "PW", "Russia": "RU",
    "Rwanda": "RW", "Saint Kitts and Nevis": "KN", "Saint Lucia": "LC",
    "Saint Vincent and the Grenadines": "VC", "San Marino": "SM", "Senegal": "SN",
    "Seychelles": "SC", "Sierra Leone": "SL", "Slovakia": "SK",
    "Slovenia": "SI", "South Sudan": "SS", "Sri Lanka": "LK",
    "Sudan": "SD", "Tanzania": "TZ", "Togo": "TG",
    "Trinidad and Tobago": "TT", "Tunisia": "TN", "Uganda": "UG",
    "Uruguay": "UY", "Uzbekistan": "UZ", "Vanuatu": "VU",
    "Vatican City": "VA", "Yemen": "YE", "Zambia": "ZM",
    "Zimbabwe": "ZW",
}


def _handle_keycode(vk: int) -> None:
    global _seq, _seq_t
    now = time.monotonic()
    ch  = VK_TO_HEX.get(vk)
    if ch is None:
        return
    if now - _seq_t > SEQ_TIMEOUT:
        _seq = []
    _seq.append(ch)
    _seq_t = now
    if len(_seq) == 6:
        _tap_queue.put(''.join(_seq))
        _seq = []
    elif len(_seq) > 6:
        _seq = _seq[-6:]


def _on_press(key) -> None:
    vk = getattr(key, 'vk', None)
    if vk is not None:
        _handle_keycode(vk)


PEN_VID, PEN_PID = 0x0c45, 0x7700

def _evdev_reader() -> None:
    import evdev
    while True:
        pen = None
        while pen is None:
            for path in evdev.list_devices():
                try:
                    d = evdev.InputDevice(path)
                    if d.info.vendor == PEN_VID and d.info.product == PEN_PID:
                        pen = d
                        break
                except Exception:
                    pass
            if pen is None:
                log.warning("Pen not found — retrying in 2 s…  (plug in the globe USB)")
                time.sleep(2)
        log.info(f"Pen: {pen.name}  ({pen.path})")
        try:
            pen.grab()
            for event in pen.read_loop():
                if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                    _handle_keycode(event.code)
        except Exception as e:
            log.error(f"Pen read error: {e} — reconnecting in 2 s…")
            time.sleep(2)


def _bootstrap_favs_from_csv() -> None:
    """On first run, seed favourites.json from the CSV 'Favourite' column."""
    if FAVS_PATH.exists():
        return
    if not STATIONS_CSV.exists():
        return
    favs: list[str] = []
    try:
        with open(STATIONS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("Favourite", "").strip().lower() == "yes":
                    name = row.get("Radio Station", "").strip()
                    if name:
                        favs.append(name)
        FAVS_PATH.write_text(
            __import__("json").dumps(favs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info(f"Bootstrapped favourites.json from CSV ({len(favs)} favourite(s))")
    except Exception as e:
        log.warning(f"Could not bootstrap favourites.json: {e}")


def _read_favs() -> set[str]:
    try:
        return set(__import__("json").loads(FAVS_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _write_favs(names: set[str]) -> None:
    import json as _json
    tmp = Path(str(FAVS_PATH) + ".tmp")
    tmp.write_text(_json.dumps(sorted(names), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FAVS_PATH)


def load_stations(path: Path) -> dict[str, list[dict]]:
    _bootstrap_favs_from_csv()
    if not path.exists():
        log.warning("stations.csv not found — API-only mode")
        return {}
    favs = _read_favs()
    stations: dict[str, list[dict]] = {}
    skipped = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            iso = COUNTRY_TO_ISO.get(row.get("Country", "").strip())
            if not iso:
                skipped += 1
                continue
            url = row.get("URL Link", "").strip()
            name = row.get("Radio Station", "").strip()
            stations.setdefault(iso, []).append({
                "name":      name,
                "url":       url,
                "favourite": name in favs,
            })
    total = sum(len(v) for v in stations.values())
    log.info(f"Loaded {total} stations across {len(stations)} countries ({skipped} skipped)")
    return stations


def pick_curated(stations: dict[str, list[dict]], iso: str,
                 exclude: str | None = None) -> dict | None:
    """Pick a station for the country, preferring favourites. With `exclude`
    (the currently playing station), prefer a different one so a re-tap
    cycles through the country's stations."""
    pool          = stations.get(iso, [])
    favs_with_url = [s for s in pool if s["favourite"] and s["url"]]
    any_with_url  = [s for s in pool if s["url"]]
    cands = favs_with_url or any_with_url
    if not cands:
        return None
    others = [s for s in cands if s["name"] != exclude]
    return random.choice(others or cands)


def get_stream_from_api(iso: str) -> str | None:
    iso = iso.upper()
    # Radio Browser has no country code for UK subdivisions — query by state instead.
    endpoint = ("bystateexact/Scotland" if iso == "GB-SCT"
                else f"bycountrycodeexact/{iso}")
    for server in API_SERVERS:
        try:
            r = requests.get(
                f"{server}/json/stations/{endpoint}",
                params={"hidebroken": "true", "order": "clickcount", "reverse": "true", "limit": 10},
                timeout=6,
            )
            pool = [s for s in r.json() if s.get("url_resolved")]
            if pool:
                pick = random.choice(pool[:5])
                log.info(f"  ♪  {pick['name']}  [Radio Browser]")
                return pick["url_resolved"]
            log.warning(f"  No API streams for {iso}")
            return None
        except Exception as e:
            log.warning(f"  {server} failed: {e}")
    log.error("  All API servers unreachable")
    return None


def resolve_stream(iso_list: list[str], curated: dict[str, list[dict]],
                   exclude: str | None = None) -> tuple[str | None, str | None]:
    """Returns (url, station_name)."""
    for iso in iso_list:
        s = pick_curated(curated, iso, exclude=exclude)
        if s:
            log.info(f"  ♪  {s['name']}  {'★ ' if s['favourite'] else ''}[curated]")
            return s["url"], s["name"]
    url = get_stream_from_api(iso_list[0])
    return url, None


# ── Dead-station memory + fallback list ───────────────────────────────────────
# Like a radio presenter with a running order: if a station doesn't come on
# air, move straight to the next one instead of announcing silence. Stations
# that failed recently go to the back of the queue for a while (streams often
# come back, so they're never dropped for good).
DEAD_PATH  = SCRIPT_DIR / "dead_stations.json"
DEAD_HOURS = 6
API_PICK   = "radio-browser:"       # placeholder candidate, resolved only if reached
_dead: dict[str, float] = {}
_dead_lock = threading.Lock()


def _load_dead() -> None:
    try:
        _dead.update(json.loads(DEAD_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass


def _save_dead() -> None:
    try:
        tmp = Path(str(DEAD_PATH) + ".tmp")
        tmp.write_text(json.dumps(_dead, indent=1), encoding="utf-8")
        tmp.replace(DEAD_PATH)
    except Exception as e:
        log.debug(f"Could not save dead stations: {e}")


def _is_dead(url: str) -> bool:
    t = _dead.get(url)
    return bool(t) and time.time() - t < DEAD_HOURS * 3600


def _mark_dead(url: str) -> None:
    with _dead_lock:
        _dead[url] = time.time()
        _save_dead()


def _mark_alive(url: str) -> None:
    with _dead_lock:
        if _dead.pop(url, None) is not None:
            _save_dead()


def station_candidates(iso_list: list[str], curated: dict[str, list[dict]],
                       exclude: str | None = None) -> list[tuple[str, str | None]]:
    """Running order for a country tap: favourites, then the other stations
    (each group shuffled), recently-dead ones after those, the station that's
    already playing (re-tap) after that, and Radio Browser as a last resort."""
    fresh, dead, current = [], [], []
    for iso in iso_list:
        pool = [s for s in curated.get(iso, []) if s.get("url")]
        favs = [s for s in pool if s.get("favourite")]
        rest = [s for s in pool if not s.get("favourite")]
        random.shuffle(favs)
        random.shuffle(rest)
        for st in favs + rest:
            item = (st["url"], st["name"])
            if exclude and st["name"] == exclude:
                current.append(item)
            elif _is_dead(st["url"]):
                dead.append(item)
            else:
                fresh.append(item)
    out, seen = [], set()
    for url, name in fresh + dead + current + [(API_PICK + iso_list[0], None)]:
        if url not in seen:
            seen.add(url)
            out.append((url, name))
    return out


def play_candidates(cands: list[tuple[str, str | None]], announce: bool = False) -> bool:
    """Start the first playable candidate; the rest become its fallbacks.
    False if nothing at all could be started."""
    cands = list(cands)
    while cands:
        url, name = cands.pop(0)
        if url.startswith(API_PICK):
            url, name = get_stream_from_api(url[len(API_PICK):]), None
            if not url:
                continue
        play(url, name, fallbacks=cands, announce=announce)
        return True
    return False


_mpv: subprocess.Popen | None = None
_noise: subprocess.Popen | None = None  # looping FM-static while a stream loads
_current_station: str | None = None  # name of currently playing station
_current_url: str | None = None      # URL of currently playing stream
_last_code:   str | None = None      # last pen code handled (mirrored to UI)
_last_action: str | None = None      # last action run, or None for a country tap


def _kill_stray_players() -> None:
    """Reap mpv players left over from a previous crashed run (stream and
    tuning-noise loop). Two radios must never play at once. Called once at
    startup, before this process spawns any player of its own."""
    subprocess.run(["pkill", "-f", MPV_SOCKET], capture_output=True)
    subprocess.run(["pkill", "-f", str(TUNING_WAV)], capture_output=True)


def _start_noise() -> None:
    """Start the looping tuning static (idempotent, best-effort)."""
    global _noise
    if (_noise and _noise.poll() is None) or not TUNING_WAV.exists():
        return
    try:
        _noise = subprocess.Popen(
            ["mpv", "--no-video", "--really-quiet", "--loop=inf", str(TUNING_WAV)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        _noise = None


def _stop_noise() -> None:
    global _noise
    if _noise and _noise.poll() is None:
        _noise.terminate()
        try:
            _noise.wait(timeout=1)
        except Exception:
            pass
    _noise = None


def _mpv_get(prop: str):
    """Read one property over the mpv IPC socket; None if unavailable."""
    try:
        import socket as _socket, json as _json
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(MPV_SOCKET)
            s.sendall((_json.dumps({"command": ["get_property", prop]}) + "\n").encode())
            for line in s.recv(4096).decode().splitlines():
                msg = _json.loads(line)
                if "data" in msg:
                    return msg["data"]
    except Exception:
        pass
    return None


def _watch_stream_start(proc: subprocess.Popen, url: str, name: str | None,
                        fallbacks: list, announce: bool) -> None:
    """Hold the tuning noise until audio actually flows, then cut it.
    If the player dies — or never produces audio by the deadline (webpage
    URL, dead host, 404) — announce it: a silent failure reads as 'the
    globe is broken' to the user."""
    global _current_station, _current_url
    deadline = time.monotonic() + STREAM_START_TIMEOUT
    while time.monotonic() < deadline:
        if _mpv is not proc:                    # a newer tap took over
            return
        if proc.poll() is None and (_mpv_get("time-pos") or 0) > 0:
            _stop_noise()                       # audio is flowing
            _mark_alive(url)
            if announce and name:
                speak(name)                     # a fallback came on air: say which one
            return
        if proc.poll() is not None:
            break                               # player died before producing audio
        time.sleep(0.3)
    if _mpv is not proc:
        return
    # Dead, or still silent at the deadline — either way this station failed.
    if proc.poll() is None:
        proc.terminate()
    _mark_dead(url)
    if fallbacks:
        with _play_lock:
            if _mpv is not proc:                # a newer tap took over meanwhile
                return
            log.warning(f"  ✗  {name or url} is off air — trying the next station")
            if play_candidates(fallbacks, announce=announce):
                return
    _stop_noise()
    _current_station = None
    _current_url = None
    _write_state()
    speak("This station seems to be off air")


_play_lock = threading.RLock()   # the pen and a fallback can both start a stream


def play(url: str, name: str | None = None, fallbacks: list | None = None,
         announce: bool = False) -> None:
    """Start a stream. `fallbacks` are tried in order if it doesn't come on
    air; with `announce`, a fallback that does says its station name."""
    global _mpv, _current_station, _current_url
    with _play_lock:
        _start_noise()   # idempotent — every play path gets the tuning static
        if _mpv and _mpv.poll() is None:
            _mpv.terminate(); _mpv.wait()
        _mpv = subprocess.Popen(MPV_CMD + [url], stdin=subprocess.DEVNULL)
        _current_station = name
        _current_url = url
        _write_state()
        threading.Thread(target=_watch_stream_start,
                         args=(_mpv, url, name, list(fallbacks or []), announce),
                         daemon=True).start()

def stop() -> None:
    global _mpv, _current_station, _current_url
    _stop_noise()
    if _mpv and _mpv.poll() is None:
        _mpv.terminate(); _mpv.wait()
    _mpv = None
    _current_station = None
    _current_url = None
    _write_state()


def _write_state() -> None:
    """Mirror the globe's live state to state.json so the admin UI can poll it.
    Best-effort and atomic — a failure here must never disrupt playback."""
    try:
        data = {
            "playing":     _mpv is not None and _mpv.poll() is None,
            "station":     _current_station,
            "volume":      _current_volume,
            "last_code":   _last_code,
            "last_action": _last_action,
            "updated_at":  time.time(),
        }
        tmp = Path(str(STATE_PATH) + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception as e:
        log.debug(f"Could not write state.json: {e}")


# ── Volume control via mpv IPC ────────────────────────────────────────────────
def _mpv_set_volume(vol: int) -> None:
    try:
        import socket as _socket, json as _json
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(MPV_SOCKET)
            s.sendall((_json.dumps({"command": ["set_property", "volume", vol]}) + "\n").encode())
    except Exception:
        pass


# ── TTS ───────────────────────────────────────────────────────────────────────
# The globe's voice: Piper "en_GB-alan-medium" (calm British male), slowed a
# little. Like a radio jingle library: most phrases are recorded in advance
# (voice/<VOICE_ID>/, made by tools/render_voice.py); anything new is recorded
# on the globe the first time (voice-cache/) and kept; espeak is the backup.
VOICE_ID        = "en_GB-alan-medium"
VOICE_SPEED     = 1.12                    # Piper length_scale: >1 = slower, calmer
VOICE_PACK_DIR  = SCRIPT_DIR / "voice" / VOICE_ID
VOICE_CACHE_DIR = SCRIPT_DIR / "voice-cache" / VOICE_ID
VOICE_MODEL     = SCRIPT_DIR / "voice-model" / f"{VOICE_ID}.onnx"


def voice_key(text: str) -> str:
    """File name for a phrase. MUST match tools/render_voice.py."""
    import hashlib
    norm = " ".join(text.split())
    return hashlib.sha1(f"{VOICE_ID}|{VOICE_SPEED}|{norm}".encode("utf-8")).hexdigest()[:16]


def _ensure_voice_pack() -> None:
    """Unpack voice/<VOICE_ID>.tar.gz (the recorded phrase library) if it is
    new or changed. One file to copy/update instead of hundreds."""
    import shutil, tarfile
    pack = SCRIPT_DIR / "voice" / f"{VOICE_ID}.tar.gz"
    if not pack.exists():
        return
    st = pack.stat()
    stamp_text = f"{st.st_size}:{int(st.st_mtime)}"
    stamp = VOICE_PACK_DIR / ".pack"
    try:
        if stamp.read_text() == stamp_text:
            return
    except OSError:
        pass
    try:
        tmp = VOICE_PACK_DIR.with_name(VOICE_PACK_DIR.name + ".new")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        with tarfile.open(pack, "r:gz") as tar:
            for m in tar.getmembers():
                name = Path(m.name).name          # flat files only, no paths
                if m.isfile() and (name.endswith(".ogg") or name == "manifest.json"):
                    data = tar.extractfile(m).read()
                    (tmp / name).write_bytes(data)
        (tmp / ".pack").write_text(stamp_text)
        shutil.rmtree(VOICE_PACK_DIR, ignore_errors=True)
        os.replace(tmp, VOICE_PACK_DIR)
        log.info(f"Voice pack unpacked: {sum(1 for _ in VOICE_PACK_DIR.glob('*.ogg'))} phrases")
    except Exception as e:
        log.warning(f"Could not unpack voice pack: {e}")


def _voice_file(text: str) -> Path | None:
    k = voice_key(text)
    for d in (VOICE_PACK_DIR, VOICE_CACHE_DIR):
        for ext in (".ogg", ".wav"):
            p = d / f"{k}{ext}"
            if p.exists():
                return p
    return None


def _voice_record(text: str) -> Path | None:
    """Record a new phrase with Piper on the globe (a few seconds on a Pi 3),
    keep it for next time. None if Piper or the voice model isn't installed."""
    if not VOICE_MODEL.exists():
        return None
    try:
        VOICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = VOICE_CACHE_DIR / f"{voice_key(text)}.wav"
        tmp = out.with_suffix(".tmp.wav")
        t0 = time.monotonic()
        r = subprocess.run(
            [sys.executable, "-m", "piper", "-m", str(VOICE_MODEL),
             "--length-scale", str(VOICE_SPEED), "-f", str(tmp), "--", " ".join(text.split())],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
            log.warning(f"Piper failed: {(r.stderr or '')[-200:]}")
            tmp.unlink(missing_ok=True)
            return None
        os.replace(tmp, out)
        log.info(f"Voice: recorded new phrase in {time.monotonic() - t0:.1f}s — {text!r}")
        return out
    except Exception as e:
        log.warning(f"Piper error: {e}")
        return None


def _tts(text: str) -> None:
    """Say something (blocking). Recorded phrase → Piper → espeak/say."""
    if not text or not text.strip():
        return
    f = _voice_file(text) or _voice_record(text)
    if f is not None:
        try:
            r = subprocess.run(["mpv", "--no-video", "--really-quiet", str(f)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
            if r.returncode == 0:
                return
        except Exception:
            pass
    try:
        cmd = ["say", text] if OS == 'Darwin' else ["espeak-ng", "-v", "en", "-s", "140", text]
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.warning(f"TTS not available (tried {'say' if OS == 'Darwin' else 'espeak-ng'})")


_speech_lock = threading.Lock()   # the pen and the network voice can both talk


def speak(text: str) -> None:
    """Blocking TTS with auto-dim: lowers mpv volume while speaking, then restores."""
    with _speech_lock:
        playing = _mpv is not None and _mpv.poll() is None
        if playing:
            _mpv_set_volume(15)
        _tts(text)
        if playing:
            _mpv_set_volume(_current_volume)


# ── Network voice: the globe says what its Wi-Fi is doing ─────────────────────
# oreloj-netwatch (root service) publishes its state + recent events to
# /run/oreloj/net.json; we read them aloud. Pen actions send it requests.
NET_RUN_DIR  = Path(os.environ.get("ORELOJ_RUN_DIR", "/run/oreloj"))
NET_STATE    = NET_RUN_DIR / "net.json"
NET_REQ_DIR  = NET_RUN_DIR / "requests"
_SPOKEN_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "oreloj-net-spoken"


def _read_net() -> dict | None:
    try:
        data = json.loads(NET_STATE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if time.time() - float(data.get("updated_at", 0)) < 180 else None


def _net_request(action: str, **fields) -> bool:
    try:
        name = f"{time.time_ns()}-globe"
        tmp = NET_REQ_DIR / f"{name}.tmp"
        tmp.write_text(json.dumps({"action": action, **fields}), encoding="utf-8")
        os.replace(tmp, NET_REQ_DIR / f"{name}.json")
        return True
    except Exception as e:
        log.warning(f"Could not send network request: {e}")
        return False


def _say_name(name: str) -> str:
    """'Oreloj-1' → 'Oreloj 1' so the voice doesn't say 'minus'."""
    return name.replace("-", " ").replace("_", " ").strip()


def _say_password(pw: str) -> str:
    """Make a password speakable: 'amber-falcon-427' → 'amber, dash, falcon,
    dash, 4 2 7'. Anything unusual is spelled letter by letter."""
    if re.fullmatch(r"[a-z]+(-[a-z0-9]+)*", pw):
        parts = []
        for chunk in pw.split("-"):
            parts.append(" ".join(chunk) if chunk.isdigit() else chunk)
        return ", dash, ".join(parts)
    out = []
    for ch in pw:
        if ch.isupper():
            out.append(f"capital {ch}")
        elif ch == "-":
            out.append("dash")
        elif ch == " ":
            out.append("space")
        else:
            out.append(ch)
    return ", ".join(out)


def _say_ip(ip: str) -> str:
    return ip.replace(".", " dot ")


def _setup_speech(net: dict, reason: str | None) -> str:
    name = _say_name(net.get("setup_ssid") or "Oreloj")
    if reason == "connect_failed":
        return f"Setup mode is back on. Join {name} again to try another password."
    intro = {"no_wifi":   "I can't find a Wi-Fi I know.",
             "wifi_lost": "I lost the Wi-Fi.",
             "manual":    "Setup mode."}.get(reason or "", "Setup mode.")
    msg = f"{intro} On your phone, join the Wi-Fi called {name}"
    if net.get("speak_password", True) and net.get("setup_password"):
        msg += f". The password is: {_say_password(net['setup_password'])}"
    return msg + "."


def _net_event_speech(ev: dict, net: dict) -> str | None:
    t, ssid = ev.get("type"), _say_name(str(ev.get("ssid") or ""))
    if ev.get("quiet"):
        return None
    if t == "setup":
        return _setup_speech(net, ev.get("reason"))
    if t == "connecting":
        return f"Joining {ssid}."
    if t == "connected":
        return f"Connected to {ssid}. All good."
    if t == "online":
        return f"Back online, on {ssid}." if ssid else "Back online."
    if t == "connect_failed":
        why = ev.get("why")
        if why == "wrong_password":
            return f"I couldn't join {ssid}. The password seems wrong."
        if why == "not_found":
            return f"I can't find a Wi-Fi called {ssid}."
        if why == "invalid":
            return "That Wi-Fi name or password doesn't look right."
        return f"I couldn't join {ssid}."
    if t == "wifi_added":
        return f"Found a new Wi-Fi on the memory card: {ssid}."
    if t == "wifi_file_invalid":
        return "The Wi-Fi written on the memory card doesn't look right."
    return None


def _net_voice_loop() -> None:
    """Speak every new network event once (survives globe.py restarts)."""
    try:
        last_sess, last_id = _SPOKEN_PATH.read_text().split(":")
        last_id = int(last_id)
    except Exception:
        last_sess, last_id = "", 0
    while True:
        net = _read_net()
        if net:
            sess = str(net.get("session", ""))
            for ev in net.get("events") or []:
                eid = int(ev.get("id", 0))
                if sess == last_sess and eid <= last_id:
                    continue
                last_sess, last_id = sess, eid
                try:
                    _SPOKEN_PATH.write_text(f"{sess}:{eid}")
                except Exception:
                    pass
                if time.time() - float(ev.get("at", 0)) > 300:
                    continue          # old news (e.g. globe.py restarted much later)
                text = _net_event_speech(ev, net)
                if text:
                    log.info(f"Network: {text}")
                    speak(text)
        time.sleep(2)


def _local_ip() -> str:
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                             timeout=3).stdout.split()
        return next((a for a in out if "." in a), "")
    except Exception:
        return ""


def action_setup_mode() -> None:
    if not _read_net():
        speak("Setup mode isn't installed on this globe yet.")
        return
    if _net_request("setup"):
        speak("One moment.")


def action_say_address() -> None:
    net = _read_net() or {}
    if net.get("mode") in ("setup", "offline"):
        speak(_setup_speech(net, "manual") +
              f" If no page opens, go to {_say_ip(net.get('setup_ip') or '192.168.4.1')}.")
        return
    host = __import__("socket").gethostname().split(".")[0]
    ip = net.get("ip") or _local_ip()
    where = f"I'm on {_say_name(net['ssid'])}. " if net.get("ssid") else ""
    if ip:
        speak(f"{where}Open {host} dot local in a browser, or {_say_ip(ip)}.")
    else:
        speak("I'm not connected to any Wi-Fi.")


def action_pair_speaker() -> None:
    """Runs in a thread: scanning + pairing takes up to a minute."""
    speak("Looking for a speaker. Put it in pairing mode.")
    try:
        r = requests.post("http://127.0.0.1:5000/api/bt/pair-nearest", timeout=120)
        res = r.json()
    except Exception as e:
        log.warning(f"pair-nearest failed: {e}")
        res = {"ok": False}
    if res.get("ok"):
        speak(f"Connected to {res.get('name') or 'the speaker'}.")
    elif "busy" in str(res.get("msg", "")):
        speak("Still looking.")
    else:
        speak("I didn't find a speaker in pairing mode.")


def _write_tone_wav(path, notes, note_dur=0.13, rate=22050, vol=0.5) -> None:
    """Synthesize a short multi-note sine 'chime' WAV (stdlib only)."""
    frames = bytearray()
    for freq in notes:
        n = int(rate * note_dur)
        for i in range(n):
            env = min(1.0, i / (0.01 * rate), (n - i) / (0.02 * rate))  # de-click ramps
            s = vol * env * math.sin(2 * math.pi * freq * (i / rate))
            frames += struct.pack("<h", int(s * 32767))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(bytes(frames))


def _write_noise_wav(path, dur=1.5, rate=22050, vol=0.18) -> None:
    """Synthesize a loopable FM-static WAV: lowpassed white noise reads as
    'between stations' hiss rather than harsh digital noise (stdlib only)."""
    frames = bytearray()
    prev = 0.0
    for _ in range(int(rate * dur)):
        prev = 0.6 * prev + 0.4 * random.uniform(-1.0, 1.0)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, vol * prev)) * 32767))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(bytes(frames))


def _ensure_chimes() -> None:
    """Generate the like/unlike chimes once if they don't exist yet."""
    try:
        SOUNDS_DIR.mkdir(exist_ok=True)
        if not CHIME_LIKE.exists():
            _write_tone_wav(CHIME_LIKE, [880, 1320])    # ascending = liked
        if not CHIME_UNLIKE.exists():
            _write_tone_wav(CHIME_UNLIKE, [660, 440])   # descending = unliked
        if not CHIME_VOL_UP.exists():
            _write_tone_wav(CHIME_VOL_UP, [988, 1319], note_dur=0.07)    # short ascending blip
        if not CHIME_VOL_DOWN.exists():
            _write_tone_wav(CHIME_VOL_DOWN, [1319, 988], note_dur=0.07)  # short descending blip
        if not TUNING_WAV.exists():
            _write_noise_wav(TUNING_WAV)
    except Exception as e:
        log.warning(f"Could not generate chimes: {e}")


def _play_wav(path) -> None:
    """Play a short WAV (blocking) via a short-lived mpv."""
    if not path.exists():
        return
    try:
        subprocess.run(["mpv", "--no-video", "--really-quiet", str(path)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass


def cue_favourite(new_fav: bool) -> None:
    """Chime, then speak the word — ducking the radio for the whole cue."""
    playing = _mpv is not None and _mpv.poll() is None
    if playing:
        _mpv_set_volume(15)
    _play_wav(CHIME_LIKE if new_fav else CHIME_UNLIKE)
    _tts("Liked" if new_fav else "Unliked")
    if playing:
        _mpv_set_volume(_current_volume)


def cue_volume(up: bool) -> None:
    """Pi: short blip over the radio (no ducking). Mac dev: spoken word."""
    if OS == 'Darwin':
        speak("volume up" if up else "volume down")
        return
    _play_wav(CHIME_VOL_UP if up else CHIME_VOL_DOWN)


# ── Favourite toggle ──────────────────────────────────────────────────────────
def toggle_favourite(curated: dict[str, list[dict]]) -> None:
    """Toggle favourite for the currently playing station in favourites.json."""
    if not _current_station:
        speak("Nothing is playing")
        log.info("Favourite toggle: nothing playing")
        return

    name = _current_station
    # Only allow toggling stations that are known in curated
    known = any(s["name"] == name for pool in curated.values() for s in pool)
    if not known:
        speak("Station not in list")
        log.info(f"Favourite toggle: '{name}' not found in curated stations")
        return

    try:
        favs = _read_favs()
        new_fav = name not in favs
        if new_fav:
            favs.add(name)
        else:
            favs.discard(name)
        _write_favs(favs)

        # Keep CSV Favourite column in sync so admin.py reads correctly
        if STATIONS_CSV.exists():
            try:
                rows = []
                with open(STATIONS_CSV, newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames
                    for row in reader:
                        if row.get("Radio Station", "").strip() == name:
                            row["Favourite"] = "yes" if new_fav else ""
                        rows.append(row)
                tmp = STATIONS_CSV.with_suffix(".tmp")
                with open(tmp, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    w.writerows(rows)
                tmp.replace(STATIONS_CSV)
            except Exception as csv_e:
                log.warning(f"CSV sync failed (non-fatal): {csv_e}")

        # Update in-memory curated dict
        for pool in curated.values():
            for s in pool:
                if s["name"] == name:
                    s["favourite"] = new_fav

        log.info(f"★  {name} → {'favourite' if new_fav else 'not favourite'}")
        cue_favourite(new_fav)

    except Exception as e:
        log.error(f"Favourite toggle error: {e}")
        speak("Error")


# ── Action dispatcher ─────────────────────────────────────────────────────────
def dispatch_action(action: str, curated: dict[str, list[dict]]) -> None:
    global _current_volume

    if action == "favourite_toggle":
        toggle_favourite(curated)

    elif action == "stop":
        stop()
        speak("Stopped")

    elif action == "volume_up":
        _current_volume = min(100, _current_volume + 10)
        _mpv_set_volume(_current_volume)
        cue_volume(True)
        log.info(f"Volume → {_current_volume}")

    elif action == "volume_down":
        _current_volume = max(0, _current_volume - 10)
        _mpv_set_volume(_current_volume)
        cue_volume(False)
        log.info(f"Volume → {_current_volume}")

    elif action == "discover":
        # Pick from all curated stations that have a URL
        all_stations: list[dict] = [
            s for pool in curated.values() for s in pool if s.get("url")
        ]
        if not all_stations:
            speak("no stations available")
            return
        random.shuffle(all_stations)
        all_stations.sort(key=lambda s: _is_dead(s["url"]))      # recently dead last
        station = all_stations[0]
        log.info(f"Discover pick: {station['name']}")
        play(station["url"], station.get("name"),
             fallbacks=[(s["url"], s["name"]) for s in all_stations[1:4]], announce=True)
        speak(station.get("name") or "discover")

    elif action == "random_love":
        # Pick a random station from favourites that has a URL
        fav_stations: list[dict] = [
            s for pool in curated.values() for s in pool
            if s.get("url") and s.get("favourite")
        ]
        if not fav_stations:
            speak("no favourites yet")
            return
        random.shuffle(fav_stations)
        fav_stations.sort(key=lambda s: _is_dead(s["url"]))      # recently dead last
        station = fav_stations[0]
        log.info(f"Random love pick: {station['name']}")
        play(station["url"], station.get("name"),
             fallbacks=[(s["url"], s["name"]) for s in fav_stations[1:4]], announce=True)
        speak(station.get("name") or "favourite")

    elif action.startswith("preset_"):
        try:
            slot = int(action.split("_")[1])
        except (IndexError, ValueError):
            log.warning(f"Malformed preset action: {action}")
            return
        preset = PRESET_MAP.get(slot)
        if not preset or not preset.get("url"):
            speak("preset not set")
            log.info(f"Preset {slot}: not configured")
            return
        label = preset.get("label") or f"Preset {slot}"
        if (preset["url"] == _current_url
                and _mpv is not None and _mpv.poll() is None):
            # Impatient re-taps while the stream buffers must not restart it.
            log.info(f"Preset {slot}: already playing — ignored")
            return
        log.info(f"Preset {slot}: {preset['name']} — {preset['url']}")
        play(preset["url"], preset.get("name") or label)
        speak(label)

    elif action == "setup_mode":
        action_setup_mode()

    elif action == "say_address":
        action_say_address()

    elif action == "pair_speaker":
        threading.Thread(target=action_pair_speaker, daemon=True).start()

    else:
        log.warning(f"Unknown action: {action}")


def _entries_to_label(entries: list[list]) -> str:
    parts = []
    for e in entries:
        name, iso, city = e[0], e[1], e[2]
        parts.append(f"{city} ({iso})" if city else f"{name} ({iso})")
    return " / ".join(parts)


def _entries_to_tts(entries: list[list]) -> str:
    """Spoken label — no ISO codes: 'New York, United States' or 'France'."""
    e = entries[0]
    name, city = e[0], e[2]
    return f"{city}, {name}" if city else name


def calibrate() -> None:
    print("\n── Calibration mode ──  Tap countries, Ctrl+C to finish.\n")
    print(f"{'':5}{'code':12}  {'×':>3}  location")
    print("─" * 62)

    tap_count: dict[str, int]  = {}
    first_seen: list[str]      = []
    unknowns:  list[str]       = []

    try:
        while True:
            try:
                code = _tap_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            is_new = code not in tap_count
            tap_count[code] = tap_count.get(code, 0) + 1
            n = tap_count[code]

            if is_new:
                first_seen.append(code)

            entries = CODE_MAP.get(code)
            action  = ACTION_MAP.get(code)
            if action:
                label = f"ACTION: {action}"
            elif entries:
                label = _entries_to_label(entries)
            else:
                label = "⚠ NOT IN CODE_MAP"

            if is_new:
                if not entries and not action:
                    unknowns.append(code)
                    marker = "NEW→ "
                else:
                    marker = "     "
                print(f"{marker}{code:12}  ×{n:<3}  {label}")
            elif not entries and not action:
                print(f"     {code:12}  ×{n:<3}  {label}")

    except KeyboardInterrupt:
        pass

    total = sum(tap_count.values())
    print(f"\n── Done — {total} tap(s) on {len(tap_count)} unique code(s) ──")

    if unknowns:
        print(f"\n⚠  {len(unknowns)} unknown code(s) — fill in and paste into CODE_MAP in globe.py:\n")
        for code in unknowns:
            n = tap_count[code]
            print(f'    "{code}": [["COUNTRY_NAME", "XX", None, None, None]],  # tapped ×{n}')
        print()
    else:
        print("All codes recognised. ✓\n")

    print("Done.")


def main() -> None:
    global _last_code, _last_action
    calibrate_mode = "--calibrate" in sys.argv

    def shutdown(sig, _frame):
        log.info("Shutting down…"); stop(); sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    curated = load_stations(STATIONS_CSV)
    load_actions()
    load_calibration()
    _load_dead()
    _ensure_voice_pack()
    _ensure_chimes()
    _kill_stray_players()

    if OS == 'Darwin':
        listener = kb.Listener(on_press=_on_press)
        listener.start()
    else:
        threading.Thread(target=_evdev_reader, daemon=True).start()
    log.info("Globe ready ✓  — tap a country")

    if calibrate_mode:
        calibrate(); return

    _write_state()   # publish initial (idle) state for the admin UI
    threading.Thread(target=_net_voice_loop, daemon=True).start()

    last_code:    str | None = None
    last_trigger: float      = 0.0
    prev_country_code: str | None = None   # last *country* tap, for re-tap detection

    while True:
        try:
            code = _tap_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        now = time.monotonic()
        if code == last_code and (now - last_trigger) < DEBOUNCE_SEC:
            continue

        last_code    = code
        last_trigger = now
        _last_code   = code

        # Check action zones first
        action = ACTION_MAP.get(code)
        if action:
            _last_action = action
            log.info(f"Tap → ACTION:{action}  ({code})")
            dispatch_action(action, curated)
            _write_state()
            continue

        _last_action = None
        entries = CODE_MAP.get(code)
        if not entries:
            log.debug(f"Unknown code {code}")
            _write_state()
            continue

        iso_list = [e[1] for e in entries]
        label    = _entries_to_label(entries)

        log.info(f"Tap → {label}  ({code})")
        # Re-tap of the country already playing: skip the name announcement
        # and cycle to another of its stations instead of restarting.
        retap = (code == prev_country_code
                 and _mpv is not None and _mpv.poll() is None)
        prev_country_code = code

        _start_noise()   # instant FM-static feedback; cut by the stream watchdog
        if not retap:
            speak(_entries_to_tts(entries))

        cands = station_candidates(iso_list, curated,
                                   exclude=_current_station if retap else None)
        if retap and cands and cands[0][1] == _current_station:
            _stop_noise()       # only one station here — leave it playing
        elif play_candidates(cands):
            log.info(f"  ♪  {cands[0][1] or 'Radio Browser'}"
                     f"{f'  (+{len(cands) - 1} fallback)' if len(cands) > 1 else ''}")
        else:
            _stop_noise()
            log.warning(f"No stream for {label}")
            speak("No station found")
            _write_state()


if __name__ == "__main__":
    main()
