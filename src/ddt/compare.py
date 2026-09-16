import json
from collections import Counter

from .db import connect, now
from .parser import CrawlError, Item


def publish(run_id: int, items: list[Item], evidence=None, drop_threshold=0.3):
    """Commit an entire validated batch, events and current state atomically."""
    evidence = evidence or {}
    current = {item.id: item for item in items}
    if len(current) != len(items):
        raise CrawlError("重复商品，拒绝发布批次")
    stamp = now()
    counts = Counter()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("SELECT * FROM crawl_runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] != "running":
            raise CrawlError("批次已失效，不能重复发布")
        base = conn.execute(
            "SELECT id FROM crawl_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        base_id = base[0] if base else None
        old = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM products")}
        previous_present = {
            r[0]
            for r in conn.execute(
                "SELECT product_id FROM product_snapshots WHERE run_id=? AND present=1", (base_id,)
            )
        }
        if previous_present and len(current) < len(previous_present) * (1 - drop_threshold):
            raise CrawlError("商品数量异常下降，批次未发布；保留上次成功结果，请检查网站")

        def event(pid, kind, before, after, os, ns):
            delta = after - before if before is not None and after is not None else None
            percent = round(delta / before * 100, 2) if delta is not None and before else None
            conn.execute(
                """INSERT INTO change_events
                (run_id,product_id,kind,old_price,new_price,delta,percent,old_status,new_status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, pid, kind, before, after, delta, percent, os, ns, stamp),
            )
            counts[kind] += 1

        for pid, item in current.items():
            prev = old.get(pid)
            conn.execute(
                """INSERT INTO products VALUES (?,?,?,?,?,?,?,?,0)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,server=excluded.server,
                price=excluded.price,status=excluded.status,last_seen=excluded.last_seen,missing_count=0""",
                (
                    pid,
                    item.title,
                    item.server,
                    item.url,
                    item.price,
                    item.status,
                    item.observed_at,
                    item.observed_at,
                ),
            )
            conn.execute(
                "INSERT INTO product_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, pid, item.title, item.server, item.price, item.status, item.observed_at, 1, None),
            )
            if not base:
                counts["baseline"] += 1
            elif not prev:
                event(pid, "new", None, item.price, None, item.status)
            else:
                if pid not in previous_present:
                    event(pid, "returned", prev["price"], item.price, prev["status"], item.status)
                elif prev["status"] != item.status:
                    event(pid, "status_changed", prev["price"], item.price, prev["status"], item.status)
                if prev["price"] != item.price:
                    event(
                        pid,
                        "decreased" if item.price < prev["price"] else "increased",
                        prev["price"],
                        item.price,
                        prev["status"],
                        item.status,
                    )
                else:
                    counts["unchanged"] += 1
        for pid, prev in old.items():
            if pid in current:
                continue
            missing = prev["missing_count"] + 1
            confirmed, reason = evidence.get(pid, (None, "本轮列表未发现，未确认成交或下架"))
            status = confirmed or (
                prev["status"]
                if prev["status"] in ("sold", "delisted")
                else "suspected_missing"
                if missing == 1
                else "missing"
            )
            conn.execute("UPDATE products SET status=?,missing_count=? WHERE id=?", (status, missing, pid))
            conn.execute(
                "INSERT INTO product_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, pid, prev["title"], prev["server"], None, status, stamp, 0, reason),
            )
            if prev["status"] != status:
                event(pid, status, prev["price"], None, prev["status"], status)
        conn.execute(
            """UPDATE crawl_runs SET status='success',finished_at=?,base_run_id=?,
                     product_count=?,summary=? WHERE id=?""",
            (stamp, base_id, len(current), json.dumps(dict(counts)), run_id),
        )
        conn.execute("UPDATE runtime SET failures=0,last_error=NULL,cooldown_until=NULL WHERE id=1")
    return dict(counts)
