#!/bin/bash
# setup.sh — runs in the Cloud Routine container before each session.
# Installs Kokoro and its dependencies, then pre-downloads the model
# weights so the routine itself doesn't stall on a cold download.

set -e

echo "=== Installing system packages ==="
# espeak-ng is Kokoro's phonemizer backend; ffmpeg does the mp3 encode.
if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    SUDO=""
fi
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq espeak-ng ffmpeg

echo "=== Installing Python packages ==="
# torch CPU-only is much smaller and faster to install than the CUDA build.
pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
pip install --quiet kokoro soundfile numpy

echo "=== Pre-downloading Kokoro weights ==="
# Pulls the ~327MB model into the HuggingFace cache now, so the first
# render in the session is fast. Also fails loudly here if network
# access to huggingface.co isn't permitted.
python3 - <<'PY'
from kokoro import KPipeline
pipeline = KPipeline(lang_code='a')
# Render one short line to force the voice tensor to download too.
for _ in pipeline("Setup check.", voice="am_michael"):
    pass
print("Kokoro is ready.")
PY

echo "=== Setup complete ==="
