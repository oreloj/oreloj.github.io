#!/usr/bin/env bash
# install-setup-mode.sh — one-time install of Oreloj "setup mode"
#
#   sudo bash ~/globe/install-setup-mode.sh
#
# Safe to run again (e.g. after an update that changes oreloj-netwatch.py).
# What it sets up:
#   • oreloj-netwatch: watches the Wi-Fi. No known network → the globe opens
#     its own password-protected "Oreloj-N" Wi-Fi so a phone can set it up.
#   • Captive portal: phones joining that Wi-Fi get the setup page by themselves.
#   • http://oreloj.local works without ":5000".
#   • SD-card files oreloj-setup.txt (setup Wi-Fi name/password) and
#     oreloj-wifi.txt (add a Wi-Fi from any computer).
#   • Removes the old, open "Oreloj" hotspot.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo bash $0"
    exit 1
fi

GLOBE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORELOJ_USER="${SUDO_USER:-$(stat -c %U "$GLOBE_DIR")}"
ORELOJ_UID="$(id -u "$ORELOJ_USER")"
step() { echo "• $*"; }

command -v nmcli >/dev/null || { echo "NetworkManager (nmcli) not found — this needs Raspberry Pi OS Bookworm or newer."; exit 1; }

step "Installing the network watchdog"
install -d -m 755 /usr/local/lib/oreloj
install -m 755 "$GLOBE_DIR/oreloj-netwatch.py" /usr/local/lib/oreloj/oreloj-netwatch.py
sed "s/^Environment=ORELOJ_USER=.*/Environment=ORELOJ_USER=$ORELOJ_USER/" \
    "$GLOBE_DIR/oreloj-netwatch.service" > /etc/systemd/system/oreloj-netwatch.service

step "Removing the old open hotspot"
if [ -f /etc/systemd/system/oreloj-hotspot.service ]; then
    systemctl disable --now oreloj-hotspot.service 2>/dev/null || true
    rm -f /etc/systemd/system/oreloj-hotspot.service
fi
nmcli -t -f NAME connection show 2>/dev/null | grep -qx hotspot && nmcli connection delete hotspot >/dev/null || true

step "Captive portal (phones open the setup page by themselves)"
install -d -m 755 /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/oreloj-captive.conf <<'EOF'
# Oreloj setup Wi-Fi: answer every name with the globe's address, so a phone
# that joins it is sent straight to the setup page.
address=/#/192.168.4.1
EOF

step "Letting the web page use port 80 (http://oreloj.local without :5000)"
echo "net.ipv4.ip_unprivileged_port_start=80" > /etc/sysctl.d/60-oreloj.conf
sysctl -q -w net.ipv4.ip_unprivileged_port_start=80 || true

step "Shared folder for Wi-Fi requests"
echo "d /run/oreloj 2770 root $ORELOJ_USER -" > /etc/tmpfiles.d/oreloj.conf
echo "d /run/oreloj/requests 2770 root $ORELOJ_USER -" >> /etc/tmpfiles.d/oreloj.conf
systemd-tmpfiles --create /etc/tmpfiles.d/oreloj.conf

step "Creating the SD-card files"
SETUP_INFO="$(ORELOJ_USER="$ORELOJ_USER" python3 /usr/local/lib/oreloj/oreloj-netwatch.py --init)"

step "Starting the watchdog"
systemctl daemon-reload
systemctl enable oreloj-netwatch.service >/dev/null 2>&1
systemctl restart oreloj-netwatch.service

step "Restarting the globe and its web page"
sudo -u "$ORELOJ_USER" XDG_RUNTIME_DIR="/run/user/$ORELOJ_UID" \
    systemctl --user restart admin.service globe.service 2>/dev/null || \
    echo "  (could not restart admin/globe — reboot once to be sure)"
# Some older globes run them as system services instead — restart those too.
systemctl try-restart admin.service globe.service 2>/dev/null || true

NAME="$(echo "$SETUP_INFO" | sed -n 's/^setup_wifi_name=//p')"
PASS="$(echo "$SETUP_INFO" | sed -n 's/^setup_wifi_password=//p')"
FILE="$(echo "$SETUP_INFO" | sed -n 's/^file=//p')"
cat <<EOF

✓ Setup mode installed.

  Setup Wi-Fi name:      $NAME
  Setup Wi-Fi password:  $PASS
  (saved on the SD card in $(basename "$FILE") — write it on a sticker under the globe)

  The globe opens this Wi-Fi by itself when it can't find a known network,
  or when you tap the "setup" spot with the pen.
EOF
