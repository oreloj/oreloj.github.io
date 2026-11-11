#!/usr/bin/env python3
"""Check that every Oreloj station stream actually plays audio.

One checker, used in three places:
  * by hand:        python3 check_stations.py            (in the project folder)
  * on the globe:   the "check streams" button in the control page (admin.py)
  * every night:    the GitHub Action .github/workflows/stream-check.yml

A stream counts as alive only when it really sends audio — not a webpage, not
a 404, not a server that answers and then sends nothing. Each failure is
retried once before it is called dead, to avoid false alarms.

Needs nothing but Python 3.9+ (no pip installs).

Usage:
    python3 check_stations.py [stations.csv] [--builtin index.html]
                              [--presets actions.json] [--json out.json]
                              [--previous old.json] [--all] [--workers N]

Exit code: 0 = all alive, 1 = at least one dead, 2 = input file missing.
"""

from __future__ import annotations

import argparse, csv, json, re, socket, ssl, sys, threading, time, warnings
import http.client, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

# First try as a media player (like the globe's mpv). Old SHOUTcast servers send a
# webpage to anything that looks like a browser. Retry as a browser if refused.
UA_PLAYER  = "Lavf/60.16.100 (Oreloj stream check)"
UA_BROWSER = "Mozilla/5.0 (compatible; Oreloj-stream-check/2.0; +https://oreloj.github.io)"
_ua = threading.local()
NEED_BYTES = 16 * 1024          # this much audio must arrive for "alive"
AUDIO_TYPES = ("audio/", "application/ogg", "application/octet-stream",
               "video/mp2t", "application/aac", "video/mp4", "binary/octet-stream")


def _ssl_ctx(verify: bool) -> ssl.SSLContext:
    if not verify:                          # lenient: bad certificates, old TLS settings
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except (ssl.SSLError, ValueError, AttributeError):
            pass
        return ctx
    try:
        import certifi                      # macOS python often lacks system CAs
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _looks_like_audio(data: bytes) -> bool:
    if data[:3] == b"ID3" or data[:4] in (b"OggS", b"fLaC", b"RIFF") or data[:1] == b"G":
        return True
    if b"ftyp" in data[:16]:                # mp4 / fMP4 segment
        return True
    for i in range(min(len(data) - 1, 4096)):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:   # MPEG / ADTS frame sync
            return True
    return False


class _Result(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


def _headers() -> dict:
    return {"User-Agent": getattr(_ua, "value", UA_PLAYER), "Accept": "*/*", "Icy-MetaData": "0"}


def _open(url: str, timeout: float, verify: bool = True):
    req = urllib.request.Request(url, headers=_headers())
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(verify))


def _read(resp, n: int, timeout: float) -> bytes:
    deadline, got = time.monotonic() + timeout, b""
    while len(got) < n and time.monotonic() < deadline:
        chunk = resp.read(min(8192, n - len(got)))
        if not chunk:
            break
        got += chunk
    return got


def _icy_v1(url: str, timeout: float) -> tuple[str, str]:
    """Old SHOUTcast servers answer 'ICY 200 OK', which urllib refuses. Speak it by hand."""
    u = urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    if u.scheme == "https":
        sock = _ssl_ctx(False).wrap_socket(sock, server_hostname=u.hostname)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    sock.sendall(f"GET {path} HTTP/1.0\r\nHost: {u.hostname}\r\nUser-Agent: {_headers()['User-Agent']}\r\n"
                 f"Icy-MetaData: 0\r\n\r\n".encode())
    deadline, data = time.monotonic() + timeout, b""
    try:
        while len(data) < NEED_BYTES + 2048 and time.monotonic() < deadline:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    if not head.startswith((b"ICY 200", b"HTTP/1.0 200", b"HTTP/1.1 200")):
        return "dead", "server refused the stream"
    if len(body) < NEED_BYTES // 2:
        return "dead", "stream stalls"
    return "ok", "audio (old SHOUTcast server)"


def _hls(url: str, text: str, timeout: float, verify: bool) -> tuple[str, str]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):       # master → lightest variant
        variants = []
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                bw = re.search(r"BANDWIDTH=(\d+)", l)
                variants.append((int(bw.group(1)) if bw else 0, lines[i + 1]))
        url = urljoin(url, min(variants)[1])
        with _open(url, timeout, verify) as r:
            lines = [l.strip() for l in r.read(256 * 1024).decode("utf-8", "replace").splitlines() if l.strip()]
    segs = [l for l in lines if not l.startswith("#")]
    if not segs:
        return "dead", "empty HLS playlist"
    with _open(urljoin(url, segs[-1]), timeout, verify) as r:
        if len(_read(r, 4096, timeout)) < 1024:
            return "dead", "HLS segment empty"
    return "ok", "audio (HLS)"


def _probe_once(url: str, timeout: float, depth: int = 0) -> tuple[str, str]:
    if not re.match(r"https?://", url or ""):
        return "dead", "not a stream address" if url else "no stream address"
    note, verify = "", True
    try:
        try:
            resp = _open(url, timeout)
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError) or "CERTIFICATE" in str(e.reason).upper():
                verify, note = False, "its security certificate is broken or outdated"
                resp = _open(url, timeout, verify=False)
            else:
                raise
    except urllib.error.HTTPError as e:
        word = {401: "access refused", 403: "access refused", 404: "not found"}.get(
            e.code, "server error" if e.code >= 500 else "error")
        return "dead", f"{word} ({e.code})"
    except http.client.BadStatusLine:
        try:
            return _icy_v1(url, timeout)
        except Exception:
            return "dead", "server unreachable"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.gaierror):
            return "dead", "server no longer exists"
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
            return "dead", "no answer (timeout)"
        return "dead", "server unreachable"

    with resp:
        final = resp.geturl()
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        icy = any(k.lower().startswith("icy-") for k in resp.headers.keys())
        path = urlsplit(final).path.lower()

        if "mpegurl" in ctype or path.endswith((".m3u8", ".m3u", ".pls")) or "scpls" in ctype:
            text = resp.read(256 * 1024).decode("utf-8", "replace")
            if "#EXT-X-" in text:
                status, detail = _hls(final, text, timeout, verify)
            else:
                targets = [l.split("=", 1)[-1].strip() if l.lower().startswith("file") else l.strip()
                           for l in text.splitlines()]
                targets = [t for t in targets if t.startswith("http")]
                if not targets:
                    return "dead", "empty playlist"
                if depth:
                    return "dead", "playlist inside a playlist"
                status, detail = _probe_once(targets[0], timeout, depth + 1)
        elif ctype.startswith("text/html"):
            return "dead", "a webpage, not a stream"
        else:
            data = _read(resp, NEED_BYTES, timeout)
            if len(data) < NEED_BYTES:
                return "dead", "stream stalls (no audio arriving)"
            if not (icy or any(ctype.startswith(t) for t in AUDIO_TYPES) or _looks_like_audio(data)):
                return "dead", f"not audio ({ctype or 'unknown type'})"
            status, detail = "ok", "audio"

    if status == "ok" and note:
        return "warn", f"plays, but {note} — may fail on the website"
    if status == "ok" and url.startswith("http://"):
        return "warn", "plays on the globe; browsers block http:// on the website"
    return status, detail


def probe(url: str, timeout: float = 8.0) -> tuple[str, str]:
    """Return (status, detail). status is 'ok', 'warn' (plays, with a caveat) or 'dead'."""
    first = None
    for ua in (UA_PLAYER, UA_BROWSER):      # 2nd try = retry before calling it dead
        _ua.value = ua
        try:
            status, detail = _probe_once(url, timeout)
        except Exception as e:              # never let one weird server crash the run
            status, detail = "dead", f"error ({type(e).__name__})"
        if status != "dead" or not re.match(r"https?://", url or ""):
            return status, detail
        first = first or (status, detail)
        time.sleep(2)
    return first


# ── Where the station lists live ────────────────────────────────────────────

def load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [{"name": r.get("Radio Station", "?"), "country": r.get("Country", ""),
                 "url": (r.get("URL Link") or "").strip(), "source": "station"}
                for r in csv.DictReader(f)]


def load_builtin(path: Path) -> list[dict]:
    """Stations hardcoded in index.html's BUILT_IN list (only those with their own stream)."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^const BUILT_IN = \[(.*?)^\];", text, re.S | re.M)
    out = []
    for line in (m.group(1) if m else "").splitlines():
        n, s, c = (re.search(rf'{k}:"([^"]*)"', line) for k in ("name", "stream", "country"))
        if n and s:
            out.append({"name": n.group(1), "country": c.group(1) if c else "",
                        "url": s.group(1), "source": "website"})
    return out


def load_presets(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [{"name": p.get("name") or f"Preset {p.get('slot')}", "country": "",
             "url": (p.get("url") or "").strip(), "source": f"preset {p.get('slot')}"}
            for p in data.get("presets", []) if (p.get("url") or "").strip()]


# ── Run ─────────────────────────────────────────────────────────────────────

def run(entries: list[dict], workers: int = 12, timeout: float = 8.0,
        progress=None, previous: dict | None = None) -> dict:
    """Check every entry (each URL tested once). Returns the report dict.

    `progress(done, total)` is called after each URL. `previous` is an earlier
    report: it carries `dead_since` / `nights` forward so a stream must stay
    dead across checks before the website hides it.
    """
    urls = list(dict.fromkeys(e["url"] for e in entries))
    results, lock, done = {}, threading.Lock(), [0]

    def job(u):
        res = probe(u, timeout) if u else ("dead", "no stream address")
        with lock:
            results[u] = res
            done[0] += 1
            if progress:
                progress(done[0], len(urls))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(job, urls))

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    prev = {e["url"]: e for e in (previous or {}).get("stations", []) if e.get("status") == "dead"}
    stations = []
    for e in entries:
        status, detail = results[e["url"]]
        row = {**e, "status": status, "detail": detail}
        if status == "dead":
            p = prev.get(e["url"])
            row["dead_since"] = p.get("dead_since", now) if p else now
            row["checks_dead"] = (p.get("checks_dead", 1) + 1) if p else 1
        stations.append(row)
    summary = {s: sum(1 for r in stations if r["status"] == s) for s in ("ok", "warn", "dead")}
    return {"checked_at": now, "summary": summary, "stations": stations}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # On a globe, its own list lives in data/ (see oreloj_data.py).
    own = Path(__file__).parent / "data" / "stations.csv"
    ap.add_argument("csv", nargs="?", type=Path,
                    default=own if own.exists() else Path("stations.csv"))
    ap.add_argument("--builtin", type=Path, help="also check index.html's BUILT_IN streams")
    ap.add_argument("--presets", type=Path, help="also check preset URLs in actions.json "
                    "(default: actions.json next to the CSV, if present)")
    ap.add_argument("--no-presets", action="store_true", help="don't check actions.json presets")
    ap.add_argument("--json", type=Path, help="write the full report to this file")
    ap.add_argument("--previous", type=Path, help="earlier report, to count repeat failures")
    ap.add_argument("--all", action="store_true", help="list working stations too")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()

    if not args.csv.exists():
        print(f"ERROR: {args.csv} not found — run this inside the Oreloj folder"); return 2
    entries = load_csv(args.csv)
    if args.builtin:
        entries += load_builtin(args.builtin)
    presets = args.presets or args.csv.with_name("actions.json")
    if not args.no_presets and presets.exists():
        entries += load_presets(presets)
    previous = None
    if args.previous and args.previous.exists():
        try:
            previous = json.loads(args.previous.read_text(encoding="utf-8"))
        except ValueError:
            pass

    tty = sys.stdout.isatty()
    def progress(done, total):
        if tty:
            print(f"\r  checking {done}/{total}…", end="", flush=True)

    print(f"Checking {len({e['url'] for e in entries})} streams…")
    report = run(entries, args.workers, args.timeout, progress, previous)
    if tty:
        print("\r" + " " * 40 + "\r", end="")

    order = {"dead": 0, "warn": 1, "ok": 2}
    seen = set()
    for r in sorted(report["stations"], key=lambda r: (order[r["status"]], r["name"].lower())):
        key = (r["name"], r["url"])
        if key in seen or (r["status"] == "ok" and not args.all):
            continue
        seen.add(key)
        mark = {"dead": "✗", "warn": "!", "ok": "✓"}[r["status"]]
        where = f"{r['name']}" + (f" ({r['country']})" if r["country"] else "") + \
                (f" [{r['source']}]" if r["source"] != "station" else "")
        print(f"  {mark}  {where:<48} {r['detail']}")
        if r["status"] != "ok":
            print(f"       {r['url'] or '—'}")

    s = report["summary"]
    print(f"\n{s['ok'] + s['warn']} playing, {s['dead']} dead"
          + (f", {s['warn']} with a caveat (!)" if s["warn"] else ""))
    if args.json:
        tmp = args.json.with_suffix(args.json.suffix + ".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(args.json)
    return 1 if s["dead"] else 0


if __name__ == "__main__":
    sys.exit(main())
