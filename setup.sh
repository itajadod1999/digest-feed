#!/bin/bash
# setup.sh — runs in the Cloud Routine container before each session.
#
# Installs into a virtualenv rather than the system Python. The container's
# Debian-patched setuptools can't build older sdist packages (docopt, which
# Kokoro needs indirectly), so a clean venv with fresh setuptools is the fix.

set -e

VENV="$HOME/kokoro-venv"

echo "=== Creating virtualenv at $VENV ==="
python3 -m venv "$VENV"

echo "=== Upgrading build tooling ==="
"$VENV/bin/pip" install --quiet --upgrade pip setuptools wheel

echo "=== Installing Kokoro ==="
"$VENV/bin/pip" install --quiet kokoro soundfile numpy torch

echo "=== Pre-downloading models ==="
# Pulls Kokoro's weights (~327MB, huggingface.co), the voice tensor, and the
# spaCy English model (github.com). Doing it here keeps the 6 AM run fast and
# makes a blocked domain fail loudly now rather than silently later.
"$VENV/bin/python" - <<'PY'
from kokoro import KPipeline
pipeline = KPipeline(lang_code='a')
for _ in pipeline("Setup check.", voice="am_michael"):
    pass
print("Kokoro is ready.")
PY

echo "=== Verifying mp3 encoding ==="
"$VENV/bin/python" - <<'PY'
import numpy as np, soundfile as sf, tempfile, os
w = np.zeros(24000, dtype="float32")
p = tempfile.mktemp(suffix=".mp3")
sf.write(p, w, 24000, format="MP3")
print("mp3 encoder OK:", os.path.getsize(p), "bytes")
os.remove(p)
PY

echo "=== Setup complete ==="
echo "Run the publisher with: $VENV/bin/python publish_kokoro.py script.txt"
