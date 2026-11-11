#!/usr/bin/env python3
"""
oreloj_data.py — where a globe keeps its own data, and how updates leave it alone.

Think of ~/globe as a recipe book that gets replaced by a new edition on every
update. A globe's own notes (its stations, pen spots, presets, favourites)
used to be scribbled in that book's margins — so a new edition could not be
dropped in without a fight. Now the notes live in a separate notebook:

    ~/globe/data/                 the globe's own data. Updates never touch it.
        stations.csv              station list (edited in the control page)
        actions.json              pen spots + 6 presets
        calibration.csv           pen code → country map
        favourites.json           favourites
        config.json               optional per-globe settings (pen USB IDs)
        backups/                  automatic safety copies
        .defaults/stations.csv    the shared list as this globe last saw it

    ~/globe/stations.csv …        the SHARED defaults that come from GitHub.
                                  A new globe starts from them.

Station fixes pushed to GitHub still reach every globe: on update, each change
to the shared list is applied to the globe's own list — unless the globe's
owner changed that same station themselves, in which case theirs wins.

By hand (on the globe):
    python3 ~/globe/oreloj_data.py status     where things are
    python3 ~/globe/oreloj_data.py update     safe update from GitHub
    python3 oreloj_data.py rescue ~/globe     one-time: bring an OLD install
                                              (or a folder that isn't a git
                                              clone) onto this system

Standard library only — it must run before anything else is installed.
"""

import csv, io, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

REPO_DIR = Path(__file__).parent
REPO_URL = os.environ.get("ORELOJ_REPO_URL", "https://github.com/oreloj/oreloj.github.io.git")
BRANCH   = "main"

TRACKED    = ("stations.csv", "actions.json", "calibration.csv")   # also in git, as defaults
LOCAL_ONLY = ("favourites.json",)                                  # never in git
STATION_KEY = "Radio Station"
STATION_URL = "URL Link"
DEFAULT_PEN = (0x0c45, 0x7700)     # SONiX pen of the Horizon globe
KEEP_BACKUPS = 12


def data_dir(repo: Path | None = None) -> Path:
    env = os.environ.get("ORELOJ_DATA_DIR")
    return Path(env) if env else Path(repo or REPO_DIR) / "data"


DATA_DIR        = data_dir()
STATIONS_CSV    = DATA_DIR / "stations.csv"
ACTIONS_FILE    = DATA_DIR / "actions.json"
CALIBRATION_CSV = DATA_DIR / "calibration.csv"
FAVS_PATH       = DATA_DIR / "favourites.json"
CONFIG_PATH     = DATA_DIR / "config.json"


# ── small helpers ─────────────────────────────────────────────────────────────

def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _git(repo: Path, *args, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "git timed out"
    except FileNotFoundError:
        return -1, "git is not installed (sudo apt install git)"


def _git_bytes(repo: Path, rev: str, name: str) -> bytes | None:
    """The committed version of a file, or None if git doesn't have it."""
    try:
        r = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{name}"],
                           capture_output=True, timeout=30)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def is_clone(repo: Path | None = None) -> bool:
    return (Path(repo or REPO_DIR) / ".git").exists()


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _backup(repo: Path, name: str, data: bytes) -> Path:
    """Keep a dated safety copy in data/backups/ (oldest ones are pruned)."""
    bdir = data_dir(repo) / "backups"
    stem, _, ext = name.rpartition(".")
    path = bdir / f"{stem}-{_stamp()}.{ext}"
    n = 1
    while path.exists():
        n += 1
        path = bdir / f"{stem}-{_stamp()}-{n}.{ext}"
    _write_atomic(path, data)
    old = sorted(bdir.glob(f"{stem}-*.{ext}"))
    for p in old[:-KEEP_BACKUPS]:
        try:
            p.unlink()
        except OSError:
            pass
    return path


# ── per-globe settings ────────────────────────────────────────────────────────

def load_config(repo: Path | None = None) -> dict:
    try:
        cfg = json.loads((data_dir(repo) / "config.json").read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def pen_ids(default: tuple = DEFAULT_PEN, repo: Path | None = None) -> tuple:
    """USB vendor/product ID of this globe's pen. data/config.json may say
    {"pen_vid": "0x0c45", "pen_pid": "0x7700"} for a globe with another pen."""
    cfg = load_config(repo)
    try:
        return (int(str(cfg["pen_vid"]), 0), int(str(cfg["pen_pid"]), 0))
    except Exception:
        return default


_PEN_RES = (
    re.compile(r"^PEN_VID\s*,\s*PEN_PID\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*,\s*(0x[0-9a-fA-F]+|\d+)", re.M),
    re.compile(r"^PEN_VID\s*=\s*(0x[0-9a-fA-F]+|\d+)[\s\S]*?^PEN_PID\s*=\s*(0x[0-9a-fA-F]+|\d+)", re.M),
)


def _keep_local_pen_ids(repo: Path) -> str | None:
    """An old install may have had its pen IDs edited straight into globe.py.
    Before an update replaces that file, copy them into data/config.json."""
    try:
        text = (repo / "globe.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for rx in _PEN_RES:
        m = rx.search(text)
        if not m:
            continue
        ids = (int(m.group(1), 0), int(m.group(2), 0))
        cfg = load_config(repo)
        if ids == DEFAULT_PEN or "pen_vid" in cfg:
            return None
        cfg["pen_vid"], cfg["pen_pid"] = f"0x{ids[0]:04x}", f"0x{ids[1]:04x}"
        _write_atomic(data_dir(repo) / "config.json",
                      (json.dumps(cfg, indent=2) + "\n").encode())
        return f"kept this globe's pen IDs {cfg['pen_vid']}:{cfg['pen_pid']} in data/config.json"
    return None


# ── first run: give the globe its own notebook ────────────────────────────────

def ensure(repo: Path | None = None) -> list[str]:
    """Make sure data/ exists and holds every data file. A file that is not
    there yet is copied from the software folder — on a globe installed before
    data/ existed, that copy IS its live, edited data. Never overwrites.
    Safe to call from several programs at once."""
    repo = Path(repo or REPO_DIR)
    d = data_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    seeded = []
    for name in TRACKED + LOCAL_ONLY:
        dst, src = d / name, repo / name
        if dst.exists() or not src.exists():
            continue
        try:
            _write_atomic(dst, src.read_bytes())
        except OSError:
            continue
        seeded.append(name)
        if name == "stations.csv" and is_clone(repo):
            # Remember which shared list this globe's own list grew out of.
            base = _git_bytes(repo, "HEAD", name)
            if base is not None and not (d / ".defaults" / name).exists():
                _write_atomic(d / ".defaults" / name, base)
    return seeded


def _tidy_tracked(repo: Path) -> list[str]:
    """After data has moved to data/, put the shared defaults in the software
    folder back to exactly what GitHub has — but only for a file whose content
    is safely in data/ (byte-for-byte). That is what lets later updates apply."""
    if not is_clone(repo):
        return []
    done = []
    for name in TRACKED:
        work, mine = repo / name, data_dir(repo) / name
        head = _git_bytes(repo, "HEAD", name)
        if head is None or not work.exists() or not mine.exists():
            continue
        cur = work.read_bytes()
        if cur != head and cur == mine.read_bytes():
            rc, _ = _git(repo, "checkout", "--", name)
            if rc == 0:
                done.append(name)
    return done


# ── shared station list → this globe's list ───────────────────────────────────

def _read_rows(data: bytes) -> tuple[list[dict], list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    rd = csv.DictReader(io.StringIO(text, newline=""))
    rows = [{k: (v or "") for k, v in r.items() if k is not None} for r in rd]
    return rows, list(rd.fieldnames or [])


def _rows_bytes(rows: list[dict], fields: list[str]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _index(rows: list[dict]) -> dict:
    """name → row, in order. A repeated name becomes (name, 1), (name, 2)…"""
    out, seen = {}, {}
    for r in rows:
        k = (r.get(STATION_KEY) or "").strip()
        n = seen.get(k, 0)
        seen[k] = n + 1
        out[(k, n)] = r
    return out


def merge_stations(base: list[dict], mine: list[dict], new: list[dict],
                   fields: list[str]) -> tuple[list[dict], dict]:
    """Apply what changed in the shared list (base → new) to this globe's list.

      • a station added to the shared list is added here (unless a station
        with that name or that stream is already here)
      • a station removed from the shared list is removed here — only if this
        globe never changed it
      • a changed detail (say, a repaired stream address) is taken — only for
        the details this globe never changed itself
      • stations this globe added, changed or deleted itself stay as they are
    """
    def same(a, b):
        return (a or "").strip() == (b or "").strip()

    B, M, N = _index(base), _index(mine), _index(new)
    out, stats = [], {"added": 0, "updated": 0, "removed": 0}
    for k, row in M.items():
        b, n = B.get(k), N.get(k)
        if b is None:                     # this globe's own station
            out.append(row)
            continue
        if n is None:                     # gone from the shared list
            if all(same(row.get(f), b.get(f)) for f in fields):
                stats["removed"] += 1
            else:
                out.append(row)
            continue
        row, touched = dict(row), False
        for f in fields:
            if not same(b.get(f), n.get(f)) and same(row.get(f), b.get(f)):
                row[f] = n.get(f, "")
                touched = True
        stats["updated"] += touched
        out.append(row)
    urls = {(r.get(STATION_URL) or "").strip() for r in out}
    names = {(r.get(STATION_KEY) or "").strip() for r in out}
    for k, n in N.items():
        if k in B or k in M:
            continue
        url = (n.get(STATION_URL) or "").strip()
        if k[0] in names or (url and url in urls):
            continue
        out.append(dict(n))
        urls.add(url)
        names.add(k[0])
        stats["added"] += 1
    return out, stats


def sync_defaults(repo: Path | None = None) -> dict:
    """Bring changes of the shared station list (as committed in this clone)
    into data/stations.csv. Does nothing on a folder that isn't a git clone:
    there is no way to know which shared list its data grew out of."""
    repo = Path(repo or REPO_DIR)
    d = data_dir(repo)
    name = "stations.csv"
    if not is_clone(repo) or not (d / name).exists():
        return {}
    new = _git_bytes(repo, "HEAD", name)
    if new is None:
        return {}
    base_path = d / ".defaults" / name
    if not base_path.exists():
        _write_atomic(base_path, new)     # start following from here
        return {}
    base = base_path.read_bytes()
    if base == new:
        return {}
    new_rows, new_fields = _read_rows(new)
    base_rows, _ = _read_rows(base)
    mine_bytes = (d / name).read_bytes()
    mine_rows, fields = _read_rows(mine_bytes)
    if not new_rows or STATION_KEY not in new_fields:
        return {"skipped": "the shared station list looks broken — left alone"}
    fields = (fields or new_fields) + [f for f in new_fields if f not in fields]
    merged, stats = merge_stations(base_rows, mine_rows, new_rows, fields)
    if any(stats.values()):
        if not merged:
            return {"skipped": "merge would empty the station list — left alone"}
        stats["backup"] = str(_backup(repo, name, mine_bytes))
        _write_atomic(d / name, _rows_bytes(merged, fields))
    _write_atomic(base_path, new)
    return stats if any(v for k, v in stats.items() if k != "backup") else {}


def startup(repo: Path | None = None) -> dict:
    """Called once by admin.py when it starts."""
    repo = Path(repo or REPO_DIR)
    seeded = ensure(repo)
    tidied = _tidy_tracked(repo)
    return {"seeded": seeded, "tidied": tidied, "stations": sync_defaults(repo)}


# ── updating ──────────────────────────────────────────────────────────────────

_IDENT = ("-c", "user.name=Oreloj globe", "-c", "user.email=globe@oreloj.local")


def _set_aside_local_changes(repo: Path) -> tuple[bool, list[str], bool]:
    """Edits made straight in the software folder would block the update.
    Save them as a .patch file in data/backups/, then set them aside.
    → (ok, notes, stashed)"""
    notes = []
    rc, status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if rc != 0:
        return False, [status], False
    if not status.strip():
        return True, notes, False
    try:
        r = subprocess.run(["git", "-C", str(repo), "diff", "--binary", "HEAD"],
                           capture_output=True, timeout=60)
        if r.returncode == 0 and r.stdout:
            p = _backup(repo, "local-changes.patch", r.stdout)
            notes.append(f"local edits saved to data/backups/{p.name}")
    except Exception as e:
        return False, [f"could not save local edits ({e}) — nothing was changed"], False
    pen = _keep_local_pen_ids(repo)
    if pen:
        notes.append(pen)
    rc, out = _git(repo, *_IDENT, "stash", "push", "-m", f"oreloj update {_stamp()}")
    if rc != 0:
        return False, notes + [f"could not set local edits aside: {out}"], False
    return True, notes, True


def safe_update(repo: Path | None = None, url: str = REPO_URL, branch: str = BRANCH) -> dict:
    """Update the software from GitHub without touching the globe's data.
    → {"ok", "updated", "msg", …}. Never forces anything: if the update can't
    be applied cleanly it is undone and the reason is reported."""
    repo = Path(repo or REPO_DIR)
    ensure(repo)
    if not is_clone(repo):
        return {"ok": False, "updated": False,
                "msg": "This globe's software folder can't update itself yet. Run once:\n"
                       f"  python3 {repo}/oreloj_data.py rescue {repo}"}
    rc, out = _git(repo, "fetch", "--quiet", url, branch)
    if rc != 0:
        return {"ok": False, "updated": False, "msg": f"Could not reach GitHub:\n{out}"}
    _, old = _git(repo, "rev-parse", "HEAD")
    _, new = _git(repo, "rev-parse", "FETCH_HEAD")
    notes = []
    if old != new:
        rc, _ = _git(repo, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD")
        if rc != 0:
            return {"ok": False, "updated": False,
                    "msg": "This globe has its own saved code changes (commits), so the "
                           "update can't be applied automatically. Nothing was changed."}
        ok, notes, stashed = _set_aside_local_changes(repo)
        if not ok:
            return {"ok": False, "updated": False, "msg": "\n".join(notes)}
        rc, out = _git(repo, "merge", "--ff-only", "FETCH_HEAD")
        if rc != 0:
            if stashed:
                _git(repo, *_IDENT, "stash", "pop")       # back to exactly how it was
            return {"ok": False, "updated": False,
                    "msg": f"Update could not be applied — nothing was changed.\n{out}"}
    tidied = _tidy_tracked(repo)
    stations = sync_defaults(repo)
    if stations.get("skipped"):
        notes.append(stations["skipped"])
    elif stations:
        notes.append("stations: {added} added, {updated} repaired, {removed} removed "
                     "(from the shared list; your own changes kept)".format(**stations))
    updated = old != new
    head = f"Updated {old[:7]} → {new[:7]}." if updated else "Already up to date."
    return {"ok": True, "updated": updated, "changed_data": bool(stations and not stations.get("skipped")),
            "msg": "\n".join([head] + notes), "from": old, "to": new, "tidied": tidied}


def adopt(repo: Path, url: str = REPO_URL, branch: str = BRANCH) -> dict:
    """Turn a plain copied folder (no git) into a proper clone, so "update
    now" works there. The globe's data is moved to data/ first; whatever
    differs from GitHub in the code is saved as a patch, then replaced."""
    repo = Path(repo)
    ensure(repo)                                   # data is safe before git touches anything
    for name in TRACKED:
        if (repo / name).exists() and not (data_dir(repo) / name).exists():
            return {"ok": False, "msg": f"could not copy {name} into data/ — stopped, nothing changed"}
    steps = (("init", "-q"),
             ("symbolic-ref", "HEAD", f"refs/heads/{branch}"),
             ("remote", "add", "origin", url),
             ("fetch", "--quiet", "origin", branch),
             ("reset", "-q", f"origin/{branch}"),
             ("branch", "-q", "--set-upstream-to", f"origin/{branch}"))
    for step in steps:
        rc, out = _git(repo, *step)
        if rc != 0:
            if step[0] in ("init", "symbolic-ref", "remote", "fetch"):
                shutil.rmtree(repo / ".git", ignore_errors=True)   # leave the folder as it was
            return {"ok": False, "msg": f"git {step[0]} failed — {out}"}
    ok, notes, _ = _set_aside_local_changes(repo)
    if not ok:
        return {"ok": False, "msg": "\n".join(notes)}
    sync_defaults(repo)                            # follow the shared list from now on
    _, head = _git(repo, "rev-parse", "--short", "HEAD")
    return {"ok": True, "updated": True,
            "msg": "\n".join([f"This folder is now a proper clone at {head}."] + notes)}


def status(repo: Path | None = None) -> str:
    repo = Path(repo or REPO_DIR)
    d = data_dir(repo)
    lines = [f"software folder : {repo}  ({'git clone' if is_clone(repo) else 'NOT a git clone'})",
             f"data folder     : {d}"]
    for name in TRACKED + LOCAL_ONLY + ("config.json",):
        p = d / name
        lines.append(f"  {name:<16} {'%7d bytes' % p.stat().st_size if p.exists() else 'not there yet'}")
    vid, pid = pen_ids(repo=repo)
    lines.append(f"pen USB IDs     : {vid:04x}:{pid:04x}")
    if is_clone(repo):
        _, head = _git(repo, "log", "-1", "--format=%h %s")
        _, st = _git(repo, "status", "--porcelain", "--untracked-files=no")
        lines.append(f"software version: {head}")
        lines.append("local code edits: " + (", ".join(l[3:] for l in st.splitlines()) or "none"))
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if len(args) > 1:
        repo = Path(args[1]).expanduser()
    elif (REPO_DIR / "globe.py").exists():
        repo = REPO_DIR
    else:
        repo = Path.home() / "globe"
    if not (repo / "globe.py").exists():
        print(f"{repo} doesn't look like the Oreloj folder (no globe.py)")
        return 2
    if cmd == "status":
        print(status(repo))
        return 0
    if cmd in ("update", "rescue"):
        res = safe_update(repo) if is_clone(repo) else (
            adopt(repo) if cmd == "rescue" else safe_update(repo))
        print(res["msg"])
        if res["ok"] and res.get("updated"):
            print("\nNow restart the globe's programs:\n  systemctl --user restart globe admin")
        return 0 if res["ok"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
