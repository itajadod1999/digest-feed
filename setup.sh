#!/bin/bash
# setup.sh — runs in the Cloud Routine container before each session.
# Installs Kokoro and pre-downloads its models so the routine itself
# doesn't stall on a cold download.
#
# No apt packages needed: espeak comes in via the espeakng-loader pip
# package, and mp3 encoding is handled by soundfile's built-in encoder.

set -e

echo "=== Installing Python packages ==="
pip install --quiet --upgrade pip
pip install --quiet kokoro soundfile numpy torch

echo "=== Pre-downloading models ==="
# Pulls the Kokoro weights (~327MB from huggingface.co), the voice tensor,
# and the spaCy English model (from github.com). Doing it here means the
# routine's own run is fast, and a blocked domain fails loudly right now
# instead of silently at 6 AM.
python3 - <<'PY'
from kokoro import KPipeline
pipeline = KPipeline(lang_code='a')
for _ in pipeline("Setup check.", voice="am_michael"):
    pass
print("Kokoro is ready.")
PY

echo "=== Verifying mp3 encoding ==="
python3 - <<'PY'
import numpy as np, soundfile as sf, tempfile, os
w = np.zeros(24000, dtype="float32")
p = tempfile.mktemp(suffix=".mp3")
sf.write(p, w, 24000, format="MP3")
print("mp3 encoder OK:", os.path.getsize(p), "bytes")
os.remove(p)
PY

echo "=== Setup complete ==="
