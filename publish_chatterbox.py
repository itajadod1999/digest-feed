#!/usr/bin/env python3
"""
publish_chatterbox.py — turn a plain-text script into a podcast episode.

One feed, both languages. Episodes are suffixed _en and _fr:

    python3.11 publish_chatterbox.py script.txt en
    python3.11 publish_chatterbox.py script_fr.txt fr

Renders locally with Chatterbox Multilingual (Resemble AI, MIT). No API key,
no per-character cost. Expect roughly 45-60 minutes for a 15-minute episode.
"""

import os
# Must be set before torch is imported: lets unsupported ops fall back to CPU
# instead of crashing on Apple Silicon.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import html
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

# ── CONFIGURE ────────────────────────────────────────────────
FEED_BASE_URL = "https://itajadod1999.github.io/digest-feed"
PODCAST_TITLE = "Daily Digest"
PODCAST_DESC = "A personal daily news briefing, in English and French."
PODCAST_AUTHOR = "Automated"

LANGS = {
    "en": {"lang_id": "en", "label": "English", "minute": 30},
    "fr": {"lang_id": "fr", "label": "Français", "minute": 0},
}

# Optional: drop a 10-20 second clean .wav of a voice you like into voices/
# as voices/en.wav and voices/fr.wav. Chatterbox clones it, and — more
# importantly — keeps the voice identical across every chunk.
VOICE_DIR = "voices"

# ── PACING ───────────────────────────────────────────────────
# SPEED is a true playback-rate multiplier applied after synthesis, with
# pitch preserved. 1.0 = exactly as rendered. 0.95 = 5% slower.
SPEED = 1.0

# CFG_WEIGHT is the real pacing dial inside the model. LOWER = slower and
# more deliberate. 0.5 is the Chatterbox default and reads quite fast for
# news; 0.3 gives a measured newsreader cadence.
CFG_WEIGHT = 0.3

# EXAGGERATION controls expressiveness. Low is right for news.
EXAGGERATION = 0.4

# Chatterbox truncates when a chunk runs long (the "forcing EOS token"
# warnings). Shorter chunks mean fewer dropped sentences.
MAX_CHUNK = 200
# ─────────────────────────────────────────────────────────────

MP3_BITRATE = "64k"
KEEP_DAYS = 30

ROOT = Path(__file__).parent
EPISODE_DIR = ROOT / "episodes"

FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
           "dimanche"]
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]

NAME_RE = re.compile(r"^digest-(\d{4}-\d{2}-\d{2})(?:_(en|fr))?$")


def parse_name(stem):
    """Return (date_string, lang) from a filename stem, or None."""
    m = NAME_RE.match(stem)
    if not m:
        return None
    return m.group(1), (m.group(2) or "en")   # legacy files count as English


def chunk_text(text, limit=MAX_CHUNK):
    """Split into short chunks on paragraph, then sentence boundaries."""
    chunks, current = [], ""

    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if len(para) <= limit and not current:
            chunks.append(para)
            continue

        sentences = re.split(r"(?<=[.!?])\s+", para)
        for s in sentences:
            if len(current) + len(s) + 1 > limit:
                if current:
                    chunks.append(current.strip())
                current = s
            else:
                current = f"{current} {s}".strip()
        if current:
            chunks.append(current.strip())
            current = ""

    return [c for c in chunks if c]


def pick_device():
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def render_audio(text, lang):
    """Run Chatterbox over the script, return (waveform_tensor, sample_rate)."""
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    device = pick_device()
    print(f"Loading Chatterbox on {device}...")
    model = ChatterboxMultilingualTTS.from_pretrained(device=device)

    ref = ROOT / VOICE_DIR / f"{lang}.wav"
    ref_path = str(ref) if ref.exists() else None
    if ref_path:
        print(f"Using reference voice: {ref.name}")
    else:
        print(f"No {VOICE_DIR}/{lang}.wav found — using the default voice.")

    chunks = chunk_text(text)
    print(f"Rendering {len(chunks)} chunks "
          f"(cfg_weight={CFG_WEIGHT}, max_chunk={MAX_CHUNK})...")

    pieces = []
    started = time.time()
    for i, chunk in enumerate(chunks, 1):
        kwargs = {
            "language_id": LANGS[lang]["lang_id"],
            "exaggeration": EXAGGERATION,
            "cfg_weight": CFG_WEIGHT,
        }
        if ref_path:
            kwargs["audio_prompt_path"] = ref_path

        wav = model.generate(chunk, **kwargs)
        pieces.append(wav.squeeze(0) if wav.dim() > 1 else wav)
        pieces.append(torch.zeros(int(model.sr * 0.3)))   # pause between chunks

        elapsed = time.time() - started
        eta = elapsed / i * (len(chunks) - i)
        print(f"  chunk {i}/{len(chunks)}  (~{eta/60:.1f} min left)", flush=True)

    return torch.cat(pieces), model.sr


def apply_speed(data, sample_rate):
    """Time-stretch to SPEED, preserving pitch. No-op at 1.0."""
    if abs(SPEED - 1.0) < 0.001:
        return data
    try:
        import librosa
        print(f"Applying speed {SPEED}x...")
        return librosa.effects.time_stretch(data, rate=SPEED)
    except Exception as e:
        print(f"  (speed adjustment skipped: {e})")
        return data


def write_mp3(waveform, sample_rate, out_path):
    """Prefer soundfile's built-in mp3 encoder; fall back to ffmpeg."""
    import soundfile as sf

    data = waveform.detach().cpu().numpy()
    data = apply_speed(data, sample_rate)

    try:
        sf.write(str(out_path), data, sample_rate, format="MP3")
        return
    except Exception as e:
        print(f"  (direct mp3 write unavailable: {e}; trying ffmpeg)")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    sf.write(wav_path, data, sample_rate)

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
             "-ac", "1", "-b:a", MP3_BITRATE, str(out_path)],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        Path(wav_path).unlink(missing_ok=True)
        raise SystemExit("Could not write mp3: soundfile's encoder failed and "
                         "ffmpeg isn't installed. Run: brew install ffmpeg")

    Path(wav_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg failed: {result.stderr[:400]}")


def build_episode(script_path, lang):
    text = Path(script_path).read_text(encoding="utf-8").strip()
    if len(text) < 200:
        raise SystemExit(f"Script is only {len(text)} chars — refusing to "
                         "publish. Something upstream probably failed.")

    words = len(text.split())
    print(f"Script: {words} words, {len(text)} chars [{lang}]")

    waveform, sample_rate = render_audio(text, lang)
    minutes = len(waveform) / sample_rate / 60
    print(f"Rendered {minutes:.1f} minutes of audio "
          f"({words / minutes:.0f} words per minute)")
    if words / minutes > 185:
        print("  NOTE: that's faster than typical news pace (~150-165 wpm). "
              "Lower CFG_WEIGHT or set SPEED below 1.0 to slow it down.")

    EPISODE_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).astimezone()
    stem = f"digest-{today.strftime('%Y-%m-%d')}_{lang}"
    out_path = EPISODE_DIR / f"{stem}.mp3"

    write_mp3(waveform, sample_rate, out_path)
    print(f"Wrote {out_path} ({out_path.stat().st_size / 1_000_000:.1f} MB)")

    (EPISODE_DIR / f"{stem}.txt").write_text(text, encoding="utf-8")
    return out_path


def prune_old_episodes():
    """Keep the most recent KEEP_DAYS days, both languages."""
    if not EPISODE_DIR.exists():
        return

    dated = []
    for mp3 in EPISODE_DIR.glob("digest-*.mp3"):
        parsed = parse_name(mp3.stem)
        if parsed:
            dated.append((parsed[0], mp3))

    keep_dates = sorted({d for d, _ in dated}, reverse=True)[:KEEP_DAYS]
    for date, mp3 in dated:
        if date not in keep_dates:
            mp3.unlink(missing_ok=True)
            mp3.with_suffix(".txt").unlink(missing_ok=True)
            print(f"Pruned {mp3.name}")


def episode_title(dt, lang):
    if lang == "fr":
        return (f"{FR_DAYS[dt.weekday()].capitalize()} {dt.day} "
                f"{FR_MONTHS[dt.month - 1]} {dt.year} — Français")
    return dt.strftime("%A, %B %-d, %Y")


def build_feed():
    """Regenerate feed.xml from every episode on disk, both languages."""
    EPISODE_DIR.mkdir(exist_ok=True)

    entries = []
    for mp3 in EPISODE_DIR.glob("digest-*.mp3"):
        parsed = parse_name(mp3.stem)
        if not parsed:
            continue
        datestr, lang = parsed
        try:
            dt = datetime.strptime(datestr, "%Y-%m-%d").replace(
                hour=6, minute=LANGS[lang]["minute"], tzinfo=timezone.utc)
        except ValueError:
            continue
        entries.append((dt, lang, mp3))

    entries.sort(key=lambda e: e[0], reverse=True)

    items = []
    for dt, lang, mp3 in entries:
        url = f"{FEED_BASE_URL}/episodes/{mp3.name}"

        script_file = mp3.with_suffix(".txt")
        summary = ""
        if script_file.exists():
            words = script_file.read_text(encoding="utf-8").split()
            summary = " ".join(words[:40]) + ("..." if len(words) > 40 else "")

        items.append(f"""    <item>
      <title>{html.escape(episode_title(dt, lang))}</title>
      <description>{html.escape(summary)}</description>
      <pubDate>{format_datetime(dt)}</pubDate>
      <guid isPermaLink="true">{html.escape(url)}</guid>
      <enclosure url="{html.escape(url)}" length="{mp3.stat().st_size}" type="audio/mpeg"/>
      <itunes:subtitle>{html.escape(LANGS[lang]['label'])}</itunes:subtitle>
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
        raise SystemExit("Usage: publish_chatterbox.py <script.txt> [en|fr]")

    script = sys.argv[1]
    lang = sys.argv[2] if len(sys.argv) > 2 else "en"

    if lang not in LANGS:
        raise SystemExit(f"Unknown language '{lang}'. Use: {', '.join(LANGS)}")

    build_episode(script, lang)
    prune_old_episodes()
    build_feed()
    print("Done.")
