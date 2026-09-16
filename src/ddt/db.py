import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def data_dir() -> Path:
    path = Path(os.getenv("DDT_DATA_DIR", "data")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect():
    conn = sqlite3.connect(data_dir() / "tracker.db", timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


DEFAULTS = dict(
    enabled=False,
    interval_minutes=60,
    delay_min=3.0,
    delay_max=8.0,
    retries=3,
    cooldown_minutes=60,
    drop_threshold=0.3,
)

SCHEMA = (Path(__file__).resolve().parent / "sql" / "schema.sql").read_text(encoding="utf-8")


def init_db():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise RuntimeError("数据库版本比程序新，请升级程序")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO settings VALUES (1,?)", (json.dumps(DEFAULTS),))
        conn.execute("INSERT OR IGNORE INTO runtime(id) VALUES (1)")


def settings():
    with connect() as conn:
        return json.loads(conn.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])


def enqueue(trigger="manual"):
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM crawl_runs WHERE status IN ('queued','running')").fetchone()
        if row:
            return dict(row)
        runtime = conn.execute("SELECT * FROM runtime WHERE id=1").fetchone()
        if runtime["cooldown_until"] and runtime["cooldown_until"] > now():
            raise ValueError(f"采集冷却中，恢复时间：{runtime['cooldown_until']}")
        cur = conn.execute("INSERT INTO crawl_runs(trigger,created_at) VALUES (?,?)", (trigger, now()))
        return dict(conn.execute("SELECT * FROM crawl_runs WHERE id=?", (cur.lastrowid,)).fetchone())
