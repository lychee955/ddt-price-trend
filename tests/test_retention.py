import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ddt.api import app
from ddt.compare import publish
from ddt.db import connect, enqueue, init_db
from ddt.export import export_site
from ddt.parser import CrawlError, Item


def batch(monkeypatch, stamp, ids=("active",), *, evidence=None, statuses=None, drop_threshold=1):
    monkeypatch.setattr("ddt.compare.now", lambda: stamp)
    run_id = enqueue()["id"]
    with connect() as conn:
        conn.execute("UPDATE crawl_runs SET status='running' WHERE id=?", (run_id,))
    publish(
        run_id,
        [Item(pid, "账号" + pid, "测试区", 10000, (statuses or {}).get(pid, "listed"), stamp) for pid in ids],
        evidence=evidence,
        drop_threshold=drop_threshold,
    )
    return run_id


@pytest.mark.parametrize("repeat_missing", [False, True])
@pytest.mark.parametrize(
    ("stamp", "expired"),
    [
        ("2026-09-23T00:00:00+00:00", False),
        ("2026-10-31T23:59:59+00:00", False),
        ("2026-11-01T00:00:00+00:00", True),
        ("2026-11-02T00:00:00+00:00", True),
    ],
)
def test_missing_retention_boundary_and_related_data(monkeypatch, repeat_missing, stamp, expired):
    first = batch(monkeypatch, "2026-08-01T00:00:00+00:00", ("active", "target"))
    batch(monkeypatch, "2026-09-02T00:00:00+00:00")
    if repeat_missing:
        batch(monkeypatch, "2026-09-03T00:00:00+00:00")
    # Existing databases derive the original streak from their snapshots after a restart.
    init_db()
    latest = batch(monkeypatch, stamp)
    with connect() as conn:
        for table, column in (
            ("products", "id"),
            ("product_snapshots", "product_id"),
            ("change_events", "product_id"),
        ):
            count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}='target'").fetchone()[0]
            assert (count == 0) == expired
        assert conn.execute("SELECT COUNT(*) FROM products WHERE id='active'").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM crawl_runs WHERE id=?", (first,)).fetchone()[0] == "success"
        assert conn.execute("SELECT base_run_id FROM crawl_runs WHERE id=?", (latest,)).fetchone()[0]
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    with TestClient(app) as client:
        assert client.get("/api/products/target/history").status_code == (404 if expired else 200)
        assert client.get("/api/products", params={"include_missing": True}).json()["total"] == (
            1 if expired else 2
        )


def test_reappearance_resets_streak_and_deleted_product_starts_fresh(monkeypatch):
    batch(monkeypatch, "2026-09-01T00:00:00+00:00", ("active", "target"))
    batch(monkeypatch, "2026-09-02T00:00:00+00:00")
    # A product that returns on the expiry date is retained with its original history.
    returned = batch(monkeypatch, "2026-11-01T00:00:00+00:00", ("active", "target"))
    with connect() as conn:
        row = conn.execute("SELECT * FROM products WHERE id='target'").fetchone()
        assert row["first_seen"] == "2026-09-01T00:00:00+00:00" and row["missing_count"] == 0
        assert conn.execute(
            "SELECT kind FROM change_events WHERE run_id=? AND product_id='target'", (returned,)
        ).fetchone()[0] == "returned"
    batch(monkeypatch, "2026-11-02T00:00:00+00:00")
    batch(monkeypatch, "2026-12-31T23:59:59+00:00")
    with connect() as conn:
        assert conn.execute("SELECT 1 FROM products WHERE id='target'").fetchone()
    batch(monkeypatch, "2027-01-01T00:00:00+00:00")
    with connect() as conn:
        assert conn.execute("SELECT 1 FROM products WHERE id='target'").fetchone() is None
    fresh_stamp = "2027-01-02T00:00:00+00:00"
    batch(monkeypatch, fresh_stamp, ("active", "target"))
    with connect() as conn:
        assert conn.execute("SELECT first_seen FROM products WHERE id='target'").fetchone()[0] == fresh_stamp
        assert conn.execute("SELECT COUNT(*) FROM product_snapshots WHERE product_id='target'").fetchone()[0] == 1
        assert [row[0] for row in conn.execute(
            "SELECT kind FROM change_events WHERE product_id='target'"
        )] == ["new"]


def test_retention_keeps_other_statuses_and_export_excludes_deleted_history(monkeypatch, tmp_path):
    ids = ("active", "publicity", "sold", "delisted", "target")
    statuses = {"publicity": "publicity"}
    batch(monkeypatch, "2026-09-01T00:00:00+00:00", ids, statuses=statuses)
    batch(
        monkeypatch, "2026-09-02T00:00:00+00:00", ("active", "publicity"),
        evidence={"sold": ("sold", "已售出"), "delisted": ("delisted", "已下架")},
        statuses=statuses,
    )
    batch(
        monkeypatch, "2026-11-01T00:00:00+00:00", ("active", "publicity"), statuses=statuses,
    )
    with connect() as conn:
        assert {row[0]: row[1] for row in conn.execute("SELECT id,status FROM products")} == {
            "active": "listed", "publicity": "publicity", "sold": "sold", "delisted": "delisted",
        }
    root = tmp_path / "public"
    manifest = export_site(root)
    assert set(manifest["histories"]) == set(ids) - {"target"}
    version = root / "versions" / manifest["version"]
    products = json.loads((version / "products.json").read_text(encoding="utf-8"))
    assert {row["id"] for row in products["items"]} == set(ids) - {"target"}
    assert not json.loads((version / "changes.json").read_text(encoding="utf-8"))


def test_many_products_expire_in_one_batch(monkeypatch):
    batch(monkeypatch, "2026-09-01T00:00:00+00:00", ("active", *(str(i) for i in range(1001))))
    batch(monkeypatch, "2026-09-02T00:00:00+00:00")
    batch(monkeypatch, "2026-11-01T00:00:00+00:00")
    with connect() as conn:
        assert [row[0] for row in conn.execute("SELECT id FROM products")] == ["active"]
        assert conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("failure", ["collapse", "late_failure"])
def test_failed_batch_preserves_expired_products_and_history(monkeypatch, failure):
    batch(monkeypatch, "2026-09-01T00:00:00+00:00", ("active", "target"))
    good = batch(monkeypatch, "2026-09-02T00:00:00+00:00")
    with connect() as conn:
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in (
            "products", "product_snapshots", "change_events",
        )}
        if failure == "late_failure":
            conn.execute("""CREATE TRIGGER reject_publish BEFORE UPDATE OF status ON crawl_runs
                WHEN NEW.status='success'
                BEGIN SELECT RAISE(ABORT, '测试发布失败'); END""")
    error = CrawlError if failure == "collapse" else sqlite3.IntegrityError
    with pytest.raises(error):
        batch(
            monkeypatch, "2026-11-01T00:00:00+00:00",
            () if failure == "collapse" else ("active",), drop_threshold=0.3,
        )
    with connect() as conn:
        for table, rows in before.items():
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert conn.execute(
            "SELECT id FROM crawl_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0] == good
