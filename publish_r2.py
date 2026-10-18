#!/usr/bin/env python3
"""
publish_r2.py — build a podcast episode and publish it to Cloudflare R2.

    python3 publish_r2.py --sync          download recent scripts + LEDGER.txt
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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
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
# Episodes are never deleted by default — the archive feeds the Saturday
# retrospective and the callback logic, and a year of daily shows is ~1 GB
# against R2's 10 GB free tier. Set an integer here only if you want pruning.
KEEP_DAYS = None
SYNC_COUNT = 7          # full scripts --sync pulls down: a week, enough for
                        # callbacks and the Saturday review
# Freshness ledger: every French phrase taught as new, and every idea segment.
# --sync writes it out as episodes/LEDGER.txt; publishing refuses a script
# whose new phrase or idea repeats one already in it.
LEDGER_KEY = "ledger.json"
IDEA_WINDOW_DAYS = 31   # an idea's person can't come back inside this window
PHRASE_SIMILARITY = 0.75  # near-duplicates (tense swaps etc.) count as repeats
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
               now=None, missing_ok=False):
    """Signed request against the R2 S3 API. Returns the response body,
    or None for a missing key when missing_ok is set."""
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
        if missing_ok and e.code == 404:
            return None
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


def r2_get(key, missing_ok=False):
    return r2_request("GET", key, missing_ok=missing_ok)


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


# A quarter-second of silence, generated once and embedded so the script has no
# runtime dependency on ffmpeg. Repeated to build longer pauses.
SILENCE_250MS = base64.b64decode(
    "SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjYwLjE2LjEwMAAAAAAAAAAAAAAA//OEwAAAAAAAAAAAAEluZm8AAAAPAAAADQAABaAAMzMzMzMzM0REREREREREVVVVVVVVVVVmZmZmZmZmd3d3d3d3d3eIiIiIiIiIiJmZmZmZmZmqqqqqqqqqqru7u7u7u7u7zMzMzMzMzN3d3d3d3d3d7u7u7u7u7u7/////////AAAAAExhdmM2MC4zMQAAAAAAAAAAAAAAACQDkAAAAAAAAAWgUwUOwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA//NExAAAAANIAAAAAExBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExFMAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExKYAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExKwAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExKwAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExKwAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExKwAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExKwAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExKwAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExKwAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMu//NExKwAAANIAAAAADEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NExKwAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NExKwAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
)

PAUSE_RE = re.compile(r"\[\[PAUSE(?::(\d+(?:\.\d+)?))?\]\]")


def pause_bytes(seconds):
    """Return mp3 bytes for roughly `seconds` of silence."""
    units = max(1, round(float(seconds) / 0.25))
    return SILENCE_250MS * units


FR_RE = re.compile(r"\[\[FR\]\](.*?)\[\[/FR\]\]", re.S)

# Bookkeeping markers. Never spoken; they feed the freshness ledger.
NEW_PHRASE_RE = re.compile(r"\[\[NEW_PHRASE:\s*(.*?)\s*\]\]")
IDEA_RE = re.compile(r"\[\[IDEA:\s*(.*?)\s*\]\]")
META_RE = re.compile(r"[ \t]*\[\[(?:NEW_PHRASE|IDEA):.*?\]\][ \t]*\n?")


def strip_markers(text):
    """Remove language markers — for the archived script and feed summary."""
    text = re.sub(r"\[\[/?FR\]\]", "", text)
    text = META_RE.sub("", text)
    return PAUSE_RE.sub("", text)


def split_by_language(text):
    """
    Return [(lang, text), ...] in reading order.

    Everything is English unless wrapped in [[FR]] ... [[/FR]]. Markers are
    stripped from the returned text so they never reach the speech engine.
    """
    def emit(lang, chunk, out):
        """Split a run of text on [[PAUSE]] markers as we append it."""
        pos = 0
        for pm in PAUSE_RE.finditer(chunk):
            head = chunk[pos:pm.start()].strip()
            if head:
                out.append((lang, head))
            out.append(("pause", pm.group(1) or "1"))
            pos = pm.end()
        rest = chunk[pos:].strip()
        if rest:
            out.append((lang, rest))

    parts, pos = [], 0
    for m in FR_RE.finditer(text):
        pre = text[pos:m.start()]
        if pre.strip():
            emit("en", pre, parts)
        fr = m.group(1)
        if fr.strip():
            emit("fr", fr, parts)
        pos = m.end()

    tail = text[pos:]
    if tail.strip():
        emit("en", tail, parts)

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


# ══ LEDGER — what's been taught, so nothing repeats ══════════

def _norm(s):
    """Casefold, drop accents and punctuation: for comparing phrases."""
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[\u2019'`]", "", s)
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def _paras(text):
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def guess_new_phrase(text):
    """Backfill only: in old scripts the new phrase is the one line said
    twice in a row inside the French lesson. Saturdays have none."""
    paras = _paras(text)
    start = next((i for i, p in enumerate(paras) if "of French" in p), None)
    if start is None:
        return None
    window = paras[start:start + 12]
    for a, b in zip(window, window[1:]):
        if a == b and len(a.split()) <= 12:
            return a
    return None


def guess_idea_text(text):
    """Backfill only: the idea segment's text, for surname matching."""
    m = re.search(r"One idea before we finish\.(.*?)"
                  r"(?:And finally, closer to home|$)", text, re.S | re.I)
    return " ".join(m.group(1).split())[:1500] if m else None


def script_keys():
    """{date: key} for every English script in the bucket."""
    keys = {}
    for k, _ in r2_list():
        parsed = parse_name(Path(k).stem) if k.endswith(".txt") else None
        if parsed and parsed[1] == "en":
            keys[parsed[0]] = k
    return keys


def backfill_ledger():
    """Rebuild the ledger from archived scripts. Runs once, when the bucket
    has no ledger yet; after that each publish appends its own entry."""
    print("No ledger in the bucket — backfilling from the archive...")
    ledger = []
    for d, key in sorted(script_keys().items()):
        text = r2_get(key).decode("utf-8", errors="replace")
        ledger.append({"date": d,
                       "new_phrase": guess_new_phrase(text),
                       "idea": None,
                       "idea_text": guess_idea_text(text)})
    return ledger


def load_ledger():
    raw = r2_get(LEDGER_KEY, missing_ok=True)
    if raw is not None:
        return json.loads(raw)
    ledger = backfill_ledger()
    save_ledger(ledger)
    return ledger


def save_ledger(ledger):
    ledger = sorted(ledger, key=lambda e: e["date"])
    r2_put(LEDGER_KEY, json.dumps(ledger, ensure_ascii=False,
                                  indent=1).encode("utf-8"),
           "application/json")


def phrase_clash(phrase, ledger):
    """Return the earlier entry this phrase repeats, or None."""
    a = _norm(phrase)
    for e in ledger:
        b = _norm(e.get("new_phrase") or "")
        if not b:
            continue
        if a == b or a in b or b in a or \
                SequenceMatcher(None, a, b).ratio() >= PHRASE_SIMILARITY:
            return e
    return None


def idea_clash(idea, ledger, today):
    """Return an entry inside the window that used the same person."""
    person = idea.split("|")[0]
    names = [w for w in _norm(person).split() if len(w) >= 3]
    if not names:
        return None
    surname = names[-1]
    cutoff = (date.fromisoformat(today)
              - timedelta(days=IDEA_WINDOW_DAYS)).isoformat()
    for e in ledger:
        if e["date"] < cutoff:
            continue
        hay = _norm(f"{e.get('idea') or ''} {e.get('idea_text') or ''}")
        if re.search(rf"\b{re.escape(surname)}\b", hay):
            return e
    return None


def check_fresh(meta, ledger, today):
    """Refuse to publish a repeat. Runs before any audio is made."""
    prior = [e for e in ledger if e["date"] != today]   # reruns are fine
    problems = []

    phrase = meta.get("new_phrase")
    if phrase:
        hit = phrase_clash(phrase, prior)
        if hit:
            problems.append(f"New phrase \"{phrase}\" repeats "
                            f"\"{hit['new_phrase']}\" from {hit['date']}.")
    else:
        print("  note: no [[NEW_PHRASE: ...]] marker (fine on Saturday)")

    idea = meta.get("idea")
    if idea and not idea.casefold().startswith("skip"):
        hit = idea_clash(idea, prior, today)
        if hit:
            what = hit.get("idea") or (hit.get("idea_text") or "")[:120]
            problems.append(f"Idea \"{idea}\" repeats a person from "
                            f"{hit['date']}: {what}")
    elif not idea:
        print("  note: no [[IDEA: ...]] marker — idea not checked")

    if problems:
        raise SystemExit("REPEAT — not published. Change the script and run "
                         "again.\n  " + "\n  ".join(problems) +
                         "\nSee episodes/LEDGER.txt for everything used.")


def write_ledger_txt(ledger, today):
    """Human-readable ledger for the routine to read before writing."""
    lines = ["FRENCH PHRASES ALREADY TAUGHT AS NEW",
             "Never teach any of these as new again — including a tense or",
             "wording variant. Draw the review from this list.", ""]
    seen = set()
    for e in reversed(ledger):
        key = _norm(e.get("new_phrase") or "")
        if key and key not in seen:
            seen.add(key)
            lines.append(f"{e['date']}  {e['new_phrase']}")

    cutoff = (date.fromisoformat(today) - timedelta(days=60)).isoformat()
    lines += ["", f"IDEA SEGMENTS, LAST 60 DAYS",
              f"Anyone dated within {IDEA_WINDOW_DAYS} days is blocked until "
              "the date shown. Don't reuse the idea itself either.", ""]
    for e in reversed(ledger):
        if e["date"] < cutoff:
            continue
        what = e.get("idea") or " ".join(
            (e.get("idea_text") or "(none)").split()[:30]) + "..."
        until = (date.fromisoformat(e["date"])
                 + timedelta(days=IDEA_WINDOW_DAYS)).isoformat()
        flag = f"  [blocked until {until}]" if until > today else ""
        lines.append(f"{e['date']}  {what}{flag}")

    (LOCAL_EPISODES / "LEDGER.txt").write_text("\n".join(lines) + "\n",
                                                encoding="utf-8")


# ══ SYNC — pull recent scripts down for continuity ═══════════

def sync_recent_scripts():
    """Download the most recent scripts and the ledger for Step 1."""
    LOCAL_EPISODES.mkdir(exist_ok=True)

    keys = script_keys()
    recent = sorted(keys, reverse=True)[:SYNC_COUNT]

    if not recent:
        print("No previous scripts in the bucket — first run.")
        return

    for d in recent:
        dest = LOCAL_EPISODES / Path(keys[d]).name
        dest.write_bytes(r2_get(keys[d]))
        print(f"  fetched {dest.name}")

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    write_ledger_txt(load_ledger(), today)
    print(f"Synced {len(recent)} script(s) and LEDGER.txt into episodes/")


# ══ FEED ═════════════════════════════════════════════════════

def parse_name(stem):
    m = NAME_RE.match(stem)
    return (m.group(1), m.group(2) or "en") if m else None


# Episodes are stamped 7:00 AM in New York, not UTC. Stamping in UTC put them
# at 2:30 AM local in podcast apps, which is what this fixes.
PUBLISH_HOUR = 7

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/New_York")
except Exception:                      # container without tzdata
    LOCAL_TZ = timezone(timedelta(hours=-4))


def episode_datetime(datestr):
    """7 AM New York on the episode's date, as an aware datetime."""
    d = datetime.strptime(datestr, "%Y-%m-%d")
    return d.replace(hour=PUBLISH_HOUR, minute=0, tzinfo=LOCAL_TZ)


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
            dt = episode_datetime(datestr)
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
    if not KEEP_DAYS:
        return                          # keep the full archive
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

    # Pull out the bookkeeping markers, check them, then drop them so they
    # never reach the speech engine.
    meta = {}
    for field, rx in (("new_phrase", NEW_PHRASE_RE), ("idea", IDEA_RE)):
        m = rx.search(text)
        meta[field] = m.group(1) if m else None
    text = META_RE.sub("", text).strip()

    today = datetime.now(timezone.utc).astimezone()
    datestr = today.strftime('%Y-%m-%d')
    ledger = load_ledger()
    check_fresh(meta, ledger, datestr)

    # Build the render list: every chunk carries the language it's read in.
    segments = split_by_language(text)
    jobs = []
    for lang, seg in segments:
        if lang == "pause":
            jobs.append(("pause", seg))
        else:
            jobs.extend((lang, c) for c in chunk_text(seg))

    clean = strip_markers(text)
    fr_words = sum(len(seg.split()) for lang, seg in segments if lang == "fr")
    spoken = [j for j in jobs if j[0] != "pause"]
    pauses = len(jobs) - len(spoken)
    print(f"Script: {len(clean.split())} words "
          f"({fr_words} French), {len(clean)} chars, "
          f"{len(spoken)} chunks, {pauses} pauses")
    for lang in sorted({l for l, _ in spoken}):
        print(f"  {lang}: {VOICES[lang]['name']}")

    audio, done = b"", 0
    for lang, chunk in jobs:
        if lang == "pause":
            audio += pause_bytes(chunk)
            continue
        done += 1
        print(f"  synthesizing {done}/{len(spoken)} [{lang}]...", flush=True)
        audio += synthesize(chunk, lang)

    stem = f"digest-{datestr}_en"

    # Audio and script first; the feed goes last so it never advertises
    # an enclosure that isn't already in the bucket.
    r2_put(f"{EPISODE_PREFIX}{stem}.mp3", audio, "audio/mpeg")
    r2_put(f"{EPISODE_PREFIX}{stem}.txt", clean.encode("utf-8"),
           "text/plain; charset=utf-8")
    save_ledger([e for e in ledger if e["date"] != datestr] +
                [{"date": datestr, "new_phrase": meta["new_phrase"],
                  "idea": meta["idea"], "idea_text": guess_idea_text(clean)}])

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
