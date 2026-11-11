# Oreloj — Developer & Maintainer Guide

How to add stations, calibrate the globe, build new features, and ship changes — explained plainly. For deeper architecture notes see [CLAUDE.md](CLAUDE.md); for first-time Pi setup see [SETUP.md](SETUP.md).

---

## The big picture

Oreloj is two programs running on a Raspberry Pi tucked in the globe:

- **`globe.py`** — listens to the pen, figures out which country (or action spot) you tapped, and plays a stream.
- **`admin.py`** — a small web server (port 5000) that serves the website **and** an admin interface for managing everything.

The whole interface — public website *and* admin panel — is one file: **`index.html`**. On the Pi it's served at **http://oreloj.local:5000**; a public copy lives at the GitHub Pages site.

### The one rule that prevents headaches

There are two kinds of file, with **opposite** ownership:

| Kind | Files | Who's in charge | How you change it |
|---|---|---|---|
| **Code & docs** | `index.html`, `globe.py`, `admin.py`, the guides | The **repo** (GitHub) | Edit the file, then deploy out to the Pi + GitHub |
| **Runtime data** | `stations.csv`, `actions.json`, `calibration.csv`, favourites | The **Pi** (you edit it live in the admin UI) | Change it in the admin UI, then **Push to GitHub** |

On the Pi, the runtime data lives in its own folder, **`~/globe/data/`**. Think of `~/globe` as a recipe book that is replaced by a new edition at every update, and `data/` as the cook's own notebook beside it: updates swap the book and never open the notebook. The data files you see directly in the repo are just the shared starting point for a new globe.

⚠️ **Never copy a data file from the repo into the Pi's `data/` folder** — the Pi's version is the real one (it has the stations you added, zones you calibrated, presets you set). Overwriting it erases that work. Always go the other way: edit on the device, push to GitHub.

---

## Adding or editing stations

Easiest way — do it in the admin UI:

1. Open **http://oreloj.local:5000**.
2. Click **+ add radio** (top right).
3. Either **search Radio Browser** (30,000+ stations) and pick one, or fill it in by hand: **Name** and **Stream URL** are required; City + Country place it on the globe; Description and Website are optional.
4. Save. It appears on the globe immediately.
5. The **heart** icon on any station marks it a favourite (favourites get played first when you tap that country).
6. To keep your changes safe, push them to GitHub (see *Shipping changes*).

> A station's **Country** decides where it lives on the globe and which taps play it. Use the same country names already in the list (e.g. `UK`, `France`, `Scotland`).

---

## Calibrating the globe

The pen sends a 6-character code (like `a0387f`) for every spot you tap. Calibration is the map from those codes to countries and actions. You only do this once per globe (or when adding a new spot).

**To assign a country or action to a blank spot:**

1. Open the admin UI → the **calibration** panel.
2. Find the action you want to assign (or use the country search). Click its **Learn** button (the keyboard icon ⌨).
3. **Tap that spot on the globe** with the pen. The code is captured and saved automatically.
4. Write a small label on the globe with a marker so you remember what's there. (Marker is safe — it won't affect the pen.)

**For action zones specifically** (favourite, volume, discover, random love, presets, stop), use the **zones** panel to see what's assigned and edit any zone.

**Setting your six presets:** open the **presets** panel and assign a station to each of slots 1–6. Then tap the matching preset spot on the globe to play it.

> A zone marked **"needs calibration"** has no code yet — just hasn't been taught a spot on the globe. Learn it as above.

You can back up the full map any time with **↓ download calibration.csv** in the calibration panel.

---

## Building a new feature (e.g. a new action)

Features that respond to a tap are called **actions**. "Discover" and "Random love" are examples. Adding one touches a few places, but the pattern is always the same:

1. **Teach the globe what to do** — in `globe.py`, add a branch in `dispatch_action()` for your new action name (it picks a station, plays it, and speaks a confirmation).
2. **Make the website do the same** — in `index.html`, add a matching branch in `runActionZone()` so the web page behaves like the physical globe, and add the action to the `KNOWN_ACTIONS` list (and a button if you want one in the toolbar).
3. **Register a zone** — add an entry in `actions.json` so the action can be assigned to a spot on the globe. (It starts uncalibrated; you assign a real spot later via the calibration panel.)
4. **Ship it** (next section) and **calibrate** the new spot on the globe.

That's exactly how "random love" was added: a `random_love` branch in both `globe.py` and `index.html`, a button on the website, and a new zone in `actions.json`.

For changes to how stations are *chosen* or how the globe *looks*, you'll mostly be editing `globe.py` (playback logic) and `index.html` (the globe view, lists, styling) — both are commented.

---

## Shipping changes ("deploying")

The easiest path: tell Claude **"deploy"** — the `oreloj-deploy` skill handles all of this. If you're doing it by hand:

**For code & docs** (`index.html`, `globe.py`, `admin.py`, guides):
- Push to GitHub (and to the `gh-pages` branch too, whenever `index.html` changed — that's the public site).
- Copy the file to the Pi: `scp index.html oreloj@oreloj.local:~/globe/`
- Restart the affected service (below).

**For data** (`stations.csv`, `actions.json`, `calibration.csv`):
- Don't push from the repo. Edit it in the admin UI, then use the **developer** panel → **github sync** → **Push now** button (needs a GitHub token pasted in once). That keeps the Pi as the source of truth and copies your changes up to GitHub.

**Restarting the Pi's programs** (both are managed by the system, so it's one command each):

```bash
ssh oreloj@oreloj.local "systemctl --user restart globe.service"   # after globe.py / data changes
ssh oreloj@oreloj.local "systemctl --user restart admin.service"   # after index.html / admin.py changes
```

🚫 Don't kill `admin.py` with `pkill` and relaunch it by hand — it runs under the system manager and that breaks it. Use `systemctl --user restart admin.service`.

Quick health check after deploying:
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://oreloj.local:5000/   # should print 200
```

---

## Checking on the Pi

```bash
ssh oreloj@oreloj.local                       # log in
systemctl --user status globe.service          # is the globe running?
journalctl --user -u globe.service -f          # watch live logs (taps, station picks, errors)
```

When in doubt about what a tap is doing, the live logs show each pen code, the country it resolved to, and the station it picked.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| A tap plays the wrong region's station | The spot's pen code maps to the wrong country, or there are no curated stations for it. Check the **calibration** panel and add stations for that country. |
| New station doesn't show up | If you edited the repo's `stations.csv` instead of the admin UI, the Pi never got it. Add it in the admin UI instead. |
| A "ghost" station appears | A bad data row. The website now auto-cleans malformed entries on load — reload the page. Check `stations.csv` for a broken row. |
| Admin page won't load | `systemctl --user restart admin.service`, then check `curl http://oreloj.local:5000/`. |
| Changes I made on the globe vanished after a deploy | Something overwrote the Pi's data files from the repo. Restore from `~/globe/data/backups/` (or an older `*.bak-*` file) on the Pi, and only ever sync data **upward** (Push to GitHub). |
| Pen not detected | Re-plug the USB. Check it's seen: `python3 -c "import evdev; print([d.name for d in map(evdev.InputDevice, evdev.list_devices())])"` |
