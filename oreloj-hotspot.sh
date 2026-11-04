#!/usr/bin/env bash
# oreloj-hotspot.sh
# Runs at boot. Waits 20 s for NetworkManager to join a known Wi-Fi network.
# If wlan0 is still unconnected, activates the "Oreloj" hotspot so the
# owner can configure Wi-Fi via the admin UI at http://192.168.4.1:5000

set -euo pipefail

# LEGACY — replaced by oreloj-netwatch (install-setup-mode.sh), which removes
# this unit. Kept only so a globe that pulls the update before running the
# installer still has a fallback. Stand down if the new watchdog is running.
if systemctl is-active --quiet oreloj-netwatch.service 2>/dev/null \
   || systemctl is-enabled --quiet oreloj-netwatch.service 2>/dev/null; then
    exit 0
fi

log() { echo "$(date '+%H:%M:%S') oreloj-hotspot: $*" >&2; }

log "Waiting 20 s for Wi-Fi..."
sleep 20

# Check whether wlan0 has an active non-hotspot connection
ACTIVE=$(nmcli -t -f DEVICE,STATE,CONNECTION device \
         | grep "^wlan0:connected:" \
         | grep -v ":hotspot$" || true)

if [ -n "$ACTIVE" ]; then
    log "Wi-Fi connected — hotspot not needed (${ACTIVE})"
    exit 0
fi

log "No Wi-Fi connection found — activating Oreloj hotspot"
if nmcli con up hotspot; then
    log "Hotspot active at 192.168.4.1"

    # Redirect port 80 → 5000 on wlan0 so the OS captive-portal probe
    # (which always hits port 80) lands on the admin UI.
    # This fires the automatic "Sign in to network" popup on iOS / Android.
    iptables -t nat -A PREROUTING -i wlan0 -p tcp --dport 80 \
             -j REDIRECT --to-port 5000 \
        && log "iptables: port 80 → 5000 redirect active" \
        || log "iptables: redirect failed (captive portal popup may not appear)"
else
    log "Failed to bring up hotspot (profile may not exist yet)"
fi
