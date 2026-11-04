#!/usr/bin/env python3
"""
render_voice.py — record the globe's phrase library with the Piper voice.

Like making a radio station's jingle library in advance: every phrase the
globe is likely to say is recorded once, here, on a fast computer, and
shipped as one small pack (voice/<VOICE_ID>.tar.gz). On the globe, globe.py
unpacks it at startup and plays the files instantly. Phrases not in the pack
are recorded on the globe itself the first time (if Piper is installed
there — see install-voice.sh), else espeak says them.

Phrases come from the project's own files, so the pack follows the data:
  • every plain-text speak("…") / _tts("…") in globe.py
  • every country / region announcement from calibration.csv
  • every station name from stations.csv, preset labels from actions.json

Never put secrets in here: the pack is public on GitHub. (The setup Wi-Fi
password is spoken at runtime only.)

Usage (needs: pip install piper-tts; ffmpeg):
  python3 tools/render_voice.py --model path/to/en_GB-alan-medium.onnx
"""

import argparse
import ast
import csv
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_globe():
    """Use globe.py's own voice settings + key function (one source of truth)."""
    spec = importlib.util.spec_from_file_location("globe_for_voice", ROOT / "globe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def phrases_from_code() -> set:
    """Plain string literals passed to speak()/_tts() in globe.py."""
    tree = ast.parse((ROOT / "globe.py").read_text(encoding="utf-8"))
    out = set()

    def strings(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.IfExp):
            yield from strings(node.body)
            yield from strings(node.orelse)
        elif isinstance(node, ast.BoolOp):          # e.g. name or "discover"
            for v in node.values:
                yield from strings(v)

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) in ("speak", "_tts"):
            for a in n.args:
                out.update(s for s in strings(a) if s.strip())
    return out


def phrases_from_data(globe) -> set:
    out = set()
    cal = ROOT / "calibration.csv"
    if cal.exists():
        for r in csv.DictReader(open(cal, encoding="utf-8")):
            entry = [r.get("country_name", ""), r.get("country_iso", ""),
                     r.get("region") or None, None, None]
            if entry[0]:
                out.add(globe._entries_to_tts([entry]))
    st = ROOT / "stations.csv"
    if st.exists():
        for r in csv.DictReader(open(st, encoding="utf-8")):
            name = (r.get("Radio Station") or "").strip()
            if name:
                out.add(name)
    out.update(f"Preset {i}" for i in range(1, 7))
    act = ROOT / "actions.json"
    if act.exists():
        try:
            for p in json.loads(act.read_text(encoding="utf-8")).get("presets", []):
                for k in ("label", "name"):
                    if (p.get(k) or "").strip():
                        out.add(p[k].strip())
        except Exception:
            pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Piper .onnx voice model")
    ap.add_argument("--only", help="render just this phrase (for testing)")
    args = ap.parse_args()

    globe = load_globe()
    from piper import PiperVoice, SynthesisConfig
    voice = PiperVoice.load(args.model)
    cfg = SynthesisConfig(length_scale=globe.VOICE_SPEED)

    phrases = {args.only} if args.only else phrases_from_code() | phrases_from_data(globe)
    phrases = sorted(" ".join(p.split()) for p in phrases)
    print(f"{len(phrases)} phrases, voice {globe.VOICE_ID} @ {globe.VOICE_SPEED}")

    pack = ROOT / "voice" / f"{globe.VOICE_ID}.tar.gz"
    pack.parent.mkdir(exist_ok=True)
    manifest = {}
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(pack, "w:gz") as tar:
        for i, text in enumerate(phrases):
            key = globe.voice_key(text)
            wav = Path(tmp) / f"{key}.wav"
            ogg = Path(tmp) / f"{key}.ogg"
            with wave.open(str(wav), "wb") as wf:
                voice.synthesize_wav(text, wf, syn_config=cfg)
            # Even loudness across phrases; Opus keeps the pack small.
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav),
                            "-af", "loudnorm=I=-16:TP=-1.5:LRA=7,"
                                   "adelay=60|60,apad=pad_dur=0.08",
                            "-ac", "1", "-c:a", "libopus", "-b:a", "40k", str(ogg)],
                           check=True)
            tar.add(ogg, arcname=ogg.name)
            manifest[key] = text
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(phrases)}  ({time.time() - t0:.0f}s)")
        data = json.dumps({"voice": globe.VOICE_ID, "speed": globe.VOICE_SPEED,
                           "phrases": manifest}, ensure_ascii=False, indent=1).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    print(f"wrote {pack.relative_to(ROOT)} — {pack.stat().st_size // 1024} KB, "
          f"{len(manifest)} phrases, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
