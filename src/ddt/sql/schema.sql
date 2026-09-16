CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat TEXT,
    cooldown_until TEXT,
    next_run_at TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    base_run_id INTEGER,
    pages_done INTEGER NOT NULL DEFAULT 0,
    product_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    summary TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS single_active_run
    ON crawl_runs ((1))
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    server TEXT NOT NULL,
    url TEXT NOT NULL,
    price INTEGER NOT NULL,
    status TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    missing_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product_snapshots (
    run_id INTEGER NOT NULL REFERENCES crawl_runs (id),
    product_id TEXT NOT NULL REFERENCES products (id),
    title TEXT NOT NULL,
    server TEXT NOT NULL,
    price INTEGER,
    source_status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    present INTEGER NOT NULL,
    evidence TEXT,
    PRIMARY KEY (run_id, product_id)
);

CREATE INDEX IF NOT EXISTS snapshot_history
    ON product_snapshots (product_id, run_id);

CREATE TABLE IF NOT EXISTS change_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES crawl_runs (id),
    product_id TEXT NOT NULL REFERENCES products (id),
    kind TEXT NOT NULL,
    old_price INTEGER,
    new_price INTEGER,
    delta INTEGER,
    percent REAL,
    old_status TEXT,
    new_status TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_run
    ON change_events (run_id, kind);

CREATE TABLE IF NOT EXISTS crawl_pages (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES crawl_runs (id),
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    count INTEGER,
    error TEXT,
    created_at TEXT NOT NULL
);

PRAGMA user_version = 1;
