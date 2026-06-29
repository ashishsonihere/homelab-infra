"""Unit tests for connectors/youtube.py — metadata/transcript/comment parsing, upsert SQL, idempotency.

All external services are MOCKED: yt-dlp and youtube-transcript-api are never invoked; the DB is the
FakeCursor from conftest.py. The idempotency test (test_second_run_inserts_zero_rows) proves the
ON CONFLICT contract: a second pass over the same data inserts 0 new rows.
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import connectors.youtube as yt  # noqa: E402
from conftest import fake_execute_values  # noqa: E402


# ---------------------------------------------------------------------------
# metadata parsing
# ---------------------------------------------------------------------------
def test_parse_video_row(video_info):
    row = yt.parse_video_row(video_info)
    assert row is not None
    # (video_id, channel_id, title, description, duration, views, likes, comments, published_at, tags, raw)
    assert row[0] == "dQw4w9WgXcQ"
    assert row[1] == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert row[2] == "Never Gonna Give You Up"
    assert row[4] == 213
    assert row[5] == 1600000000
    assert row[6] == 17000000
    assert row[7] == 2200000
    # published_at derived from the unix timestamp
    assert row[8] is not None and row[8].year == 2010


def test_parse_video_row_trims_heavy_blobs(video_info):
    row = yt.parse_video_row(video_info)
    raw = row[10].adapted  # psycopg2 Json wrapper exposes the original via .adapted
    assert "formats" not in raw
    assert "thumbnails" not in raw
    assert "automatic_captions" not in raw
    assert raw["title"] == "Never Gonna Give You Up"


def test_parse_video_row_no_id():
    assert yt.parse_video_row({"title": "no id"}) is None


def test_parse_channel_row(video_info):
    row = yt.parse_channel_row(video_info)
    assert row is not None
    assert row[0] == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert row[1] == "Rick Astley"
    assert row[2] == "@RickAstleyYT"          # handle (uploader_id starting with @)
    assert row[3] == 3900000                   # subscriber_count


def test_published_at_falls_back_to_upload_date():
    info = {"id": "x", "upload_date": "20200115"}
    row = yt.parse_video_row(info)
    assert row[8] is not None and row[8].year == 2020 and row[8].month == 1


# ---------------------------------------------------------------------------
# transcript parsing
# ---------------------------------------------------------------------------
def test_parse_transcript_from_dicts(transcript_segments):
    segs = yt.parse_transcript(transcript_segments)
    assert len(segs) == 3
    assert segs[0] == {"text": "Hey everyone welcome back", "start": 0.0, "dur": 2.5}
    assert all(set(s.keys()) == {"text", "start", "dur"} for s in segs)


def test_parse_transcript_from_objects():
    class Snippet:
        def __init__(self, text, start, duration):
            self.text, self.start, self.duration = text, start, duration

    segs = yt.parse_transcript([Snippet("hi", 1, 2), Snippet("bye", 3, 4)])
    assert segs == [{"text": "hi", "start": 1.0, "dur": 2.0},
                    {"text": "bye", "start": 3.0, "dur": 4.0}]


# ---------------------------------------------------------------------------
# json3 captions (PRIMARY transcript path) — parser + track picking + fetch/backoff
# ---------------------------------------------------------------------------
def test_parse_json3(json3_payload):
    segs = yt.parse_json3(json3_payload)
    # 5 events in the fixture, but newline-only + segless cues are dropped → 3 real cues
    assert len(segs) == 3
    # event 0 concatenates its two segs' utf8; ms → seconds
    assert segs[0] == {"text": "Hey everyone welcome back", "start": 0.0, "dur": 2.5}
    assert segs[1]["text"] == "today we talk about inventory sync"
    assert segs[1]["start"] == 2.5 and segs[1]["dur"] == 3.0
    assert all(set(s.keys()) == {"text", "start", "dur"} for s in segs)


def test_parse_json3_empty():
    assert yt.parse_json3({}) == []
    assert yt.parse_json3({"events": []}) == []
    assert yt.parse_json3({"events": [{"tStartMs": 0, "segs": [{"utf8": "  "}]}]}) == []


def test_pick_caption_track_prefers_manual_json3(video_info):
    url, lang = yt.pick_caption_track(video_info, ["en"])
    # manual subtitles ('subtitles') beat automatic_captions; json3 ext is chosen over vtt
    assert url == "https://example.test/en.json3"
    assert lang == "en"


def test_pick_caption_track_falls_back_to_auto():
    info = {"automatic_captions": {"en": [{"ext": "json3", "url": "https://x/a.json3"}]}}
    url, lang = yt.pick_caption_track(info, ["en"])
    assert url == "https://x/a.json3" and lang == "en"


def test_pick_caption_track_any_lang_when_no_preferred():
    info = {"automatic_captions": {"de": [{"ext": "json3", "url": "https://x/de.json3"}]}}
    url, lang = yt.pick_caption_track(info, ["en"])  # 'en' missing → take the only json3 track
    assert url == "https://x/de.json3" and lang == "de"


def test_pick_caption_track_none_when_no_json3():
    info = {"subtitles": {"en": [{"ext": "vtt", "url": "https://x/en.vtt"}]}}
    assert yt.pick_caption_track(info, ["en"]) == (None, None)


def test_fetch_json3_captions_success(monkeypatch, video_info, json3_payload):
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return json3_payload

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResp()

    monkeypatch.setattr(yt.httpx, "Client", FakeClient)
    segs, source, lang = yt.fetch_json3_captions(video_info, langs=["en"], proxy="")
    assert source == "yt-dlp-json3"
    assert lang == "en"
    assert len(segs) == 3
    assert segs[0]["text"] == "Hey everyone welcome back"


def test_fetch_json3_captions_retries_on_429(monkeypatch, video_info, json3_payload):
    """First call returns 429, second succeeds → backoff then success. time.sleep is stubbed."""
    monkeypatch.setattr(yt.time, "sleep", lambda *a, **k: None)
    calls = {"n": 0}

    class Resp429:
        status_code = 429

        def raise_for_status(self):
            raise AssertionError("should not be called on 429")

        def json(self):
            raise AssertionError

    class RespOK:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return json3_payload

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            calls["n"] += 1
            return Resp429() if calls["n"] == 1 else RespOK()

    monkeypatch.setattr(yt.httpx, "Client", FakeClient)
    segs, source, lang = yt.fetch_json3_captions(video_info, langs=["en"], proxy="", max_retries=4)
    assert calls["n"] == 2          # retried exactly once after the 429
    assert source == "yt-dlp-json3" and len(segs) == 3


def test_fetch_json3_captions_no_track_returns_none():
    # no captions at all → (None,None,None) so the api fallback can take over
    assert yt.fetch_json3_captions({"id": "x"}, langs=["en"], proxy="") == (None, None, None)


def test_whisper_stub_returns_none():
    # documented STUB — must return the (None,None,None) "fallback unavailable" sentinel
    assert yt.whisper_transcribe("anyid") == (None, None, None)


# ---------------------------------------------------------------------------
# comment-tree construction
# ---------------------------------------------------------------------------
def test_parse_comment_rows_builds_tree(comment_dicts):
    rows = yt.parse_comment_rows("dQw4w9WgXcQ", comment_dicts)
    assert len(rows) == 5
    by_id = {r[0]: r for r in rows}
    # top-level: parent == 'root' → NULL
    assert by_id["Ugxc1"][2] is None
    assert by_id["Ugxc2"][2] is None
    # missing parent field → also NULL (top-level)
    assert by_id["Ugxc-noparent"][2] is None
    # replies point at their parent comment id (self-ref tree)
    assert by_id["Ugxc1.r1"][2] == "Ugxc1"
    assert by_id["Ugxc1.r2"][2] == "Ugxc1"
    # every row carries the correct video_id and parsed like_count + published_at
    assert by_id["Ugxc1"][1] == "dQw4w9WgXcQ"
    assert by_id["Ugxc1"][5] == 42
    assert by_id["Ugxc1"][6] is not None


def test_parse_comment_rows_skips_idless():
    rows = yt.parse_comment_rows("vid", [{"text": "no id"}, {"id": "ok", "text": "kept"}])
    assert [r[0] for r in rows] == ["ok"]


# ---------------------------------------------------------------------------
# upsert SQL building + idempotency (FakeCursor emulates ON CONFLICT)
# ---------------------------------------------------------------------------
def _ingest_once(cur, video_info, comment_dicts):
    """Run the upsert path the way process_video() does, but against FakeCursor."""
    yt.upsert_channel(cur, yt.parse_channel_row(video_info))
    yt.upsert_video(cur, yt.parse_video_row(video_info))
    yt.save_transcript(cur, video_info["id"], [{"text": "x", "start": 0, "dur": 1}], "youtube_transcript_api", "en")
    yt.upsert_comments(cur, yt.parse_comment_rows(video_info["id"], comment_dicts))


def test_upsert_inserts_expected_rows(fake_cursor, monkeypatch, video_info, comment_dicts):
    monkeypatch.setattr(yt, "execute_values", fake_execute_values)
    _ingest_once(fake_cursor, video_info, comment_dicts)
    assert fake_cursor.count("youtube_channels") == 1
    assert fake_cursor.count("youtube_videos") == 1
    assert fake_cursor.count("youtube_comments") == 5
    assert fake_cursor.new_inserts == 7  # 1 channel + 1 video + 5 comments


def test_second_run_inserts_zero_rows(fake_cursor, monkeypatch, video_info, comment_dicts):
    """IDEMPOTENCY: a second identical ingest must insert 0 NEW rows (ON CONFLICT DO UPDATE)."""
    monkeypatch.setattr(yt, "execute_values", fake_execute_values)
    _ingest_once(fake_cursor, video_info, comment_dicts)
    first = fake_cursor.new_inserts
    assert first == 7
    _ingest_once(fake_cursor, video_info, comment_dicts)   # re-run
    assert fake_cursor.new_inserts == first                # no new rows added
    # row counts unchanged → no duplicates
    assert fake_cursor.count("youtube_channels") == 1
    assert fake_cursor.count("youtube_videos") == 1
    assert fake_cursor.count("youtube_comments") == 5


def test_save_transcript_noop_when_empty(fake_cursor):
    assert yt.save_transcript(fake_cursor, "vid", None, None, None) is False
    assert yt.save_transcript(fake_cursor, "vid", [], "src", "en") is False


# ---------------------------------------------------------------------------
# seed → target resolution
# ---------------------------------------------------------------------------
def test_seed_to_target_keywords(monkeypatch):
    monkeypatch.setattr(yt, "MAX_VIDEOS", 30)
    assert yt.seed_to_target("inventory saas", "keywords") == "ytsearch30:inventory saas"


def test_seed_to_target_channels_handle():
    assert yt.seed_to_target("RickAstleyYT", "channels") == "https://www.youtube.com/@RickAstleyYT/videos"
    assert yt.seed_to_target("@RickAstleyYT", "channels") == "https://www.youtube.com/@RickAstleyYT/videos"


def test_seed_to_target_channels_ucid():
    cid = "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert yt.seed_to_target(cid, "channels") == f"https://www.youtube.com/channel/{cid}/videos"


def test_seed_to_target_urls_passthrough():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert yt.seed_to_target(url, "urls") == url


# ---------------------------------------------------------------------------
# transcript fetch error handling (mock youtube-transcript-api at module boundary)
# ---------------------------------------------------------------------------
def test_fetch_transcript_handles_disabled(monkeypatch):
    """Simulate TranscriptsDisabled → returns (None,None,None) gracefully, no exception escapes."""
    import types

    errors_mod = types.ModuleType("youtube_transcript_api._errors")

    class TranscriptsDisabled(Exception):
        pass

    class NoTranscriptFound(Exception):
        pass

    class VideoUnavailable(Exception):
        pass

    errors_mod.TranscriptsDisabled = TranscriptsDisabled
    errors_mod.NoTranscriptFound = NoTranscriptFound
    errors_mod.VideoUnavailable = VideoUnavailable

    api_mod = types.ModuleType("youtube_transcript_api")

    class YouTubeTranscriptApi:
        def __init__(self, **kw):
            pass

        def list(self, vid):
            raise TranscriptsDisabled()

    api_mod.YouTubeTranscriptApi = YouTubeTranscriptApi
    api_mod._errors = errors_mod

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", api_mod)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", errors_mod)

    assert yt.fetch_transcript("vid", proxy="") == (None, None, None)


def test_fetch_transcript_success(monkeypatch, transcript_segments):
    """Happy path: a mocked api returns segments → parsed list + source + lang."""
    import types

    errors_mod = types.ModuleType("youtube_transcript_api._errors")
    for name in ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable"):
        setattr(errors_mod, name, type(name, (Exception,), {}))

    class FakeTranscript:
        language_code = "en"
        is_translatable = True

        def fetch(self):
            return transcript_segments

        def translate(self, lang):
            return self

    class FakeListing:
        def find_transcript(self, langs):
            return FakeTranscript()

        def __iter__(self):
            return iter([FakeTranscript()])

    class YouTubeTranscriptApi:
        def __init__(self, **kw):
            pass

        def list(self, vid):
            return FakeListing()

    api_mod = types.ModuleType("youtube_transcript_api")
    api_mod.YouTubeTranscriptApi = YouTubeTranscriptApi
    api_mod._errors = errors_mod

    monkeypatch.setitem(sys.modules, "youtube_transcript_api", api_mod)
    monkeypatch.setitem(sys.modules, "youtube_transcript_api._errors", errors_mod)

    segs, source, lang = yt.fetch_transcript("vid", langs=["en"], proxy="")
    assert source == "youtube_transcript_api"
    assert lang == "en"
    assert len(segs) == 3
    assert segs[0]["text"] == "Hey everyone welcome back"
