"""Verified SQLite state bundles for disposable runners."""

import json
import shutil
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from .db import connect, data_dir, now

MAX_STATE_BYTES = 100 * 1024 * 1024


def inspect_database(path):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError("数据库缺失或超过 100 MiB 状态上限")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("数据库完整性检查失败")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != 1:
            raise ValueError("不支持的数据库版本")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("数据库外键检查失败")
        required = {"settings", "runtime", "crawl_runs", "products", "product_snapshots", "change_events", "crawl_pages"}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise ValueError("数据库缺少必要表")
        if conn.execute("SELECT 1 FROM crawl_runs WHERE status IN ('queued','running')").fetchone():
            raise ValueError("数据库包含未结束任务，请先人工检查并恢复")
        if not conn.execute("SELECT 1 FROM runtime WHERE id=1").fetchone():
            raise ValueError("数据库运行状态缺失")
        if not conn.execute("SELECT 1 FROM settings WHERE id=1").fetchone():
            raise ValueError("数据库配置缺失")
        latest = conn.execute("SELECT MAX(id) FROM crawl_runs WHERE status='success'").fetchone()[0]
        return {"database_version": version, "latest_success_id": latest}


def pack_state(output, metadata=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "tracker.db"
    if path.exists():
        raise ValueError("状态输出目录已有数据库，请使用新目录")
    with connect() as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
    manifest = {
        **(metadata or {}), **inspect_database(path), "format": 1, "created_at": now(),
        "sha256": sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest


def verify_state(source, expected_run_id=None):
    source = Path(source)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    path = source / "tracker.db"
    if manifest.get("format") != 1 or not path.is_file():
        raise ValueError("无效的状态备份")
    if path.stat().st_size > MAX_STATE_BYTES or path.stat().st_size != manifest.get("size"):
        raise ValueError("状态备份大小不匹配或超过上限")
    if sha256(path.read_bytes()).hexdigest() != manifest.get("sha256"):
        raise ValueError("状态备份校验和不匹配")
    if expected_run_id is not None and str(manifest.get("workflow_run_id")) != str(expected_run_id):
        raise ValueError("状态备份所属工作流不匹配")
    info = inspect_database(path)
    if any(manifest.get(key) != value for key, value in info.items()):
        raise ValueError("状态备份元数据不匹配")
    return manifest


def restore_state(source, expected_run_id=None):
    manifest = verify_state(source, expected_run_id)
    target = data_dir() / "tracker.db"
    if any(target.with_name(name).exists() for name in ("tracker.db", "tracker.db-wal", "tracker.db-shm")):
        raise ValueError("目标目录已有数据库，拒绝覆盖")
    shutil.copyfile(Path(source) / "tracker.db", target)
    return manifest
