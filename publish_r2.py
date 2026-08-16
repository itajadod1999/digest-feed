#!/usr/bin/env python3
"""
publish_r2.py — build a podcast episode and publish it to Cloudflare R2.

    python3 publish_r2.py --sync          download recent scripts for context
    python3 publish_r2.py script.txt      synthesize, upload, rebuild feed
    python3 publish_r2.py --list-voices   see available Google voices

No git push anywhere. Audio, scripts, and feed.xml all live in R2.
STDLIB ONLY — nothing to pip install, which is what keeps this working
inside a cloud container.

Required environment variables:
    GOOGLE_API_KEY          Google Cloud Text-to-Speech API key
    R2_ACCOUNT_ID           Cloudflare account ID
    R2_ACCESS_KEY_ID        R2 API token access key
    R2_SECRET_ACCESS_KEY    R2 API token secret
    R2_BUCKET               bucket name, e.g. digest-feed
    R2_PUBLIC_URL           public base URL, e.g. https://pub-abc123.r2.dev
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

# ── CONFIGURE ────────────────────────────────────────────────
PODCAST_TITLE = "Daily Digest"
PODCAST_DESC = "A personal daily news briefing."
PODCAST_AUTHOR = "Automated"

# Voices per language. The script marks French passages with [[FR]] ... [[/FR]];
# everything else is read in English. Run --list-voices fr-FR to confirm what
# your project actually offers before changing these.
VOICES = {
    "en": {"code": "en-US", "name": "en-US-Chirp3-HD-Charon", "rate": 1.0},
    "fr": {"code": "fr-FR", "name": "fr-FR-Chirp3-HD-Charon", "rate": 0.95},
}
# If Chirp 3 HD has no French voice on your project, swap in:
#   "fr-FR-Neural2-D" (male) or "fr-FR-Neural2-A" (female) — both free-tier.

LANGUAGE_CODE = "en-US"   # default for --list-voices

EPISODE_PREFIX = "episodes/"
MAX_CHUNK = 4500        # Google's limit is 5000 bytes per request
KEEP_DAYS = 30
SYNC_COUNT = 3          # how many recent scripts --sync pulls down
# ─────────────────────────────────────────────────────────────

TTS_ROOT = "https://texttospeech.googleapis.com/v1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

ROOT = Path(__file__).parent
LOCAL_EPISODES = ROOT / "episodes"

NAME_RE = re.compile(r"^digest-(\d{4}-\d{2}-\d{2})(?:_(en|fr))?$")


def env(name):
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"Environment variable {name} is not set.")
    return v


# ══ CLOUDFLARE R2 (S3-compatible, AWS SigV4) ═════════════════

def _sign_key(secret, datestamp, region, service):
    k = hmac.new(f"AWS4{secret}".encode(), datestamp.encode(),
                 hashlib.sha256).digest()
    k = hmac.new(k, region.encode(), hashlib.sha256).digest()
    k = hmac.new(k, service.encode(), hashlib.sha256).digest()
    return hmac.new(k, b"aws4_request", hashlib.sha256).digest()


def _canonical_query(params):
    """Sorted, RFC3986-encoded query string."""
    if not params:
        return ""
    items = sorted(params.items())
    return "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}="
        f"{urllib.parse.quote(str(v), safe='-_.~')}"
        for k, v in items
    )


def r2_request(method, key="", query=None, body=b"", content_type=None,
               now=None):
    """Signed request against the R2 S3 API. Returns the response body."""
    account = env("R2_ACCOUNT_ID")
    access_key = env("R2_ACCESS_KEY_ID")
    secret = env("R2_SECRET_ACCESS_KEY")
    bucket = env("R2_BUCKET")

    host = f"{account}.r2.cloudflarestorage.com"
    region, service = "auto", "s3"

    # Each path segment is encoded, but the slashes between them are not.
    encoded_key = "/".join(urllib.parse.quote(p, safe="") for p in key.split("/")) if key else ""
    canonical_uri = f"/{bucket}" + (f"/{encoded_key}" if encoded_key else "")

    now = now or datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
    }
    if content_type:
        headers["content-type"] = content_type

    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(
        f"{k}:{headers[k].strip()}\n" for k in sorted(headers))

    canonical_request = "\n".join([
        method,
        canonical_uri,
        _canonical_query(query),
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amzdate,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    signature = hmac.new(
        _sign_key(secret, datestamp, region, service),
        string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    url = f"https://{host}{canonical_uri}"
    qs = _canonical_query(query)
    if qs:
        url += f"?{qs}"

    req = urllib.request.Request(url, data=body or None, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(f"R2 {method} {canonical_uri} failed "
                         f"({e.code}): {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(
            f"Could not reach {host}: {e.reason}. In a cloud routine, check "
            "that network access permits r2.cloudflarestorage.com."
        )


def r2_put(key, body, content_type):
    r2_request("PUT", key, body=body, content_type=content_type)
    print(f"  uploaded {key} ({len(body) / 1_000_000:.2f} MB)")


def r2_get(key):
    return r2_request("GET", key)


def r2_delete(key):
    r2_request("DELETE", key)
    print(f"  deleted {key}")


def r2_list(prefix=EPISODE_PREFIX):
    """Return [(key, size_bytes)] for everything under the prefix."""
    results, token = [], None
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"

    while True:
        query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            query["continuation-token"] = token

        root = ET.fromstring(r2_request("GET", "", query=query))
        for c in root.findall(f"{ns}Contents"):
            results.append((c.findtext(f"{ns}Key"),
                            int(c.findtext(f"{ns}Size") or 0)))

        if root.findtext(f"{ns}IsTruncated") == "true":
            token = root.findtext(f"{ns}NextContinuationToken")
        else:
            break

    return results


# ══ GOOGLE TEXT-TO-SPEECH ════════════════════════════════════

def tts_call(path, payload=None, query=None):
    url = f"{TTS_ROOT}/{path}"
    params = dict(query or {})
    params["key"] = env("GOOGLE_API_KEY")
    url += "?" + urllib.parse.urlencode(params)

    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(f"Google TTS error {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Could not reach Google TTS: {e.reason}")


def list_voices(lang_code=None):
    lang_code = lang_code or LANGUAGE_CODE
    data = tts_call("voices", query={"languageCode": lang_code})
    names = sorted(v["name"] for v in data.get("voices", []))
    print(f"Voices for {lang_code}:")
    for tier in ("Chirp3-HD", "Neural2", "Studio", "Wavenet", "Standard"):
        matches = [n for n in names if tier.lower() in n.lower()]
        if matches:
            print(f"\n{tier}:")
            for n in matches:
                print(f"  {n}")
    print(f"\n{len(names)} voices total.")


FR_RE = re.compile(r"\[\[FR\]\](.*?)\[\[/FR\]\]", re.S)


def strip_markers(text):
    """Remove language markers — for the archived script and feed summary."""
    return re.sub(r"\[\[/?FR\]\]", "", text)


def split_by_language(text):
    """
    Return [(lang, text), ...] in reading order.

    Everything is English unless wrapped in [[FR]] ... [[/FR]]. Markers are
    stripped from the returned text so they never reach the speech engine.
    """
    parts, pos = [], 0
    for m in FR_RE.finditer(text):
        pre = text[pos:m.start()].strip()
        if pre:
            parts.append(("en", pre))
        fr = m.group(1).strip()
        if fr:
            parts.append(("fr", fr))
        pos = m.end()

    tail = text[pos:].strip()
    if tail:
        parts.append(("en", tail))

    return parts or [("en", text.strip())]


def chunk_text(text, limit=MAX_CHUNK):
    """Split on paragraphs so audio seams fall on natural pauses."""
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


def synthesize(text, lang="en"):
    """Return raw mp3 bytes for one chunk, in the given language's voice."""
    v = VOICES[lang]
    payload = {
        "input": {"text": text},
        "voice": {"languageCode": v["code"], "name": v["name"]},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": v["rate"]},
    }
    data = tts_call("text:synthesize", payload=payload)
    if "audioContent" not in data:
        raise SystemExit(f"Unexpected TTS response: {str(data)[:300]}")
    return base64.b64decode(data["audioContent"])


# ══ SYNC — pull recent scripts down for continuity ═══════════

def sync_recent_scripts():
    """Download the most recent scripts so Step 0 has context to read."""
    LOCAL_EPISODES.mkdir(exist_ok=True)

    txts = sorted((k for k, _ in r2_list() if k.endswith(".txt")),
                  reverse=True)[:SYNC_COUNT]

    if not txts:
        print("No previous scripts in the bucket — first run.")
        return

    for key in txts:
        dest = LOCAL_EPISODES / Path(key).name
        dest.write_bytes(r2_get(key))
        print(f"  fetched {dest.name}")

    print(f"Synced {len(txts)} script(s) into episodes/")


# ══ FEED ═════════════════════════════════════════════════════

def parse_name(stem):
    m = NAME_RE.match(stem)
    return (m.group(1), m.group(2) or "en") if m else None


def episode_title(dt):
    return dt.strftime("%A, %B %-d, %Y")


def build_and_upload_feed(summaries):
    """Rebuild feed.xml from the bucket listing and upload it."""
    public = env("R2_PUBLIC_URL").rstrip("/")
    sizes = {k: s for k, s in r2_list()}

    entries = []
    for key, size in sizes.items():
        if not key.endswith(".mp3"):
            continue
        parsed = parse_name(Path(key).stem)
        if not parsed:
            continue
        datestr, lang = parsed
        try:
            dt = datetime.strptime(datestr, "%Y-%m-%d").replace(
                hour=6, minute=30, tzinfo=timezone.utc)
        except ValueError:
            continue
        entries.append((dt, key, size))

    entries.sort(reverse=True)

    items = []
    for dt, key, size in entries:
        url = f"{public}/{key}"
        summary = summaries.get(key, "")
        items.append(f"""    <item>
      <title>{html.escape(episode_title(dt))}</title>
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
    <link>{html.escape(public)}</link>
    <description>{html.escape(PODCAST_DESC)}</description>
    <language>en-us</language>
    <itunes:author>{html.escape(PODCAST_AUTHOR)}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""
    r2_put("feed.xml", feed.encode("utf-8"), "application/rss+xml")
    print(f"Feed rebuilt with {len(items)} episode(s)")
    print(f"  {public}/feed.xml")


def prune_old():
    """Delete episodes older than the most recent KEEP_DAYS days."""
    dated = {}
    for key, _ in r2_list():
        parsed = parse_name(Path(key).stem)
        if parsed:
            dated.setdefault(parsed[0], []).append(key)

    keep = set(sorted(dated, reverse=True)[:KEEP_DAYS])
    for date, keys in dated.items():
        if date not in keep:
            for k in keys:
                r2_delete(k)


# ══ MAIN ═════════════════════════════════════════════════════

def publish(script_path):
    text = Path(script_path).read_text(encoding="utf-8").strip()
    if len(text) < 200:
        raise SystemExit(f"Script is only {len(text)} chars — refusing to "
                         "publish. Something upstream probably failed.")

    # Build the render list: every chunk carries the language it's read in.
    segments = split_by_language(text)
    jobs = [(lang, c) for lang, seg in segments for c in chunk_text(seg)]

    clean = strip_markers(text)
    fr_words = sum(len(seg.split()) for lang, seg in segments if lang == "fr")
    print(f"Script: {len(clean.split())} words "
          f"({fr_words} French), {len(clean)} chars, {len(jobs)} chunks")
    for lang in sorted({l for l, _ in jobs}):
        print(f"  {lang}: {VOICES[lang]['name']}")

    audio = b""
    for i, (lang, chunk) in enumerate(jobs, 1):
        print(f"  synthesizing {i}/{len(jobs)} [{lang}]...", flush=True)
        audio += synthesize(chunk, lang)

    today = datetime.now(timezone.utc).astimezone()
    stem = f"digest-{today.strftime('%Y-%m-%d')}_en"

    # Audio and script first; the feed goes last so it never advertises
    # an enclosure that isn't already in the bucket.
    r2_put(f"{EPISODE_PREFIX}{stem}.mp3", audio, "audio/mpeg")
    r2_put(f"{EPISODE_PREFIX}{stem}.txt", clean.encode("utf-8"),
           "text/plain; charset=utf-8")

    words = clean.split()
    summary = " ".join(words[:40]) + ("..." if len(words) > 40 else "")

    prune_old()
    build_and_upload_feed({f"{EPISODE_PREFIX}{stem}.mp3": summary})

    # Keep a local copy so the next run has it even without --sync
    LOCAL_EPISODES.mkdir(exist_ok=True)
    (LOCAL_EPISODES / f"{stem}.txt").write_text(clean, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(
            "Usage:\n"
            "  python3 publish_r2.py --sync\n"
            "  python3 publish_r2.py script.txt\n"
            "  python3 publish_r2.py --list-voices")

    arg = sys.argv[1]
    if arg == "--sync":
        sync_recent_scripts()
    elif arg == "--list-voices":
        list_voices()
    else:
        publish(arg)
    print("Done.")
