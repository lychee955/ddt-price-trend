import io
import json
import sqlite3
import zipfile
from hashlib import sha256

import pytest
from cryptography.fernet import Fernet

from ddt import actions
from ddt.api import products
from ddt.compare import publish
from ddt.db import connect, data_dir, enqueue, now
from ddt.export import export_site
from ddt.encrypted_state import pack_encrypted, read_encrypted
from ddt.parser import Blocked, Item
from ddt.state import pack_state, restore_state, verify_state
from ddt.worker import after


def publish_batch(prices):
    run = enqueue()
    with connect() as conn:
        conn.execute("UPDATE crawl_runs SET status='running' WHERE id=?", (run["id"],))
    publish(run["id"], [Item(pid, "账号" + pid, "测试区", price, "listed", now())
                        for pid, price in prices.items()], drop_threshold=1)
    return run["id"]


def test_state_roundtrip_retains_history_and_cooldown(tmp_path, monkeypatch):
    baseline = publish_batch({"1": 10000, "2": 20000})
    latest = publish_batch({"1": 8000})
    until = after(3600)
    with connect() as conn:
        conn.execute("UPDATE runtime SET failures=3,cooldown_until=? WHERE id=1", (until,))
    bundle = tmp_path / "bundle"
    pack_state(bundle, {"workflow_run_id": "123"})
    monkeypatch.setenv("DDT_DATA_DIR", str(tmp_path / "restored"))
    restore_state(bundle, 123)
    with connect() as conn:
        assert conn.execute("SELECT failures,cooldown_until FROM runtime").fetchone()[:] == (3, until)
        assert conn.execute("SELECT base_run_id FROM crawl_runs WHERE id=?", (latest,)).fetchone()[0] == baseline
        assert conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0] == 4
    with pytest.raises(ValueError, match="冷却"):
        enqueue()
    with pytest.raises(ValueError, match="拒绝覆盖"):
        restore_state(bundle)


def test_state_rejects_tampering_missing_files_and_wrong_owner(tmp_path):
    bundle = tmp_path / "bundle"
    pack_state(bundle, {"workflow_run_id": "123"})
    with pytest.raises(ValueError, match="工作流"):
        verify_state(bundle, 456)
    path = bundle / "tracker.db"
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(ValueError, match="校验和"):
        verify_state(bundle)
    with pytest.raises(FileNotFoundError):
        verify_state(tmp_path / "absent")


def test_state_rejects_active_or_incomplete_database(tmp_path):
    enqueue()
    with pytest.raises(ValueError, match="未结束"):
        pack_state(tmp_path / "active")
    with connect() as conn:
        conn.execute("UPDATE crawl_runs SET status='interrupted'")
    bundle = tmp_path / "bundle"
    pack_state(bundle)
    with sqlite3.connect(bundle / "tracker.db") as conn:
        conn.execute("DROP TABLE crawl_pages")
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sha256"] = sha256((bundle / "tracker.db").read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="必要表"):
        verify_state(bundle)


def test_failed_workflow_state_is_used_and_missing_latest_state_stops():
    previous = {"id": 12, "status": "completed", "conclusion": "failure"}
    old = {"id": 20, "workflow_run": {"id": 11}}
    latest = {"id": 21, "workflow_run": {"id": 12}}
    assert actions.choose_artifact(previous, [old, latest], "collect") == latest
    with pytest.raises(ValueError, match="未保存状态"):
        actions.choose_artifact(previous, [old], "collect")
    with pytest.raises(ValueError, match="没有有效"):
        actions.choose_artifact(previous, [], "collect")
    assert actions.choose_artifact(previous, [old], "recover") == old
    with pytest.raises(ValueError, match="未结束"):
        actions.choose_artifact({**previous, "status": "in_progress"}, [latest], "collect")


def test_failed_crawl_can_be_backed_up_and_restored_without_reset(tmp_path, monkeypatch):
    publish_batch({"1": 10000})

    def blocked(*args):
        raise Blocked("禁止访问", 7200)

    monkeypatch.setattr("ddt.worker.Crawler.collect", blocked)
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    actions.collect_once()
    assert "failed=true" in outputs.read_text()
    bundle = tmp_path / "failure"
    pack_state(bundle)
    monkeypatch.setenv("DDT_DATA_DIR", str(tmp_path / "restore-failure"))
    restore_state(bundle)
    with connect() as conn:
        assert conn.execute("SELECT status FROM crawl_runs ORDER BY id DESC").fetchone()[0] == "blocked"
        assert conn.execute("SELECT failures FROM runtime").fetchone()[0] == 1
        assert conn.execute("SELECT price FROM products").fetchone()[0] == 10000
        assert conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0] == 1
    monkeypatch.setattr("ddt.worker.Crawler.collect", lambda *args: pytest.fail("冷却中不得访问网站"))
    actions.collect_once()
    assert "result=cooldown" in outputs.read_text()


def test_remote_recovery_keeps_failures_and_adds_cooldown(tmp_path, monkeypatch):
    with connect() as conn:
        conn.execute("UPDATE runtime SET failures=2 WHERE id=1")
    monkeypatch.setenv("DDT_BACKUP_KEY", Fernet.generate_key().decode())
    encrypted = tmp_path / "state.enc"
    pack_encrypted(encrypted, {"workflow_run_id": "12"})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.write(encrypted, "state.enc")
    artifact = {"id": 21, "workflow_run": {"id": 12}}

    class FakeGitHub:
        def state_artifacts(self):
            return [artifact]

        def previous_run(self, *args):
            return {"id": 13, "status": "completed", "conclusion": "cancelled"}

        def download(self, *args):
            return buffer.getvalue()

    monkeypatch.setenv("GITHUB_RUN_ID", "14")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("DDT_DATA_DIR", str(tmp_path / "recovered"))
    actions.restore_remote(FakeGitHub(), "recover", "main")
    with connect() as conn:
        row = conn.execute("SELECT failures,cooldown_until FROM runtime").fetchone()
        assert row[0] == 2 and row[1] > now()
    with pytest.raises(ValueError, match="冷却"):
        enqueue()


def test_unpack_rejects_traversal_and_extra_files(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../tracker.db", "bad")
        archive.writestr("manifest.json", "{}")
    with pytest.raises(ValueError, match="仅包含"):
        actions.unpack_bundle(buffer.getvalue(), tmp_path)


def test_export_matches_api_and_sanitizes_failed_runtime(tmp_path):
    publish_batch({"1": 10000, "2": 20000})
    latest = publish_batch({"1": 8000})
    failure = enqueue()
    with connect() as conn:
        conn.execute("UPDATE crawl_runs SET status='failed',error='secret-url' WHERE id=?", (failure["id"],))
        conn.execute("UPDATE runtime SET last_error='secret-runtime',failures=2 WHERE id=1")
    original_dir = data_dir()
    root = tmp_path / "public"
    manifest = export_site(root)
    assert data_dir() == original_dir
    version = root / "versions" / manifest["version"]
    expected = products(min_price=None, max_price=None, page=1, page_size=100)
    result = json.loads((version / "products.json").read_text(encoding="utf-8"))
    assert result["items"] == expected["items"]
    assert result["run_id"] == latest
    assert manifest["latest_success_at"]
    assert not list(root.rglob("*.db"))
    assert "secret" not in "".join(path.read_text(encoding="utf-8") for path in root.rglob("*.json"))
    history = json.loads((version / manifest["histories"]["1"]).read_text(encoding="utf-8"))
    assert len(history["snapshots"]) == 2 and history["gaps"][0]["status"] == "failed"
    assert json.loads((root / "manifest.json").read_text())["version"] == manifest["version"]


def test_export_empty_database_is_readable(tmp_path):
    manifest = export_site(tmp_path / "public")
    version = tmp_path / "public" / "versions" / manifest["version"]
    assert json.loads((version / "products.json").read_text())["items"] == []
    assert manifest["latest_success_at"] is None


def test_encrypted_roundtrip_and_authentication(tmp_path, monkeypatch):
    publish_batch({"1": 12345})
    until = after(3600)
    with connect() as conn:
        conn.execute("UPDATE runtime SET failures=3,cooldown_until=? WHERE id=1", (until,))
    key = tmp_path / "backup.key"
    key.write_bytes(Fernet.generate_key())
    encrypted = tmp_path / "state.enc"
    manifest = pack_encrypted(encrypted, {"workflow_run_id": "123"}, key)
    assert b"SQLite format" not in encrypted.read_bytes()
    assert b"sha256" not in encrypted.read_bytes()
    assert list(tmp_path.glob("*.db")) == []
    assert read_encrypted(encrypted, 123, key) == manifest
    with pytest.raises(ValueError, match="工作流"):
        read_encrypted(encrypted, 456, key)
    wrong_key = tmp_path / "wrong.key"
    wrong_key.write_bytes(Fernet.generate_key())
    with pytest.raises(ValueError, match="认证失败"):
        read_encrypted(encrypted, key_file=wrong_key, restore=True)
    damaged = tmp_path / "damaged.enc"
    damaged.write_bytes(encrypted.read_bytes()[:-8] + b"aaaaaaaa")
    with pytest.raises(ValueError, match="认证失败"):
        read_encrypted(damaged, key_file=key)
    monkeypatch.setenv("DDT_DATA_DIR", str(tmp_path / "restored-encrypted"))
    read_encrypted(encrypted, 123, key, restore=True)
    with connect() as conn:
        assert conn.execute("SELECT failures,cooldown_until FROM runtime").fetchone()[:] == (3, until)
        assert conn.execute("SELECT price FROM products").fetchone()[0] == 12345
    with pytest.raises(ValueError, match="拒绝覆盖"):
        read_encrypted(encrypted, key_file=key, restore=True)


def test_encryption_requires_key_and_rejects_plain_artifact(tmp_path, monkeypatch):
    monkeypatch.delenv("DDT_BACKUP_KEY", raising=False)
    with pytest.raises(ValueError, match="密钥缺失"):
        pack_encrypted(tmp_path / "state.enc")
    assert not (tmp_path / "state.enc").exists()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("tracker.db", "private")
        archive.writestr("manifest.json", "{}")
    with pytest.raises(ValueError, match="拒绝明文"):
        actions.unpack_bundle(buffer.getvalue(), tmp_path)
