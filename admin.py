#!/usr/bin/env python3
"""
admin.py — Oreloj Globe Admin
Flask web UI: station management, Bluetooth speaker selection, Wi-Fi setup.

Install:  pip install flask --break-system-packages
Run:      python3 ~/globe/admin.py
Access:   http://oreloj.local:5000
"""

import csv, datetime, hashlib, io, json, os, re, secrets, select, subprocess, threading, time, urllib.request
from pathlib import Path
from flask import Flask, send_file, send_from_directory, jsonify, request, abort, redirect

SCRIPT_DIR       = Path(__file__).parent

def _build_time() -> str:
    """mtime of this file as ISO-8601 UTC — updated whenever the file is deployed via scp."""
    try:
        ts = Path(__file__).stat().st_mtime
        return datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return "unknown"
# This globe's own data lives in data/ — updates never touch it (see oreloj_data.py).
import sys
sys.path.insert(0, str(SCRIPT_DIR))
import oreloj_data
STATIONS_CSV     = oreloj_data.STATIONS_CSV
FAVS_PATH        = oreloj_data.FAVS_PATH
ACTIONS_FILE     = oreloj_data.ACTIONS_FILE
CALIBRATION_CSV  = oreloj_data.CALIBRATION_CSV
CAL_FIELDS       = ["code", "country_iso", "country_name", "region", "lat", "lng"]
CSV_FIELDS   = ["Continent", "Country", "City", "Radio Station",
                 "Description", "Website", "URL Link", "Favourite", "Lat", "Lng"]

# Set this to the raw GitHub URL of stations.csv once it's committed to the
# oreloj repo, e.g.:
#   "https://raw.githubusercontent.com/<username>/oreloj/main/stations.csv"
# The Sync button in the UI calls /api/sync which fetches from this URL.
SYNC_URL = ""

# Set this to the SSH remote or HTTPS URL of the globe repo once it's on GitHub,
# e.g. "https://github.com/<username>/oreloj.git"
# (Only needed for documentation/reference — the update endpoint uses `git pull`
# directly on the Pi's local clone at ~/globe.)
REPO_URL = ""

app = Flask(__name__)


# ── CORS (allow index.html on GitHub Pages to reach the Pi API) ───────────────

_PORT80_OK = False   # set at startup if we could also listen on port 80
_OUR_HOSTS = ("localhost", "127.0.0.1", "192.168.4.1")   # + any *.local name


def _setup_url() -> str:
    return f"http://{SETUP_NET_IP}{'' if _PORT80_OK else ':5000'}/setup"


@app.before_request
def _handle_options():
    addr = request.remote_addr or ""
    if addr.startswith("192.168.4."):
        # A phone on the setup Wi-Fi is using the page: tell netwatch not to
        # interrupt the hotspot for a retry right now.
        try:
            NET_ACTIVITY.touch()
        except Exception:
            pass
        # Captive portal: the phone asked for some other website (DNS on the
        # setup Wi-Fi points every name at us) — send it to the setup page.
        host = (request.host or "").split(":")[0].lower()
        if host and host not in _OUR_HOSTS and not host.endswith(".local") \
                and not request.path.startswith(("/api/", "/vendor/", "/setup")):
            return redirect(_setup_url(), 302)
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp


@app.after_request
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 10) -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -1, f"{cmd[0]}: not found"


def _parse_bt_lines(text: str) -> list:
    """Parse 'Device MAC Name' lines from bluetoothctl output."""
    devices = []
    for line in text.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) == 3 and parts[0] == "Device":
            devices.append({"mac": parts[1], "name": parts[2]})
    return devices


def _bt_is_paired(mac: str) -> bool:
    """Ground truth for pairing — 'bluetoothctl pair' can claim success while
    the speaker silently drops the bond, so always re-check with 'info'."""
    _, info = _run(["bluetoothctl", "info", mac])
    return bool(re.search(r"Paired:\s*yes", info, re.IGNORECASE))


# ── network watchdog (oreloj-netwatch, a root service) ───────────────────────
# netwatch owns the Wi-Fi chip. We read its state from net.json and hand it
# requests as small JSON files — never call nmcli to change Wi-Fi ourselves
# while it is running (two drivers on one wheel = fights).

NET_RUN_DIR   = Path(os.environ.get("ORELOJ_RUN_DIR", "/run/oreloj"))
NET_STATE     = NET_RUN_DIR / "net.json"
NET_SCAN      = NET_RUN_DIR / "scan.json"
NET_REQ_DIR   = NET_RUN_DIR / "requests"
NET_ACTIVITY  = NET_RUN_DIR / "activity"
NETWATCH_SRC  = SCRIPT_DIR / "oreloj-netwatch.py"
NETWATCH_INST = Path("/usr/local/lib/oreloj/oreloj-netwatch.py")
SETUP_NET_IP  = "192.168.4.1"


def _net_state() -> dict | None:
    """netwatch's published state, or None if it isn't installed/running."""
    try:
        data = json.loads(NET_STATE.read_text(encoding="utf-8"))
    except Exception:
        return None
    # A Wi-Fi switch can block netwatch for ~2 minutes; older than that = dead.
    if time.time() - float(data.get("updated_at", 0)) > 180:
        return None
    return data


def _net_request(action: str, **fields) -> bool:
    """Queue a request for netwatch. Written as .tmp then renamed, so netwatch
    never reads a half-written file."""
    try:
        name = f"{time.time_ns()}-{secrets.token_hex(3)}"
        tmp = NET_REQ_DIR / f"{name}.tmp"
        tmp.write_text(json.dumps({"action": action, **fields}), encoding="utf-8")
        os.chmod(tmp, 0o660)
        os.replace(tmp, NET_REQ_DIR / f"{name}.json")
        return True
    except Exception:
        return False


def _file_hash(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        return None


def _netwatch_outdated() -> bool:
    """True when ~/globe has a newer watchdog than the installed copy (the
    installer must be re-run; 'update now' alone can't, it needs sudo)."""
    src, inst = _file_hash(NETWATCH_SRC), _file_hash(NETWATCH_INST)
    return bool(src and inst and src != inst)


# ── PipeWire combine sink (3.5mm + all paired BT speakers) ───────────────────

_COMBINE_CONF = Path.home() / ".config/pipewire/pipewire.conf.d/combine-sink.conf"
_FALLBACK_ALSA_SINK = "alsa_output.platform-3f00b840.mailbox.stereo-fallback"

_COMBINE_NODE_TMPL = """              {
                  audio.position = [ FL FR ]
                  combine.audio.position = [ FL FR ]
                  target.object = "%s"
              }"""

_COMBINE_CONF_TMPL = """context.modules = [
  {   name = libpipewire-module-combine-stream
      args = {
          combine.props = {
              node.name = "combine_sink"
              node.description = "Combined (3.5mm + BT)"
              media.class = "Audio/Sink"
          }
          nodes = [
%s
          ]
      }
  }
]
"""


def _rebuild_combine_sink() -> bool:
    """Regenerate the combine-sink config from the current ALSA sink(s) plus
    every paired Bluetooth speaker, restarting the audio stack only when the
    config actually changed. Returns True if the stack was reconfigured.
    A missing target is tolerated by the combine module, so it is safe to list
    speakers that are not currently connected."""
    rc, out = _run(["pactl", "list", "short", "sinks"])
    alsa = [f.split("\t")[1] for f in out.splitlines()
            if rc == 0 and "\t" in f and f.split("\t")[1].startswith("alsa_output")]
    if not alsa:
        alsa = [_FALLBACK_ALSA_SINK]
    _, paired_out = _run(["bluetoothctl", "devices", "Paired"])
    bt = [f"bluez_output.{d['mac'].replace(':', '_')}.1"
          for d in _parse_bt_lines(paired_out)]
    nodes = "\n".join(_COMBINE_NODE_TMPL % t for t in alsa + bt)
    text = _COMBINE_CONF_TMPL % nodes
    try:
        if _COMBINE_CONF.exists() and _COMBINE_CONF.read_text(encoding="utf-8") == text:
            return False
        _COMBINE_CONF.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(_COMBINE_CONF) + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, _COMBINE_CONF)
    except Exception:
        return False
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    subprocess.run(
        ["systemctl", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"],
        env=env, timeout=15, capture_output=True,
    )
    time.sleep(2)  # let the stack settle before anything touches audio again
    return True


# ── Favourites JSON ───────────────────────────────────────────────────────────

def read_favs() -> list:
    """Return list of favourite station names."""
    if not FAVS_PATH.exists():
        return []
    try:
        return json.loads(FAVS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def write_favs(names: list) -> None:
    tmp = Path(str(FAVS_PATH) + ".tmp")
    tmp.write_text(json.dumps(sorted(names), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, FAVS_PATH)


# ── CSV ───────────────────────────────────────────────────────────────────────

def read_csv() -> list:
    if not STATIONS_CSV.exists():
        return []
    with open(STATIONS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(rows: list) -> None:
    tmp = Path(str(STATIONS_CSV) + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, STATIONS_CSV)


def _mpv_set_volume(level: int) -> None:
    """Best-effort: set system output volume via PulseAudio/PipeWire or ALSA."""
    rc, _ = _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
    if rc != 0:
        _run(["amixer", "-q", "sset", "Master", f"{level}%"])


def _notify_globe() -> None:
    """Restart globe.py so it picks up CSV changes immediately.
    Runs best-effort — a failure here is non-fatal (stations were still saved)."""
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", "globe"],
            env=env, timeout=5, capture_output=True,
        )
    except Exception:
        pass


# ── API: status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    rc, _ = _run(["pgrep", "-x", "mpv"])
    playing = rc == 0

    _, bt_out = _run(["bluetoothctl", "devices", "Connected"])
    bt = _parse_bt_lines(bt_out)

    _, wifi_out = _run(["nmcli", "-t", "-f", "ACTIVE,NAME,TYPE",
                        "connection", "show", "--active"])
    wifi = None
    hotspot_mode = False
    for line in wifi_out.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "yes":
            name, ctype = parts[1], parts[2]
            if name in ("lo", ""):
                continue
            if name in ("hotspot", "oreloj-setup"):
                hotspot_mode = True
            elif ctype in ("802-11-wireless", "ethernet"):
                wifi = name
                break

    net = _net_state()
    if net and net.get("mode") in ("setup", "offline"):
        hotspot_mode = True
    return jsonify({"playing": playing, "bt": bt, "wifi": wifi,
                    "hotspot_mode": hotspot_mode, "build_time": _build_time()})


@app.get("/api/now")
def api_now():
    """Live globe state for the web UI mirror: what's playing, volume, the
    last pen tap (used by "tap pen to assign"), and favourites."""
    try:
        st = json.loads((SCRIPT_DIR / "state.json").read_text(encoding="utf-8"))
    except Exception:
        st = {}
    return jsonify({
        "playing":     bool(st.get("playing")),
        "station":     st.get("station"),
        "volume":      st.get("volume"),
        "last_code":   st.get("last_code"),
        "last_action": st.get("last_action"),
        "updated_at":  st.get("updated_at"),
        "favourites":  read_favs(),
    })


# ── API: stations ─────────────────────────────────────────────────────────────

@app.get("/api/stations")
def api_stations_get():
    return jsonify(read_csv())


@app.post("/api/stations")
def api_stations_post():
    data = request.json or {}
    rows = read_csv()
    row  = {k: data.get(k, "") for k in CSV_FIELDS}
    rows.append(row)
    write_csv(rows)
    _notify_globe()
    return jsonify({"ok": True, "index": len(rows) - 1})


@app.put("/api/stations/<int:idx>")
def api_stations_put(idx):
    rows = read_csv()
    if not (0 <= idx < len(rows)):
        abort(404)
    for k in CSV_FIELDS:
        if k in (request.json or {}):
            rows[idx][k] = request.json[k]
    write_csv(rows)
    _notify_globe()
    return jsonify({"ok": True})


@app.delete("/api/stations/<int:idx>")
def api_stations_delete(idx):
    rows = read_csv()
    if not (0 <= idx < len(rows)):
        abort(404)
    rows.pop(idx)
    write_csv(rows)
    _notify_globe()
    return jsonify({"ok": True})


@app.get("/api/stations/json")
def api_stations_json():
    """Return stations.csv as a JSON array (field names normalised for the web UI)."""
    rows = read_csv()
    return jsonify([
        {
            "name":      r.get("Radio Station", ""),
            "city":      r.get("City", ""),
            "country":   r.get("Country", ""),
            "stream":    r.get("URL Link", ""),
            "web":       r.get("Website", ""),
            "desc":      r.get("Description", ""),
            "continent": r.get("Continent", ""),
            "lat":       float(r["Lat"])  if r.get("Lat")  else 0,
            "lng":       float(r["Lng"])  if r.get("Lng")  else 0,
        }
        for r in rows
    ])


# ── API: favourites ───────────────────────────────────────────────────────────

@app.get("/api/favourites")
def api_favs_get():
    """Return list of favourite station names."""
    return jsonify({"favourites": read_favs()})


@app.post("/api/favourites")
def api_favs_post():
    """Toggle a station's favourite status by name. Body: {"name": "…"}."""
    name = (request.json or {}).get("name", "").strip()
    if not name:
        abort(400)
    favs = read_favs()
    if name in favs:
        favs.remove(name)
        added = False
    else:
        favs.append(name)
        favs.sort()
        added = True
    write_favs(favs)
    _notify_globe()
    return jsonify({"favourites": favs, "added": added})


# ── API: stream check ("check streams" button) ────────────────────────────────
# Runs check_stations.py's checker in the background over this globe's own
# stations + presets, and keeps the last report in stream_health.json.

HEALTH_PATH = SCRIPT_DIR / "stream_health.json"
_CHECK      = {"running": False, "done": 0, "total": 0, "error": None}
_CHECK_LOCK = threading.Lock()


def _run_stream_check() -> None:
    try:
        import sys
        if str(SCRIPT_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPT_DIR))
        import check_stations as sc
        entries = [{"name": r.get("Radio Station", ""), "country": r.get("Country", ""),
                    "url": (r.get("URL Link") or "").strip(), "source": "station"}
                   for r in read_csv()]
        for p in read_actions().get("presets", []):
            if (p.get("url") or "").strip():
                entries.append({"name": p.get("name") or f"Preset {p.get('slot')}", "country": "",
                                "url": p["url"].strip(), "source": f"preset {p.get('slot')}"})
        try:
            previous = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        except Exception:
            previous = None

        def progress(done, total):
            _CHECK["done"], _CHECK["total"] = done, total

        report = sc.run(entries, workers=8, timeout=8, progress=progress, previous=previous)
        tmp = Path(str(HEALTH_PATH) + ".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, HEALTH_PATH)
    except Exception as e:
        _CHECK["error"] = f"{type(e).__name__}: {e}"
    finally:
        _CHECK["running"] = False


@app.post("/api/streams/check")
def api_streams_check():
    """Start a check of every stream (≈30 s). Poll GET /api/streams/health for progress."""
    with _CHECK_LOCK:
        if not _CHECK["running"]:
            _CHECK.update(running=True, done=0, total=0, error=None)
            threading.Thread(target=_run_stream_check, daemon=True).start()
    return jsonify({"ok": True, "running": True})


@app.get("/api/streams/health")
def api_streams_health():
    try:
        report = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
    except Exception:
        report = None
    return jsonify({**_CHECK, "report": report})


# ── Actions JSON helpers ──────────────────────────────────────────────────────

def read_actions() -> dict:
    if not ACTIONS_FILE.exists():
        return {"zones": [], "presets": []}
    try:
        return json.loads(ACTIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"zones": [], "presets": []}


def write_actions(data: dict) -> None:
    tmp = Path(str(ACTIONS_FILE) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ACTIONS_FILE)


# ── API: zones ────────────────────────────────────────────────────────────────

@app.get("/api/zones")
def api_zones_get():
    """Return full actions.json (zones + presets)."""
    return jsonify(read_actions())


@app.post("/api/zones")
def api_zones_post():
    """Add a new zone. Body: {code, action, label, description, position}."""
    body = request.json or {}
    code = body.get("code", "").strip()
    action = body.get("action", "").strip()
    if not code or not action:
        abort(400)
    data = read_actions()
    data.setdefault("zones", [])
    data["zones"].append({
        "code":        code,
        "action":      action,
        "label":       body.get("label", code),
        "description": body.get("description", ""),
        "position":    body.get("position", None),
    })
    write_actions(data)
    _notify_globe()
    return jsonify({"ok": True})


@app.put("/api/zones/<code>")
def api_zones_put(code):
    """Update any field (including code and action) for a zone."""
    data = read_actions()
    zones = data.get("zones", [])
    zone = next((z for z in zones if z["code"] == code), None)
    if zone is None:
        abort(404)
    body = request.json or {}
    for field in ("label", "description", "position", "action"):
        if field in body:
            zone[field] = body[field]
    # Allow renaming the code itself
    if "new_code" in body and body["new_code"].strip():
        zone["code"] = body["new_code"].strip()
    write_actions(data)
    _notify_globe()
    return jsonify({"ok": True})


@app.delete("/api/zones/<code>")
def api_zones_delete(code):
    """Remove a zone by code."""
    data = read_actions()
    zones = data.get("zones", [])
    new_zones = [z for z in zones if z["code"] != code]
    if len(new_zones) == len(zones):
        abort(404)
    data["zones"] = new_zones
    write_actions(data)
    _notify_globe()
    return jsonify({"ok": True})


# ── API: presets ──────────────────────────────────────────────────────────────

@app.get("/api/presets")
def api_presets_get():
    """Return presets list."""
    return jsonify({"presets": read_actions().get("presets", [])})


@app.put("/api/presets/<int:slot>")
def api_presets_put(slot):
    """Update a preset slot (1-6). Body: {name, url, label}."""
    if not (1 <= slot <= 6):
        abort(400)
    data = read_actions()
    presets = data.get("presets", [])
    preset = next((p for p in presets if p.get("slot") == slot), None)
    if preset is None:
        # Create the slot if it doesn't exist yet
        preset = {"slot": slot, "name": "", "url": "", "label": f"Preset {slot}"}
        presets.append(preset)
        data["presets"] = presets
    body = request.json or {}
    for field in ("name", "url", "label"):
        if field in body:
            preset[field] = body[field]
    write_actions(data)
    _notify_globe()
    return jsonify({"ok": True})


# ── Calibration CSV helpers ────────────────────────────────────────────────────

def read_calibration() -> list:
    """Return list of dicts from calibration.csv."""
    if not CALIBRATION_CSV.exists():
        return []
    with open(CALIBRATION_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_calibration(rows: list) -> None:
    """Atomically write rows to calibration.csv."""
    tmp = Path(str(CALIBRATION_CSV) + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAL_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, CALIBRATION_CSV)


# ── API: calibration ──────────────────────────────────────────────────────────

@app.get("/api/calibration")
def api_calibration_get():
    """Return full calibration map as JSON."""
    rows = read_calibration()
    # Normalise: lat/lng to float or None
    out = []
    for r in rows:
        out.append({
            "code":         r.get("code", ""),
            "country_iso":  r.get("country_iso", ""),
            "country_name": r.get("country_name", ""),
            "region":       r.get("region", ""),
            "lat":          float(r["lat"]) if r.get("lat") else None,
            "lng":          float(r["lng"]) if r.get("lng") else None,
        })
    return jsonify({"rows": out})


@app.post("/api/calibration")
def api_calibration_post():
    """Add or update a calibration row. A code+country_iso+region triple is unique.
    Returns 409 if the code is already assigned to a different country_iso."""
    body = request.json or {}
    code         = body.get("code", "").strip()
    country_iso  = body.get("country_iso", "").strip().upper()
    country_name = body.get("country_name", "").strip()
    region       = body.get("region", "").strip()
    lat          = body.get("lat", "")
    lng          = body.get("lng", "")

    if not code or not country_iso:
        abort(400)

    rows = read_calibration()

    # Check for conflict: code already mapped to a DIFFERENT country_iso
    for r in rows:
        if r["code"] == code and r["country_iso"].upper() != country_iso:
            # It's a conflict only if it's not the same row being updated
            if not (r["code"] == code and r["country_iso"].upper() == country_iso and r.get("region", "") == region):
                return jsonify({"ok": False, "conflict": True,
                                "existing": r["country_name"]}), 409

    # Find existing row for this code+iso+region triple and update it,
    # or append a new row.
    key = (code, country_iso, region)
    updated = False
    for r in rows:
        if (r["code"] == code and r["country_iso"].upper() == country_iso
                and r.get("region", "") == region):
            r["country_name"] = country_name
            r["lat"]  = lat if lat is not None else ""
            r["lng"]  = lng if lng is not None else ""
            updated = True
            break

    if not updated:
        rows.append({
            "code":         code,
            "country_iso":  country_iso,
            "country_name": country_name,
            "region":       region,
            "lat":          lat if lat is not None else "",
            "lng":          lng if lng is not None else "",
        })

    write_calibration(rows)
    _notify_globe()
    return jsonify({"ok": True, "updated": updated})


@app.delete("/api/calibration/<code>")
def api_calibration_delete(code):
    """Remove all rows for a code, or a specific row if ?iso=XX&region=YY provided."""
    iso    = request.args.get("iso", "").strip().upper()
    region = request.args.get("region", "").strip()

    rows = read_calibration()
    original_len = len(rows)

    if iso:
        # Remove only the matching iso (and optional region) row
        new_rows = [r for r in rows if not (
            r["code"] == code
            and r["country_iso"].upper() == iso
            and (not region or r.get("region", "") == region)
        )]
    else:
        # Remove ALL rows for this code
        new_rows = [r for r in rows if r["code"] != code]

    if len(new_rows) == original_len:
        abort(404)

    write_calibration(new_rows)
    _notify_globe()
    return jsonify({"ok": True, "removed": original_len - len(new_rows)})


@app.get("/api/calibration/export")
def api_calibration_export():
    """Return calibration.csv as a file download."""
    if not CALIBRATION_CSV.exists():
        abort(404)
    return send_file(str(CALIBRATION_CSV), mimetype="text/csv",
                     as_attachment=True, download_name="calibration.csv")


# ── API: Bluetooth ────────────────────────────────────────────────────────────

@app.get("/api/bt/devices")
def api_bt_devices():
    _, paired_out = _run(["bluetoothctl", "devices", "Paired"])
    _, conn_out   = _run(["bluetoothctl", "devices", "Connected"])
    paired    = _parse_bt_lines(paired_out)
    connected = {d["mac"] for d in _parse_bt_lines(conn_out)}
    for d in paired:
        d["connected"] = d["mac"] in connected
    return jsonify(paired)


_MAC_ONLY_RE = re.compile(r'^([0-9A-F]{2}[:-]){5}[0-9A-F]{2}$', re.IGNORECASE)
_BT_LOCK = threading.Lock()   # one scan/pair at a time (UI and pen can both ask)
_BT_LAST_SCAN: dict = {}      # what the last scan saw — for troubleshooting


def _bt_info(mac: str) -> dict:
    """Parse `bluetoothctl info MAC`: Name, Alias, Icon, Paired, RSSI (int),
    and whether it advertises an audio sink (= it's a speaker/headphones)."""
    _, info = _run(["bluetoothctl", "info", mac])
    d = {}
    for line in info.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        if k in ("Name", "Alias", "Icon", "Paired", "Connected") and k not in d:
            d[k] = v
        elif k == "RSSI":                      # "-60" or "0xffffffc4 (-60)"
            m = re.search(r"(-?\d+)\)?$", v)
            if m:
                d["RSSI"] = int(m.group(1))
        elif k == "UUID" and ("Audio Sink" in v or "0000110b-" in v.lower()):
            d["audio_sink"] = True
    return d


def _bt_scan(seconds: float = 10) -> dict:
    """Look for Bluetooth devices for `seconds`.
    Returns {MAC: {"name", "rssi", "icon", "speaker"}} — rssi is the signal
    strength (closer to 0 = nearer); None = remembered by the Pi but not heard
    during this scan.

    The ground truth is `bluetoothctl info`, asked WHILE discovery is still
    running (BlueZ drops a device's RSSI when discovery stops). The live event
    output is only a bonus: on Oreloj 1 it stayed silent when piped, and a
    scan that trusted it found nothing at all (2026-11-04)."""
    proc = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    proc.stdin.write("scan on\n")
    proc.stdin.flush()

    heard = set()   # MACs that showed up in the live output
    event_re = re.compile(r'\[(?:NEW|CHG)\]\s+Device\s+([0-9A-F]{2}(?::[0-9A-F]{2}){5})', re.IGNORECASE)
    deadline = time.time() + seconds
    while time.time() < deadline:
        # Keep draining the output so bluetoothctl never blocks on a full pipe.
        ready, _, _ = select.select([proc.stdout], [], [], 0.3)
        if ready:
            line = proc.stdout.readline()
            if not line:
                break
            m = event_re.search(line)
            if m:
                heard.add(m.group(1).upper())

    # Diagnostics: did discovery really start? (readable via /api/bt/power)
    _, show = _run(["bluetoothctl", "show"])
    _BT_LAST_SCAN.update({"at": time.time(), "heard_live": len(heard),
                          "discovering": bool(re.search(r"Discovering:\s*yes", show, re.I))})

    found = {}
    _, known_out = _run(["bluetoothctl", "devices"])
    for d in _parse_bt_lines(known_out)[:60]:
        mac = d["mac"].upper()
        info = _bt_info(mac)
        name = info.get("Name") or info.get("Alias") or d["name"]
        if _MAC_ONLY_RE.match(name or ""):
            continue                               # no friendly name — hide
        rssi = info.get("RSSI")
        if rssi is None and mac in heard:
            rssi = -100                            # heard, strength unknown
        icon = info.get("Icon", "")
        found[mac] = {"name": name, "rssi": rssi, "icon": icon,
                      "speaker": icon.startswith("audio-") or bool(info.get("audio_sink"))}

    try:
        proc.stdin.write("scan off\nquit\n")
        proc.stdin.flush()
        proc.wait(timeout=2)
    except Exception:
        proc.kill()
    _BT_LAST_SCAN.update({"named": len(found),
                          "in_range": sum(1 for f in found.values() if f["rssi"] is not None)})
    print(f"bt scan: {len(found)} named device(s), "
          f"{sum(1 for f in found.values() if f['rssi'] is not None)} in range", flush=True)
    return found


def _bt_connected(mac: str) -> bool:
    _, info = _run(["bluetoothctl", "info", mac])
    return bool(re.search(r"Connected:\s*yes", info, re.IGNORECASE))


def _bt_connect_retry(mac: str, tries: int = 4) -> bool:
    """Connect and CHECK it — `bluetoothctl connect` can report success while
    the audio link never comes up (or the audio stack is still restarting)."""
    for _ in range(tries):
        _run(["bluetoothctl", "connect", mac], timeout=15)
        time.sleep(2)
        if _bt_connected(mac):
            return True
        time.sleep(2)
    return False


def _bt_pair_and_use(mac: str) -> dict:
    """Pair + trust + connect + add to the audio output.

    Order matters: some speakers (KRK GoAux "K3" on Oreloj 1) drop a fresh
    pairing if no connection follows within seconds. So connect right away,
    THEN rebuild the audio output (which restarts the audio stack and drops
    the link), then reconnect and verify. Only report success once the
    speaker is really connected."""
    rc, out = _run(["bluetoothctl", "pair", mac], timeout=25)
    time.sleep(1)  # let bluez settle before reading the bond state back
    if not _bt_is_paired(mac):
        return {"ok": False, "paired": False,
                "msg": out or "pairing failed — is the speaker in pairing mode?"}
    _run(["bluetoothctl", "trust", mac])
    _bt_connect_retry(mac, tries=2)                # keep the fresh pairing alive
    reconfigured = _rebuild_combine_sink()
    connected = _bt_connect_retry(mac, tries=4)
    paired = _bt_is_paired(mac)
    if connected and paired:
        return {"ok": True, "paired": True, "connected": True,
                "audio_reconfigured": reconfigured, "msg": out}
    if not paired:
        msg = ("the speaker accepted pairing, then dropped it — switch it off and "
               "on, put it in pairing mode again and retry")
    else:
        msg = ("paired, but the sound link didn't come up yet — switch the speaker "
               "off and on; the globe reconnects it within 2 minutes")
    return {"ok": False, "paired": paired, "connected": False,
            "audio_reconfigured": reconfigured, "msg": msg}


@app.post("/api/bt/scan")
def api_bt_scan():
    """10-second BT scan; returns only devices with a resolved friendly name."""
    with _BT_LOCK:
        found = _bt_scan(10)
    _, paired_out = _run(["bluetoothctl", "devices", "Paired"])
    paired = {d["mac"].upper() for d in _parse_bt_lines(paired_out)}
    devs = [{"mac": mac, "name": d["name"], "rssi": d["rssi"], "paired": mac in paired,
             "speaker": d["speaker"], "in_range": d["rssi"] is not None}
            for mac, d in found.items()]
    # Nearest first; devices the Pi only remembers (not heard now) at the end.
    devs.sort(key=lambda d: -(d["rssi"] if d["rssi"] is not None else -999))
    return jsonify(devs)


@app.post("/api/bt/pair-nearest")
def api_bt_pair_nearest():
    """One-tap speaker setup (pen "pair speaker" spot, or the setup page):
    find the nearest speaker that is in pairing mode and start using it."""
    if not _BT_LOCK.acquire(timeout=1):
        return jsonify({"ok": False, "msg": "busy — a Bluetooth scan is already running"}), 409
    try:
        _run(["rfkill", "unblock", "bluetooth"])
        _run(["bluetoothctl", "power", "on"], timeout=10)
        found = _bt_scan(12)
        _, paired_out = _run(["bluetoothctl", "devices", "Paired"])
        paired = {d["mac"].upper() for d in _parse_bt_lines(paired_out)}
        near = sorted(((mac, d) for mac, d in found.items()
                       if mac not in paired and d["rssi"] is not None),
                      key=lambda md: -md[1]["rssi"])
        speakers = [(mac, d) for mac, d in near if d["speaker"]]
        # Some speakers don't say what they are until paired: as a last resort
        # try the nearest devices that aren't obviously phones/computers.
        unknown = [(mac, d) for mac, d in near if not d["speaker"]
                   and not d["icon"].startswith(("phone", "computer", "input"))][:2]
        tried = None
        for mac, d in speakers[:3] + unknown:
            res = _bt_pair_and_use(mac)
            res.update({"mac": mac, "name": d["name"]})
            if res.get("ok") or res.get("paired"):    # it's the speaker, even if not connected yet
                return jsonify(res)
            if res.get("msg", "").startswith("the speaker accepted pairing"):
                tried = tried or res                  # remember the most useful failure
        if tried:
            tried["msg"] = f"{tried['name']}: {tried['msg']}"
            return jsonify(tried)
        heard = ", ".join(d["name"] for _, d in near[:6]) or "nothing"
        return jsonify({"ok": False, "msg": f"no speaker in pairing mode found (heard: {heard})"})
    finally:
        _BT_LOCK.release()


@app.post("/api/bt/connect")
def api_bt_connect():
    mac = (request.json or {}).get("mac", "").strip()
    if not mac:
        abort(400)
    rc, out = _run(["bluetoothctl", "connect", mac], timeout=15)
    reconfigured = False
    if rc == 0 and _bt_is_paired(mac):
        # Covers speakers paired outside the UI (e.g. over SSH) that are
        # missing from the combine sink.
        reconfigured = _rebuild_combine_sink()
        if reconfigured:  # the audio restart drops the link — bring it back
            rc, out = _run(["bluetoothctl", "connect", mac], timeout=15)
    return jsonify({"ok": rc == 0, "audio_reconfigured": reconfigured, "msg": out})


@app.post("/api/bt/disconnect")
def api_bt_disconnect():
    mac = (request.json or {}).get("mac", "").strip()
    if not mac:
        abort(400)
    rc, out = _run(["bluetoothctl", "disconnect", mac])
    return jsonify({"ok": rc == 0, "msg": out})


@app.post("/api/bt/pair")
def api_bt_pair():
    mac = (request.json or {}).get("mac", "").strip()
    if not mac:
        abort(400)
    return jsonify(_bt_pair_and_use(mac))


@app.post("/api/bt/forget")
def api_bt_forget():
    """Remove a paired speaker (so a new one can take its place)."""
    mac = (request.json or {}).get("mac", "").strip()
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        abort(400)
    rc, out = _run(["bluetoothctl", "remove", mac], timeout=10)
    _rebuild_combine_sink()
    return jsonify({"ok": rc == 0, "msg": out})


@app.get("/api/bt/power")
def api_bt_power_get():
    _, out = _run(["bluetoothctl", "show"])
    return jsonify({"powered": bool(re.search(r"Powered:\s*yes", out, re.IGNORECASE)),
                    "last_scan": _BT_LAST_SCAN})


@app.post("/api/bt/power")
def api_bt_power_set():
    on = bool((request.json or {}).get("on"))
    if on:
        _run(["rfkill", "unblock", "bluetooth"])  # best effort — clear any soft block first
    rc, out = _run(["bluetoothctl", "power", "on" if on else "off"], timeout=10)

    reconnected = []
    if on and rc == 0:
        # Powering the adapter on does NOT restore connections — reconnect the
        # known speaker(s) ourselves so audio is ready without a manual tap.
        time.sleep(1.5)  # let the adapter settle before connect attempts
        _, paired_out = _run(["bluetoothctl", "devices", "Paired"])
        for d in _parse_bt_lines(paired_out):
            crc, _ = _run(["bluetoothctl", "connect", d["mac"]], timeout=12)
            if crc == 0:
                reconnected.append(d["name"])

    return jsonify({"ok": rc == 0, "powered": on if rc == 0 else None,
                    "reconnected": reconnected, "msg": out})


# ── API: Wi-Fi ────────────────────────────────────────────────────────────────

def _wifi_live_scan() -> list:
    _run(["nmcli", "device", "wifi", "rescan"])
    time.sleep(2)
    _, out = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,ACTIVE",
                   "device", "wifi", "list"])
    networks, seen = [], set()
    for line in out.splitlines():
        # rsplit from right so SSIDs with colons survive
        parts = line.rsplit(":", 3)
        if len(parts) < 4:
            continue
        ssid, signal, security, active = (p.strip() for p in parts)
        ssid = ssid.replace("\\:", ":")
        if ssid and ssid not in seen:
            seen.add(ssid)
            networks.append({
                "ssid":      ssid,
                "signal":    int(signal) if signal.isdigit() else 0,
                "security":  security or "open",
                "connected": active.lower() == "yes",
            })
    networks.sort(key=lambda x: -x["signal"])
    return networks


@app.get("/api/wifi/networks")
def api_wifi_networks():
    net = _net_state()
    if net and net.get("mode") in ("setup", "offline", "searching", "connecting"):
        # The Wi-Fi chip is busy being the setup hotspot and can't look around;
        # use the list netwatch saved just before it opened the hotspot.
        try:
            nets = json.loads(NET_SCAN.read_text(encoding="utf-8")).get("networks", [])
        except Exception:
            nets = []
        return jsonify([{**n, "connected": False, "cached": True} for n in nets])
    networks = _wifi_live_scan()
    if net:
        saved = set(net.get("saved") or [])
        for n in networks:
            n["saved"] = n["ssid"] in saved
    return jsonify(networks)


@app.post("/api/wifi/connect")
def api_wifi_connect():
    data     = request.json or {}
    ssid     = str(data.get("ssid", ""))
    password = str(data.get("password", ""))
    hidden   = bool(data.get("hidden"))
    if not ssid.strip():
        abort(400)
    if password and not (8 <= len(password) <= 63):
        return jsonify({"ok": False, "msg": "Wi-Fi passwords are 8 to 63 characters"}), 400
    if _net_state():
        # netwatch does the switch (and rolls back if it fails). The answer
        # comes later — the globe says it out loud, and /api/net shows it.
        ok = _net_request("connect", ssid=ssid, password=password, hidden=hidden)
        return jsonify({"ok": ok, "queued": True,
                        "msg": f"Switching to {ssid} — listen to the globe." if ok
                               else "could not reach the network watchdog"})
    # Older install without netwatch: do it directly.
    cmd = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    rc, out = _run(cmd, timeout=30)
    return jsonify({"ok": rc == 0, "msg": out})


@app.get("/api/net")
def api_net():
    """Network watchdog state for the UI: online / setup / connecting, the
    setup Wi-Fi name + password, saved networks and the last few events."""
    net = _net_state()
    if not net:
        return jsonify({"netwatch": False})
    return jsonify({"netwatch": True, "outdated": _netwatch_outdated(),
                    "hostname": os.uname().nodename.split(".")[0],
                    **{k: net.get(k) for k in (
                        "mode", "reason", "target", "ssid", "ip", "setup_ssid",
                        "setup_password", "setup_ip", "saved", "events", "session")}})


@app.post("/api/net/setup")
def api_net_setup():
    """Open the setup Wi-Fi now (same as tapping the pen "setup" spot)."""
    if not _net_state():
        return jsonify({"ok": False, "msg": "setup mode isn't installed on this globe yet"}), 503
    return jsonify({"ok": _net_request("setup")})


@app.post("/api/net/leave")
def api_net_leave():
    """Close the setup Wi-Fi and go back to a known network if one is around."""
    if not _net_state():
        return jsonify({"ok": False}), 503
    return jsonify({"ok": _net_request("leave")})


@app.post("/api/wifi/rescan")
def api_wifi_rescan():
    """Look for networks again. In setup mode this closes the setup Wi-Fi for
    ~20 s (the chip can't look around while it IS that network)."""
    if not _net_state():
        return jsonify({"ok": False}), 503
    return jsonify({"ok": _net_request("rescan")})


@app.post("/api/wifi/forget")
def api_wifi_forget():
    ssid = str((request.json or {}).get("ssid", ""))
    if not ssid or not _net_state():
        abort(400)
    return jsonify({"ok": _net_request("forget", ssid=ssid)})


# ── API: volume ──────────────────────────────────────────────────────────────

@app.post("/api/volume")
def api_volume():
    """Set system output volume. Body: {"level": 0-100}."""
    level = int((request.json or {}).get("level", 80))
    level = max(0, min(100, level))
    _mpv_set_volume(level)
    return jsonify({"ok": True})


# ── API: sync from remote CSV ─────────────────────────────────────────────────

@app.post("/api/sync")
def api_sync():
    """Fetch a remote stations.csv (SYNC_URL) and add any new stations locally."""
    if not SYNC_URL:
        return jsonify({"ok": False, "added": 0,
                        "msg": "SYNC_URL not configured in admin.py"}), 400
    try:
        req = urllib.request.Request(SYNC_URL, headers={"User-Agent": "oreloj/1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return jsonify({"ok": False, "added": 0, "msg": str(e)}), 502

    remote = list(csv.DictReader(io.StringIO(raw)))
    local  = read_csv()
    existing_urls = {r["URL Link"].strip() for r in local if r.get("URL Link")}

    added = 0
    for row in remote:
        url = row.get("URL Link", "").strip()
        if url and url not in existing_urls:
            local.append({k: row.get(k, "") for k in CSV_FIELDS})
            existing_urls.add(url)
            added += 1

    if added:
        write_csv(local)
        _notify_globe()

    return jsonify({"ok": True, "added": added})


# ── API: git update ───────────────────────────────────────────────────────────

@app.post("/api/update")
def api_update():
    """Update the software from GitHub, then restart. The globe's own data
    (data/) is never touched; fixes to the shared station list are merged in
    (see oreloj_data.safe_update). Always answers 200 so the page can show why
    an update didn't happen."""
    try:
        res = oreloj_data.safe_update(SCRIPT_DIR)
    except Exception as e:
        return jsonify({"ok": False, "updated": False, "restart": False,
                        "msg": f"update failed: {type(e).__name__}: {e}"})
    if not res.get("ok"):
        return jsonify({**res, "restart": False})

    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    if res.get("updated"):
        # Restart globe immediately, then restart admin after a short delay
        # (admin restart kills this very response, so we detach it).
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", "globe.service"],
                env=env, timeout=5, capture_output=True,
            )
            subprocess.Popen(
                ["bash", "-c",
                 "sleep 2 && systemctl --user restart admin.service"],
                env=env,
            )
        except Exception as e:
            return jsonify({**res, "restart": False, "warn": str(e)})
        return jsonify({**res, "restart": True})

    if res.get("changed_data"):
        _notify_globe()          # same software, but the station list moved on
    return jsonify({**res, "restart": False})


# ── captive portal ───────────────────────────────────────────────────────────
# When a phone joins the setup Wi-Fi, it fires a background probe to one of
# these well-known URLs to test for internet. We answer with a redirect
# instead, which pops up the "Sign in to network" sheet showing /setup —
# no typing needed. (DNS on the setup Wi-Fi points every name at the Pi, and
# admin.py also listens on port 80 — see install-setup-mode.sh.)

for _path in [
    "/hotspot-detect.html",        # iOS / macOS
    "/library/test/success.html",  # macOS (older)
    "/generate_204",               # Android
    "/gen_204",                    # Android (alt)
    "/connecttest.txt",            # Windows
    "/redirect",                   # Windows (alt)
    "/ncsi.txt",                   # Windows (alt)
    "/success.txt",                # Firefox
    "/canonical.html",             # Firefox (alt)
]:
    app.add_url_rule(
        _path, f"captive_{_path.strip('/').replace('/', '_').replace('.', '_')}",
        lambda: redirect(_setup_url(), 302),
    )


@app.get("/setup")
def setup_page():
    """Small, fast phone page: pick a Wi-Fi, pair a speaker."""
    return send_file(SCRIPT_DIR / "setup.html")


# ── Frontend ──────────────────────────────────────────────────────────────────
# The full UI lives in index.html alongside this file.
# GET / serves it directly via send_file below.
# (no embedded HTML — index.html is served directly)
# ── run ───────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    """Serve the unified index.html UI."""
    return send_file(SCRIPT_DIR / "index.html")


@app.get("/vendor/<path:filename>")
def vendor(filename):
    """Serve the bundled map libraries, world outline and fonts, so the UI
    works with no internet (e.g. phone joined to the Oreloj setup hotspot)."""
    return send_from_directory(SCRIPT_DIR / "vendor", filename, max_age=86400)


def _serve_port_80() -> None:
    """Also answer on port 80, so http://oreloj.local works without :5000 and
    phones' captive-portal checks land here. Needs the sysctl from
    install-setup-mode.sh; without it this quietly does nothing."""
    global _PORT80_OK
    try:
        from werkzeug.serving import make_server
        srv = make_server("0.0.0.0", 80, app, threaded=True)
    except Exception as e:
        print(f"port 80 not available ({e}) — using :5000 only")
        return
    _PORT80_OK = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()


if __name__ == "__main__":
    print("Oreloj admin — http://oreloj.local:5000")
    try:
        _d = oreloj_data.startup(SCRIPT_DIR)
        if _d["seeded"]:
            print(f"data folder: copied {', '.join(_d['seeded'])} into {oreloj_data.DATA_DIR}")
        if _d["stations"] and not _d["stations"].get("skipped"):
            print(f"data folder: shared station list merged in — {_d['stations']}")
            _notify_globe()
    except Exception as _e:
        print(f"data folder check failed ({type(_e).__name__}: {_e}) — carrying on")
    _serve_port_80()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
