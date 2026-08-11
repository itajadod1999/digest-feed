#!/usr/bin/env python3
"""
publish_google.py — turn a plain-text script into a podcast episode.

    python3 publish_google.py script.txt
    python3 publish_google.py --list-voices     (see what's available)

Uses Google Cloud Text-to-Speech. At ~450k characters/month this should sit
inside Google's permanent free tier for Chirp 3 HD (1M chars/month).

AUTH — two options, tried in this order:

  1. GOOGLE_API_KEY        — a plain API key. Stdlib only, nothing to install.
                             Try this first; it's far simpler.
  2. GOOGLE_SERVICE_ACCOUNT_JSON — the full contents of a service-account JSON
                             key. Requires: pip install google-auth
                             Use only if the API key is rejected.
"""

import base64
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

# ── CONFIGURE ────────────────────────────────────────────────
FEED_BASE_URL = "https://itajadod1999.github.io/digest-feed"
PODCAST_TITLE = "Daily Digest"
PODCAST_DESC = "A personal daily news briefing."
PODCAST_AUTHOR = "Automated"

LANGUAGE_CODE = "en-US"

# Chirp 3 HD is the best conversational tier and has a 1M chars/month free
# allowance. Run --list-voices to see exactly what your project offers.
# Male-sounding: Charon, Fenrir, Orus, Puck
# Female-sounding: Aoede, Kore, Leda, Zephyr
VOICE = "en-US-Chirp3-HD-Charon"

# Fallback if Chirp 3 isn't available: "en-US-Neural2-J" (male, $16/1M, 1M free)
# Cheapest good option: "en-US-Wavenet-D" (male, $4/1M, 4M free)

# 1.0 is natural. Some Chirp 3 voices ignore this — if so, it's silently fine.
SPEAKING_RATE = 1.0

MAX_CHUNK = 4500       # API limit is 5000 bytes per request
KEEP_DAYS = 30
# ─────────────────────────────────────────────────────────────

API_ROOT = "https://texttospeech.googleapis.com/v1"

ROOT = Path(__file__).parent
EPISODE_DIR = ROOT / "episodes"

# Matches digest-2026-08-03.mp3, digest-2026-08-03_en.mp3, ..._fr.mp3
NAME_RE = re.compile(r"^digest-(\d{4}-\d{2}-\d{2})(?:_(en|fr))?$")

FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi",
           "dimanche"]
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]


# ── AUTH ─────────────────────────────────────────────────────

def get_auth():
    """Return (url_suffix, headers) for authenticating requests."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    if api_key:
        return f"?key={api_key}", {}

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        try:
            from google.oauth2 import service_account
            import google.auth.transport.requests
        except ImportError:
            raise SystemExit(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but google-auth isn't "
                "installed. Run:  pip install google-auth requests"
            )
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        return "", {"Authorization": f"Bearer {creds.token}"}

    raise SystemExit(
        "No credentials. Set GOOGLE_API_KEY (simplest) or "
        "GOOGLE_SERVICE_ACCOUNT_JSON."
    )


def api_post(path, payload, auth):
    suffix, headers = auth
    req = urllib.request.Request(
        f"{API_ROOT}/{path}{suffix}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        if e.code in (401, 403):
            raise SystemExit(
                f"Auth rejected ({e.code}): {body}\n\n"
                "If you're using GOOGLE_API_KEY, this API may require a "
                "service account instead. Switch to "
                "GOOGLE_SERVICE_ACCOUNT_JSON (see the header of this file)."
            )
        raise SystemExit(f"TTS API error {e.code}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(
            f"Could not reach texttospeech.googleapis.com: {e.reason}. "
            "In a cloud routine, check the environment's network access."
        )


def list_voices():
    """Print available English voices so you can pick one that exists."""
    suffix, headers = get_auth()
    req = urllib.request.Request(
        f"{API_ROOT}/voices{suffix}" if suffix
        else f"{API_ROOT}/voices?languageCode={LANGUAGE_CODE}",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{e.code}: {e.read().decode('utf-8')[:400]}")

    names = sorted(v["name"] for v in data.get("voices", [])
                   if any(c.startswith("en-US") for c in v["languageCodes"]))
    for tier in ("Chirp3-HD", "Neural2", "Studio", "Wavenet", "Standard"):
        matches = [n for n in names if tier.lower() in n.lower()]
        if matches:
            print(f"\n{tier}:")
            for n in matches:
                print(f"  {n}")
    print(f"\n{len(names)} en-US voices total.")


# ── SYNTHESIS ────────────────────────────────────────────────

def chunk_text(text, limit=MAX_CHUNK):
    """Split on paragraphs so audio seams land on natural pauses."""
    chunks, current = [], ""

    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if len(para) > limit:
            if current:
                chunks.append(current)
                current = ""
            for s in re.split(r"(?<=[.!?])\s+", para):
                if len(current) + len(s) + 1 > limit:
                    if current:
                        chunks.append(current.strip())
                    current = s
                else:
                    current = f"{current} {s}".strip()
            continue

        if len(current) + len(para) + 2 > limit:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}".strip()

    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def synthesize(text, auth):
    """Return raw mp3 bytes for one chunk."""
    payload = {
        "input": {"text": text},
        "voice": {"languageCode": LANGUAGE_CODE, "name": VOICE},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": SPEAKING_RATE},
    }
    data = api_post("text:synthesize", payload, auth)
    if "audioContent" not in data:
        raise SystemExit(f"Unexpected API response: {str(data)[:300]}")
    return base64.b64decode(data["audioContent"])


def build_episode(script_path):
    auth = get_auth()

    text = Path(script_path).read_text(encoding="utf-8").strip()
    if len(text) < 200:
        raise SystemExit(f"Script is only {len(text)} chars — refusing to "
                         "publish. Something upstream probably failed.")

    chunks = chunk_text(text)
    print(f"Script: {len(text.split())} words, {len(text)} chars, "
          f"{len(chunks)} chunks, voice {VOICE}")
    print(f"  (~{len(text)/1_000_000*100:.1f}% of a 1M free-tier month)")

    audio = b""
    for i, chunk in enumerate(chunks, 1):
        print(f"  synthesizing {i}/{len(chunks)} ({len(chunk)} chars)...",
              flush=True)
        audio += synthesize(chunk, auth)

    EPISODE_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).astimezone()
    stem = f"digest-{today.strftime('%Y-%m-%d')}_en"
    out_path = EPISODE_DIR / f"{stem}.mp3"

    out_path.write_bytes(audio)
    (EPISODE_DIR / f"{stem}.txt").write_text(text, encoding="utf-8")

    print(f"Wrote {out_path} ({len(audio) / 1_000_000:.1f} MB)")
    return out_path


# ── FEED ─────────────────────────────────────────────────────

def parse_name(stem):
    m = NAME_RE.match(stem)
    return (m.group(1), m.group(2) or "en") if m else None


def prune_old_episodes():
    if not EPISODE_DIR.exists():
        return
    dated = []
    for mp3 in EPISODE_DIR.glob("digest-*.mp3"):
        parsed = parse_name(mp3.stem)
        if parsed:
            dated.append((parsed[0], mp3))

    keep = sorted({d for d, _ in dated}, reverse=True)[:KEEP_DAYS]
    for date, mp3 in dated:
        if date not in keep:
            mp3.unlink(missing_ok=True)
            mp3.with_suffix(".txt").unlink(missing_ok=True)
            print(f"Pruned {mp3.name}")


def episode_title(dt, lang):
    if lang == "fr":
        return (f"{FR_DAYS[dt.weekday()].capitalize()} {dt.day} "
                f"{FR_MONTHS[dt.month - 1]} {dt.year} — Français")
    return dt.strftime("%A, %B %-d, %Y")


def build_feed():
    """Rebuild feed.xml from every episode on disk, including older ones."""
    EPISODE_DIR.mkdir(exist_ok=True)

    entries = []
    for mp3 in EPISODE_DIR.glob("digest-*.mp3"):
        parsed = parse_name(mp3.stem)
        if not parsed:
            continue
        datestr, lang = parsed
        try:
            dt = datetime.strptime(datestr, "%Y-%m-%d").replace(
                hour=6, minute=(30 if lang == "en" else 0), tzinfo=timezone.utc)
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
    if len(sys.argv) > 1 and sys.argv[1] == "--list-voices":
        list_voices()
        sys.exit(0)

    if len(sys.argv) < 2:
        raise SystemExit("Usage: python3 publish_google.py <script.txt>")

    build_episode(sys.argv[1])
    prune_old_episodes()
    build_feed()
    print("Done.")
