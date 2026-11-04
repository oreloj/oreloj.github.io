#!/usr/bin/env bash
# install-voice.sh — let the globe record NEW phrases in its Piper voice.
#
#   bash ~/globe/install-voice.sh          (no sudo; needs internet, ~150 MB)
#
# The recorded phrase library (voice/<voice>.tar.gz) works without this:
# countries, stations and fixed messages already play in the new voice.
# This adds Piper on the globe for everything else — new stations, Wi-Fi
# names, the setup password — recorded once (a few seconds on a Pi 3),
# then kept. Without it, those few phrases use the old espeak voice.

set -uo pipefail
GLOBE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VOICE_ID="$(sed -n 's/^VOICE_ID *= *"\(.*\)".*/\1/p' "$GLOBE_DIR/globe.py" | head -1)"
VOICE_ID="${VOICE_ID:-en_GB-alan-medium}"
step() { echo "• $*"; }

if ! python3 -m pip --version >/dev/null 2>&1; then
    echo "pip is missing — install it once with:  sudo apt install -y python3-pip"
    exit 1
fi

step "Installing Piper (voice engine) — a few minutes on a Pi 3"
python3 -m pip install --user --break-system-packages --quiet piper-tts || {
    echo "Could not install piper-tts (no internet?). The globe keeps using espeak for new phrases."
    exit 1
}

step "Downloading the voice $VOICE_ID"
mkdir -p "$GLOBE_DIR/voice-model"
python3 -m piper.download_voices "$VOICE_ID" --data-dir "$GLOBE_DIR/voice-model" || {
    echo "Could not download the voice. Try again later."
    exit 1
}

step "Test: recording one phrase"
T0=$(date +%s)
if python3 -m piper -m "$GLOBE_DIR/voice-model/$VOICE_ID.onnx" -f /tmp/oreloj-voice-test.wav -- "Hello from Oreloj." 2>/dev/null; then
    echo "  ok, took $(( $(date +%s) - T0 )) s"
    rm -f /tmp/oreloj-voice-test.wav
else
    echo "  Piper is installed but could not speak — new phrases will use espeak."
fi

step "Restarting the globe"
systemctl --user restart globe.service 2>/dev/null || true
echo "✓ Voice installed."
