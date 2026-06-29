"""Integration / stress harness for the YouTube connector — REAL yt-dlp + httpx + DB.

Guarded behind RUN_INTEGRATION=1 because it needs: network, yt-dlp (+ youtube-transcript-api as the
secondary fallback), and a live Postgres (PG_DSN) with youtube_schema.sql already applied. It is
intended to run SERVER-SIDE (the homelab worker container), NOT in local CI. Under pytest it auto-skips
unless the guard is set.

What it does:
  1. Runs the full per-video pipeline (metadata → transcript → comments) against real video ids. The
     transcript path is yt-dlp json3 captions (primary) → youtube-transcript-api (secondary); on a
     datacenter IP the json3 path is what actually returns content.
  2. Asserts rows actually landed (channels ≥1, videos ≥1, ≥1 transcript) and no exception escaped.
  3. Proves IDEMPOTENCY end-to-end: a second pass over the same ids inserts 0 net-new video rows.

Run server-side:
    RUN_INTEGRATION=1 YT_FETCH_COMMENTS=1 \
    PG_DSN=postgresql://devcore:...@devcore-postgres:5432/market_research \
    python tests/stress_youtube.py
or via pytest:
    RUN_INTEGRATION=1 pytest tests/stress_youtube.py -s

Env:
  RUN_INTEGRATION=1            required to run (otherwise skip/exit)
  STRESS_VIDEO_IDS            comma list of real 11-char ids (default: a few stable, caption-rich ids)
  YT_FETCH_COMMENTS=1         also exercise the comment path (slower)
  PG_DSN                      live DB
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import psycopg2  # noqa: E402

# default ids: stable, public, have captions + comments (TED-Ed style + a classic). Override via env.
DEFAULT_IDS = "dQw4w9WgXcQ,9bZkp7q19f0,arj7oStGLkU"
VIDEO_IDS = [v.strip() for v in os.environ.get("STRESS_VIDEO_IDS", DEFAULT_IDS).split(",") if v.strip()]


def _counts(cur):
    counts = {}
    for t in ("youtube_channels", "youtube_videos", "youtube_comments", "youtube_video_insights"):
        cur.execute(f"SELECT count(*) FROM {t}")
        counts[t] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM youtube_videos WHERE transcript IS NOT NULL")
    counts["transcripts"] = cur.fetchone()[0]
    return counts


def run_stress():
    import connectors.youtube as yt

    dsn = os.environ["PG_DSN"]
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    before = _counts(cur)
    print(f"[stress] before: {before}", flush=True)

    results = []
    for vid in VIDEO_IDS:
        print(f"[stress] processing {vid}", flush=True)
        r = yt.process_video(cur, conn, vid)   # uses YT_FETCH_COMMENTS env
        results.append(r)
        print(f"[stress]   -> {r}", flush=True)

    after = _counts(cur)
    print(f"[stress] after run 1: {after}", flush=True)

    # row-count assertions
    assert after["youtube_videos"] >= before["youtube_videos"] + 1, "no videos inserted"
    assert after["youtube_channels"] >= 1, "no channels inserted"
    assert after["transcripts"] >= 1, "no transcripts captured for any test video"
    assert any(r["video"] for r in results), "no video metadata stored"

    # idempotency: a second pass must not grow the video table
    videos_after_1 = after["youtube_videos"]
    for vid in VIDEO_IDS:
        yt.process_video(cur, conn, vid)
    after2 = _counts(cur)
    print(f"[stress] after run 2 (idempotency): {after2}", flush=True)
    assert after2["youtube_videos"] == videos_after_1, "second run created duplicate video rows"

    cur.close()
    conn.close()
    print("[stress] PASS — pipeline ran, rows present, idempotent", flush=True)
    return after2


# pytest entrypoint (auto-skips without the guard) -----------------------------
def test_stress_pipeline():
    import pytest
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("integration test — set RUN_INTEGRATION=1 (needs network + yt-dlp + DB)")
    run_stress()


if __name__ == "__main__":
    if os.environ.get("RUN_INTEGRATION") != "1":
        print("refusing to run: set RUN_INTEGRATION=1 (needs network + yt-dlp + live DB)")
        sys.exit(2)
    run_stress()
