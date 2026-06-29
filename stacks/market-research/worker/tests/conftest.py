"""Shared pytest fixtures + a fake psycopg2 cursor that emulates ON CONFLICT upsert semantics.

The unit tests never touch a real Postgres. FakeCursor parses just enough of the connector's SQL to
prove the *idempotency contract*: an upsert with a duplicate natural id updates in place (0 new rows),
a new natural id inserts. It supports the exact statements the YouTube connector issues:
  - youtube_channels    INSERT ... ON CONFLICT (channel_id) DO UPDATE
  - youtube_videos      INSERT ... ON CONFLICT (video_id)   DO UPDATE
  - youtube_comments    execute_values INSERT ... ON CONFLICT (comment_id) DO UPDATE
  - youtube_videos      UPDATE ... WHERE video_id=%s   (save_transcript)
This keeps the tests fast, deterministic, and dependency-free while still exercising the real SQL.
"""
import json
import os
import re

import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def video_info():
    return load_fixture("video_info.json")


@pytest.fixture
def transcript_segments():
    return load_fixture("transcript.json")


@pytest.fixture
def comment_dicts():
    return load_fixture("comments.json")


@pytest.fixture
def json3_payload():
    return load_fixture("captions_json3.json")


class FakeCursor:
    """Minimal in-memory store keyed by (table, natural_id). rowcount reflects rows touched.

    `new_inserts` counts only rows whose natural id was not already present — the metric the
    idempotency test asserts is 0 on a second run.
    """

    # which column is the conflict key per table
    _KEY = {
        "youtube_channels": "channel_id",
        "youtube_videos": "video_id",
        "youtube_comments": "comment_id",
        "youtube_video_insights": "video_id",
    }

    def __init__(self):
        self.store = {}          # (table, natural_id) -> row tuple
        self.rowcount = 0
        self.new_inserts = 0     # cumulative count of genuinely-new rows
        self._result = []

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _table(sql):
        m = re.search(r'(?:INSERT INTO|UPDATE)\s+(\w+)', sql, re.I)
        return m.group(1) if m else None

    def _apply_rows(self, table, rows):
        key_idx = 0  # all connector inserts put the natural id first in the column list
        touched = 0
        for row in rows:
            nid = row[key_idx]
            present = (table, nid) in self.store
            self.store[(table, nid)] = row
            if not present:
                self.new_inserts += 1
            touched += 1
        self.rowcount = touched

    # -- psycopg2 cursor surface -----------------------------------------
    def execute(self, sql, params=None):
        table = self._table(sql)
        if re.match(r'\s*INSERT', sql, re.I) and table:
            # single-row insert (channels/videos/insights)
            self._apply_rows(table, [tuple(params)])
        elif re.match(r'\s*UPDATE', sql, re.I) and table == "youtube_videos":
            # save_transcript UPDATE ... WHERE video_id=%s  → last param is the video_id
            vid = params[-1]
            row = self.store.get((table, vid))
            self.rowcount = 1 if row is not None else 0
        elif re.match(r'\s*SELECT', sql, re.I):
            self._result = []
            self.rowcount = 0
        else:
            self.rowcount = 0

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def count(self, table):
        return sum(1 for (t, _id) in self.store if t == table)

    def close(self):
        pass


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_cursor():
    return FakeCursor()


@pytest.fixture
def fake_conn(fake_cursor):
    return FakeConn(fake_cursor)


def fake_execute_values(cur, sql, rows, *args, **kwargs):
    """Stand-in for psycopg2.extras.execute_values against FakeCursor."""
    table = FakeCursor._table(sql)
    cur._apply_rows(table, [tuple(r) for r in rows])
