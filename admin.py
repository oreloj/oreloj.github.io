#!/usr/bin/env python3
"""
admin.py — Oreloj Globe Admin
Flask web UI: station management, Bluetooth speaker selection, Wi-Fi setup.

Install:  pip install flask --break-system-packages
Run:      python3 ~/globe/admin.py
Access:   http://oreloj.local:5000
"""

import csv, datetime, io, json, os, re, select, subprocess, threading, time, urllib.request
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
STATIONS_CSV     = SCRIPT_DIR / "stations.csv"
FAVS_PATH        = SCRIPT_DIR / "favourites.json"
ACTIONS_FILE     = SCRIPT_DIR / "actions.json"
CALIBRATION_CSV  = SCRIPT_DIR / "calibration.csv"
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

@app.before_request
def _handle_options():
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
            if name == "hotspot":
                hotspot_mode = True
            elif ctype in ("802-11-wireless", "ethernet"):
                wifi = name
                break

    return jsonify({"playing": playing, "bt": bt, "wifi": wifi,
                    "hotspot_mode": hotspot_mode, "build_time": _build_time()})


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


@app.post("/api/bt/scan")
def api_bt_scan():
    """10-second BT scan; returns only devices with a resolved friendly name."""
    # Matches: [NEW|CHG|DEL] Device AA:BB:CC:DD:EE:FF <rest>
    event_re    = re.compile(r'\[(NEW|CHG|DEL)\]\s+Device\s+([0-9A-F]{2}(?::[0-9A-F]{2}){5})\s*(.*)', re.IGNORECASE)
    # nameless devices get a default alias = their MAC with ':' OR '-' separators
    mac_only_re = re.compile(r'^([0-9A-F]{2}[:-]){5}[0-9A-F]{2}$', re.IGNORECASE)
    # CHG property keys that carry no friendly name — ignore these lines entirely
    _NON_NAME = ("RSSI:", "UUIDs:", "Class:", "Icon:", "Paired:", "Trusted:",
                 "Blocked:", "Connected:", "LegacyPairing:", "ManufacturerData:",
                 "ServiceData:", "AdvertisingFlags:", "TxPower:", "AddressType:")
    discovered = {}  # mac -> name

    proc = subprocess.Popen(
        ["bluetoothctl"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    proc.stdin.write("scan on\n")
    proc.stdin.flush()

    deadline = time.time() + 10
    while time.time() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.3)
        if ready:
            line = proc.stdout.readline()
            if not line:
                break
            m = event_re.search(line)
            if not m:
                continue
            event, mac, rest = m.group(1).upper(), m.group(2).upper(), m.group(3).strip()

            if rest.startswith("Name: "):
                # Resolved friendly name — best source, always overwrite
                discovered[mac] = rest[6:].strip()
            elif rest.startswith("Alias: "):
                # Alias — use only if no real Name yet
                if mac not in discovered or mac_only_re.match(discovered[mac]):
                    discovered[mac] = rest[7:].strip()
            elif event == "NEW" and rest and not any(rest.startswith(k) for k in _NON_NAME):
                # [NEW] Device MAC FriendlyName — initial appearance
                if mac not in discovered:
                    discovered[mac] = rest if not mac_only_re.match(rest) else mac
            # All other CHG lines (RSSI, UUIDs, Class, etc.) are intentionally ignored

    try:
        proc.stdin.write("scan off\nquit\n")
        proc.stdin.flush()
        proc.wait(timeout=2)
    except Exception:
        proc.kill()

    # bluetoothctl only emits a Name in scan events the FIRST time it sees a
    # device this session; already-cached devices yield only RSSI [CHG] noise.
    # So merge the persistent device list — it carries resolved names — and let
    # it win over anything we picked up live (which may be a bare MAC alias).
    _, known_out = _run(["bluetoothctl", "devices"])
    for d in _parse_bt_lines(known_out):
        if not mac_only_re.match(d["name"]):
            discovered[d["mac"].upper()] = d["name"]

    _, paired_out = _run(["bluetoothctl", "devices", "Paired"])
    paired = {d["mac"].upper() for d in _parse_bt_lines(paired_out)}

    return jsonify([
        {"mac": mac, "name": name, "paired": mac in paired}
        for mac, name in discovered.items()
        if not mac_only_re.match(name)  # hide devices whose name never resolved
    ])


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
    rc, out = _run(["bluetoothctl", "pair", mac], timeout=20)
    time.sleep(1)  # let bluez settle before reading the bond state back
    if not _bt_is_paired(mac):
        return jsonify({"ok": False, "paired": False,
                        "msg": out or "pairing failed — is the speaker in pairing mode?"})
    _run(["bluetoothctl", "trust", mac])
    reconfigured = _rebuild_combine_sink()
    crc, _ = _run(["bluetoothctl", "connect", mac], timeout=15)
    return jsonify({"ok": True, "paired": True, "connected": crc == 0,
                    "audio_reconfigured": reconfigured, "msg": out})


@app.get("/api/bt/power")
def api_bt_power_get():
    _, out = _run(["bluetoothctl", "show"])
    return jsonify({"powered": bool(re.search(r"Powered:\s*yes", out, re.IGNORECASE))})


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

@app.get("/api/wifi/networks")
def api_wifi_networks():
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
        if ssid and ssid not in seen:
            seen.add(ssid)
            networks.append({
                "ssid":      ssid,
                "signal":    int(signal) if signal.isdigit() else 0,
                "security":  security or "open",
                "connected": active.lower() == "yes",
            })
    networks.sort(key=lambda x: -x["signal"])
    return jsonify(networks)


@app.post("/api/wifi/connect")
def api_wifi_connect():
    data     = request.json or {}
    ssid     = data.get("ssid", "").strip()
    password = data.get("password", "").strip()
    if not ssid:
        abort(400)
    cmd = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    rc, out = _run(cmd, timeout=30)
    return jsonify({"ok": rc == 0, "msg": out})


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
    """Run `git pull origin main` on ~/globe and restart services if anything changed."""
    globe_dir = str(Path.home() / "globe")
    rc, out = _run(["git", "-C", globe_dir, "pull", "origin", "main"], timeout=30)
    if rc != 0:
        return jsonify({"ok": False, "updated": False, "msg": out, "restart": False}), 500

    updated = "Already up to date." not in out
    if updated:
        env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
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
            return jsonify({"ok": True, "updated": True, "msg": out,
                            "restart": False, "warn": str(e)})
        return jsonify({"ok": True, "updated": True, "msg": out, "restart": True})

    return jsonify({"ok": True, "updated": False, "msg": out, "restart": False})


# ── captive portal ───────────────────────────────────────────────────────────
# When a device joins the Oreloj hotspot, the OS fires a background probe to
# one of these well-known URLs to test for internet.  We return a redirect
# instead of the expected response, which triggers the "Sign in to network"
# popup — automatically opening the admin UI without typing anything.
#
# Requires: iptables rule redirecting port 80 → 5000 on wlan0
# (added by oreloj-hotspot.sh when the hotspot activates).

_CAPTIVE_URL = "http://192.168.4.1:5000/"

for _path in [
    "/hotspot-detect.html",        # iOS / macOS
    "/library/test/success.html",  # macOS (older)
    "/generate_204",               # Android
    "/gen_204",                    # Android (alt)
    "/connecttest.txt",            # Windows
    "/redirect",                   # Windows (alt)
    "/ncsi.txt",                   # Windows (alt)
    "/success.txt",                # Firefox
]:
    app.add_url_rule(
        _path, f"captive_{_path.strip('/').replace('/', '_')}",
        lambda: redirect(_CAPTIVE_URL, 302),
    )


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


if __name__ == "__main__":
    print("Oreloj admin — http://oreloj.local:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
