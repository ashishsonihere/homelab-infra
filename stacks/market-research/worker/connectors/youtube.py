"""YouTube connector — CORRELATED channels→videos→comments, MEMORY-SAFE (streams; flushes per video).

Mirrors arctic_reddit.py's design: natural-id UNIQUE upserts (ON CONFLICT DO UPDATE → idempotent
re-runs, never duplicates), env-driven config, psycopg2 + execute_values, and per-video streaming so a
whole channel's comment set is never held in RAM. ZERO-COST extraction (no video download):

  1. METADATA   — yt-dlp (YoutubeDL, skip_download). extract_flat to list a channel/playlist/search
                  cheaply, then a full per-video extract for stats+tags. Upserts youtube_channels +
                  youtube_videos per video (flush-as-you-go).
  2. TRANSCRIPTS — PRIMARY: yt-dlp json3 captions. We read the per-video info dict's `subtitles`
                  (manual) then `automatic_captions` (auto), pick ONE preferred lang per YT_LANGS, grab
                  that track's json3 `url`, fetch it with httpx (429-backoff), and parse the json3
                  `events[].segs[].utf8` timed text. This works from datacenter IPs where the deprecated
                  timedtext XML endpoint that youtube-transcript-api uses returns an empty body.
                  SECONDARY fallback: youtube-transcript-api (only if yt-dlp yields no captions).
                  Both honor YT_PROXY. Stored as transcript jsonb array of {text,start,dur} +
                  transcript_source ('yt-dlp-json3' | 'youtube_transcript_api') + transcript_lang.
                  Self-hosted Whisper FALLBACK is a documented STUB (interface only) — see whisper_transcribe().
  3. COMMENTS   — yt-dlp getcomments (capped by YT_COMMENT_LIMIT), streamed into youtube_comments as a
                  parent_id self-referential tree (top-level → parent_id NULL), memory-safe, ON CONFLICT.

Idempotent everywhere: re-running updates rows, never inserts duplicates.

Run:  python -m connectors.youtube
Env:
  PG_DSN            postgres dsn (required)
  YT_SEEDS          comma-separated seeds (URLs, channel ids/handles, or keyword queries)
  YT_MODE           urls | channels | keywords  (default: urls)
  YT_MAX_VIDEOS     cap of videos per seed (default 50)
  YT_FETCH_COMMENTS 1 to pull comments (default 0 — comments are the slow part)
  YT_COMMENT_LIMIT  max comments per video (default 200)
  YT_LANGS          transcript language preference, comma list (default 'en,en-orig,en-US')
  YT_TRANSLATE      target lang to translate a transcript into if no preferred match (default '' = off,
                    only applies to the youtube-transcript-api fallback)
  YT_PROXY          optional http(s) proxy for caption fetches (helps with IpBlocked / 429)
  YT_VIDEO_SLEEP    polite sleep (seconds) between videos to avoid rate limits (default 1.0)
"""
import os
import json
import random
import time
import datetime as dt

import httpx
import psycopg2
from psycopg2.extras import Json, execute_values

# yt-dlp / youtube-transcript-api are imported lazily inside the functions that need them so that
# importing this module for unit tests (which monkeypatch these helpers) does not require the heavy
# deps to be installed. The Dockerfile.scrape image ships them; tests mock them.

PG_DSN = os.environ.get("PG_DSN", "postgresql://devcore@devcore-postgres:5432/market_research")
SEEDS = [s.strip() for s in os.environ.get("YT_SEEDS", "").split(",") if s.strip()]
MODE = os.environ.get("YT_MODE", "urls")                       # urls | channels | keywords
MAX_VIDEOS = int(os.environ.get("YT_MAX_VIDEOS", "50"))
FETCH_COMMENTS = os.environ.get("YT_FETCH_COMMENTS", "0") != "0"
COMMENT_LIMIT = int(os.environ.get("YT_COMMENT_LIMIT", "200"))
LANGS = [s.strip() for s in os.environ.get("YT_LANGS", "en,en-orig,en-US").split(",") if s.strip()]
TRANSLATE = os.environ.get("YT_TRANSLATE", "").strip()
PROXY = os.environ.get("YT_PROXY", "").strip()                  # CAPTION (json3) proxy — tiny data; use residential here
META_PROXY = os.environ.get("YT_META_PROXY", "").strip()        # metadata proxy: OPT-IN, default box IP (datacenter proxy breaks the player response + would burn GB)
VIDEO_SLEEP = float(os.environ.get("YT_VIDEO_SLEEP", "1.0"))   # polite gap between videos (rate-limit guard)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _ts(epoch):
    """Unix epoch (yt-dlp 'timestamp') → tz-aware datetime, or None."""
    try:
        return dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc)
    except Exception:
        return None


def _date_ts(yyyymmdd):
    """yt-dlp 'upload_date' (YYYYMMDD string) → tz-aware datetime, or None."""
    try:
        return dt.datetime.strptime(str(yyyymmdd), "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def _published_at(info):
    """Prefer the precise unix timestamp; fall back to the date-only upload_date."""
    return _ts(info.get("timestamp")) or _date_ts(info.get("upload_date"))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. METADATA (yt-dlp)
# ---------------------------------------------------------------------------
def _ydl(opts=None):
    """Construct a YoutubeDL with skip_download + quiet defaults. Imported lazily."""
    from yt_dlp import YoutubeDL
    base = {"skip_download": True, "quiet": True, "no_warnings": True, "ignoreerrors": True,
            "ignore_no_formats_error": True}  # return metadata+captions even when YT serves no DL format to this IP
    if META_PROXY:
        base["proxy"] = META_PROXY
    if opts:
        base.update(opts)
    return YoutubeDL(base)


def seed_to_target(seed, mode):
    """Map a seed + mode to a yt-dlp-resolvable target string.

    urls     → the URL/id as-is (a watch URL, channel URL, or playlist URL)
    channels → a channel id (UC...), a handle (@name or name), or a channel URL → /videos listing
    keywords → ytsearchN:<query> so extract_flat returns the top N search results
    """
    if mode == "keywords":
        return f"ytsearch{MAX_VIDEOS}:{seed}"
    if mode == "channels":
        if seed.startswith("http"):
            return seed
        if seed.startswith("UC") and len(seed) >= 20:
            return f"https://www.youtube.com/channel/{seed}/videos"
        handle = seed if seed.startswith("@") else f"@{seed}"
        return f"https://www.youtube.com/{handle}/videos"
    return seed  # urls mode: pass through


def list_video_ids(target):
    """extract_flat listing → generator of (video_id, channel_id_or_None). Memory-safe: yields as it
    walks entries instead of returning a giant list. A single watch URL yields just that one id."""
    info = _ydl({"extract_flat": True, "playlistend": MAX_VIDEOS}).extract_info(target, download=False)
    if not info:
        return
    # a single video resolves to a dict with _type='video' (or no entries)
    entries = info.get("entries")
    if entries is None:
        vid = info.get("id")
        if vid:
            yield vid, info.get("channel_id")
        return
    n = 0
    for e in entries:
        if not e:
            continue
        # nested playlists (e.g. a channel's tabs) → recurse one level into their entries
        sub = e.get("entries")
        if sub is not None:
            for se in sub:
                if se and se.get("id"):
                    yield se["id"], se.get("channel_id")
                    n += 1
                    if n >= MAX_VIDEOS:
                        return
            continue
        vid = e.get("id")
        if vid:
            yield vid, e.get("channel_id")
            n += 1
            if n >= MAX_VIDEOS:
                return


def extract_video(video_id):
    """Full per-video extract (stats, tags, channel + caption track URLs). Returns the info dict or None.

    A full (non-flat) extract populates info['subtitles'] (manual) and info['automatic_captions'] (auto)
    with per-language format lists that include json3 track URLs — that's what fetch_json3_captions()
    consumes. We don't pass writesubtitles (that would write files); we only read the in-memory URLs.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    return _ydl({"writesubtitles": False, "writeautomaticsub": False}).extract_info(url, download=False)


def parse_channel_row(info):
    """yt-dlp info dict → tuple for youtube_channels upsert, or None if no channel_id."""
    cid = info.get("channel_id") or info.get("uploader_id")
    if not cid:
        return None
    return (
        cid,
        info.get("channel") or info.get("uploader"),
        info.get("uploader_id") if str(info.get("uploader_id", "")).startswith("@") else None,
        _int(info.get("channel_follower_count")),
        None,  # video_count: not present on a single-video extract; backfilled by channel-mode listings
        None,  # view_count (channel-level): not available per-video
        None,  # country: rarely present per-video
        None,  # description (channel-level): not the video description
        Json({"uploader_url": info.get("uploader_url"), "channel_url": info.get("channel_url")}),
    )


def parse_video_row(info):
    """yt-dlp info dict → tuple for youtube_videos upsert, or None if no id."""
    vid = info.get("id")
    if not vid:
        return None
    # trim the raw dict: drop the huge 'formats'/'thumbnails'/'automatic_captions' blobs we don't store
    raw = {k: v for k, v in info.items()
           if k not in ("formats", "thumbnails", "automatic_captions", "subtitles",
                        "requested_formats", "heatmap", "comments")}
    return (
        vid,
        info.get("channel_id") or info.get("uploader_id"),
        info.get("title"),
        info.get("description"),
        _int(info.get("duration")),
        _int(info.get("view_count")),
        _int(info.get("like_count")),
        _int(info.get("comment_count")),
        _published_at(info),
        Json(info.get("tags") or []),
        Json(raw),
    )


def upsert_channel(cur, row):
    """Idempotent channel upsert. COALESCE keeps a previously-known richer value when a per-video
    extract supplies NULL for a channel-level field (so channel-mode listings can enrich it later)."""
    cur.execute(
        """INSERT INTO youtube_channels
           (channel_id,title,handle,subscriber_count,video_count,view_count,country,description,metadata)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (channel_id) DO UPDATE SET
             title=COALESCE(EXCLUDED.title, youtube_channels.title),
             handle=COALESCE(EXCLUDED.handle, youtube_channels.handle),
             subscriber_count=COALESCE(EXCLUDED.subscriber_count, youtube_channels.subscriber_count),
             video_count=COALESCE(EXCLUDED.video_count, youtube_channels.video_count),
             view_count=COALESCE(EXCLUDED.view_count, youtube_channels.view_count),
             country=COALESCE(EXCLUDED.country, youtube_channels.country),
             description=COALESCE(EXCLUDED.description, youtube_channels.description),
             metadata=youtube_channels.metadata || EXCLUDED.metadata,
             last_audited_at=now()""",
        row,
    )


def upsert_video(cur, row):
    """Idempotent video upsert (stats refresh on re-run). Does NOT touch transcript columns — those are
    owned by save_transcript() so a metadata re-run never clobbers a previously-fetched transcript."""
    cur.execute(
        """INSERT INTO youtube_videos
           (video_id,channel_id,title,description,duration_sec,view_count,like_count,comment_count,
            published_at,tags,raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (video_id) DO UPDATE SET
             channel_id=COALESCE(EXCLUDED.channel_id, youtube_videos.channel_id),
             title=EXCLUDED.title, description=EXCLUDED.description,
             duration_sec=EXCLUDED.duration_sec, view_count=EXCLUDED.view_count,
             like_count=EXCLUDED.like_count, comment_count=EXCLUDED.comment_count,
             published_at=COALESCE(EXCLUDED.published_at, youtube_videos.published_at),
             tags=EXCLUDED.tags, raw=EXCLUDED.raw, updated_at=now()""",
        row,
    )


# ---------------------------------------------------------------------------
# 2. TRANSCRIPTS — PRIMARY: yt-dlp json3 captions; SECONDARY: youtube-transcript-api; Whisper STUB last
# ---------------------------------------------------------------------------
# WHY json3-via-yt-dlp is primary: youtube-transcript-api 1.0.3 hits YouTube's deprecated timedtext XML
# endpoint, which returns an EMPTY body from datacenter IPs (-> "ParseError: no element found"). yt-dlp
# surfaces the same captions as a json3 track URL that DOES serve content from those IPs. We fetch ONE
# preferred language only (fetching all 4 variants at once triggered HTTP 429).

def parse_transcript(segments):
    """Normalize raw youtube-transcript-api segments → list of {text,start,dur}.

    Accepts either the library's FetchedTranscriptSnippet objects (attrs .text/.start/.duration) or
    plain dicts ({'text','start','duration'}), so the same parser works for live data and JSON fixtures.
    """
    out = []
    for s in segments:
        if isinstance(s, dict):
            text, start, dur = s.get("text"), s.get("start"), s.get("duration")
        else:
            text, start, dur = getattr(s, "text", None), getattr(s, "start", None), getattr(s, "duration", None)
        out.append({"text": text or "", "start": float(start or 0), "dur": float(dur or 0)})
    return out


def parse_json3(payload):
    """Parse YouTube json3 caption payload → list of {text,start,dur}.

    json3 shape: {"events":[{"tStartMs":int,"dDurationMs":int,"segs":[{"utf8":"..."}]}, ...]}. Each event
    is one timed cue; its text is the concatenation of its segs' utf8. Events with no segs (e.g. pure
    layout/append-newline cues) or empty text are skipped. ms → seconds for start/dur.
    """
    out = []
    for ev in (payload or {}).get("events", []) or []:
        segs = ev.get("segs") or []
        text = "".join(s.get("utf8", "") for s in segs)
        if not text.strip():
            continue
        out.append({
            "text": text,
            "start": (ev.get("tStartMs") or 0) / 1000.0,
            "dur": (ev.get("dDurationMs") or 0) / 1000.0,
        })
    return out


def pick_caption_track(info, langs=None):
    """From a yt-dlp info dict, pick ONE json3 caption track for the best-matching language.

    Prefers manual `subtitles` over `automatic_captions`; within those, prefers languages in `langs`
    order, then any json3-capable track. Returns (json3_url, lang) or (None, None). We deliberately
    return a SINGLE track — fetching every variant at once triggers HTTP 429.
    """
    langs = langs or LANGS
    for source in ("subtitles", "automatic_captions"):
        tracks = info.get(source) or {}
        # 1. preferred languages, in order
        for want in langs:
            url = _json3_url(tracks.get(want))
            if url:
                return url, want
        # 2. any track that offers a json3 format (first available language)
        for lang, fmts in tracks.items():
            url = _json3_url(fmts)
            if url:
                return url, lang
    return None, None


def _json3_url(fmts):
    """Given a caption track's format list, return the json3 entry's url (or None)."""
    for f in fmts or []:
        if f.get("ext") == "json3" and f.get("url"):
            return f["url"]
    return None


def fetch_json3_captions(info, langs=None, proxy=None, max_retries=8):
    """PRIMARY transcript path: pick one json3 track from `info` and fetch+parse it with httpx.

    Exponential backoff with jitter on HTTP 429. Honors YT_PROXY. Returns (segments, 'yt-dlp-json3',
    lang) or (None, None, None) when there's no track / all retries fail / the body is empty.
    """
    langs = langs or LANGS
    proxy = PROXY if proxy is None else proxy
    url, lang = pick_caption_track(info, langs)
    if not url:
        return None, None, None
    client_kwargs = {"timeout": 30, "follow_redirects": True}
    if proxy:
        client_kwargs["proxy"] = proxy
    for attempt in range(max_retries):
        try:
            with httpx.Client(**client_kwargs) as c:
                r = c.get(url)
            if r.status_code == 429:
                wait = min(60, 2 ** attempt) + random.uniform(0, 2)
                print(f"  [{info.get('id')}] json3 429 — backoff {wait:.0f}s (attempt {attempt+1}/{max_retries})", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            segments = parse_json3(r.json())
            if segments:
                return segments, "yt-dlp-json3", lang
            return None, None, None  # empty track → let the api fallback try
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"  [{info.get('id')}] json3 fetch failed: {repr(e)[:80]}", flush=True)
                return None, None, None
            wait = min(30, 2 ** attempt) + random.uniform(0, 1)
            print(f"  [{info.get('id')}] json3 error (attempt {attempt+1}/{max_retries}): {repr(e)[:60]} — retry in {wait:.0f}s", flush=True)
            time.sleep(wait)
    return None, None, None


def fetch_transcript(video_id, langs=None, translate=None, proxy=None):
    """SECONDARY transcript path: youtube-transcript-api. Returns (segments, source, lang) or (None,None,None).

    Used only as a fallback when yt-dlp json3 yields nothing. Language strategy: try the preferred
    `langs` in order; if none match but a transcript exists, take the first available one and (if
    `translate` is set and supported) translate it. Gracefully swallows TranscriptsDisabled /
    NoTranscriptFound / IpBlocked and any other library error → (None,None,None).
    """
    langs = langs or LANGS
    translate = TRANSLATE if translate is None else translate
    proxy = PROXY if proxy is None else proxy
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            TranscriptsDisabled, NoTranscriptFound, VideoUnavailable,
        )
        try:
            from youtube_transcript_api._errors import IpBlocked  # newer versions
        except Exception:  # pragma: no cover - older lib without IpBlocked
            IpBlocked = ()
    except Exception as e:  # pragma: no cover - lib not installed (handled at call sites)
        print(f"  [{video_id}] transcript lib unavailable: {repr(e)[:60]}", flush=True)
        return None, None, None

    # Proxy support: youtube-transcript-api v1.x takes a proxy_config; older takes proxies=. Try both.
    kwargs = {}
    if proxy:
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig
            kwargs["proxy_config"] = GenericProxyConfig(http_url=proxy, https_url=proxy)
        except Exception:
            kwargs["proxies"] = {"http": proxy, "https": proxy}

    try:
        api = YouTubeTranscriptApi(**kwargs)
        listing = api.list(video_id)
        transcript = None
        # 1. preferred language, manually-created first then auto-generated
        try:
            transcript = listing.find_transcript(langs)
        except Exception:
            transcript = None
        # 2. else any transcript; optionally translate it
        if transcript is None:
            transcript = next(iter(listing))
            if translate:
                try:
                    if transcript.is_translatable:
                        transcript = transcript.translate(translate)
                except Exception:
                    pass
        fetched = transcript.fetch()
        segments = parse_transcript(fetched)
        return segments, "youtube_transcript_api", getattr(transcript, "language_code", None)
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return None, None, None
    except IpBlocked:  # type: ignore[misc]
        print(f"  [{video_id}] transcript IP-blocked — set YT_PROXY to recover", flush=True)
        return None, None, None
    except Exception as e:
        print(f"  [{video_id}] transcript error: {repr(e)[:80]}", flush=True)
        return None, None, None


def whisper_transcribe(video_id, audio_url=None):  # pragma: no cover - documented STUB, no impl
    """SELF-HOSTED WHISPER FALLBACK — INTERFACE STUB ONLY (not implemented).

    Intended for videos where BOTH caption paths return nothing (captions disabled / none found).
    The homelab has NO GPU, so a real implementation would run whisper.cpp (or faster-whisper on CPU,
    tiny/base model) against the audio-only stream — never downloading video.

    A production implementation would:
      1. Use yt-dlp to extract the bestaudio stream URL (format 'bestaudio', skip_download=True) — or
         stream it directly to ffmpeg without writing a file, to stay disk-safe.
      2. Pipe 16 kHz mono PCM into a self-hosted Whisper endpoint (e.g. a faster-whisper HTTP service
         on the devcore VM, or the whisper.cpp `server`), reading WHISPER_URL from the environment.
      3. Return the same shape as fetch_transcript(): (segments, "whisper", lang) where each segment is
         {text,start,dur}, so save_transcript() can store it identically.

    Returning (None, None, None) here means "fallback unavailable" — callers must handle that. Wire this
    up only once a CPU Whisper service exists; until then transcripts come from yt-dlp json3 captions
    (primary) or youtube-transcript-api (secondary).
    """
    _ = (video_id, audio_url)
    return None, None, None


def save_transcript(cur, video_id, segments, source, lang):
    """Persist a transcript onto the (already-upserted) video row. Idempotent: re-running overwrites
    with the freshest transcript. Only writes when we actually have segments."""
    if not segments:
        return False
    cur.execute(
        """UPDATE youtube_videos
           SET transcript=%s, transcript_source=%s, transcript_lang=%s, updated_at=now()
           WHERE video_id=%s""",
        (Json(segments), source, lang, video_id),
    )
    return True


# ---------------------------------------------------------------------------
# 3. COMMENTS (yt-dlp getcomments) — streamed into a parent_id self-ref tree
# ---------------------------------------------------------------------------
def extract_comments(video_id, limit=None):
    """Per-video extract WITH comments (getcomments=True), capped via max_comments. Returns the raw
    comment dicts (yt-dlp already flattens replies into a flat list with 'parent' pointers). yt-dlp
    streams comments internally; we cap so a viral video never balloons memory."""
    limit = COMMENT_LIMIT if limit is None else limit
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "getcomments": True,
        # cap total comments fetched; 'all'/'all' would pull replies too but we bound by the int cap
        "extractor_args": {"youtube": {"max_comments": [str(limit), "all", str(limit), "all"]}},
    }
    info = _ydl(opts).extract_info(url, download=False)
    if not info:
        return []
    return info.get("comments") or []


def parse_comment_rows(video_id, comments):
    """yt-dlp comment dicts → rows for youtube_comments. parent_id is the self-ref tree: yt-dlp marks a
    top-level comment with parent == 'root', which we map to NULL so the FK/tree matches reddit_comments.
    """
    rows = []
    for c in comments:
        cid = c.get("id")
        if not cid:
            continue
        parent = c.get("parent")
        parent_id = None if (not parent or parent == "root") else parent
        rows.append((
            cid,
            video_id,
            parent_id,
            c.get("author"),
            c.get("text"),
            _int(c.get("like_count")),
            _ts(c.get("timestamp")),
        ))
    return rows


def upsert_comments(cur, rows):
    """Idempotent bulk comment upsert (like_count/text refresh on re-run). Caller flushes per video so
    we never hold more than one video's comments in RAM."""
    if not rows:
        return 0
    execute_values(
        cur,
        """INSERT INTO youtube_comments
           (comment_id,video_id,parent_id,author,text,like_count,published_at)
           VALUES %s
           ON CONFLICT (comment_id) DO UPDATE SET
             text=EXCLUDED.text, like_count=EXCLUDED.like_count,
             parent_id=COALESCE(EXCLUDED.parent_id, youtube_comments.parent_id)""",
        rows,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
def process_video(cur, conn, video_id, fetch_comments=None):
    """Full per-video pipeline: metadata → transcript → (optional) comments, each flushed (committed)
    so a crash mid-channel keeps everything already saved. Returns a dict of what was stored."""
    fetch_comments = FETCH_COMMENTS if fetch_comments is None else fetch_comments
    result = {"video_id": video_id, "video": False, "transcript": False, "comments": 0}

    info = extract_video(video_id)
    if not info:
        print(f"  [{video_id}] no metadata (private/removed?) — skip", flush=True)
        return result

    ch_row = parse_channel_row(info)
    if ch_row:
        upsert_channel(cur, ch_row)               # channel must exist before the video FK
    v_row = parse_video_row(info)
    if v_row:
        upsert_video(cur, v_row)
        result["video"] = True
    conn.commit()

    # transcript chain: yt-dlp json3 (primary) → youtube-transcript-api (secondary) → Whisper stub (last)
    segments, source, lang = fetch_json3_captions(info)
    if not segments:
        segments, source, lang = fetch_transcript(video_id)
    if not segments:
        segments, source, lang = whisper_transcribe(video_id)
    if save_transcript(cur, video_id, segments, source, lang):
        result["transcript"] = True
        result["transcript_source"] = source
        conn.commit()

    if fetch_comments:
        try:
            comments = extract_comments(video_id)
            rows = parse_comment_rows(video_id, comments)
            result["comments"] = upsert_comments(cur, rows)
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"  [{video_id}] comments error: {repr(e)[:80]}", flush=True)
    return result


def run():
    if not SEEDS:
        print("youtube: no YT_SEEDS provided — nothing to do", flush=True)
        return "youtube: no seeds"
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    seen = set()
    totals = {"videos": 0, "transcripts": 0, "comments": 0}
    for seed in SEEDS:
        target = seed_to_target(seed, MODE)
        print(f"youtube[{MODE}]: {seed} → {target}", flush=True)
        try:
            ids = list_video_ids(target)
        except Exception as e:
            print(f"  listing error for {seed!r}: {repr(e)[:80]}", flush=True)
            continue
        for video_id, _cid in ids:
            if video_id in seen:
                continue
            seen.add(video_id)
            try:
                r = process_video(cur, conn, video_id)
            except Exception as e:
                conn.rollback()
                print(f"  [{video_id}] FAILED: {repr(e)[:100]}", flush=True)
                continue
            totals["videos"] += 1 if r["video"] else 0
            totals["transcripts"] += 1 if r["transcript"] else 0
            totals["comments"] += r["comments"]
            if totals["videos"] % 25 == 0 and totals["videos"]:
                print(f"  …{totals['videos']} videos, {totals['transcripts']} transcripts, "
                      f"{totals['comments']} comments", flush=True)
            if VIDEO_SLEEP > 0:
                time.sleep(VIDEO_SLEEP)   # polite gap between videos (avoids caption-fetch 429s)
    cur.close()
    conn.close()
    msg = (f"youtube: done — {totals['videos']} videos, {totals['transcripts']} transcripts, "
           f"{totals['comments']} comments")
    print(msg, flush=True)
    return msg


if __name__ == "__main__":
    run()
