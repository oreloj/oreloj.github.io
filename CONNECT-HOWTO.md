# Oreloj — connecting at a new place (Wi-Fi + Bluetooth)

Owner's guide for globes with **setup mode** (installed by
`install-setup-mode.sh`, Nov 2026). Older globes: see "Globes without setup
mode" at the bottom.

Think of the globe like a hotel guest: normally it sits on your Wi-Fi like
any device. When it can't find a Wi-Fi it knows, it opens its own little
Wi-Fi — **its "setup Wi-Fi"** — so your phone can walk over and tell it
where to go.

Each globe has its own setup Wi-Fi name and password, e.g. **Oreloj-1** /
`amber-falcon-427`. They're printed by the installer, saved on the SD card in
`oreloj-setup.txt`, shown in the web page (system → wi-fi), and the globe
says them out loud in setup mode. Write them on a sticker under the globe.

---

## Wi-Fi — three ways

### 1. Phone (normal way)

1. Switch the globe on. If it doesn't know any Wi-Fi around, after about a
   minute it says *"I can't find a Wi-Fi I know. On your phone, join the
   Wi-Fi called Oreloj 1…"*.
   - Want to change Wi-Fi while it's still online? Tap the **setup** spot
     with the pen (or *system → wi-fi → open setup Wi-Fi* in the web page).
2. On your phone, join **Oreloj-1** with its password. A setup page opens by
   itself. (If not, open a browser at **http://192.168.4.1**.)
3. Tap your network, type its password, **Join**. Your phone drops off
   Oreloj-1 — normal. Listen to the globe:
   - *"Connected to … All good."* → done.
   - *"The password seems wrong"* / *"I can't find a Wi-Fi called …"* → the
     setup Wi-Fi comes back within a minute; join it again and retry.

The globe also opens the setup Wi-Fi **by itself** if your Wi-Fi drops for
more than 2 minutes, and keeps trying your known networks every few minutes
(never while your phone is on the setup page). So after a power cut it
reconnects by itself once the router is back.

### 2. Before you move (from home)

While the globe is still online: web page → *system → wi-fi → Scan*, or the
phone page **http://oreloj.local/setup** → *Other network…* — add the new
place's network. If it isn't in range the globe says so and stays on your
current Wi-Fi (it rolls back), but you can add it at the new place with way 1.

### 3. SD card (from any computer)

1. Unplug the globe, put its SD card in a Mac/PC. Open **bootfs**.
2. Open `oreloj-wifi.txt`, fill in:
   ```
   wifi_name=My Network
   wifi_password=my password
   ```
   Capitals and spaces count. If the name ends with a space (the
   "Freebox Bestron " case), put it in quotes: `wifi_name="Freebox Bestron "`.
3. Save, eject, card back in, power on. The globe says *"Found a new Wi-Fi on
   the memory card"*, joins it, and empties the file again (your password
   doesn't stay on the card).

Forgot the setup password? It's in `oreloj-setup.txt` on the same card — you
can also change it there (8–63 characters).

---

## Finding the globe's web page

- Same Wi-Fi as the globe: **http://oreloj.local** (Oreloj 2:
  `http://oreloj2.local`). `:5000` still works too.
- `.local` not working (some Android phones, guest networks)? Tap the
  **say address** spot: the globe reads out its number, e.g.
  *"192 dot 168 dot 1 dot 147"*.
- The public site https://oreloj.github.io/ **can't** change globe settings —
  browsers block a secure page from talking to a device on your network.

---

## Bluetooth speaker

**Fast way:** put the speaker in pairing mode, then tap the **pair speaker**
spot with the pen (or *Pair nearest speaker* on the phone setup page). The
globe picks the closest speaker in pairing mode and says *"Connected to …"*.
From then on it reconnects the speaker by itself within 2 minutes whenever
it's on and in range.

To swap speakers: phone setup page → Speaker → **Forget** the old one, then
pair the new one.

If pairing fails:
1. Turn Bluetooth **off on phones nearby** — speakers latch onto the last
   phone they saw.
2. Wipe the speaker's device list (Bose SoundLink III: hold the Bluetooth
   button a full 10 seconds until a voice confirms).
3. Try again. Web page → *system → bluetooth → Scan* lists everything it sees.

### If the globe seems dead (taps register, no sound)

Almost never a crash — it's the speaker link; the globe keeps playing into
the empty headphone jack. Switch the speaker on and wait 2 minutes, or pair
again. **Plan B that always works:** a 3.5 mm aux cable from the Pi's jack to
the speaker's AUX input.

---

## Globes without setup mode

Run once on the globe (it needs the globe online and your password):

```
ssh -t oreloj@oreloj.local "cd ~/globe && git pull && sudo bash install-setup-mode.sh"
```

Until then, the only offline fix is the old cloud-init trick: on the SD card,
add the network under `access-points:` in `network-config`, and change the
`i=rpi-imager-…-netN` number in `cmdline.txt` (and `instance-id` in
`meta-data`) so the Pi re-reads it. Ask Claude — it's fiddly by hand.
