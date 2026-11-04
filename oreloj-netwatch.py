#!/usr/bin/env python3
"""
oreloj-netwatch.py — keeps an Oreloj globe reachable.

Runs as root (system service), and is the ONLY thing that changes the Wi-Fi
chip's job. Think of it as the globe's receptionist: it either sits on your
home Wi-Fi like a normal device ("online"), or it opens its own little
password-protected Wi-Fi ("setup mode") so a phone can join it and tell it
which network or speaker to use.

  * Boot:          no known Wi-Fi after BOOT_GRACE s      -> setup mode
  * Wi-Fi drops:   offline for more than DROP_GRACE s     -> setup mode
  * In setup mode: every RETRY_EVERY s, if no phone is using the setup
                   Wi-Fi, quietly try the known networks again
  * Requests:      admin.py / globe.py drop small JSON files into
                   /run/oreloj/requests/ — "setup", "connect", "leave",
                   "forget", "rescan". Nobody else touches nmcli for Wi-Fi.
  * SD card:       /boot/firmware/oreloj-wifi.txt lets you add a Wi-Fi
                   from any computer; it is emptied again after use.
                   /boot/firmware/oreloj-setup.txt holds the setup Wi-Fi
                   name + password (created on first run).

State is published to /run/oreloj/net.json for admin.py (web UI) and
globe.py (which speaks the events out loud).

Usage:
  oreloj-netwatch.py            run the watchdog (systemd)
  oreloj-netwatch.py --init     create the SD-card files, print the setup Wi-Fi
"""

import grp
import json
import os
import pwd
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

# ── settings (env overrides exist so the logic can be tested off-device) ──────
NMCLI    = os.environ.get("ORELOJ_NMCLI", "nmcli")
IW       = os.environ.get("ORELOJ_IW", "iw")
IFACE    = os.environ.get("ORELOJ_IFACE", "wlan0")
RUN_DIR  = Path(os.environ.get("ORELOJ_RUN_DIR", "/run/oreloj"))
USER     = os.environ.get("ORELOJ_USER", "oreloj")

BOOT_GRACE    = int(os.environ.get("ORELOJ_BOOT_GRACE", "60"))    # s after start
DROP_GRACE    = int(os.environ.get("ORELOJ_DROP_GRACE", "120"))   # s offline
RETRY_EVERY   = int(os.environ.get("ORELOJ_RETRY_EVERY", "180"))  # s in setup
MANUAL_HOLD   = int(os.environ.get("ORELOJ_MANUAL_HOLD", "600"))  # s, pen/UI setup
ACTIVITY_HOLD = int(os.environ.get("ORELOJ_ACTIVITY_HOLD", "180"))
TICK          = float(os.environ.get("ORELOJ_TICK", "3"))
SETTLE        = float(os.environ.get("ORELOJ_SETTLE", "1"))       # sleep multiplier

REQ_DIR       = RUN_DIR / "requests"
STATE_FILE    = RUN_DIR / "net.json"
SCAN_FILE     = RUN_DIR / "scan.json"
ACTIVITY_FILE = RUN_DIR / "activity"

SETUP_CON   = "oreloj-setup"     # NetworkManager profile for the setup hotspot
TRY_CON     = "oreloj-try"       # temporary profile while testing a new network
LEGACY_APS  = ("hotspot",)       # old open "Oreloj" hotspot profile — removed
SETUP_IP    = "192.168.4.1"

SETUP_FILE_NAME = "oreloj-setup.txt"
WIFI_FILE_NAME  = "oreloj-wifi.txt"

WORDS = """amber anchor apple arrow aspen atlas bamboo banjo beacon birch bison
blue breeze cactus canyon cedar cello cobalt comet coral cosmos cricket delta
desert ember falcon fern fjord flint forest galaxy garnet ginger glacier harbor
hazel heron honey indigo island jade jasmine jungle kiwi lagoon lemon lily lotus
lunar maple marble meadow melon mint nectar nova ocean olive orbit orchid otter
panda pepper pine planet plum polar prism quartz radar raven reef river robin
saffron salt sierra sonar spruce summit tango tide tiger topaz tulip tundra
velvet violet walnut willow zebra""".split()


def log(msg: str) -> None:
    print(f"netwatch: {msg}", flush=True)


def nap(seconds: float) -> None:
    time.sleep(seconds * SETTLE)


# ── small helpers ─────────────────────────────────────────────────────────────

def run(cmd: list, timeout: int = 20) -> tuple:
    """Run a command, never raise. Returns (returncode, combined output)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).rstrip("\r\n")   # keep spaces inside values
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -1, f"{cmd[0]}: not found"


def nm(*args, timeout: int = 20) -> tuple:
    return run([NMCLI, *args], timeout=timeout)


def split_terse(line: str) -> list:
    """Split one line of `nmcli -t` output. Colons inside values are escaped
    as '\\:' and backslashes as '\\\\'."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def boot_dir() -> Path:
    """The SD card's FAT partition (readable from any Mac/PC)."""
    env = os.environ.get("ORELOJ_BOOT_DIR")
    if env:
        return Path(env)
    for p in ("/boot/firmware", "/boot"):
        if (Path(p) / "config.txt").exists():
            return Path(p)
    return Path("/boot/firmware")


def read_kv(path: Path) -> dict:
    """Parse simple key=value lines. A value in "double quotes" keeps
    leading/trailing spaces (some router names end with a space)."""
    kv = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return kv
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().lower()
        if len(val.strip()) >= 2 and val.strip()[0] == '"' and val.strip()[-1] == '"':
            val = val.strip()[1:-1]
        else:
            val = val.strip()
        kv[key] = val
    return kv


def valid_psk(pw: str) -> bool:
    return 8 <= len(pw) <= 63 or bool(re.fullmatch(r"[0-9a-fA-F]{64}", pw or ""))


def gen_password() -> str:
    return f"{secrets.choice(WORDS)}-{secrets.choice(WORDS)}-{secrets.randbelow(900) + 100}"


def default_setup_name() -> str:
    h = socket.gethostname().split(".")[0].lower()
    m = re.fullmatch(r"oreloj-?(\d*)", h)
    if m:
        return f"Oreloj-{m.group(1) or '1'}"
    return f"Oreloj-{h}"[:32]


def group_id() -> int:
    try:
        return grp.getgrnam(USER).gr_gid
    except KeyError:
        try:
            return pwd.getpwnam(USER).pw_gid
        except KeyError:
            return 0


def write_private(path: Path, text: str, mode: int = 0o640) -> None:
    """Atomic write, readable by root + the oreloj group only."""
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, mode)
        if os.geteuid() == 0:
            os.chown(tmp, 0, group_id())
    except Exception:
        pass
    os.replace(tmp, path)


# ── SD-card files ─────────────────────────────────────────────────────────────

SETUP_TEMPLATE = """# ── Oreloj setup Wi-Fi ─────────────────────────────────────────────────
# This globe's own little Wi-Fi network. It switches on by itself when the
# globe can't find a Wi-Fi it knows, or when you tap the "setup" spot with
# the pen. Join it with your phone to pick a new Wi-Fi or speaker.
#
# You can change the name and password here (password: 8 to 63 characters).
# speak_password=yes lets the globe say the password out loud in setup mode.
setup_wifi_name={name}
setup_wifi_password={password}
speak_password={speak}
"""

WIFI_TEMPLATE = """# ── Add a Wi-Fi network to this Oreloj globe ──────────────────────────
# 1. Type the network name and password after the = signs below.
#    Capitals and spaces count. If the name starts or ends with a space,
#    put it in "double quotes".
# 2. Save, eject the card, put it back in the globe and switch it on.
# The globe adds the network, then empties this file again so your
# password doesn't stay on the card. Leave the password empty for an
# open network.
wifi_name=
wifi_password=
"""


def load_setup_conf(create: bool = True) -> dict:
    """Read (and if needed create/repair) oreloj-setup.txt on the SD card.
    Falls back to /etc/oreloj/ if the card's FAT partition isn't writable."""
    candidates = [boot_dir() / SETUP_FILE_NAME, Path("/etc/oreloj") / SETUP_FILE_NAME]
    path = next((p for p in candidates if p.exists()), candidates[0])
    kv = read_kv(path)
    name = (kv.get("setup_wifi_name") or "").strip() or default_setup_name()
    name = name.encode("utf-8")[:32].decode("utf-8", "ignore")
    pw = kv.get("setup_wifi_password", "")
    speak = kv.get("speak_password", "yes").strip().lower() in ("yes", "y", "true", "1", "on")
    repaired = False
    if not valid_psk(pw):
        if pw:
            log(f"setup password in {path.name} is not 8-63 characters — making a new one")
        pw = gen_password()
        repaired = True
    if create and (repaired or "setup_wifi_name" not in kv or "speak_password" not in kv):
        text = SETUP_TEMPLATE.format(name=name, password=pw, speak="yes" if speak else "no")
        for target in [path] + [c for c in candidates if c != path]:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                path = target
                break
            except Exception as e:
                log(f"could not write {target}: {e}")
    return {"name": name, "password": pw, "speak_password": speak, "path": str(path)}


def ensure_wifi_template() -> None:
    p = boot_dir() / WIFI_FILE_NAME
    if not p.exists():
        try:
            p.write_text(WIFI_TEMPLATE, encoding="utf-8")
        except Exception as e:
            log(f"could not create {p}: {e}")


# ── NetworkManager queries ────────────────────────────────────────────────────

def wlan_status() -> tuple:
    """(connected: bool, connection_name: str)."""
    rc, out = nm("-g", "GENERAL.STATE,GENERAL.CONNECTION", "device", "show", IFACE)
    if rc != 0:
        return False, ""
    lines = out.splitlines() + ["", ""]
    return lines[0].strip().startswith("100"), lines[1].rstrip("\r")


def wlan_ip() -> str:
    rc, out = nm("-g", "IP4.ADDRESS", "device", "show", IFACE)
    if rc != 0 or not out.strip():
        return ""
    return out.split("|")[0].strip().split("/")[0]


def con_ssid(name_or_uuid: str) -> str:
    rc, out = nm("-g", "802-11-wireless.ssid", "connection", "show", name_or_uuid)
    return out.split("\n")[0].rstrip("\r") if rc == 0 else ""


def con_exists(name: str) -> bool:
    rc, _ = nm("-g", "connection.id", "connection", "show", name)
    return rc == 0


def saved_wifi() -> list:
    """Saved client Wi-Fi profiles: [{name, uuid, ssid}] (setup/AP ones excluded)."""
    rc, out = nm("-t", "-f", "NAME,UUID,TYPE", "connection", "show")
    if rc != 0:
        return []
    res = []
    for line in out.splitlines():
        parts = split_terse(line)
        if len(parts) < 3 or parts[2] != "802-11-wireless":
            continue
        name, uuid = parts[0], parts[1]
        if name in (SETUP_CON, TRY_CON) + LEGACY_APS:
            continue
        rc2, info = nm("-g", "802-11-wireless.ssid,802-11-wireless.mode",
                       "connection", "show", uuid)
        lines = info.splitlines() + ["", ""]
        if rc2 != 0 or lines[1].strip() == "ap":
            continue
        res.append({"name": name, "uuid": uuid, "ssid": lines[0].rstrip("\r")})
    return res


def scan_networks() -> list:
    """Live scan (only meaningful while NOT hosting the setup hotspot)."""
    rc, out = nm("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                 "ifname", IFACE, "--rescan", "yes", timeout=30)
    best = {}
    if rc == 0:
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) < 3 or not parts[0]:
                continue
            ssid, sig, sec = parts[0], parts[1], parts[2]
            sig = int(sig) if sig.isdigit() else 0
            if ssid not in best or sig > best[ssid]["signal"]:
                best[ssid] = {"ssid": ssid, "signal": sig, "security": sec or "open"}
    return sorted(best.values(), key=lambda n: -n["signal"])


def has_clients() -> bool:
    """Is a phone/laptop currently joined to the setup hotspot?"""
    rc, out = run([IW, "dev", IFACE, "station", "dump"], timeout=5)
    if rc == 0:
        return "Station" in out
    rc, out = run(["ip", "neigh", "show", "dev", IFACE], timeout=5)
    return rc == 0 and bool(re.search(r"\b(REACHABLE|DELAY|PROBE)\b", out))


def recent_activity() -> bool:
    try:
        return time.time() - ACTIVITY_FILE.stat().st_mtime < ACTIVITY_HOLD
    except OSError:
        return False


def classify_failure(out: str) -> str:
    low = out.lower()
    if "secrets were required" in low or "802-1x" in low or "psk" in low \
            or "password" in low or "4-way" in low:
        return "wrong_password"
    if "no network with ssid" in low or "not found" in low:
        return "not_found"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    return "failed"


# ── the watchdog ──────────────────────────────────────────────────────────────

class Watch:
    def __init__(self):
        self.session = secrets.token_hex(4)
        self.events = []
        self.event_id = 0
        self.mode = "starting"
        self.reason = None
        self.target = None
        self.started = time.monotonic()
        self.offline_since = None
        self.next_retry = 0.0
        self.conf = {}
        self.ssid = ""
        self.ip = ""
        self.saved = []
        self.saved_at = 0.0
        self.scan = []
        self.announce_online = False

    # events ------------------------------------------------------------------
    def emit(self, etype: str, **kw) -> None:
        self.event_id += 1
        ev = {"id": self.event_id, "type": etype, "at": time.time(), **kw}
        self.events = (self.events + [ev])[-12:]
        log(f"event {etype} {({k: v for k, v in kw.items()})}")
        self.write_state()

    def write_state(self) -> None:
        if time.monotonic() - self.saved_at > 30:
            self.refresh_saved()
        data = {
            "mode": self.mode, "reason": self.reason, "target": self.target,
            "ssid": self.ssid, "ip": self.ip, "iface": IFACE,
            "setup_ssid": self.conf.get("name"),
            "setup_password": self.conf.get("password"),
            "setup_ip": SETUP_IP,
            "speak_password": self.conf.get("speak_password", True),
            "saved": [s["ssid"] for s in self.saved],
            "events": self.events, "session": self.session,
            "updated_at": time.time(),
        }
        try:
            write_private(STATE_FILE, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            log(f"could not write state: {e}")

    def refresh_saved(self) -> None:
        self.saved = saved_wifi()
        self.saved_at = time.monotonic()

    def refresh_link(self) -> bool:
        connected, con = wlan_status()
        online = connected and con not in (SETUP_CON, TRY_CON) + LEGACY_APS
        if online:
            self.ssid = con_ssid(con) or con
            self.ip = wlan_ip()
        elif connected and con == SETUP_CON:
            self.ssid, self.ip = "", SETUP_IP
        else:
            self.ssid, self.ip = "", ""
        return online

    def do_scan(self) -> list:
        self.scan = scan_networks()
        own = self.conf.get("name")
        self.scan = [n for n in self.scan if n["ssid"] != own]
        try:
            write_private(SCAN_FILE, json.dumps({"at": time.time(), "networks": self.scan},
                                                ensure_ascii=False), mode=0o644)
        except Exception as e:
            log(f"could not write scan: {e}")
        return self.scan

    # setup hotspot -------------------------------------------------------------
    def ensure_setup_profile(self) -> bool:
        name, pw = self.conf["name"], self.conf["password"]
        sec = ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", pw,
               "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
               "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable"]
        if con_exists(SETUP_CON):
            rc, out = nm("connection", "modify", SETUP_CON,
                         "802-11-wireless.ssid", name, *sec)
        else:
            rc, out = nm("connection", "add", "type", "wifi", "ifname", IFACE,
                         "con-name", SETUP_CON, "autoconnect", "no", "ssid", name,
                         "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                         "802-11-wireless.channel", "6",
                         "ipv4.method", "shared", "ipv4.addresses", f"{SETUP_IP}/24",
                         "ipv6.method", "disabled", *sec)
        if rc != 0:
            log(f"setup profile error: {out[-200:]}")
        return rc == 0

    def enter_setup(self, reason: str, quiet: bool = False) -> None:
        now = time.monotonic()
        if self.mode != "setup":
            self.conf = load_setup_conf()
            self.do_scan()                 # scan BEFORE the chip turns into a hotspot
            self.ensure_setup_profile()
            rc, out = nm("--wait", "25", "connection", "up", SETUP_CON,
                         "ifname", IFACE, timeout=40)
            if rc != 0:
                log(f"could not start setup Wi-Fi: {out[-200:]}")
                self.mode, self.reason = "offline", reason
                self.next_retry = now + 60
                self.refresh_link()
                self.write_state()
                return
            self.mode = "setup"
        self.reason = reason
        self.next_retry = now + (MANUAL_HOLD if reason == "manual" else RETRY_EVERY)
        self.refresh_link()
        self.emit("setup", reason=reason, quiet=quiet,
                  setup_ssid=self.conf.get("name"))

    def leave_setup(self) -> None:
        if self.mode in ("setup", "offline") or wlan_status()[1] == SETUP_CON:
            nm("connection", "down", SETUP_CON, timeout=20)
            nap(2)

    # joining networks ----------------------------------------------------------
    def wait_online(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds * SETTLE
        while True:
            if self.refresh_link():
                return True
            if time.monotonic() >= deadline:
                return False
            nap(1)

    def try_known(self) -> bool:
        """Leave setup mode and look for any saved network. True if online."""
        self.leave_setup()
        self.mode = "searching"
        self.write_state()
        visible = {n["ssid"]: n["signal"] for n in self.do_scan()}
        self.refresh_saved()
        if not self.wait_online(8):    # NetworkManager often reconnects by itself
            cands = sorted((s for s in self.saved if s["ssid"] in visible),
                           key=lambda s: -visible[s["ssid"]])
            for c in cands:
                log(f"trying saved network {c['ssid']!r}")
                rc, _ = nm("--wait", "30", "connection", "up", c["uuid"],
                           "ifname", IFACE, timeout=45)
                if rc == 0 and self.wait_online(5):
                    break
        if self.refresh_link():
            self.mode, self.reason, self.offline_since = "online", None, None
            self.emit("online", ssid=self.ssid)
            return True
        return False

    def connect(self, ssid: str, password: str, hidden: bool = False) -> None:
        prev_online = self.mode == "online" and self.refresh_link()
        _, prev_con = wlan_status()
        self.mode, self.target = "connecting", ssid
        self.emit("connecting", ssid=ssid)
        self.leave_setup()
        visible = {n["ssid"] for n in self.do_scan()}
        if con_exists(TRY_CON):
            nm("connection", "delete", TRY_CON)
        cmd = ["--wait", "45", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        cmd += ["ifname", IFACE, "name", TRY_CON]
        if hidden or ssid not in visible:
            cmd += ["hidden", "yes"]
        rc, out = nm(*cmd, timeout=70)
        ok = False
        if rc == 0:
            deadline = time.monotonic() + 10 * SETTLE
            while True:
                connected, con = wlan_status()
                if connected and con == TRY_CON:
                    ok = True
                    break
                if time.monotonic() >= deadline:
                    break
                nap(1)
        self.target = None
        if ok:
            # Keep exactly one profile per network name: drop older copies
            # (e.g. one with a password that has since changed).
            for s in saved_wifi():
                if s["ssid"] == ssid:
                    nm("connection", "delete", s["uuid"])
            nm("connection", "modify", TRY_CON, "connection.id", ssid,
               "connection.autoconnect", "yes")
            self.refresh_saved()
            self.mode, self.reason, self.offline_since = "online", None, None
            self.refresh_link()
            self.emit("connected", ssid=ssid)
            return
        nm("connection", "delete", TRY_CON)
        why = classify_failure(out)
        if ssid not in visible and why != "wrong_password":
            why = "not_found"          # mistyped name, or too far away
        log(f"could not join {ssid!r}: {why} ({out[-160:]})")
        self.emit("connect_failed", ssid=ssid, why=why)
        # Roll back: the network we were on before, then any known one,
        # then the setup hotspot again.
        if prev_online and prev_con and prev_con not in (SETUP_CON, TRY_CON):
            nm("--wait", "30", "connection", "up", prev_con, timeout=45)
            if self.wait_online(5):
                self.mode, self.reason = "online", None
                self.emit("online", ssid=self.ssid)
                return
            if self.try_known():
                return
        self.mode = "searching"
        self.enter_setup("connect_failed")

    # requests from admin.py / globe.py -----------------------------------------
    def process_requests(self) -> None:
        try:
            files = sorted(REQ_DIR.glob("*.json"))
        except OSError:
            return
        for f in files:
            try:
                req = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                req = None
            try:
                f.unlink()
            except OSError:
                pass
            if isinstance(req, dict):
                try:
                    self.handle(req)
                except Exception as e:
                    log(f"request failed: {e}")

    def handle(self, req: dict) -> None:
        action = str(req.get("action", ""))
        log(f"request: {action}")
        if action == "setup":
            if self.mode == "setup":
                self.next_retry = time.monotonic() + MANUAL_HOLD
                self.emit("setup", reason="manual", repeat=True,
                          setup_ssid=self.conf.get("name"))
            else:
                self.enter_setup("manual")
        elif action == "connect":
            ssid = str(req.get("ssid", ""))
            pw = str(req.get("password", ""))
            if not ssid or len(ssid.encode("utf-8")) > 32 or (pw and not valid_psk(pw)):
                self.emit("connect_failed", ssid=ssid, why="invalid")
                return
            self.connect(ssid, pw, bool(req.get("hidden")))
        elif action == "leave":
            if self.mode in ("setup", "offline"):
                if not self.try_known():
                    self.mode = "searching"
                    self.enter_setup(self.reason or "no_wifi", quiet=True)
        elif action == "forget":
            ssid = str(req.get("ssid", ""))
            for s in saved_wifi():
                if s["ssid"] == ssid:
                    nm("connection", "delete", s["uuid"])
            self.refresh_saved()
            self.write_state()
        elif action == "rescan":
            if self.mode == "online":
                self.do_scan()
                self.write_state()
        else:
            log(f"unknown request {action!r}")

    # SD-card Wi-Fi file ----------------------------------------------------------
    def apply_wifi_file(self) -> None:
        path = boot_dir() / WIFI_FILE_NAME
        kv = read_kv(path)
        ssid = kv.get("wifi_name", "")
        pw = kv.get("wifi_password", "")
        if not ssid:
            ensure_wifi_template()
            return
        if len(ssid.encode("utf-8")) > 32 or (pw and not valid_psk(pw)):
            log(f"{WIFI_FILE_NAME}: name too long or password not 8-63 characters — ignored")
            self.emit("wifi_file_invalid", ssid=ssid)
        else:
            for s in saved_wifi():
                if s["ssid"] == ssid:
                    nm("connection", "delete", s["uuid"])
            cmd = ["connection", "add", "type", "wifi", "ifname", IFACE,
                   "con-name", ssid, "ssid", ssid, "connection.autoconnect", "yes",
                   "connection.autoconnect-priority", "10"]
            if pw:
                cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", pw]
            rc, out = nm(*cmd)
            if rc == 0:
                log(f"added Wi-Fi {ssid!r} from the SD card")
                self.announce_online = True
                self.emit("wifi_added", ssid=ssid)
                nm("connection", "up", ssid, "ifname", IFACE, timeout=45)
            else:
                log(f"could not add Wi-Fi from SD card: {out[-200:]}")
                self.emit("wifi_file_invalid", ssid=ssid)
        try:
            path.write_text(WIFI_TEMPLATE, encoding="utf-8")   # wipe the password
        except Exception as e:
            log(f"could not reset {path}: {e}")

    # main loop -------------------------------------------------------------------
    def startup(self) -> None:
        self.conf = load_setup_conf()
        ensure_wifi_template()
        rc, out = nm("-t", "-f", "NAME", "connection", "show")
        for name in out.splitlines() if rc == 0 else []:
            if name in LEGACY_APS:
                log(f"removing old open hotspot profile {name!r}")
                nm("connection", "delete", name)
        self.ensure_setup_profile()
        self.apply_wifi_file()
        if wlan_status()[1] == SETUP_CON:      # service restarted mid-setup
            self.mode, self.reason = "setup", "no_wifi"
            self.next_retry = time.monotonic() + RETRY_EVERY
        self.refresh_link()
        self.write_state()
        log(f"started — setup Wi-Fi is {self.conf['name']!r}")

    def tick(self) -> None:
        self.process_requests()
        now = time.monotonic()
        online = self.refresh_link()
        if self.mode == "starting":
            if online:
                self.mode = "online"
                if self.announce_online:
                    self.announce_online = False
                    self.emit("connected", ssid=self.ssid)
            elif now - self.started > BOOT_GRACE * SETTLE:
                self.enter_setup("no_wifi")
        elif self.mode == "online":
            if online:
                self.offline_since = None
            else:
                self.offline_since = self.offline_since or now
                if now - self.offline_since > DROP_GRACE * SETTLE:
                    self.offline_since = None
                    self.enter_setup("wifi_lost")
        elif self.mode in ("setup", "offline"):
            if online:                              # something else brought Wi-Fi back
                self.mode, self.reason = "online", None
                self.emit("online", ssid=self.ssid)
            elif self.mode == "setup" and wlan_status()[1] != SETUP_CON:
                self.mode = "searching"             # hotspot vanished — bring it back
                self.enter_setup(self.reason or "no_wifi", quiet=True)
            elif now >= self.next_retry and not has_clients() and not recent_activity():
                if not self.try_known():
                    self.mode = "searching"
                    self.enter_setup(self.reason or "no_wifi", quiet=True)
            elif now >= self.next_retry:
                self.next_retry = now + 30          # someone is using it — check again soon
        elif self.mode == "searching":              # interrupted mid-search
            if not online:
                self.enter_setup(self.reason or "no_wifi", quiet=True)
            else:
                self.mode = "online"
        self.write_state()


def ensure_run_dirs() -> None:
    gid = group_id()
    for d, mode in ((RUN_DIR, 0o2770), (REQ_DIR, 0o2770)):
        d.mkdir(parents=True, exist_ok=True)
        try:
            if os.geteuid() == 0:
                os.chown(d, 0, gid)
            os.chmod(d, mode)
        except Exception:
            pass


def main() -> None:
    if "--init" in sys.argv:
        conf = load_setup_conf()
        ensure_wifi_template()
        print(f"setup_wifi_name={conf['name']}")
        print(f"setup_wifi_password={conf['password']}")
        print(f"file={conf['path']}")
        return
    ensure_run_dirs()
    w = Watch()
    try:
        w.startup()
    except Exception as e:
        log(f"startup problem (continuing): {e}")
    while True:
        try:
            w.tick()
        except Exception as e:
            log(f"tick error: {e}")
        time.sleep(TICK)


if __name__ == "__main__":
    main()
