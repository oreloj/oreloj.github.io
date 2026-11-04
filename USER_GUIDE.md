# Oreloj — User Guide

*Your globe that plays live radio from anywhere in the world.*

Point the pen at a country, and Oreloj tunes into a real radio station broadcasting there right now. Tap France, hear Paris. Tap Brazil, hear Rio. Tap somewhere you've never been and discover who's on air.

---

## 1. Getting started

**Turn it on.** Plug the globe in. It takes about a minute to wake up. There's no screen — it's ready when tapping a country plays sound.

**Connect it to your Wi-Fi** (first time only). The globe needs internet to stream radio.

1. When the globe can't find a Wi-Fi it knows, it opens its own **setup Wi-Fi** and tells you out loud: *"On your phone, join the Wi-Fi called Oreloj 1. The password is…"*. The name and password are also on the sticker under the globe.
2. On your phone, join that network. A setup page pops up by itself. (If it doesn't, open a browser and go to **http://192.168.4.1**.)
3. Tap your home network, type its password, tap **Join**.
4. Your phone leaves the setup Wi-Fi — that's normal. The globe says *"Connected to … All good."* If it says the password seems wrong, the setup Wi-Fi comes back in a minute: join it again and retry.

**Choose how you'll hear it:**
- **Bluetooth speaker** (recommended) — put the speaker in pairing mode, then tap **Pair nearest speaker** on the setup page, or tap the **pair speaker** spot on the globe. It says *"Connected to …"*.
- **Headphone jack** — plug a speaker or headphones into the globe's 3.5mm socket. This always works as a backup.

---

## 2. Playing radio

**Tap a country with the pen.** The globe says the country's name out loud, then a station from that region starts playing. Give it a few seconds to tune in.

**Tap the same country again** to hop to the next station there — like flipping through channels.

**Tap a new country** any time to switch. The old station stops and the new one begins.

That's the whole idea. Explore the globe and listen.

---

## 3. The special spots on the globe

Besides countries, your globe has a few **marked spots** that do things when you tap them (your globe's builder labels these with a pen). Common ones:

| Spot | What it does |
|---|---|
| ♥ **Favourite** | While a station plays, tap to save it as a favourite (you'll hear a chime and "Liked"). Tap again to remove it. |
| **Discover** | Plays a surprise station from anywhere in the world. |
| **Random love** | Plays a surprise station — but only from your favourites. |
| **Volume up / down** | Makes it louder or quieter. |
| **Stop** | Stops playback. |
| **Presets 1–6** | Your six saved go-to stations, one tap each. |
| **Setup** | Opens the globe's setup Wi-Fi so you can change Wi-Fi or speaker from your phone. Tap again to hear the name and password again. |
| **Say address** | The globe reads out where to find its web page (e.g. *"oreloj dot local, or 192 dot 168 dot 1 dot 147"*). |
| **Pair speaker** | Pairs the nearest Bluetooth speaker that's in pairing mode. |

Not every globe has all of these, and the labels are wherever they're written on the surface.

---

## 4. Using Oreloj from your phone or computer

You can also drive the globe (or just browse it) from a web page. On the same Wi-Fi, open **http://oreloj.local**. You'll see a spinning globe with dots for stations.

- **Tap a dot** (or search by name at the top) to play a station.
- **♥ random love** and **⚄ discover** buttons (top right) work just like the spots on the globe.
- **Heart icon** on any station saves it to your favourites.
- **Volume slider** at the top.
- **+ add radio** lets you add your own station (see the Developer Guide).

There's also a public version online at **https://oreloj.github.io/** — the same globe, browseable from anywhere, though it can't control the physical device (browsers block a public website from talking to devices on your home network).

---

## 5. Managing your speaker & Wi-Fi later

Open **http://oreloj.local/setup** for the simple phone page (Wi-Fi, saved networks, speakers), or **http://oreloj.local** → the **system** panel:
- **volume** — overall loudness.
- **bluetooth** — scan, pair, connect or switch speakers.
- **wi-fi** — connect to a different network, see the setup Wi-Fi name and password, or open the setup Wi-Fi.
- **update** — pulls the latest improvements onto the globe.

**Moving house?** Nothing to prepare: at the new place the globe opens its setup Wi-Fi by itself. Or, from any computer, write the new Wi-Fi into `oreloj-wifi.txt` on the globe's memory card (see CONNECT-HOWTO.md).

---

## 6. If something's not right

| Problem | Try this |
|---|---|
| No sound when I tap | Check the speaker is on and connected (system → bluetooth), or plug into the headphone jack. Make sure the globe has Wi-Fi. |
| It says a country but no music | That region may not have a station handy — tap again, or try a nearby country. |
| Can't reach oreloj.local | Make sure your phone/laptop is on the **same Wi-Fi** as the globe. Give the globe a minute after powering on. Still nothing? Tap the **say address** spot and type the number it reads out. |
| Wi-Fi changed / moved house | Wait a minute after switching on: the globe opens its setup Wi-Fi by itself and tells you its name. Or tap the **setup** spot. |
| Lost the setup Wi-Fi password | It's in `oreloj-setup.txt` on the globe's memory card, and the globe says it out loud in setup mode. |
| It feels stuck | Unplug the globe, wait 10 seconds, plug it back in. |

---

Enjoy the trip. 🌍📻
