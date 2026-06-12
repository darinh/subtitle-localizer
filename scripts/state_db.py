"""Resumable SQLite state for the pipeline (survives crashes mid-wave).

Two tables:
  titles : one row per film/episode (lifecycle status + counts + versions)
  slices : one row per worker range (attempt, status, checksum, model) so a crash
           can tell which slices completed / are stale / need redispatch.

This is intentionally small; the authoritative integrity guard is still the
finalize gate-chain + the delivery manifest. The DB makes orchestration resumable.
"""
import os
import sqlite3
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
  key            TEXT PRIMARY KEY,
  video          TEXT,
  n_cues_src     INTEGER,
  n_cues_out     INTEGER,
  status         TEXT DEFAULT 'pending',  -- pending|extracted|sliced|translating|
                                          -- harvested|built|reviewed|done|failed
  error          TEXT,
  prompt_version TEXT,
  config_version TEXT,
  updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS slices (
  title          TEXT,
  part           INTEGER,
  lo             INTEGER,
  hi             INTEGER,
  attempt        INTEGER DEFAULT 1,
  status         TEXT DEFAULT 'pending',  -- pending|dispatched|written|stale
  model          TEXT,
  checksum       TEXT,
  size           INTEGER,
  mtime          REAL,
  superseded_by  INTEGER,
  updated_at     TEXT,
  PRIMARY KEY (title, part)
);
"""


def db_path(cfg=None):
    cfg = cfg or config.load()
    return str(config.ROOT / "pipeline.db")


def connect(cfg=None):
    con = sqlite3.connect(db_path(cfg))
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def upsert_title(con, key, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(fields)
    qs = ", ".join("?" for _ in fields)
    sets = ", ".join(f"{c}=excluded.{c}" for c in fields)
    con.execute(
        f"INSERT INTO titles (key, {cols}) VALUES (?, {qs}) "
        f"ON CONFLICT(key) DO UPDATE SET {sets}",
        (key, *fields.values()))
    con.commit()


def set_status(con, key, status, error=None):
    con.execute("UPDATE titles SET status=?, error=?, updated_at=? WHERE key=?",
                (status, error, now(), key))
    con.commit()


def record_slice(con, title, part, lo, hi, **fields):
    fields["updated_at"] = now()
    base = {"title": title, "part": part, "lo": lo, "hi": hi}
    base.update(fields)
    cols = ", ".join(base)
    qs = ", ".join("?" for _ in base)
    sets = ", ".join(f"{c}=excluded.{c}" for c in base if c not in ("title", "part"))
    con.execute(
        f"INSERT INTO slices ({cols}) VALUES ({qs}) "
        f"ON CONFLICT(title, part) DO UPDATE SET {sets}",
        tuple(base.values()))
    con.commit()


if __name__ == "__main__":
    con = connect()
    n = con.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
    print(f"state db ok at {db_path()}  ({n} titles)")
    for row in con.execute("SELECT status, COUNT(*) FROM titles GROUP BY status"):
        print(" ", row[0], row[1])
