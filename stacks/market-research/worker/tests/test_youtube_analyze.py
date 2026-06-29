"""Unit tests for connectors/youtube_analyze.py — Pydantic schema, transcript flattening, LLM parsing,
and insight upsert idempotency. OpenRouter is never called; httpx.post is monkeypatched.

youtube_analyze imports OPENROUTER_API_KEY/PG_DSN at import time (os.environ[...]), so the test sets
both BEFORE importing the module.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("PG_DSN", "postgresql://test@localhost/test")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")

import connectors.youtube_analyze as ya  # noqa: E402
from pydantic import ValidationError  # noqa: E402


# ---------------------------------------------------------------------------
# Pydantic schema validation
# ---------------------------------------------------------------------------
def test_valid_insight():
    ins = ya.VideoInsight.model_validate({
        "scorecard_rating": 8,
        "summary": "Covers inventory sync pain.",
        "actionable_insights": ["build a sync tool"],
        "pain_points": ["manual reconciliation"],
        "ideas": ["auto-reconcile SaaS"],
    })
    assert ins.scorecard_rating == 8
    assert ins.pain_points == ["manual reconciliation"]


def test_rating_clamped_high():
    ins = ya.VideoInsight.model_validate({"scorecard_rating": 99, "summary": "x"})
    assert ins.scorecard_rating == 10


def test_rating_clamped_low_and_coerced_from_string():
    assert ya.VideoInsight.model_validate({"scorecard_rating": "0", "summary": "x"}).scorecard_rating == 1
    assert ya.VideoInsight.model_validate({"scorecard_rating": "7", "summary": "x"}).scorecard_rating == 7


def test_lists_coerced_from_null_and_string():
    ins = ya.VideoInsight.model_validate({
        "scorecard_rating": 5, "summary": "x",
        "actionable_insights": None,           # null → []
        "pain_points": "single string",        # str → [str]
    })
    assert ins.actionable_insights == []
    assert ins.pain_points == ["single string"]
    assert ins.ideas == []                      # missing → default []


def test_missing_summary_rejected():
    with pytest.raises(ValidationError):
        ya.VideoInsight.model_validate({"scorecard_rating": 5})


# ---------------------------------------------------------------------------
# transcript flattening
# ---------------------------------------------------------------------------
def test_transcript_to_text_from_list():
    txt = ya.transcript_to_text([{"text": "hello", "start": 0, "dur": 1},
                                 {"text": "world", "start": 1, "dur": 1}])
    assert txt == "hello world"


def test_transcript_to_text_from_json_string():
    txt = ya.transcript_to_text('[{"text": "a"}, {"text": "b"}]')
    assert txt == "a b"


def test_transcript_to_text_empty():
    assert ya.transcript_to_text(None) == ""
    assert ya.transcript_to_text([]) == ""


def test_transcript_truncated(monkeypatch):
    monkeypatch.setattr(ya, "TRANSCRIPT_CHARS", 5)
    txt = ya.transcript_to_text([{"text": "abcdefghij"}])
    assert len(txt) == 5


# ---------------------------------------------------------------------------
# LLM response parsing (validate_insight extracts JSON + validates)
# ---------------------------------------------------------------------------
def test_validate_insight_extracts_json_from_noise():
    raw = 'Sure! Here is the JSON:\n{"scorecard_rating": 6, "summary": "ok", "pain_points": ["p"]}\nThanks'
    ins = ya.validate_insight(raw)
    assert ins.scorecard_rating == 6
    assert ins.pain_points == ["p"]


def test_validate_insight_bad_json_raises():
    with pytest.raises(Exception):
        ya.validate_insight("not json at all")


def test_call_llm_success(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content":
                    '{"scorecard_rating": 9, "summary": "rich", "ideas": ["x"]}'}}]}

    monkeypatch.setattr(ya.httpx, "post", lambda *a, **k: FakeResp())
    ins = ya.call_llm("title", "long enough transcript text here")
    assert ins is not None
    assert ins.scorecard_rating == 9
    assert ins.ideas == ["x"]


def test_call_llm_returns_none_on_persistent_failure(monkeypatch):
    monkeypatch.setattr(ya.time, "sleep", lambda *a, **k: None)  # don't actually wait

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ya.httpx, "post", boom)
    assert ya.call_llm("t", "x" * 100) is None


# ---------------------------------------------------------------------------
# insight upsert + idempotency (FakeCursor)
# ---------------------------------------------------------------------------
def test_upsert_insight_idempotent(fake_cursor):
    ins = ya.VideoInsight.model_validate({"scorecard_rating": 7, "summary": "s"})
    ya.upsert_insight(fake_cursor, "vid1", ins, "openai/gpt-4o-mini")
    assert fake_cursor.count("youtube_video_insights") == 1
    assert fake_cursor.new_inserts == 1
    # re-run with same video_id → 0 new rows
    ya.upsert_insight(fake_cursor, "vid1", ins, "openai/gpt-4o-mini")
    assert fake_cursor.count("youtube_video_insights") == 1
    assert fake_cursor.new_inserts == 1
