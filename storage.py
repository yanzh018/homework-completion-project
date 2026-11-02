"""Local SQLite records. Uploaded originals and derived ink never share an output directory."""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("HANDWRITING_DATA", ROOT / "data")).resolve()


def uid():
    return uuid.uuid4().hex


@contextmanager
def db():
    DATA.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATA / "library.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init():
    for folder in ("originals", "pages", "glyphs", "exports"):
        (DATA / folder).mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS profiles(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS documents(
          id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(id), name TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind IN ('sample','assignment')), original TEXT NOT NULL,
          sha256 TEXT NOT NULL, status TEXT DEFAULT 'processing', error TEXT,
          created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS pages(
          document_id TEXT REFERENCES documents(id), number INTEGER, width REAL, height REAL,
          image TEXT, ink TEXT, meta TEXT NOT NULL, PRIMARY KEY(document_id,number));
        CREATE TABLE IF NOT EXISTS glyphs(
          id TEXT PRIMARY KEY, profile_id TEXT REFERENCES profiles(id), document_id TEXT,
          page INTEGER, label TEXT NOT NULL DEFAULT '', original_label TEXT DEFAULT '', bbox TEXT NOT NULL,
          image TEXT NOT NULL, metrics TEXT NOT NULL, confidence REAL DEFAULT 0,
          classification TEXT DEFAULT 'unknown', status TEXT DEFAULT 'pending',
          origin TEXT NOT NULL DEFAULT 'extracted' CHECK(origin IN ('extracted','inferred')),
          parent TEXT, note TEXT DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(document_id,page) REFERENCES pages(document_id,number));
        CREATE INDEX IF NOT EXISTS glyph_lookup ON glyphs(profile_id,status,label);
        CREATE TABLE IF NOT EXISTS glyph_reviews(
          glyph_id TEXT PRIMARY KEY REFERENCES glyphs(id) ON DELETE CASCADE,
          snapshot TEXT NOT NULL, suggestion TEXT NOT NULL, event TEXT NOT NULL,
          outcome TEXT NOT NULL, updated TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS assignments(
          id TEXT PRIMARY KEY, profile_id TEXT REFERENCES profiles(id), document_id TEXT REFERENCES documents(id),
          title TEXT NOT NULL, questions TEXT DEFAULT '', settings TEXT NOT NULL, answers TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1, latest TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, status TEXT, progress REAL DEFAULT 0, message TEXT, result TEXT,
          created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS cloud_events(
          id TEXT PRIMARY KEY, service TEXT, model TEXT, action TEXT, payload_summary TEXT,
          usage TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS comparisons(
          id TEXT PRIMARY KEY, profile_id TEXT REFERENCES profiles(id), assignment_id TEXT REFERENCES assignments(id),
          export_id TEXT, reference_id TEXT REFERENCES glyphs(id), payload TEXT NOT NULL,
          result TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
        """)


def rows(query, parameters=()):
    with db() as c:
        return [dict(x) for x in c.execute(query, parameters)]


def one(query, parameters=()):
    result = rows(query, parameters)
    if not result:
        raise ValueError("找不到记录")
    return result[0]


def execute(query, parameters=()):
    with db() as c:
        c.execute(query, parameters)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def page_record(document_id, number):
    p = one("SELECT * FROM pages WHERE document_id=? AND number=?", (document_id, number))
    p["meta"] = json.loads(p["meta"])
    return p


def glyph_record(row):
    g = dict(row)
    for key in ("bbox", "metrics"):
        g[key] = json.loads(g[key])
    g["image_url"] = "/files/glyph/" + g["id"]
    return g


def task_update(task, status, progress, message, result=None):
    execute("UPDATE tasks SET status=?,progress=?,message=?,result=? WHERE id=?",
            (status, progress, message, dumps(result), task))
