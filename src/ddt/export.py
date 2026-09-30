"""Export a consistent, public, read-only snapshot without exposing runtime logs."""

import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from . import api
from .db import connect, now


def public_run(run):
    if not run:
        return None
    value = dict(run)
    value["error"] = "采集未成功，保留上次成功价格" if value.get("error") else None
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def export_site(output: Path):
    """Use one SQLite backup for every file; run in a standalone CLI process."""
    output = Path(output).resolve()
    version = uuid.uuid4().hex
    root = output / "versions" / version
    previous_dir = os.environ.get("DDT_DATA_DIR")
    with tempfile.TemporaryDirectory(prefix="ddt-export-") as temporary:
        with connect() as source, closing(sqlite3.connect(Path(temporary) / "tracker.db")) as target:
            source.backup(target)
        os.environ["DDT_DATA_DIR"] = temporary
        try:
            overview = api.overview()
            overview["latest"] = public_run(overview["latest"])
            overview["active"] = None
            overview["runtime"] = {
                "worker_online": False,
                "cooldown_until": overview["runtime"]["cooldown_until"],
                "last_error": "采集暂停，请查看采集记录" if overview["runtime"]["last_error"] else None,
            }
            write_json(root / "overview.json", overview)
            items, histories = [], {}
            page = 1
            while True:
                products = api.products(min_price=None, max_price=None, page=page, page_size=100)
                items.extend(products["items"])
                if len(items) >= products["total"]:
                    break
                page += 1
            for product in items:
                pid = product["id"]
                name = sha256(pid.encode()).hexdigest() + ".json"
                histories[pid] = "history/" + name
                history = api.history(pid)
                for snapshot in history["snapshots"]:
                    snapshot["evidence"] = (
                        "本轮列表未发现；状态以明确证据为准，不推断成交" if not snapshot["present"] else None
                    )
                write_json(root / "history" / name, history)
            write_json(root / "products.json", {
                "items": items, "servers": products["servers"], "run_id": products["run_id"],
            })
            runs = []
            page = 1
            while True:
                result = api.crawls(page=page)
                runs.extend(public_run(run) for run in result["items"])
                if len(runs) >= result["total"]:
                    break
                page += 1
            write_json(root / "runs.json", {"items": runs})
            with connect() as conn:
                latest_id = products["run_id"]
                events = [dict(row) for row in conn.execute(
                    "SELECT * FROM change_events WHERE run_id=? ORDER BY id DESC", (latest_id,),
                )]
            write_json(root / "changes.json", events)
            manifest = {
                "format": 1, "version": version, "generated_at": now(),
                "latest_success_at": overview["latest"]["finished_at"] if overview["latest"] else None,
                "interval_minutes": 120, "histories": histories,
            }
            # Publish the pointer only after every versioned file is complete.
            pending = output / "manifest.pending"
            write_json(pending, manifest)
            pending.replace(output / "manifest.json")
            return manifest
        finally:
            if previous_dir is None:
                os.environ.pop("DDT_DATA_DIR", None)
            else:
                os.environ["DDT_DATA_DIR"] = previous_dir
