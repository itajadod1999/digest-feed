#!/usr/bin/env python3
"""
publish_kokoro.py — same as publish.py, but renders audio locally with
Kokoro instead of calling a paid TTS API. No API key, no per-character cost.

  1. Reads a script file (plain text, no markdown).
  2. Renders it with Kokoro (runs on CPU, no GPU needed).
  3. Writes one mp3 into episodes/.
  4. Rebuilds feed.xml from whatever episodes exist on disk.

Usage:
    python publish_kokoro.py script.txt

Requires (installed by setup.sh):
    espeak-ng, ffmpeg, and: pip install kokoro soundfile numpy
"""

import html
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

# ── CONFIGURE ────────────────────────────────────────────────
FEED_BASE_URL = "https://itajadod1999.github.io/digest-feed"
PODCAST_TITLE = "Daily Digest"
PODCAST_DESC = "A personal daily news briefing."
PODCAST_AUTHOR = "Automated"

# Kokoro voices. lang_code 'a' = American English, 'b' = British English.
# American male:   am_michael, am_fenrir, am_puck, am_adam
# American female: af_heart, af_bella, af_nicole, af_sarah
# British:         bm_george, bm_lewis, bf_emma, bf_isabella
LANG_CODE = "a"
VOICE = "am_michael"
SPEED = 1.05          # 1.0 is natural; 1.05-1.15 reads more like news pace

SAMPLE_RATE = 24000   # Kokoro outputs 24kHz — don't change
MP3_BITRATE = "64k"   # 64k mono is plenty for speech; keeps the repo small
KEEP_EPISODES = 30
# ─────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
EPISODE_DIR = ROOT / "episodes"


def render_audio(text):
    """Run Kokoro over the script, return a single concatenated waveform."""
    import numpy as np
    from kokoro import KPipeline

    pipeline = KPipeline(lang_code=LANG_CODE)

    # Kokoro splits on blank lines by default and yields one segment at a
    # time. That's why the prompt insists on paragraph breaks — the seams
    # land on natural pauses.
    segments = []
    for i, (_graphemes, _phonemes, audio) in enumerate(
        pipeline(text, voice=VOICE, speed=SPEED), start=1
    ):
        # Kokoro hands back a torch tensor; convert to numpy first.
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        audio = np.asarray(audio, dtype="float32")

        segments.append(audio)
        # A short silence between paragraphs, so it doesn't run together
        segments.append(np.zeros(int(SAMPLE_RATE * 0.35), dtype="float32"))
        print(f"  rendered segment {i}", flush=True)

    if not segments:
        raise SystemExit("Kokoro produced no audio. Check the script contents.")

    return np.concatenate(segments)


def write_mp3(waveform, out_path):
    """
    Write the mp3. Tries soundfile's built-in MP3 encoder first (no external
    tools needed), and only falls back to ffmpeg if that isn't available.
    """
    import soundfile as sf

    # Preferred path: libsndfile encodes mp3 directly.
    try:
        sf.write(str(out_path), waveform, SAMPLE_RATE, format="MP3")
        return
    except Exception as e:
        print(f"  (direct mp3 write unavailable: {e}; trying ffmpeg)")

    # Fallback: write a wav, transcode with ffmpeg.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    sf.write(wav_path, waveform, SAMPLE_RATE)

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", wav_path,
             "-ac", "1", "-b:a", MP3_BITRATE,
             str(out_path)],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        Path(wav_path).unlink(missing_ok=True)
        raise SystemExit(
            "Could not write an mp3: soundfile's encoder failed and ffmpeg "
            "isn't installed. Run:  brew install ffmpeg"
        )

    Path(wav_path).unlink(missing_ok=True)

    if result.returncode != 0:
        raise SystemExit(f"ffmpeg failed: {result.stderr[:400]}")


def build_episode(script_path):
    text = Path(script_path).read_text(encoding="utf-8").strip()
    if len(text) < 200:
        raise SystemExit(f"Script is only {len(text)} chars — refusing to publish. "
                         "Something upstream probably failed.")

    words = len(text.split())
    print(f"Script: {words} words, {len(text)} chars")

    waveform = render_audio(text)
    duration = len(waveform) / SAMPLE_RATE
    print(f"Rendered {duration / 60:.1f} minutes of audio")

    EPISODE_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).astimezone()
    stem = f"digest-{today.strftime('%Y-%m-%d')}"
    out_path = EPISODE_DIR / f"{stem}.mp3"

    write_mp3(waveform, out_path)
    print(f"Wrote {out_path} ({out_path.stat().st_size / 1_000_000:.1f} MB)")

    (EPISODE_DIR / f"{stem}.txt").write_text(text, encoding="utf-8")
    return out_path


def prune_old_episodes():
    if not EPISODE_DIR.exists():
        return
    mp3s = sorted(EPISODE_DIR.glob("digest-*.mp3"), reverse=True)
    for old in mp3s[KEEP_EPISODES:]:
        old.unlink(missing_ok=True)
        old.with_suffix(".txt").unlink(missing_ok=True)
        print(f"Pruned {old.name}")


def build_feed():
    """Regenerate feed.xml from the episodes on disk."""
    EPISODE_DIR.mkdir(exist_ok=True)
    mp3s = sorted(EPISODE_DIR.glob("digest-*.mp3"), reverse=True)

    items = []
    for mp3 in mp3s:
        datestr = mp3.stem.replace("digest-", "")
        try:
            dt = datetime.strptime(datestr, "%Y-%m-%d").replace(
                hour=6, tzinfo=timezone.utc)
        except ValueError:
            continue

        pretty_date = dt.strftime("%A, %B %-d, %Y")
        size = mp3.stat().st_size
        url = f"{FEED_BASE_URL}/episodes/{mp3.name}"

        script_file = mp3.with_suffix(".txt")
        summary = ""
        if script_file.exists():
            words = script_file.read_text(encoding="utf-8").split()
            summary = " ".join(words[:40]) + ("..." if len(words) > 40 else "")

        items.append(f"""    <item>
      <title>{html.escape(pretty_date)}</title>
      <description>{html.escape(summary)}</description>
      <pubDate>{format_datetime(dt)}</pubDate>
      <guid isPermaLink="true">{html.escape(url)}</guid>
      <enclosure url="{html.escape(url)}" length="{size}" type="audio/mpeg"/>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{html.escape(PODCAST_TITLE)}</title>
    <link>{html.escape(FEED_BASE_URL)}</link>
    <description>{html.escape(PODCAST_DESC)}</description>
    <language>en-us</language>
    <itunes:author>{html.escape(PODCAST_AUTHOR)}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (ROOT / "feed.xml").write_text(feed, encoding="utf-8")
    print(f"Rebuilt feed.xml with {len(items)} episode(s)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python publish_kokoro.py script.txt")

    if "YOUR_USERNAME" in FEED_BASE_URL:
        raise SystemExit("Set FEED_BASE_URL at the top of publish_kokoro.py first.")

    build_episode(sys.argv[1])
    prune_old_episodes()
    build_feed()
    print("Done.")
